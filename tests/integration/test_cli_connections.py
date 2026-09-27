import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayland.broker.discovery import discover
from wayland.config import Settings, app_root
from wayland.operator import OperatorRuntime
from wayland.providers import cli_transport


def fake_cli(tmp_path, monkeypatch, item="agent_message", delay=0):
    program = tmp_path / "codex-fixture"
    program.write_text(
        "#!/usr/bin/env python3\n"
        + f"""
import json,os,sys,time,pathlib
args=sys.argv[1:]
root=pathlib.Path(args[args.index('--cd')+1]).parent
(root/'pid').write_text(str(os.getpid()))
(root/'arguments.json').write_text(json.dumps(args))
(root/'environment.json').write_text(json.dumps(dict(os.environ)))
prompt=sys.stdin.read()
time.sleep({delay})
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps({{"answer":"Verified fixture"}}))
print(json.dumps({{"type":"turn.started"}}))
print(json.dumps({{"type":"item.completed","item":{{"type":{item!r}}}}}))
print(json.dumps({{"type":"turn.completed"}}))
"""
    )
    program.chmod(0o700)
    monkeypatch.setattr(cli_transport, "executable", lambda: str(program))
    return program


def test_cli_structured_response_is_isolated_and_uses_existing_auth(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("IBKR_PASSWORD", "must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    root = app_root(tmp_path / "runtime")
    result = asyncio.run(
        cli_transport.analyze(
            root, Settings(), "Only analyze supplied evidence", {"input": "fixture"}, {"type": "object"}
        )
    )
    assert result.output == {"answer": "Verified fixture"}
    args = json.loads((root / "data/analysis/arguments.json").read_text())
    assert all(
        flag in args for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema")
    )
    assert "read-only" in args
    assert "features.shell_tool=false" in args and "features.plugins=false" in args
    assert "features.apps=false" in args and 'web_search="disabled"' in args
    env = json.loads((root / "data/analysis/environment.json").read_text())
    assert "IBKR_PASSWORD" not in env and "OPENAI_API_KEY" not in env
    assert not list((root / "data/analysis").glob("request-*"))


def test_cli_tool_activity_is_rejected(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, item="command_execution")
    with pytest.raises(RuntimeError, match="tool activity"):
        asyncio.run(cli_transport.analyze(app_root(tmp_path / "runtime"), Settings(), "test", "test", {}))


def test_cli_timeout_terminates_process_and_removes_temporary_files(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, delay=60)
    root = app_root(tmp_path / "runtime")
    with pytest.raises(TimeoutError):
        asyncio.run(cli_transport.analyze(root, Settings(analysis_timeout_seconds=1), "test", "test", {}))
    pid = int((root / "data/analysis/pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not list((root / "data/analysis").glob("request-*"))


def test_existing_codex_login_does_not_start_another_login(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    with (
        patch("wayland.connections.executable", return_value="codex"),
        patch("wayland.connections.subprocess.run", return_value=SimpleNamespace(returncode=0)),
        patch("wayland.connections.subprocess.Popen") as login,
    ):
        result = runtime.dispatch("connect_provider", provider="codex")
        assert "existing" in result["message"]
        assert runtime.dispatch("snapshot")["providers"]["openai"]["connected"]
        login.assert_not_called()
        assert "url" not in result


def test_ibkr_button_opens_official_app_and_enables_observation(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    with (
        patch("wayland.connections.ibkr_application", return_value=Path("/Applications/IB Gateway.app")),
        patch("wayland.connections.subprocess.run") as opened,
        patch.object(runtime.broker_observer, "start") as start,
    ):
        result = runtime.dispatch("connect_provider", provider="ibkr")
        opened.assert_called_once()
        assert opened.call_args.args[0] == ["open", "-a", "/Applications/IB Gateway.app"]
        start.assert_called_once()
        assert "url" not in result


@pytest.mark.parametrize(
    "accounts,configured,allowed",
    [
        (["DU123"], "", True),
        (["U123"], "", False),
        (["DU123", "U456"], "", False),
        (["DU123"], "DU999", False),
    ],
)
def test_paper_discovery_verifies_reported_accounts(accounts, configured, allowed):
    client = MagicMock()
    client.connectAsync = AsyncMock()
    client.managedAccounts.return_value = accounts
    with patch("wayland.broker.discovery.socket.create_connection"):
        if allowed:
            result = asyncio.run(discover(Settings(ibkr_account=configured), lambda: client))
            assert result["ibkr_account"] == "DU123"
            assert client.connectAsync.call_args.kwargs["readonly"] is True
        else:
            with pytest.raises(ValueError):
                asyncio.run(discover(Settings(ibkr_account=configured), lambda: client))
    client.disconnect.assert_called()
