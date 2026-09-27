"""Stateless Codex CLI analysis using CLI-owned authentication, never token extraction."""

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace

# No developer integrations, executable tools, hooks, web access or peer agents.
DISABLED = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "apps",
    "plugins",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "computer_use",
    "image_generation",
    "memories",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "sleep_tool",
    "code_mode",
    "code_mode_host",
    "goals",
)


def executable():
    found = shutil.which("codex")
    if found:
        return found
    for path in (
        Path.home() / ".local/bin/codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError("Codex CLI is not installed. Open Connections to install it.")


def environment():
    # Keep CLI-owned authentication discovery, but do not inherit broker/API credentials
    # or the development agent's current thread/session context.
    allowed = {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "CODEX_HOME",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def arguments(folder, schema, output, model, instructions):
    command = [
        executable(),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "--cd",
        str(folder),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(output),
    ]
    for key, value in {
        "approval_policy": "never",
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "history.persistence": "none",
        "developer_instructions": instructions,
        "features.skip_host_skill_discovery": True,
        **{"features." + name: False for name in DISABLED},
    }.items():
        command.extend(["-c", key + "=" + json.dumps(value)])
    if model:
        command.extend(["--model", model])
    return [*command, "-"]


async def analyze(root, config, instructions, content, schema):
    folder = root / "data/analysis"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="request-", dir=folder) as directory:
        work = Path(directory)
        schema_file, output = work / "schema.json", work / "answer.json"
        schema_file.write_text(json.dumps(schema))
        command = arguments(work, schema_file, output, config.openai_model, instructions)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment(),
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(content).encode()), config.analysis_timeout_seconds
            )
            if process.returncode:
                raise RuntimeError(
                    "Codex analysis failed. Check CLI login, model access or quota in Connections."
                )
            # Never accept a result from a turn that attempted executable/external tools.
            completed = False
            started = False
            for line in stdout.splitlines():
                event = json.loads(line)
                if event.get("type") == "turn.started":
                    started = True
                if event.get("type") == "turn.completed":
                    completed = True
                item = event.get("item", {})
                if item.get("type") == "error" and not started:
                    message = item.get("message", "")
                    if message.startswith(
                        (
                            "Under-development features enabled:",
                            "Code Mode is unavailable because code-mode host is disabled.",
                        )
                    ):
                        continue
                    raise RuntimeError("Codex rejected the isolated analysis configuration")
                if item.get("type") not in (None, "agent_message", "reasoning", "todo_list"):
                    raise RuntimeError("Unexpected Codex tool activity; analysis rejected")
            if not completed or not output.is_file() or output.stat().st_size > 1_000_000:
                raise RuntimeError("Codex returned no completed structured answer")
            result = json.loads(output.read_text())
            reported = re.search(rb"(?m)^model:\s*([A-Za-z0-9_.:/-]+)\s*$", stderr)
            return SimpleNamespace(
                id="codex-exec",
                model=reported.group(1).decode()
                if reported
                else config.openai_model or "Codex default (not reported)",
                output=result,
                status="completed",
            )
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
