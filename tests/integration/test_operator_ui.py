import curses
import time
from unittest.mock import patch

import pytest

from wayland.config import app_root
from wayland.operator import OperatorRuntime
from wayland.tui.ui import Dashboard


class Screen:
    def __init__(self, width=120, height=40):
        self.width, self.height = width, height
        self.writes = []

    def getmaxyx(self):
        return self.height, self.width

    def bkgd(self, *args):
        pass

    def clearok(self, *args):
        pass

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, style):
        assert 0 <= y < self.height and 0 <= x < self.width
        self.writes.append((y, x, text, style))

    def move(self, y, x):
        assert 0 <= y < self.height and 0 <= x < self.width

    def refresh(self):
        pass


@pytest.fixture
def runtime(tmp_path):
    return OperatorRuntime(app_root(tmp_path))


@pytest.fixture
def ui(runtime):
    view = Dashboard(str(runtime.root))
    view.data = runtime.dispatch("snapshot")
    view.submit = lambda action, **params: view.updates.put(
        ("done", (action, runtime.dispatch(action, **params), params))
    )
    view.update()
    return view


def render(ui, width=120, height=40):
    screen = Screen(width, height)
    with patch("curses.curs_set"), patch("curses.has_colors", return_value=False):
        ui.render(screen)
    return screen


@pytest.mark.parametrize("view", ["chat", "plan", "tools", "files", "processes", "market", "setup"])
@pytest.mark.parametrize("size", [(70, 18), (120, 40), (180, 50)])
def test_all_tabs_render_real_runtime(ui, view, size):
    ui.view = view
    screen = render(ui, *size)
    assert any("Wayland" in text for _, _, text, _ in screen.writes)
    assert len(ui.hit_rows) == 5


def test_sidebar_mouse_tabs_and_drafts(ui):
    render(ui)
    key = ui.hit_rows[2][2]
    ui.mouse(0, 8, ui.hit_rows[2][0])
    ui.mouse(3, 8, ui.hit_rows[2][0])
    assert ui.selected == key
    assert ui.focus == "sidebar"
    ui.key("\t")
    assert ui.focus == "history"
    ui.key("\t")
    for c in "test draft":
        ui.input_key(c)
    assert ui.buffer == "test draft"
    ui.key(curses.KEY_F2)
    ui.key(curses.KEY_RIGHT)
    assert ui.focus == "tabs"
    ui.key("\x1b")
    assert ui.view == "chat"
    assert ui.buffer == "test draft"


def test_slash_completion_does_not_send(ui):
    ui.focus = "chat"
    ui.buffer = "/pos"
    ui.cursor = len(ui.buffer)
    ui.key("\r")
    assert ui.buffer.strip() == "/positions"
    ui.key("\r")
    assert ui.view == "files"
    assert ui.buffer == ""


def test_settings_edit_validates_and_rejects_live_accounts(runtime):
    runtime.dispatch("trading_settings", patch={"openai_model": "test-model"})
    assert runtime.dispatch("snapshot")["tradingSettings"]["openai_model"] == "test-model"
    with pytest.raises(ValueError):
        runtime.dispatch("trading_settings", patch={"mode": "LIVE"})
    with pytest.raises(ValueError):
        runtime.dispatch("trading_settings", patch={"account_allowlist": "U123"})
    with pytest.raises(ValueError):
        runtime.dispatch("trading_settings", patch={"max_open_positions": "-1"})
    assert runtime.settings().mode == "PAPER"


def test_role_lifecycle_and_restart(runtime):
    new = runtime.dispatch("create_agent", name="Research")
    tid = new["threadId"]
    assert tid.startswith("wayland:")
    runtime.dispatch("rename", threadId=tid, name="Renamed")
    runtime.dispatch("archive", threadId=tid)
    assert tid in runtime.dispatch("snapshot")["archived"]
    runtime.dispatch("restore", threadId=tid)
    restarted = OperatorRuntime(runtime.root)
    assert restarted.dispatch("snapshot")["threads"][tid]["name"] == "Renamed"


def test_missing_api_shows_waiting_and_preserves_prompt(runtime, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tid = next(iter(runtime.dispatch("snapshot")["threads"]))
    runtime.dispatch("send", threadId=tid, text="Explain the current state", clientId="c1")
    deadline = time.monotonic() + 3
    while runtime.active and time.monotonic() < deadline:
        time.sleep(0.01)
    thread = runtime.dispatch("snapshot")["threads"][tid]
    assert thread["status"]["type"] == "waiting"
    assert any(i.get("clientId") == "c1" for i in thread["items"])
    assert "configuration" in thread["items"][-1]["text"]
    assert thread["plan"][-1]["status"] == "pending"


def test_kill_persists_in_execution_store(runtime):
    runtime.dispatch("kill", enabled=True)
    with runtime.store() as store:
        assert store.get("kill_switch") is True
        assert store.recent()[0]["kind"] == "kill_switch"


def test_f10_navigation_and_appearance(ui):
    ui.key(curses.KEY_F10)
    assert ui.panel == "CONNECTIONS"
    ui.key("s")
    for _ in range(12):
        ui.key(curses.KEY_DOWN)
        render(ui)
        assert any(hit[3] == ui.hub_selected for hit in ui.hub_hits)
    ui.hub_action("c")
    assert ui.panel == "APPEARANCE"
    render(ui)


def test_no_development_runtime_imports():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/wayland"
    for file in root.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.Import):
                assert not any(n.name.startswith(("tyrell", "codex_dashboard")) for n in node.names)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("tyrell", "codex_dashboard"))


