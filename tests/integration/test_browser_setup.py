import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from wayland.broker.portal import unpack_gateway
from wayland.config import app_root
from wayland.credentials import api_key
from wayland.operator import OperatorRuntime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = OperatorRuntime(app_root(tmp_path))
    yield runtime
    runtime.browser_setup.close()


def test_browser_form_saves_verified_key_without_chat_or_audit_leaks(runtime):
    url = runtime.dispatch("browser_login", provider="openai")["url"]
    with urllib.request.urlopen(url) as response:
        page = response.read().decode()
        assert "https://platform.openai.com/api-keys" in page
        assert "type='password'" in page
        assert response.headers["Cache-Control"] == "no-store"
    body = urllib.parse.urlencode({"key": "test-private-key", "model": "test-model"}).encode()
    origin = "http://" + urllib.parse.urlsplit(url).netloc
    with patch("openai.OpenAI") as sdk:
        client = sdk.return_value.__enter__.return_value
        with urllib.request.urlopen(urllib.request.Request(url, body, {"Origin": origin})) as response:
            assert b"OpenAI connected" in response.read()
        client.models.retrieve.assert_called_once_with("test-model")
        assert sdk.call_args.kwargs["api_key"] == "test-private-key"
    assert api_key(runtime.root) == "test-private-key"
    assert (runtime.root / "secrets/openai.key").stat().st_mode & 0o777 == 0o600
    assert "test-private-key" not in json.dumps(runtime.dispatch("snapshot"))
    with runtime.store() as store:
        assert "test-private-key" not in json.dumps(store.recent())
    restarted = OperatorRuntime(runtime.root)
    assert api_key(restarted.root) == "test-private-key"
    assert restarted.settings().openai_model == "test-model"


def test_browser_rejects_cross_origin_expired_and_bad_credentials(runtime):
    url = runtime.dispatch("browser_login", provider="openai")["url"]
    body = b"key=must-not-leak&model=test-model"
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(url, body, {"Origin": "https://evil.example"}))
    assert error.value.code == 403
    with patch("openai.OpenAI", side_effect=ValueError("must-not-leak")):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(url, body, {"Origin": "http://" + runtime.browser_setup.address})
            )
        assert b"must-not-leak" not in error.value.read()
    assert api_key(runtime.root) is None
    runtime.browser_setup.expires = 0
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(url)
    assert error.value.code == 403


def test_ibkr_auth_never_implies_execution_ready_and_rejects_live(runtime):
    portal = runtime.browser_setup.portal
    portal.url = "https://localhost:42099"
    portal.get = MagicMock(side_effect=[{"authenticated": False, "connected": True}])
    portal.check()
    assert not portal.snapshot()["connected"]
    for accounts in (["U12345"], ["DU12345", "U12345"], []):
        portal.get = MagicMock(
            side_effect=[{"authenticated": True, "connected": True}, {"accounts": accounts}]
        )
        portal.check()
        assert not portal.snapshot()["connected"]
        assert not runtime.settings().ibkr_account
    portal.get = MagicMock(
        side_effect=[{"authenticated": True, "connected": True}, {"accounts": ["DU12345"]}]
    )
    portal.check()
    assert portal.snapshot()["connected"]
    assert runtime.settings().ibkr_account == "DU12345"
    snapshot = runtime.dispatch("snapshot")
    assert snapshot["trading"]["operating_state"] == "RECONCILING"
    assert "feed not connected" in snapshot["trading"]["broker_status"]
    portal.get = MagicMock(side_effect=urllib.error.HTTPError(portal.url, 401, "Unauthorized", {}, None))
    portal.check()
    assert not portal.snapshot()["connected"]
    assert "Waiting" in portal.snapshot()["status"]


def test_gateway_archive_rejects_path_escape(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escaped", "bad")
    with pytest.raises(ValueError, match="Unsafe"):
        unpack_gateway(stream.getvalue(), tmp_path / "gateway")
    assert not (tmp_path / "escaped").exists()


def test_gateway_lifetime_pipe_stops_child_when_owner_disappears(tmp_path):
    from wayland.broker import gateway_process

    pidfile = tmp_path / "child.pid"
    script = (
        "import os,time,pathlib;pathlib.Path("
        + repr(str(pidfile))
        + ").write_text(str(os.getpid()));time.sleep(60)"
    )
    supervisor = subprocess.Popen(
        [sys.executable, gateway_process.__file__, sys.executable, "-c", script], stdin=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        supervisor.stdin.close()
        supervisor.wait(timeout=8)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)