def test_additional_prompt_is_queued_and_plan_updates(runtime, monkeypatch):
    import asyncio
    import threading

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    runtime.dispatch("trading_settings", patch={"openai_model": "test-model"})
    gate = threading.Event()
    calls = []

    async def reply(tid, text, context, config, cancel):
        calls.append(text)
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return "Analysis: " + text

    monkeypatch.setattr(runtime, "model_reply", reply)
    tid = next(iter(runtime.dispatch("snapshot")["threads"]))
    runtime.dispatch("send", threadId=tid, text="First request", clientId="first")
    result = runtime.dispatch("send", threadId=tid, text="Additional context", clientId="second")
    assert result["queued"]
    thread = runtime.dispatch("snapshot")["threads"][tid]
    assert thread["plan"][-1]["status"] == "pending"
    gate.set()
    deadline = time.monotonic() + 3
    while runtime.active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["First request", "Additional context"]
    thread = runtime.dispatch("snapshot")["threads"][tid]
    assert all(step["status"] == "completed" for step in thread["plan"])
    assert thread["status"]["type"] == "idle"


def test_appearance_and_links_keep_tyrell_behavior(tmp_path):
    from wayland.tui.appearance import Appearance, parse_color
    from wayland.tui.clipboard import selection_text
    from wayland.tui.links import link_ranges, web_url

    assert parse_color("#5fd7ff") == parse_color("95,215,255")
    assert parse_color("Coral") == 203
    assert not web_url("javascript:alert(1)")
    url = "https://example.com/actions/runs/123"
    assert link_ranges("(" + url + ")")[0][2] == url
    rows = [{"copy_text": None}, {"copy_text": "Trading evidence"}, {"copy_text": None}]
    assert selection_text(rows, (0, 0), (2, 0)) == "Trading evidence"
    assert Appearance(tmp_path).path.parent == tmp_path


def test_model_chat_has_no_tools_and_audits_model(runtime, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from wayland.config import Settings

    response = SimpleNamespace(
        id="test-response",
        model="reported-test-model",
        status="completed",
        output_parsed=SimpleNamespace(answer="Evidence is insufficient."),
    )
    parse = AsyncMock(return_value=response)

    class Client:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(parse=parse)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("openai.AsyncOpenAI", Client)
    tid = next(iter(runtime.dispatch("snapshot")["threads"]))
    result = asyncio.run(
        runtime.model_reply(
            tid,
            "Analyze",
            {"market": None, "account": "DO-NOT-SEND"},
            Settings(openai_model="requested-test-model"),
            threading.Event(),
        )
    )
    assert result == "Evidence is insufficient."
    args = parse.call_args.kwargs
    assert "tools" not in args
    assert args["store"] is False
    assert "DO-NOT-SEND" not in args["instructions"]
    with runtime.store() as store:
        events = store.recent()
        assert any("reported-test-model" in e["payload"] for e in events)
        assert any("requested-test-model" in e["payload"] for e in events)


def test_connections_are_global_and_removed_from_setup(ui, runtime):
    ui.setup.visible(ui, 40)
    assert not any(row["action"] in ("openai_model", "ibkr_account") for row in ui.setup.rows)
    ui.key(curses.KEY_F10)
    ui.hub_action("h")
    ui.connections.expanded.add("OpenAI API")
    render(ui)
    assert ui.panel == "CONNECTIONS"
    field = next(i for i, row in enumerate(ui.connections.rows) if row["action"] == "openai_model")
    ui.connections.activate(ui, field)
    assert ui.panel is None
    assert ui.wizard["return_panel"] == "CONNECTIONS"
    ui.buffer = "shared-model"
    ui.entered()
    ui.update()
    assert ui.panel == "CONNECTIONS"
    snapshot = runtime.dispatch("snapshot")
    assert all(t["model"] == "shared-model" for t in snapshot["threads"].values())
    ui.switch(ui.rows[-1][0])
    render(ui)
    assert any("shared-model" in row["text"] for row in ui.connections.rows)


def test_connections_keyboard_reaches_last_field_and_cancel_preserves_draft(ui):
    ui.buffer = "Keep my draft"
    ui.hub_action("h")
    ui.connections.expanded.add("IBKR paper connection")
    render(ui)
    for _ in range(40):
        if ui.connections.rows[ui.connections.index]["action"] == "ibkr_client_id":
            break
        ui.key(curses.KEY_DOWN)
        render(ui)
    assert ui.connections.rows[ui.connections.index]["action"] == "ibkr_client_id"
    ui.key("\r")
    assert ui.wizard["field"] == "ibkr_client_id"
    ui.key("\x1b")
    assert ui.panel == "CONNECTIONS"
    assert ui.buffer == "Keep my draft"


@pytest.mark.parametrize("size", [(70, 18), (120, 40)])
def test_connection_buttons_open_browser_and_preserve_prompt(ui, runtime, size):
    ui.buffer = "An unfinished trading question"
    with (
        patch.object(
            runtime.browser_setup, "start", return_value={"url": "http://127.0.0.1:42100/test/openai"}
        ) as start,
        patch("wayland.tui.ui.webbrowser.open", return_value=True) as browser,
    ):
        ui.key(curses.KEY_F10)
        render(ui, *size)
        assert ui.panel == "CONNECTIONS"
        assert ui.connections.rows[ui.connections.index]["action"] == "login:ibkr"
        ui.key("o")
        ui.update()
        start.assert_called_once_with("openai")
        browser.assert_called_once()
        assert ui.buffer == "An unfinished trading question"
        assert ui.wizard is None
