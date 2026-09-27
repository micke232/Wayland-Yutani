import curses
import os
import queue
import re
import select
import signal
import sys
import termios
import threading
import time
import unicodedata
import uuid
import webbrowser
from functools import lru_cache
from typing import Any

from ..agents import ROLE_HELP
from .appearance import Appearance
from .client import request
from .clipboard import copy_text, selection_text
from .commands import choices as command_choices
from .composer import DraftLayout
from .connections import ConnectionsForm, connection_badges, connections_text
from .files_view import FilesView
from .hub_settings import diagnostics_text, settings_text
from .links import web_url
from .presentation import (
    PALETTE,
    TimelineCache,
    activity_indicator,
    markdown_rows,
    plan_rows,
    scrollbar_geometry,
    status_tone,
    theme,
    viewport,
)
from .processes import process_rows
from .progress import conversation_text, estimate_label, plan_only_stop
from .setup_view import SetupForm, preview_rows
from .state import status_label
from .terminal_input import enhanced_key, key_sequences

HELP = """# Wayland · Help

## Navigation
F2 focuses / cycles tabs; Left and Right select tabs.
Tab moves between the sidebar, history and Prompt.
Click a role, tab, scrollbar or Prompt to focus it.
Esc returns to Chat. PgUp/PgDn and the wheel scroll history.

## Roles and conversation
F3 creates an analytical role. F5 renames it. F4 opens the archive.
Type / to browse commands. Up/Down selects; Tab or Enter inserts.
Chat uses Codex CLI with its existing login. It cannot place orders.
If needed, Connect starts the official Codex browser login.
Ctrl+Q closes this view; the independent Wayland service keeps running.

## Trading views
Market shows stored market/setup evidence and freshness.
Positions and Orders show broker snapshots and durable intentions.
Audit shows why decisions were accepted, rejected or blocked.
Plan tracks the selected role's current analysis.

## Settings
F10 opens Connections. I opens the official IBKR client; O connects Codex CLI.\nS opens other settings, including appearance, data and risk limits.
Connections are shared by every role. Advanced settings include the optional IBKR adapter.
Setup edits global quality and risk settings.
Saved settings apply to new analyses and the next monitor start.
Settings never unlock LIVE or approve a pending order.

## Safety
/kill on blocks entries. /kill off removes only that gate.
Reconciliation, freshness and deterministic risk checks still apply.
IBKR order submission remains disabled pending paper verification.
"""


HELP += ROLE_HELP


def help_rows(width, content=HELP):
    rows = []
    for text, spans in markdown_rows(content, width, wrap, crop):
        styled = []
        for part, tone in spans:
            mapped = {"agent": "surface", "strong": "accent", "inlinecode": "warning"}.get(tone, tone)
            for chunk in re.split(r"(\bWorking\b|\bReady\b|\bWaiting\b)", part):
                if chunk:
                    styled.append(
                        (
                            chunk,
                            {"Working": "working", "Ready": "success", "Waiting": "warning"}.get(
                                chunk, mapped
                            ),
                        )
                    )
        rows.append({"text": text, "spans": styled})
    return rows


ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]")


def clean(text):
    text = str(text)
    if text.isprintable():
        return text
    return "".join(c if c.isprintable() else " " for c in ANSI.sub("", text))


@lru_cache(maxsize=4096)
def cell_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cells(text):
    if text.isascii():
        return len(text)
    return sum(map(cell_width, text))


def crop(text, width):
    text = clean(text)
    if width < 0:
        return ""
    if text.isascii():
        return text[:width]
    used = 0
    for index, char in enumerate(text):
        used += cell_width(char)
        if used > width:
            return text[:index]
    return text


def wrap(text, width):
    lines = []
    for paragraph in str(text).splitlines() or [""]:
        paragraph = clean(paragraph)
        while cells(paragraph) > width:
            part = crop(paragraph, width)
            split = part.rfind(" ")
            if split > width // 2:
                part = part[:split]
            lines.append(part)
            paragraph = paragraph[len(part) :].lstrip()
        lines.append(paragraph)
    return lines


def duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600:
        return "%dh %02dm" % (seconds // 3600, seconds // 60 % 60)
    return "%dm %02ds" % (seconds // 60, seconds % 60)


class Dashboard:
    def __init__(self, directory, demo=None):
        self.directory = directory
        self.data = demo or {"threads": {}, "tasks": [], "models": [], "requests": [], "connected": False}
        self.demo = demo is not None
        self.selected = None
        self.focus = "sidebar"
        self.command_index = 0
        self.command_query = None
        self.command_dismissed = None
        self.command_hits = []
        self.buffer = ""
        self.cursor = 0
        self.last_input_at = None
        self.drafts = {}
        self.pending_messages = {}
        self.notice = "F2 Tabs · F5 Rename · F1 Help"
        self.scroll = 0
        self.plan_scroll = 0
        self.panel = None
        self.panel_scroll = 0
        self.hub_hits = []
        self.hub_selected = "h"
        self.hub_saved = ""
        self.wizard = None
        self.approval = None
        self.answers = {}
        self.commands: queue.Queue[Any] = queue.Queue()
        self.updates: queue.Queue[Any] = queue.Queue()
        self.stopped = threading.Event()
        self.rows = []
        self.escape = ""
        self.escape_at = 0.0
        self.pasting = False
        self.paste_buffer = ""
        self.view = "chat"
        self.styles = {name: 0 for name in PALETTE}
        self.appearance = Appearance(directory)
        self.timeline_key = None
        self.timeline_rows = []
        self.timeline_length = 0
        self.mouse_enabled = True
        self.native_terminal = os.environ.get("TERM_PROGRAM") == "Apple_Terminal"
        self.native_selecting = False
        self.native_copy_mode = False
        self.timeline_cache = TimelineCache()
        self.refresh_requested = threading.Event()
        self.hit_copy = None
        self.hit_text_view = None
        self.hit_request = None
        # OSC 22 names differ between terminals; use Ghostty's documented CSS names.
        self.pointer_supported = (
            os.environ.get("TERM_PROGRAM", "").lower() == "ghostty"
            or os.environ.get("TERM") == "xterm-ghostty"
        )
        self.pointer_shape = None
        self.pointer_position = None
        self.pointer_layout_ready = False
        self.history_rows = []
        self.history_cells = {}
        self.history_bounds = None
        self.history_context = None
        self.history_snapshot = None
        self.selection_anchor = None
        self.selection_end = None
        self.selection_dragging = False
        self.scrollbar = None
        self.scrollbar_grab = None
        self.key_sequences = key_sequences()
        self.hit_rows = []
        self.sidebar_width = 24
        self.tab_y = 5
        self.chat_tab_end = 0
        self.tool_tab_end = 0
        self.hit_tabs = []
        self.hit_connections = []
        self.screen_height = 24
        self.browse = "agents"
        self.draft_width = 40
        self.draft_offset = 0
        self.draft_height = 3
        self.draft_top = 0
        self.draft_left = 0
        self.draft_cache = None
        self.preferred_column = None
        self.draw_context = None
        self.setup = SetupForm()
        self.connections = ConnectionsForm()
        self.files = FilesView()
        self.process_scroll = 0

    def polling(self):
        while not self.stopped.is_set():
            try:
                selected = self.selected
                snapshot = request(
                    self.directory,
                    "snapshot",
                    view=self.view,
                    filesRoot=self.files.locations.get(selected, {}).get("root")
                    if self.view == "files"
                    else None,
                    filesScope=self.files.locations.get(selected, {}).get("scope", "working"),
                    threadId=selected.split(":", 1)[1]
                    if selected and not selected.startswith("task:")
                    else None,
                )
                if selected != self.selected:
                    continue  # A response for the previous agent omits the new agent's history.
                self.updates.put(("snapshot_for", (selected, snapshot)))
            except Exception as error:  # noqa: BLE001 -- UI boundary must preserve input and report failures
                self.updates.put(("offline", str(error)))
            thread = self.current()
            delay = 0.15 if self.is_active(thread) or self.pending_messages else 0.5
            self.refresh_requested.wait(delay)
            self.refresh_requested.clear()

    def worker(self):
        while not self.stopped.is_set():
            try:
                action, params = self.commands.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                result = request(self.directory, action, **params)
                self.updates.put(("done", (action, result, params)))
                self.refresh_requested.set()
            except Exception as error:  # noqa: BLE001 -- UI boundary must preserve input and report failures
                self.updates.put(("error", (str(error), action, params)))

    def submit(self, action, **params):
        if self.demo:
            self.notice = "Demo · no messages are sent"
            return
        if action == "send":
            client_id = params.setdefault("clientId", str(uuid.uuid4()))
            thread = self.data.get("threads", {}).get(params["threadId"], {})
            items = self.display_items(thread)
            self.pending_messages[client_id] = {
                "id": client_id,
                "clientId": client_id,
                "threadId": params["threadId"],
                "type": "userMessage",
                "text": params["text"],
                "delivery": "sending",
                "afterId": items[-1].get("id") if items else None,
            }
        self.commands.put((action, params))
        self.refresh_requested.set()
        self.notice = "" if action == "send" else "Connecting to chat…" if action == "select" else "Working…"

    def display_items(self, thread):
        items = list(thread.get("items", []))
        delivered = {item.get("clientId") for item in items if item.get("type") == "userMessage"}
        for client_id, pending in list(self.pending_messages.items()):
            if pending["threadId"] != thread.get("id"):
                continue
            if client_id in delivered:
                del self.pending_messages[client_id]
                continue
            index = next(
                (
                    i + 1
                    for i, item in enumerate(items)
                    if pending["afterId"] is not None
                    and pending["afterId"] in (item.get("id"), item.get("clientId"))
                ),
                0 if pending["afterId"] is None else len(items),
            )
            items.insert(index, pending)
        note = plan_only_stop(thread, items)
        if note:
            items.append(note)
        return items

    def update(self):
        changed = False
        while True:
            try:
                kind, value = self.updates.get_nowait()
            except queue.Empty:
                break
            if kind == "snapshot_for":
                selected, value = value
                if selected != self.selected:
                    continue
                kind = "snapshot"
            changed = changed or kind != "snapshot" or value != self.data
            if kind == "snapshot":
                self.data = value
                enabled = value.get("settings", {}).get("mouseEnabled", True)
                if self.mouse_enabled != enabled:
                    self.mouse_enabled = enabled
                    self.configure_mouse()
            elif kind == "offline":
                self.data["connected"] = False
                self.data["error"] = "Wayland service unavailable"
            elif kind == "error":
                message, action, params = value
                self.notice = message
                if action == "send":
                    pending = self.pending_messages.get(params.get("clientId"))
                    if pending:
                        pending["delivery"] = "unconfirmed"
                    if self.selected == "thread:" + params["threadId"] and not self.buffer:
                        self.buffer = params["text"]
                        self.cursor = len(self.buffer)
                self.panel, self.panel_scroll = "# Request failed\n\n" + message, 0
            elif kind == "done":
                action, result, params = value
                self.notice = ""
                if action == "connect_provider":
                    if result.get("url") and not webbrowser.open(result["url"], new=2):
                        self.panel = (
                            "Browser could not open. Open the official download page:\n\n" + result["url"]
                        )
                    else:
                        self.notice = result.get("message", "")
                if action == "send":
                    pending = self.pending_messages.get(params.get("clientId"))
                    if pending:
                        pending["delivery"] = "delivered"
                    self.drafts.pop("thread:" + params["threadId"], None)
                elif action == "create_agent":
                    self.data.setdefault("threads", {})[result["threadId"]] = result["thread"]
                    self.browse = "agents"
                    self.switch("thread:" + result["threadId"])
                elif action == "rename":
                    for name in ("threads", "archived", "hiddenThreads"):
                        if result["threadId"] in self.data.get(name, {}):
                            self.data[name][result["threadId"]]["name"] = result["name"]
                elif action in ("archive", "remove"):
                    self.data.get("threads", {}).pop(result["threadId"], None)
                    self.selected = None
                    self.buffer, self.cursor = "", 0
                elif action == "archives":
                    self.data["archived"] = result["archived"]
                elif action == "restore":
                    self.browse, self.selected = "agents", None
                elif action == "app_settings":
                    self.data.setdefault("settings", {}).update(result)
                    self.mouse_enabled = result.get("mouseEnabled", True)
                    self.configure_mouse()
                elif action == "trading_settings":
                    self.data["tradingSettings"] = result
                    self.notice = "Trading settings saved"
                elif action == "diagnostics":
                    self.panel, self.panel_scroll = diagnostics_text(result), 0
                elif action == "broker_connection":
                    self.data.setdefault("trading", {})["broker_enabled"] = result["enabled"]
                    self.notice = (
                        "Paper connection requested" if result["enabled"] else "Disconnect requested"
                    )
                elif action == "kill":
                    self.data.setdefault("trading", {})["kill_switch"] = result["enabled"]
                    self.notice = "Entry kill switch " + ("ON" if result["enabled"] else "OFF")
        collection, prefix = {
            "agents": ("threads", "thread:"),
            "archive": ("archived", "archive:"),
            "hidden": ("hiddenThreads", "hidden:"),
        }[self.browse]
        self.rows = [(prefix + t["id"], t) for t in self.data.get(collection, {}).values()]
        self.rows.sort(
            key=lambda row: (unicodedata.normalize("NFC", row[1].get("name") or "").casefold(), row[0])
        )
        if self.selected and not any(key == self.selected for key, _ in self.rows):
            self.selected = None
        if self.rows and self.selected is None:
            self.switch(self.rows[0][0])
            changed = True
        return changed

    def switch(self, key):
        self.native_copy_mode = False
        self.clear_selection()
        if self.wizard and self.wizard["kind"] in ("rename", "setup"):
            self.buffer = self.drafts.get(self.selected, "")
            self.wizard = None
        if self.selected:
            self.drafts[self.selected] = self.buffer
        self.selected = key
        self.buffer = self.drafts.get(key, "")
        self.cursor = len(self.buffer)
        self.scroll = 0
        self.plan_scroll = 0
        if key.startswith("thread:"):
            self.submit("select", threadId=key[7:])
        elif key.startswith("archive:"):
            self.submit("read_archive", threadId=key[8:])

    def browser(self, mode):
        if self.wizard and self.wizard["kind"] == "rename":
            self.buffer = self.drafts.get(self.selected, "")
            self.wizard = None
        if self.selected:
            self.drafts[self.selected] = self.buffer
        self.browse, self.selected = mode, None
        self.buffer, self.cursor, self.scroll = "", 0, 0
        self.focus = "sidebar"
        if mode == "archive":
            self.submit("archives")
        self.notice = {
            "agents": "Agents · select a chat",
            "archive": "Loading archive…",
            "hidden": "Hidden agents · /restore restores the selected agent",
        }[mode]

    def target_thread(self, args):
        if args:
            query = " ".join(args).casefold()
            candidates = {
                t["id"]: t
                for collection in ("threads", "archived", "hiddenThreads")
                for t in self.data.get(collection, {}).values()
            }
            exact = [
                t for t in candidates.values() if query in (t["id"].casefold(), t.get("name", "").casefold())
            ]
            matches = exact or [
                t
                for t in candidates.values()
                if query in t.get("name", "").casefold() or t["id"].startswith(query)
            ]
            if len(matches) != 1:
                raise ValueError("No unique match. Select the agent in the list or enter its full name.")
            return matches[0]["id"]
        if self.selected and not self.selected.startswith("task:"):
            return self.selected.split(":", 1)[1]
        raise ValueError("Select an agent first")

    def cursor_blink_phase(self, now=None):
        if self.focus != "chat" or self.panel or self.native_copy_mode:
            return None
        now = time.monotonic() if now is None else now
        elapsed = now - self.last_input_at if self.last_input_at is not None else now
        return int(max(0, elapsed) / 0.85) % 2

    @staticmethod
    def is_active(entry):
        status = entry.get("status")
        return isinstance(status, dict) and status.get("type") == "active"

    def current(self):
        return next((t for key, t in self.rows if key == self.selected), {})

    def put(self, screen, y, x, text, width=None, style=0):
        h, w = screen.getmaxyx()
        if not (0 <= y < h and 0 <= x < w):
            return
        width = min(width if width is not None else w - x - 1, w - x - 1)
        if width < 1:
            return
        try:
            screen.addstr(y, x, crop(text, width), style)
        except curses.error:
            pass

    def band(self, screen, y, x, width, text, tone="surface", bold=False):
        style = self.styles[tone] | (curses.A_BOLD if bold else 0)
        self.put(screen, y, x, " " * width, width, style)
        self.put(screen, y, x, text, width, style)

    def toggle_view(self, target=None, direction=1):
        self.cancel_setup_editor()
        self.clear_selection()
        if target is None:
            views = ("chat", "plan", "market", "tools", "files", "processes", "setup")
            self.view = views[(views.index(self.view) + direction) % len(views)]
        else:
            self.view = "chat" if self.view == target else target
        self.scroll = 0
        self.plan_scroll = 0
        self.focus = "tabs" if target is None else "history"

    def cancel_setup_editor(self):
        if self.wizard and self.wizard["kind"] == "setup":
            self.buffer = self.drafts.get(self.selected, "")
            self.cursor = len(self.buffer)
            self.wizard = None

    def return_to_chat(self):
        self.cancel_setup_editor()
        self.clear_selection()
        self.view, self.focus = "chat", "chat"
        self.scroll = self.plan_scroll = 0

    def scroll_content(self, delta):
        if not self.selection_dragging:
            self.clear_selection()
        if self.view == "files":
            self.files.scroll = max(0, self.files.scroll - delta)
        elif self.view in ("processes", "market"):
            self.process_scroll = max(0, self.process_scroll - delta)
        elif self.view == "setup":
            self.setup.scroll = max(0, self.setup.scroll - delta)
        elif self.view == "plan":
            self.plan_scroll = max(0, self.plan_scroll - delta)
        else:
            self.scroll = max(0, self.scroll + delta)

    def clear_selection(self):
        self.selection_anchor = self.selection_end = None
        self.selection_dragging = False
        self.pending_link = None
        self.history_snapshot = None

    def selection_point(self, x, y):
        text_rows = [row for row, value in self.history_cells.items() if value["text"]]
        if not text_rows:
            return None
        y = min(text_rows, key=lambda row: abs(row - y))
        row = self.history_cells[y]
        return row["index"], len(crop(row["text"], max(0, x - row["x"])))

    def finish_selection(self, x=None, y=None):
        self.scrollbar_grab = None
        if not self.selection_dragging:
            return
        if x is not None:
            self.selection_end = self.selection_point(x, y)
        pending = getattr(self, "pending_link", None)
        self.pending_link = None
        if pending and x is not None and (x, y) == pending[:2] and self.link_at(x, y) == pending[2]:
            self.clear_selection()
            if web_url(pending[2]):
                threading.Thread(target=self.open_link, args=(pending[2],), daemon=True).start()
            return
        self.selection_dragging = False
        text = selection_text(self.history_rows, self.selection_anchor, self.selection_end)
        if not text:
            self.clear_selection()
            return

    def copy_selection(self):
        text = selection_text(self.history_rows, self.selection_anchor, self.selection_end)
        if not text:
            return
        try:
            copy_text(text)
            self.clear_selection()
        except Exception as error:  # noqa: BLE001 -- UI boundary must preserve input and report failures
            self.notice = "Could not copy selection: " + str(error)

    def seek_scrollbar(self, y):
        self.clear_selection()
        bar = self.scrollbar
        if not bar:
            return
        travel = bar["height"] - bar["size"]
        offset = (
            round(max(0, min(travel, y - bar["top"] - (self.scrollbar_grab or 0))) * bar["maximum"] / travel)
            if travel
            else 0
        )
        if self.view == "files":
            self.files.scroll = offset
        elif self.view in ("processes", "market"):
            self.process_scroll = offset
        elif self.view == "setup":
            self.setup.scroll = offset
        elif self.view == "plan":
            self.plan_scroll = offset
        else:
            self.scroll = bar["maximum"] - offset

    def begin_rename(self):
        if self.wizard:
            self.notice = "Finish or cancel the current form first"
            return
        try:
            tid = self.target_thread([])
        except ValueError as error:
            self.notice = str(error)
            return
        self.drafts[self.selected] = self.buffer
        self.wizard = {"kind": "rename", "threadId": tid, "label": "Rename agent"}
        self.buffer, self.cursor, self.focus = "", 0, "chat"
        self.notice = "Type a new name · Enter saves · Esc cancels"

    def draft_layout(self):
        if not self.draft_cache or (self.draft_cache.text, self.draft_cache.width) != (
            self.buffer,
            self.draft_width,
        ):
            self.draft_cache = DraftLayout(self.buffer, self.draft_width)
        return self.draft_cache

    def insert_text(self, text):
        self.last_input_at = time.monotonic()
        self.buffer = self.buffer[: self.cursor] + text + self.buffer[self.cursor :]
        self.cursor += len(text)
        self.preferred_column = None

    def move_vertical(self, delta):
        layout = self.draft_layout()
        row, column = layout.position(self.cursor)
        if self.preferred_column is None:
            self.preferred_column = column
        self.cursor = layout.index_at(row + delta, self.preferred_column)

    def render_native_history(self, screen):
        """Give Terminal whole text rows, without dashboard columns or box borders."""
        h, w = screen.getmaxyx()
        rows = []
        for item in self.display_items(self.current()):
            kind = item.get("type")
            if kind not in ("userMessage", "agentMessage", "appNotice"):
                continue
            text = conversation_text(item)
            if not text.strip():
                continue
            speaker = {"userMessage": "YOU", "agentMessage": "AGENT", "appNotice": "TYRELL"}[kind]
            rows.append({"text": speaker, "tone": "accent"})
            for part, _ in markdown_rows(text, max(8, w - 1), wrap, crop):
                rows.append({"text": part.rstrip(), "tone": "base"})
            rows.append({"text": "", "tone": "base"})
        visible, self.scroll = viewport(rows, max(1, h - 1), self.scroll)
        self.history_bounds = None
        self.hit_copy = self.hit_request = self.scrollbar = None
        self.history_cells = {}
        for y, row in enumerate(visible):
            self.put(screen, y, 0, row["text"], w - 1, self.styles[row["tone"]])
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.refresh()

    def render(self, screen):
        screen.bkgd(" ", self.styles["base"])
        screen.erase()
        if self.native_selection_mode():
            self.render_native_history(screen)
            return
        h, w = screen.getmaxyx()
        self.last_width = w
        self.pointer_layout_ready = h >= 18 and w >= 70
        context = (h, w, self.selected, self.view, self.browse, bool(self.panel))
        if self.draw_context != context:
            if hasattr(screen, "clearok"):
                screen.clearok(True)
            self.draw_context = context
        if h < 18 or w < 70:
            self.put(screen, 0, 0, "Resize the terminal to at least 70 × 18. Ctrl+Q closes the view.")
            screen.refresh()
            self.update_pointer()
            return
        s = self.styles
        bold = curses.A_BOLD
        labels = [status_label(t, self.provider_connected(t)) for t in self.data.get("threads", {}).values()]
        work = sum(v in ("working", "quiet (active)") for v in labels)
        wait = sum(v in ("approval", "question", "waiting") for v in labels)
        badges = connection_badges(self.data, w - (64 if w >= 120 else 28), self.demo)
        connection_x = w - sum(cells(text) + 2 for text, _ in badges) - 1
        self.band(screen, 0, 0, w - 1, "", "surface", True)
        self.put(
            screen,
            0,
            0,
            "  Wayland Trading · Models reason. Code controls execution.",
            connection_x - 1,
            s["surface"] | bold,
        )
        self.hit_connections = []
        for text, tone in badges:
            self.put(screen, 0, connection_x, text, cells(text), s[tone] | bold)
            self.hit_connections.append((connection_x, connection_x + cells(text)))
            connection_x += cells(text) + 2
        metrics = "  %d Agents   " % len(labels)
        self.put(screen, 1, 0, metrics, style=s["muted"])
        self.put(screen, 1, cells(metrics), "● %d working" % work, style=s["working"])
        self.put(screen, 1, cells(metrics) + 16, "● %d waiting" % wait, style=s["warning"])
        self.put(
            screen,
            1,
            cells(metrics) + 32,
            "● %d Ready" % sum(v in ("idle", "saved") for v in labels),
            style=s["success"],
        )
        if w >= 110 and self.data.get("preventingSleep"):
            self.put(screen, 1, w - 24, "Keeping Mac awake", style=s["muted"])
        self.band(screen, 2, 1, w - 3, "", "divider")

        sidebar = min(33, max(24, w // 4))
        self.sidebar_width, self.screen_height = sidebar, h
        self.hit_rows = []
        for y in range(3, h - 2):
            self.put(screen, y, sidebar, "│", 1, s["accent" if self.focus == "sidebar" else "muted"])
        sidebar_title = {"agents": "AGENTS", "archive": "ARCHIVE", "hidden": "HIDDEN"}[self.browse]
        self.put(screen, 3, 2, sidebar_title, sidebar - 3, s["accent"] | bold)
        index = next((i for i, (key, _) in enumerate(self.rows) if key == self.selected), 0)
        stride = 1
        capacity = max(1, (h - 11) // stride)
        start = max(0, index - capacity + 1)
        for n, (key, thread) in enumerate(self.rows[start : start + capacity]):
            y = 5 + n * stride
            self.hit_rows.append((y, y + stride, key))
            label = (
                thread["status"]
                if key.startswith("task:")
                else status_label(thread, self.provider_connected(thread))
            )
            if self.browse == "archive":
                label = "Archived"
            tone = status_tone(label)
            selected = key == self.selected
            name = thread.get("name") or thread.get("title", "Unnamed")
            icon = "◈"
            self.put(screen, y, 2, icon, 2, s["base"])
            self.put(screen, y, 5, "●", 1, s[tone])
            available = min(20, sidebar - 8)
            if cells(name) > available:
                name = crop(name, available - 1) + "…"
            self.put(screen, y, 7, name, available, s["agentname"] | (curses.A_UNDERLINE if selected else 0))
        self.put(screen, h - 6, 2, "F5  Rename", sidebar - 3, s["muted"])
        self.put(
            screen,
            h - 5,
            2,
            "F4  " + ("Archive" if self.browse == "agents" else "Back to agents"),
            sidebar - 3,
            s["accent"],
        )
        self.put(screen, h - 4, 2, "F3  New agent", sidebar - 3, s["accent"])
        self.put(screen, h - 3, 2, "↑↓ Select · Enter Write", sidebar - 3, s["muted"])

        x, width = sidebar + 2, w - sidebar - 4
        t = self.current()
        name = t.get("name") or t.get("title") or "Select a chat or create a task"
        self.hit_copy = None
        self.hit_text_view = None
        show_copy = self.native_terminal and self.view == "chat"
        self.put(screen, 3, x, name, width - (22 if show_copy else 7), s["base"] | bold)
        if show_copy:
            text_x, copy_x = x + width - 20, x + width - 13
            self.put(screen, 3, text_x, "[Text]", 6, s["accent"])
            self.hit_text_view = (text_x, text_x + 6, 3)
            has_selection = self.selection_anchor is not None and self.selection_end != self.selection_anchor
            self.put(screen, 3, copy_x, "[Copy]", 6, s["accent" if has_selection else "muted"] | bold)
            self.hit_copy = (copy_x, copy_x + 6, 3)
        self.put(screen, 3, x + width - 5, "[Esc]", 5, s["accent"])
        model = "Codex CLI · " + (t.get("model") or "Not configured")
        self.put(screen, 4, x, model, width, s["accent"])
        tab_y = 6
        trading = self.data.get("trading", {})
        detail = trading.get("broker_status", "Broker not verified")
        detail += " · Kill: " + ("ON" if trading.get("kill_switch") else "OFF")
        self.put(screen, 5, x, detail, width, s["muted"])
        items = self.display_items(t)
        messages = sum(
            i.get("type") in ("userMessage", "agentMessage") and bool(conversation_text(i).strip())
            for i in items
        )
        tool_count = sum(i.get("type") not in ("userMessage", "agentMessage") for i in items)
        steps = t.get("plan", [])
        done = sum(step["status"] == "completed" for step in steps)
        plan_caption = " PLAN %d/%d " % (done, len(steps)) if steps else " PLAN Pending "
        tabs = [
            ("chat", " CHAT %d " % messages),
            ("plan", plan_caption),
            ("market", " MARKET "),
            ("tools", " AUDIT %d " % tool_count),
            ("files", " POSITIONS "),
            ("processes", " ORDERS "),
            ("setup", " SETUP "),
        ]
        if sum(len(title) + 1 for _, title in tabs) > width:
            tabs = [
                ("chat", " CHAT "),
                ("plan", plan_caption),
                ("market", " MARKET "),
                ("tools", " AUDIT "),
                ("files", " POSITIONS "),
                ("processes", " ORDERS "),
                ("setup", " SETUP "),
            ]
        if sum(len(title) + 1 for _, title in tabs) > width:
            tabs = [(view, title.strip()) for view, title in tabs]
        if sum(len(title) + 1 for _, title in tabs) > width:
            tabs = [
                (
                    view,
                    {
                        "market": "Mkt",
                        "chat": "Chat",
                        "plan": ("Plan%d/%d" % (done, len(steps)) if steps else "Plan…"),
                        "tools": "Audit",
                        "files": "Pos",
                        "processes": "Orders",
                        "setup": "Setup",
                    }[view],
                )
                for view, _ in tabs
            ]
        self.tab_y, self.hit_tabs = tab_y, []
        self.band(screen, tab_y, x, width, "", "surface")
        tab_x = x
        for view, title in tabs:
            self.band(
                screen, tab_y, tab_x, len(title), title, "selected" if self.view == view else "surface", True
            )
            if self.focus == "tabs" and self.view == view:
                self.put(
                    screen, tab_y, tab_x, title, len(title), s["accent"] | curses.A_BOLD | curses.A_UNDERLINE
                )
            self.hit_tabs.append((tab_x, tab_x + len(title), view))
            tab_x += len(title) + 1
        label = (
            status_label(t, self.provider_connected(t))
            if t and not (self.selected or "").startswith("task:")
            else "planned"
        )
        if self.browse == "archive":
            label = "Archived"
        active = self.is_active(t)
        elapsed = (
            time.time() - t["startedAt"]
            if active and t.get("startedAt")
            else (t.get("durationMs") or 0) / 1000
        )
        thinking = bool(items and items[-1].get("type") == "reasoning")
        activity = activity_indicator(label, time.monotonic(), thinking)
        if elapsed:
            activity += " · " + duration(elapsed)
        if self.view == "setup":
            activity = "Saving…" if self.setup.pending else "Validated paper settings · LIVE locked"
        self.put(screen, tab_y + 1, x, activity, width, s[status_tone(label)])
        # macOS curses drops long Unicode runs when using terminfo's repeat-char
        # optimization. Underlined spaces give us a reliable, thin divider.
        self.put(
            screen,
            tab_y + 2,
            x,
            " " * width,
            width,
            s["historyfocus" if self.focus == "history" else "muted"] | curses.A_UNDERLINE,
        )
        body_start = tab_y + 3
        self.draft_width = width - 5
        layout = self.draft_layout()
        minimum = 3 if h >= 28 else 2 if h >= 20 else 1
        maximum = 6 if h >= 34 else 4 if h >= 26 else minimum
        self.draft_height = max(minimum, min(maximum, len(layout.lines)))
        composer_bottom = h - 4
        composer_top = composer_bottom - self.draft_height - 1
        self.draft_top, self.draft_left = composer_top + 1, x + 2
        cursor_row, cursor_column = layout.position(self.cursor)
        self.draft_offset = min(self.draft_offset, cursor_row)
        self.draft_offset = max(self.draft_offset, cursor_row - self.draft_height + 1, 0)
        self.draft_offset = min(self.draft_offset, max(0, len(layout.lines) - self.draft_height))
        # Budget the task area after the composer so even small terminals retain chat space.
        room = max(0, composer_top - 1 - body_start - 5)
        estimate = estimate_label(t, label)
        if estimate and room and self.view == "plan":
            self.put(screen, body_start, x, estimate, width, s["muted"])
            body_start, room = body_start + 1, room - 1
        if self.view == "plan":
            self.put(screen, body_start, x, "PLAN · %d/%d done" % (done, len(steps)), width, s["accent"])
            body_start += 1
        elif self.view == "tools":
            self.put(screen, body_start, x, "AUDIT · recorded decisions and runtime events", width, s["tool"])
            body_start += 1
        available = max(1, composer_top - 1 - body_start)
        content_width = width - 2
        history_context = (self.selected, self.view, content_width)
        if self.history_context != history_context:
            self.clear_selection()
            self.history_context = history_context
        cache_key = (
            self.selected,
            self.view,
            content_width,
            tuple((i.get("id"), i.get("type"), i.get("text"), i.get("delivery")) for i in items),
        )
        if cache_key != self.timeline_key and not self.selection_dragging:
            self.timeline_rows = self.timeline_cache.render(
                items, content_width, self.view, wrap, crop, self.selected
            )
            if (
                self.history_snapshot is not None
                and self.selection_anchor is not None
                and self.selection_end is not None
            ):
                # Keep a completed selection only while its source rows still match.
                # Appending a reply must never freeze the entire conversation.
                last = max(self.selection_anchor[0], self.selection_end[0]) + 1
                if self.history_snapshot[:last] != self.timeline_rows[:last]:
                    self.clear_selection()
            if self.timeline_key and self.timeline_key[:3] == cache_key[:3] and self.scroll:
                self.scroll += max(0, len(self.timeline_rows) - self.timeline_length)
            self.timeline_length = len(self.timeline_rows)
            self.timeline_key = cache_key
        source_rows = (
            self.history_snapshot
            if self.selection_dragging and self.history_snapshot is not None
            else self.timeline_rows
        )
        visible, self.scroll = viewport(source_rows, available, self.scroll)
        total = len(source_rows)
        offset = max(0, total - available - self.scroll)
        if self.view == "plan":
            rows = (
                self.history_snapshot
                if self.selection_dragging and self.history_snapshot is not None
                else plan_rows(steps, content_width, wrap)
            )
            self.plan_scroll = min(self.plan_scroll, max(0, len(rows) - available))
            visible = [
                {**row, "source_index": index}
                for index, row in enumerate(
                    rows[self.plan_scroll : self.plan_scroll + available], self.plan_scroll
                )
            ]
            total, offset = len(rows), self.plan_scroll
            source_rows = rows
        elif self.view == "files":
            visible = self.files.visible(self, available, content_width)
            source_rows = self.files.rows
            total, offset = len(source_rows), self.files.scroll
            self.files.hits = {
                body_start + row: line["source_index"] for row, line in enumerate(visible) if line["action"]
            }
        elif self.view in ("processes", "market"):
            source_rows = process_rows(dict(self.data, _view=self.view), self.selected, content_width, wrap)
            self.process_scroll = min(self.process_scroll, max(0, len(source_rows) - available))
            visible = [
                {**row, "source_index": index}
                for index, row in enumerate(
                    source_rows[self.process_scroll : self.process_scroll + available], self.process_scroll
                )
            ]
            total, offset = len(source_rows), self.process_scroll
        elif self.view == "setup":
            visible = self.setup.visible(self, available)
            source_rows = self.setup.rows
            total, offset = len(source_rows), self.setup.scroll
            self.setup.hits = {body_start + row: line["source_index"] for row, line in enumerate(visible)}
        self.history_rows = source_rows
        self.history_cells = {}
        self.history_bounds = (x, body_start, content_width, available)
        if not visible:
            text = t.get("prompt") or (
                "No tool output yet."
                if self.view == "tools"
                else "Write a message to the agent below.\n\nF3 creates an agent. Configure shared connections in F10."
            )
            if self.browse != "agents":
                text = (
                    "Select a chat in the list. /restore adds it to the sidebar.\n\nF4 returns to agents."
                    if self.rows
                    else "This list is empty. F4 returns to agents."
                )
            if t.get("error"):
                text += "\n" + t["error"]
            if self.view == "plan":
                text = "The agent has not shared a plan yet.\n\nSteps will appear here as the agent publishes its plan."
            for row, line in enumerate(wrap(text, content_width)[:available]):
                self.put(screen, body_start + row, x, line, content_width, s["muted"])
        for row, line in enumerate(visible):
            inset = line["inset"]
            self.band(
                screen,
                body_start + row,
                x + inset,
                content_width - inset,
                line["text"],
                line["tone"],
                line["header"],
            )
            if line.get("footer"):
                self.put(
                    screen,
                    body_start + row,
                    x + inset,
                    " " * (content_width - inset),
                    content_width - inset,
                    s[line["tone"]] | curses.A_UNDERLINE,
                )
            if line.get("setup_selected"):
                self.put(
                    screen,
                    body_start + row,
                    x + inset,
                    line["text"],
                    content_width - inset,
                    s[line["tone"] if line["tone"] in ("success", "error") else "accent"]
                    | curses.A_UNDERLINE,
                )
            offset_x = x + inset
            for text, tone in line.get("spans", []):
                self.put(
                    screen, body_start + row, offset_x, text, max(0, x + content_width - offset_x), s[tone]
                )
                offset_x += cells(text)
            if line.get("copy_text") is not None:
                text_x = x + inset + line.get("copy_offset", 0)
                self.history_cells[body_start + row] = {
                    "index": line["source_index"],
                    "x": text_x,
                    "text": line["copy_text"],
                    "links": line.get("links", []),
                }
                for left, right, url in line.get("links", []):
                    self.put(
                        screen,
                        body_start + row,
                        text_x + cells(line["copy_text"][:left]),
                        line["copy_text"][left:right],
                        cells(line["copy_text"][left:right]),
                        s["accent"] | curses.A_UNDERLINE,
                    )
                if self.selection_anchor is not None and self.selection_end is not None:
                    first, last = sorted((self.selection_anchor, self.selection_end))
                    index = line["source_index"]
                    if first[0] <= index <= last[0]:
                        text = line["copy_text"]
                        left = first[1] if index == first[0] else 0
                        right = last[1] if index == last[0] else len(text)
                        self.put(
                            screen,
                            body_start + row,
                            text_x + cells(text[:left]),
                            text[left:right],
                            style=s["selected"],
                        )

        thumb, size, maximum = scrollbar_geometry(total, available, offset)
        self.scrollbar = {
            "x": x + width - 1,
            "top": body_start,
            "height": available,
            "thumb": thumb,
            "size": size,
            "maximum": maximum,
        }
        for row in range(available):
            selected = maximum and thumb <= row < thumb + size
            self.put(
                screen,
                body_start + row,
                x + width - 1,
                "█" if selected else "│",
                1,
                s["scrollthumb" if selected else "scrolltrack"],
            )

        pending = [
            r for r in self.data.get("requests", []) if r.get("params", {}).get("threadId") == t.get("id")
        ]
        status_text = "↑ History · scroll down for latest" if self.scroll else self.notice
        if self.view == "plan":
            status_text = "Scroll / PgUp / PgDn to browse · checklist updates automatically"
        if self.focus == "history" and not self.selection_anchor:
            status_text = "History"
        if self.view == "setup" and self.setup.rows:
            status_text = (
                self.notice
                if self.wizard and self.wizard["kind"] == "setup"
                else self.setup.rows[self.setup.index]["help"]
            )
        status_tone_name = "muted"
        self.hit_request = None
        if pending or label in ("approval", "question"):
            status_text, status_tone_name = "● Waiting · Click to review request", "warning"
            self.hit_request = (x, x + width, composer_top - 1)
        if t.get("externalWriter") and not pending:
            status_text, status_tone_name = "Viewing · Agent is controlled by another client", "warning"
        if label == "waiting" and not pending:
            status_text, status_tone_name = "● Waiting · See the latest reply or Connections", "warning"
        if not self.provider_connected(t):
            error = self.data.get("error") or "Wayland service reconnecting…"
            status_text, status_tone_name = "Disconnected · " + str(error), "warning"
        if self.data.get("sleepError"):
            status_text, status_tone_name = "Could not prevent sleep: " + self.data["sleepError"], "warning"
        self.put(screen, composer_top - 1, x, status_text, width, s[status_tone_name])
        self.draw_commands(screen, composer_top - 1, x, width)
        border = s["accent" if self.focus == "chat" else "muted"]
        self.put(screen, composer_top, x, "╭" + "─" * (width - 2) + "╮", width, border)
        prompt = self.wizard["label"] if self.wizard else "Prompt"
        self.put(screen, composer_top, x + 2, " " + crop(prompt, width - 8) + " ", width - 5, border | bold)
        placeholder = ""
        for row in range(self.draft_height):
            y = self.draft_top + row
            self.band(screen, y, x + 1, width - 2, "", "input")
            self.put(screen, y, x, "│", 1, border)
            self.put(screen, y, x + width - 1, "│", 1, border)
            index = self.draft_offset + row
            text = layout.lines[index] if index < len(layout.lines) else ""
            if not self.buffer and row == 0:
                self.put(screen, y, self.draft_left, placeholder, self.draft_width, s["inputmuted"])
            else:
                self.put(screen, y, self.draft_left, text, self.draft_width, s["input"])
        self.put(screen, composer_bottom, x, "╰" + "─" * (width - 2) + "╯", width, border)
        if len(layout.lines) > self.draft_height:
            counter = " %d/%d lines " % (cursor_row + 1, len(layout.lines))
            self.put(screen, composer_bottom, x + width - len(counter) - 3, counter, style=border)
        if self.wizard and self.wizard["kind"] == "rename":
            self.put(screen, h - 3, x, "Type a new name · Enter saves · Esc cancels", width, s["muted"])
        self.band(
            screen,
            h - 1,
            0,
            w - 1,
            " F1 Help  F2 Tabs  F3 New  F4 Archive  F5 Rename  F6 Context  F10 Connect  Tab Focus  Ctrl+Q Quit",
            "surface",
        )
        if self.panel == "APPEARANCE":
            self.appearance.render(self, screen)
        elif self.panel == "CONNECTIONS":
            self.connections.render(self, screen)
        elif self.panel:
            for y in range(2, h - 1):
                self.band(screen, y, 1, w - 3, "", "surface")
            self.hub_hits = []
            is_help = (
                self.panel == HELP or self.panel in ("HUB SETTINGS", "SKILLS") or self.panel.startswith("# ")
            )
            rich_content = (
                settings_text(self.data, self.directory)
                if self.panel == "HUB SETTINGS"
                else self.skills_panel()
                if self.panel == "SKILLS"
                else self.panel
            )
            help_content = help_rows(w - 8, rich_content) if is_help else []
            is_preview = self.panel.startswith("AGENT SETUP PREVIEW")
            preview = preview_rows(self.panel, w - 8, wrap) if is_preview else []
            panel_lines = (
                [row["text"] for row in help_content]
                if is_help
                else [row["text"] for row in preview]
                if is_preview
                else wrap(
                    connections_text(self.data, self.demo) if self.panel == "CONNECTIONS" else self.panel,
                    w - 8,
                )
            )
            self.put(screen, 2, w - 8, "[Esc]", 5, s["accent"])
            self.panel_scroll = min(self.panel_scroll, max(0, len(panel_lines) - (h - 7)))
            for y, line in enumerate(panel_lines[self.panel_scroll : self.panel_scroll + h - 7], 4):
                if is_help:
                    if self.panel == "HUB SETTINGS":
                        for action in ("1", "2", "3", "D", "H", "M", "K", "G", "C"):
                            if line.lstrip().startswith("[" + action + "]"):
                                self.hub_hits.append((6, w - 4, y, action.lower()))
                    offset_x = 4
                    for text, tone in help_content[self.panel_scroll + y - 4]["spans"]:
                        self.put(
                            screen,
                            y,
                            offset_x,
                            text,
                            max(0, w - 4 - offset_x),
                            s[tone] | (bold if tone == "accent" else 0),
                        )
                        offset_x += cells(text)
                    # Indented text starts with spaces at x=4. Draw the selection
                    # marker afterwards so those spaces cannot erase it.
                    if self.panel == "HUB SETTINGS" and line.lstrip().startswith(
                        "[" + self.hub_selected.upper() + "]"
                    ):
                        self.put(screen, y, 4, "›", 1, s["accent"] | bold)
                    continue
                heading = preview[self.panel_scroll + y - 4]["heading"] if is_preview else y == 4
                self.put(
                    screen,
                    y,
                    4,
                    line,
                    w - 8,
                    s["error" if is_preview and preview[self.panel_scroll + y - 4].get("error") else "accent"]
                    | bold
                    if heading
                    else s["surface"],
                )
            footer = (
                (
                    (self.hub_saved + " · " if self.hub_saved else "")
                    + "↑↓ Select · Enter Edit · PgUp/PgDn Scroll · [Esc]"
                )
                if self.panel == "HUB SETTINGS"
                else "↑↓ / PgUp / PgDn scroll · [Esc]"
            )
            if self.approval and self.approval["method"] != "item/tool/requestUserInput":
                footer += " · y approve once / n deny"
            self.put(screen, h - 3, 4, footer, w - 8, s["warning"])
        try:
            curses.curs_set(1 if self.cursor_blink_phase() == 0 else 0)
            if self.focus == "chat" and not self.panel:
                screen.move(
                    self.draft_top + cursor_row - self.draft_offset,
                    min(x + width - 3, self.draft_left + cursor_column),
                )
        except curses.error:
            pass
        screen.refresh()
        self.update_pointer()

    def open_hub_settings(self):
        self.appearance.cancel(self)
        if self.wizard:
            self.notice = "Finish or cancel the current input before opening Settings"
            return
        self.approval = None
        self.panel, self.panel_scroll = "CONNECTIONS", 0

    def hub_action(self, action):
        if action == "m":
            self.submit("app_settings", patch={"mouseEnabled": not self.mouse_enabled})
        elif action == "c":
            self.panel = "APPEARANCE"
        elif action == "g":
            self.panel, self.view, self.focus = None, "setup", "history"
        elif action == "k":
            self.submit("kill", enabled=not self.data.get("trading", {}).get("kill_switch", False))
        elif action in ("h", "1", "2", "3"):
            self.panel, self.panel_scroll = "CONNECTIONS", 0
        elif action == "d":
            self.submit("diagnostics")

    def begin_handoff(self):
        self.panel = "# Role context\n\nEach Wayland role keeps its own analytical conversation.\nTrading state is shared inside Wayland only. No development sessions are imported."
        self.panel_scroll = 0

    def provider_connected(self, thread):
        return self.data.get("connected", False)

    def open_requests(self):
        self.notice = "No broker permissions can be granted through chat. See Setup and Connections."

    def next_question(self):
        self.open_requests()

    def decide(self, accept):
        self.open_requests()

    def available_skills(self):
        return []

    def skills_panel(self):
        return "# Analytical capabilities\n\nWayland roles use supplied trading context without broker tools."

    def command_options(self):
        if self.focus != "chat" or self.panel or self.wizard or self.pasting:
            return []
        if self.command_query != self.buffer:
            self.command_query = self.buffer
            self.command_index = 0
            self.command_dismissed = None
        if self.command_dismissed == self.buffer:
            return []
        return command_choices(self.buffer, self.available_skills())

    def complete_command(self, options):
        if options:
            self.buffer = options[self.command_index % len(options)][0] + " "
            self.cursor = len(self.buffer)
            self.command_hits = []

    def draw_commands(self, screen, top, x, width):
        self.command_hits = []
        options = self.command_options()
        if not options:
            return
        count = min(7, len(options), max(0, top - 3))
        if not count:
            return
        self.command_index %= len(options)
        first = min(max(0, self.command_index - count + 1), len(options) - count)
        self.history_cells.pop(top - count - 1, None)
        self.band(
            screen,
            top - count - 1,
            x,
            width,
            " Commands %d/%d · ↑↓ · Tab/Enter · Esc" % (self.command_index + 1, len(options)),
            "surface",
        )
        for row, index in enumerate(range(first, first + count)):
            name, description = options[index]
            y = top - count + row
            self.band(
                screen,
                y,
                x,
                width,
                ("› " if index == self.command_index else "  ") + name + "  " + description,
                "selected" if index == self.command_index else "surface",
            )
            self.command_hits.append((x, x + width, y, index))
            self.history_cells.pop(y, None)

    def entered(self, intervention=False):
        text = self.buffer.strip()
        if not text:
            return
        if self.wizard:
            kind = self.wizard["kind"]
            if kind == "rename":
                self.submit("rename", threadId=self.current()["id"], name=text)
            elif kind == "new":
                self.submit("create_agent", name=text)
            elif kind == "setup":
                self.submit("trading_settings", patch={self.wizard["field"]: text})
            return_panel = self.wizard.get("return_panel")
            self.wizard = None
            self.buffer = self.drafts.get(self.selected, "")
            self.cursor = len(self.buffer)
            if return_panel:
                self.panel = return_panel
            return
        if text.startswith("/"):
            self.buffer, self.cursor = "", 0
            command, _, args = text.partition(" ")
            views = {
                "/chat": "chat",
                "/plan": "plan",
                "/tasks": "plan",
                "/audit": "tools",
                "/log": "tools",
                "/positions": "files",
                "/orders": "processes",
                "/market": "market",
                "/setup": "setup",
            }
            if command in views:
                self.view, self.focus = views[command], "history"
                self.scroll = self.plan_scroll = self.process_scroll = 0
            elif command in ("/quit", "/q"):
                self.stopped.set()
            elif command == "/help":
                self.panel, self.panel_scroll = HELP, 0
            elif command == "/settings":
                self.open_hub_settings()
            elif command == "/connections":
                self.panel, self.panel_scroll = "CONNECTIONS", 0
            elif command == "/mouse":
                self.submit("app_settings", patch={"mouseEnabled": not self.mouse_enabled})
            elif command == "/kill":
                if args not in ("on", "off"):
                    self.notice = "Usage: /kill on | off"
                else:
                    self.submit("kill", enabled=args == "on")
            elif command == "/new":
                self.wizard = {"kind": "new", "field": "name", "label": "New analytical role · Name"}
                self.focus = "chat"
            elif command == "/rename":
                if args:
                    self.submit("rename", threadId=self.current()["id"], name=args)
                else:
                    self.begin_rename()
            elif command in ("/archives", "/hidden", "/agents"):
                self.browser({"/archives": "archive", "/hidden": "hidden", "/agents": "agents"}[command])
            elif command in ("/archive", "/remove", "/restore"):
                self.submit(command[1:], threadId=self.current()["id"])
            elif command == "/interrupt":
                self.submit("interrupt", threadId=self.current()["id"])
            else:
                self.notice = "Unknown command. Type / to browse."
            return
        if not self.current() or self.browse != "agents":
            self.notice = "Select an active role or restore it from the archive."
            return
        self.drafts[self.selected] = text
        self.clear_selection()
        self.view = "chat"
        self.submit("send", threadId=self.current()["id"], text=text)
        self.buffer, self.cursor, self.scroll = "", 0, 0

    def input_key(self, key):
        if self.pasting:
            if isinstance(key, str):
                self.paste_buffer += key
                if self.paste_buffer.endswith("\x1b[201~"):
                    pasted = self.paste_buffer[:-6].replace("\r\n", "\n").replace("\r", "\n")
                    text = "".join(c for c in pasted if c.isprintable() or c in "\n\t")[:100000]
                    if self.panel == "APPEARANCE":
                        self.appearance.paste(self, text)
                    else:
                        self.insert_text(text)
                        self.focus = "chat"
                        self.notice = "Text pasted. Press Enter to send."
                    self.paste_buffer, self.pasting = "", False
            return
        if self.escape:
            if not isinstance(key, str):
                return
            self.escape += key
            self.escape_at = time.monotonic()
            newlines = ("\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;2~", "\x1b\r", "\x1b\n")
            if self.escape in newlines:
                self.escape, self.focus = "", "chat"
                self.insert_text("\n")
            elif self.escape.startswith("\x1b[M"):
                # Legacy X10 reports contain three coordinate bytes after the prefix.
                if len(self.escape) == 6:
                    button, x, y = (ord(c) - 32 for c in self.escape[3:])
                    if self.mouse_enabled:
                        self.mouse(button, x - 1, y - 1)
                    self.escape = ""
            elif self.escape == "\x1b[200~":
                self.pasting, self.escape = True, ""
            elif self.escape in self.key_sequences:
                mapped, self.escape = self.key_sequences[self.escape], ""
                self.key(mapped)
            elif self.escape.startswith("\x1b["):
                if len(self.escape) > 2 and "@" <= key <= "~":
                    match = re.fullmatch(r"\x1b\[<(\d+);(\d+);(\d+)([mM])", self.escape)
                    legacy = re.fullmatch(r"\x1b\[(\d+);(\d+);(\d+)M", self.escape)
                    if match and self.mouse_enabled:
                        if match[4] == "M":
                            self.mouse(int(match[1]), int(match[2]) - 1, int(match[3]) - 1)
                        else:
                            self.finish_selection(int(match[2]) - 1, int(match[3]) - 1)
                    elif legacy and self.mouse_enabled:
                        self.mouse(int(legacy[1]) - 32, int(legacy[2]) - 1, int(legacy[3]) - 1)
                    else:
                        mapped = enhanced_key(self.escape)
                        if mapped == "copy":
                            self.copy_selection()
                        elif mapped == "intervene":
                            if (
                                self.focus == "chat"
                                and not self.wizard
                                and not self.panel
                                and not self.buffer.lstrip().startswith("/")
                            ):
                                self.entered(intervention=True)
                        elif mapped == "newline":
                            self.focus = "chat"
                            self.insert_text("\n")
                        elif mapped is not None:
                            self.key(mapped)
                    # Unknown terminal reports are consumed as a unit, never inserted.
                    self.escape = ""
                elif len(self.escape) > 128:
                    self.escape = "\x1b["  # Drain an oversized report up to its final byte.
            elif self.escape == "\x1bO":
                pass
            else:
                self.escape = ""
                self.key("\x1b")
            return
        if key == "\x1b":
            self.escape, self.escape_at = key, time.monotonic()
            return
        self.key(key)

    def native_selection_mode(self):
        return (
            self.native_terminal
            and self.native_copy_mode
            and self.focus == "history"
            and self.view == "chat"
            and not self.panel
            and not self.wizard
        )

    def configure_mouse(self):
        curses.mousemask(0)
        # Hover shapes need motion reports even without a button held down.
        sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l")
        if self.mouse_enabled and not self.native_selection_mode():
            sys.stdout.write("\x1b[?1003h\x1b[?1006h" if self.pointer_supported else "\x1b[?1002h\x1b[?1006h")
        sys.stdout.flush()
        self.update_pointer()

    def open_link(self, url):
        try:
            if not webbrowser.open(url, new=2):
                self.notice = "Could not open link in the browser."
        except (OSError, webbrowser.Error):
            self.notice = "Could not open link in the browser."

    def link_at(self, x, y):
        if self.panel or self.wizard:
            return None
        row = self.history_cells.get(y)
        if row:
            for left, right, url in row.get("links", []):
                if row["x"] + cells(row["text"][:left]) <= x < row["x"] + cells(row["text"][:right]):
                    return url
        return None

    def pointer_at(self, x, y):
        if self.command_options() and any(
            left <= x < right and y == row for left, right, row, _ in self.command_hits
        ):
            return "pointer"
        if self.panel == "APPEARANCE":
            return (
                "pointer"
                if any(left <= x < right and y == row for left, right, row, *_ in self.appearance.hits)
                else "default"
            )
        if self.panel == "HUB SETTINGS":
            return (
                "pointer"
                if any(left <= x < right and y == row for left, right, row, _ in self.hub_hits)
                else "default"
            )
        if not self.pointer_layout_ready or self.panel:
            return "default"
        if self.hit_request and y == self.hit_request[2] and self.hit_request[0] <= x < self.hit_request[1]:
            return "pointer"
        if y == 0 and any(left <= x < right for left, right in self.hit_connections):
            return "pointer"
        if 0 <= x < self.sidebar_width and any(top <= y < bottom for top, bottom, _ in self.hit_rows):
            return "pointer"
        if y == self.tab_y and any(left <= x < right for left, right, _ in self.hit_tabs):
            return "pointer"
        if (
            self.view == "setup"
            and y in self.setup.hits
            and self.history_bounds
            and self.history_bounds[0] <= x < self.history_bounds[0] + self.history_bounds[2]
        ):
            return "pointer"
        if (
            self.view == "files"
            and y in self.files.hits
            and self.history_bounds
            and self.history_bounds[0] <= x < self.history_bounds[0] + self.history_bounds[2]
        ):
            return "pointer"
        if not self.selection_dragging and self.link_at(x, y):
            return "pointer"
        row = self.history_cells.get(y)
        if self.selection_dragging or (row and row["x"] <= x < row["x"] + cells(row["text"])):
            return "text"
        if (
            self.draft_left <= x < self.draft_left + self.draft_width
            and self.draft_top <= y < self.draft_top + self.draft_height
        ):
            return "text"
        return "default"

    def update_pointer(self, x=None, y=None):
        if x is not None:
            self.pointer_position = (x, y)
        if not self.pointer_supported:
            return
        shape = (
            (self.pointer_at(*self.pointer_position) if self.pointer_position else "default")
            if self.mouse_enabled
            else "text"
        )
        if shape != self.pointer_shape:
            sys.stdout.write("\x1b]22;" + shape + "\x1b\\")
            sys.stdout.flush()
            self.pointer_shape = shape

    def mouse(self, button, x, y):
        if self.panel == "CONNECTIONS":
            self.connections.mouse(self, button, x, y)
            return
        self.update_pointer(x, y)
        options = self.command_options()
        if options:
            hit = next(
                (index for left, right, row, index in self.command_hits if left <= x < right and y == row),
                None,
            )
            if hit is not None:
                if button == 0:
                    self.command_index = hit
                    self.complete_command(options)
                elif button in (64, 65):
                    self.command_index = (self.command_index + (1 if button == 65 else -1)) % len(options)
                return
        if self.panel == "APPEARANCE":
            self.appearance.mouse(self, button, x, y)
            return
        if button == 35:  # Motion with no button down is hover only.
            return
        if button == 32 and self.selection_dragging and not self.panel:
            self.pending_link = None
            self.selection_end = self.selection_point(x, y)
            if self.history_bounds:
                _, top, _, height = self.history_bounds
                if y < top:
                    self.scroll_content(1)
                elif y >= top + height:
                    self.scroll_content(-1)
            return
        if button == 32 and self.scrollbar_grab is not None and not self.panel:
            self.seek_scrollbar(y)
            return
        if button == 3:
            self.finish_selection(x, y)
            return
        if button & 64 and not button & 32 and button & 3 in (0, 1):
            delta = 3 if button & 1 == 0 else -3
            if self.panel:
                self.panel_scroll = max(0, self.panel_scroll - delta)
            elif x > self.sidebar_width:
                self.scroll_content(delta)
            return
        if button == 0 and self.panel == "HUB SETTINGS":
            for left, right, row, action in self.hub_hits:
                if left <= x < right and y == row:
                    self.hub_action(action)
                    return
        if button != 0 or self.panel:
            return
        if self.hit_request and y == self.hit_request[2] and self.hit_request[0] <= x < self.hit_request[1]:
            self.open_requests()
            return
        if self.hit_copy and y == self.hit_copy[2] and self.hit_copy[0] <= x < self.hit_copy[1]:
            self.copy_selection()
            return
        if (
            self.hit_text_view
            and y == self.hit_text_view[2]
            and self.hit_text_view[0] <= x < self.hit_text_view[1]
        ):
            self.clear_selection()
            self.native_copy_mode = True
            self.focus = "history"
            return
        self.clear_selection()
        if y == 0 and any(left <= x < right for left, right in self.hit_connections):
            self.panel, self.panel_scroll = "CONNECTIONS", 0
            return
        bar = self.scrollbar
        if bar and x == bar["x"] and bar["top"] <= y < bar["top"] + bar["height"]:
            relative = y - bar["top"] - bar["thumb"]
            self.scrollbar_grab = relative if 0 <= relative < bar["size"] else bar["size"] // 2
            self.seek_scrollbar(y)
            self.focus = "history"
        elif 0 <= x <= self.sidebar_width and 3 <= y < self.screen_height - 2:
            self.focus = "sidebar"
            for top, bottom, key in self.hit_rows:
                if top <= y < bottom:
                    self.switch(key)
                    return
        elif y == self.tab_y:
            for left, right, view in self.hit_tabs:
                if left <= x < right:
                    self.cancel_setup_editor()
                    self.view, self.scroll, self.plan_scroll = view, 0, 0
                    self.focus = "tabs"
                    break
        elif (
            self.history_bounds
            and self.history_bounds[0] <= x < self.history_bounds[0] + self.history_bounds[2]
            and self.history_bounds[1] <= y < self.history_bounds[1] + self.history_bounds[3]
        ):
            self.focus = "history"
            if self.view == "files" and y in self.files.hits:
                self.files.activate(self.files.hits[y], ui=self)
                return
            if self.view == "setup":
                if y in self.setup.hits and not self.wizard:
                    self.setup.activate(self, self.setup.hits[y])
                return
            point = self.selection_point(x, y)
            if point is not None:
                self.history_snapshot = list(self.history_rows)
                self.selection_anchor = self.selection_end = point
                self.selection_dragging = True
                url = self.link_at(x, y)
                self.pending_link = (x, y, url) if url else None
        elif self.draft_top - 1 <= y <= self.draft_top + self.draft_height:
            self.focus = "chat"
            if self.draft_top <= y < self.draft_top + self.draft_height:
                self.cursor = self.draft_layout().index_at(
                    self.draft_offset + y - self.draft_top, max(0, x - self.draft_left)
                )
                self.preferred_column = None

    def key(self, key):
        if self.focus == "chat" and not self.panel:
            self.last_input_at = time.monotonic()
        if self.native_copy_mode and key in ("\t", "\x1b", "\r", "\n", curses.KEY_ENTER):
            self.native_copy_mode = False
            self.clear_selection()
            self.focus = "chat"
            return
        options = self.command_options()
        if options and key in (curses.KEY_UP, curses.KEY_DOWN, "\t", "\r", curses.KEY_ENTER, "\x1b"):
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                self.command_index = (self.command_index + (1 if key == curses.KEY_DOWN else -1)) % len(
                    options
                )
            elif key == "\x1b":
                self.command_dismissed = self.buffer
                self.command_hits = []
            else:
                self.complete_command(options)
            return
        if key not in (curses.KEY_UP, curses.KEY_DOWN):
            self.preferred_column = None
        if key in ("\x11", "\x03"):
            self.stopped.set()
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, buttons = curses.getmouse()
                if buttons & curses.BUTTON4_PRESSED:
                    self.mouse(64, x, y)
                elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                    self.mouse(65, x, y)
                elif buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    self.mouse(0, x, y)
            except curses.error:
                pass
        elif key == curses.KEY_F10:
            self.open_hub_settings()
        elif self.panel:
            if self.panel == "APPEARANCE":
                self.appearance.key(self, key)
            elif self.panel == "CONNECTIONS":
                self.connections.key(self, key)
            elif self.panel == "HUB SETTINGS" and key in (
                curses.KEY_UP,
                curses.KEY_DOWN,
                "\n",
                "\r",
                curses.KEY_ENTER,
            ):
                actions = ["h", "m", "k", "g", "c", "d"]
                if key in ("\n", "\r", curses.KEY_ENTER):
                    self.hub_action(self.hub_selected)
                else:
                    self.hub_selected = actions[
                        (actions.index(self.hub_selected) + (1 if key == curses.KEY_DOWN else -1))
                        % len(actions)
                    ]
                    rows = help_rows(
                        max(20, self.last_width - 8) if hasattr(self, "last_width") else 72,
                        settings_text(self.data, self.directory),
                    )
                    self.panel_scroll = max(
                        0,
                        next(
                            (
                                i
                                for i, r in enumerate(rows)
                                if r["text"].lstrip().startswith("[" + self.hub_selected.upper() + "]")
                            ),
                            0,
                        )
                        - 5,
                    )
            elif self.panel == "HUB SETTINGS" and key in (
                "1",
                "2",
                "3",
                "d",
                "D",
                "h",
                "H",
                "m",
                "M",
                "k",
                "K",
                "g",
                "G",
                "c",
                "C",
            ):
                self.hub_action(key.lower())
            elif key == "\x1b":
                self.panel = None
                self.return_to_chat()
                if not self.wizard or self.wizard["kind"] != "answer":
                    self.approval = None
            elif key in (curses.KEY_NPAGE, curses.KEY_DOWN):
                self.panel_scroll += 10 if key == curses.KEY_NPAGE else 1
            elif key in (curses.KEY_PPAGE, curses.KEY_UP):
                self.panel_scroll = max(0, self.panel_scroll - (10 if key == curses.KEY_PPAGE else 1))
            elif self.approval and key in ("y", "n"):
                self.decide(key == "y")
        elif key == "\x1b":
            if self.files.picker is not None:
                self.files.picker = None
                self.files.index = self.files.scroll = 0
                self.focus = "history"
                return
            if self.setup.branch_picker:
                self.setup.branch_picker = None
                self.setup.index = self.setup.scroll = 0
                self.focus = "history"
                return
            if self.setup.folder is not None:
                self.setup.close_folder()
                self.focus = "history"
                return
            if self.wizard and self.wizard.get("return_panel"):
                self.panel = self.wizard["return_panel"]
                self.buffer = self.drafts.get(self.selected, "")
                self.cursor = len(self.buffer)
                self.wizard = None
                return
            if self.wizard:
                if self.wizard["kind"] == "rename":
                    self.notice = "Rename cancelled"
                self.buffer = self.drafts.get(self.selected, "")
                self.cursor = len(self.buffer)
            self.wizard = None
            self.approval = None
            self.return_to_chat()
        elif (
            self.wizard
            and self.wizard["kind"] == "new"
            and self.wizard["field"] == "model"
            and key in (curses.KEY_UP, curses.KEY_DOWN)
        ):
            models = self.wizard["models"]
            index = models.index(self.buffer) if self.buffer in models else 0
            self.buffer = models[(index + (1 if key == curses.KEY_DOWN else -1)) % len(models)]
            self.cursor = len(self.buffer)
        elif key == curses.KEY_F1:
            self.panel, self.panel_scroll = HELP, 0
        elif key == curses.KEY_F2:
            self.toggle_view()
        elif key == "\x0f":
            self.toggle_view("tools")
        elif key == curses.KEY_F5:
            self.begin_rename()
        elif key == curses.KEY_F4:
            self.browser("archive" if self.browse == "agents" else "agents")
        elif key == curses.KEY_F6:
            self.begin_handoff()
        elif key == curses.KEY_F3:
            if self.wizard:
                return
            self.view = "chat"
            self.wizard = {"kind": "new", "field": "name", "label": "New agent · Name"}
            if self.selected:
                self.drafts[self.selected] = self.buffer
            self.buffer, self.cursor, self.focus = "", 0, "chat"
        elif key == "\t":
            self.clear_selection()
            focuses = ("sidebar", "history", "chat")
            self.focus = (
                "history" if self.focus == "tabs" else focuses[(focuses.index(self.focus) + 1) % len(focuses)]
            )
        elif self.focus == "tabs" and key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.toggle_view(direction=-1 if key == curses.KEY_LEFT else 1)
        elif self.focus == "tabs" and key in (curses.KEY_DOWN, "\r", curses.KEY_ENTER):
            self.focus = "history"
        elif self.focus == "tabs" and key == curses.KEY_UP:
            self.focus = "sidebar"
        elif key == curses.KEY_PPAGE:
            self.scroll_content(10)
        elif key == curses.KEY_NPAGE:
            self.scroll_content(-10)
        elif (
            self.view == "files"
            and self.focus == "history"
            and key
            in (
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                " ",
                "\r",
                curses.KEY_ENTER,
            )
        ):
            self.clear_selection()
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                self.files.move(
                    -1 if key == curses.KEY_UP else 1, self.history_bounds[3] if self.history_bounds else 10
                )
            else:
                self.files.activate(
                    direction=-1 if key == curses.KEY_LEFT else 1 if key == curses.KEY_RIGHT else 0, ui=self
                )
        elif (
            self.view == "setup"
            and self.focus == "history"
            and key
            in (
                curses.KEY_UP,
                curses.KEY_DOWN,
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                " ",
                "\r",
                curses.KEY_ENTER,
            )
        ):
            if key in (curses.KEY_UP, curses.KEY_DOWN):
                self.setup.move(
                    self,
                    -1 if key == curses.KEY_UP else 1,
                    self.history_bounds[3] if self.history_bounds else 10,
                )
            elif not self.wizard:
                self.setup.activate(self, direction=-1 if key == curses.KEY_LEFT else 1)
        elif self.focus == "sidebar" and key in (curses.KEY_UP, curses.KEY_DOWN):
            i = next((i for i, (k, _) in enumerate(self.rows) if k == self.selected), 0)
            if self.rows:
                self.switch(
                    self.rows[max(0, min(len(self.rows) - 1, i + (1 if key == curses.KEY_DOWN else -1)))][0]
                )
        elif self.focus == "chat" and key in (curses.KEY_UP, curses.KEY_DOWN):
            self.move_vertical(-1 if key == curses.KEY_UP else 1)
        elif self.focus == "history" and key in (curses.KEY_UP, curses.KEY_DOWN):
            self.scroll_content(1 if key == curses.KEY_UP else -1)
        elif key == "\n":
            if self.focus == "sidebar":
                self.focus = "chat"
        elif key in ("\r", curses.KEY_ENTER):
            if self.focus in ("sidebar", "history"):
                self.focus = "chat"
            else:
                self.entered()
        elif self.focus in ("history", "tabs"):
            return
        elif key == "\x15":
            self.buffer, self.cursor = "", 0
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]
        elif key == "\x01":
            self.cursor = 0
        elif key == curses.KEY_HOME:
            row, _ = self.draft_layout().position(self.cursor)
            self.cursor = self.draft_layout().index_at(row, 0)
        elif key in (curses.KEY_END, "\x05"):
            if self.focus == "sidebar":
                self.scroll = 0
            elif key == curses.KEY_END:
                row, _ = self.draft_layout().position(self.cursor)
                self.cursor = self.draft_layout().index_at(row, self.draft_width)
            else:
                self.cursor = len(self.buffer)
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
        elif isinstance(key, str) and key.isprintable():
            self.focus = "chat"
            self.insert_text(key)

    def run(self, screen):
        curses.raw()  # Capture Ctrl+Q instead of terminal XON flow control.
        curses.noecho()  # Only the composer renderer may draw typed input.
        curses.nonl()  # Keep Enter (CR) separate from the unused Ctrl+J (LF).
        self.styles = theme(self.appearance.values)
        # Decode complete reports ourselves; curses must not consume their prefixes.
        screen.keypad(False)
        self.key_sequences = key_sequences()
        screen.timeout(100)
        # Kitty keyboard protocol distinguishes Cmd+C from ordinary text and Ctrl+C.
        # Push/pop the mode so the shell's keyboard behavior is restored on exit.
        sys.stdout.write("\x1b[?2004h\x1b[>1u")
        sys.stdout.flush()
        self.configure_mouse()
        if not self.demo:
            threading.Thread(target=self.polling, daemon=True).start()
            threading.Thread(target=self.worker, daemon=True).start()
        terminal_poll = select.poll()
        terminal_poll.register(sys.stdin.fileno(), select.POLLIN)
        previous_handlers = {}
        for sig in (signal.SIGHUP, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: self.stopped.set())
        repaint_native = True
        native_size = None
        last_cursor_phase = None
        try:
            while not self.stopped.is_set():
                frame_started = time.monotonic()
                # A closed PTY can make get_wch fail immediately instead of waiting.
                # Do not keep repainting an orphaned terminal at full CPU.
                try:
                    if not sys.stdin.isatty() or not sys.stdout.isatty():
                        break
                    termios.tcgetattr(sys.stdin.fileno())
                    if any(
                        events & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
                        for _, events in terminal_poll.poll(0)
                    ):
                        break
                except (OSError, ValueError, termios.error):
                    break
                updated = self.update()
                if self.escape == "\x1b" and time.monotonic() - self.escape_at > 0.15:
                    self.escape = ""
                    self.key("\x1b")
                    repaint_native = True
                native_mode = self.native_selection_mode()
                if native_mode != self.native_selecting:
                    self.native_selecting = native_mode
                    self.draw_context = None
                    screen.clearok(True)
                    self.clear_selection()
                    self.configure_mouse()
                    repaint_native = True
                size = screen.getmaxyx()
                # Native Cmd+C belongs to Terminal. Leave its selection intact by
                # keeping the displayed frame still until keyboard input or resize.
                active = self.is_active(self.current())
                pulse = self.cursor_blink_phase(frame_started)
                if repaint_native or size != native_size or (not native_mode and (updated or active)):
                    self.render(screen)
                elif not native_mode and pulse != last_cursor_phase:
                    # Cursor visibility is a terminal operation; do not redraw the
                    # entire history just to blink the insertion point.
                    try:
                        curses.curs_set(1 if pulse == 0 else 0)
                    except curses.error:
                        pass
                last_cursor_phase = pulse
                native_size = size
                repaint_native = False
                received_input = False
                try:
                    key = screen.get_wch()
                    received_input = True
                    repaint_native = True
                    self.input_key(key)
                    screen.timeout(0)
                    # Drain bursts before repainting; a trackpad can send many reports.
                    deadline = time.monotonic() + 0.008
                    while time.monotonic() < deadline:
                        self.input_key(screen.get_wch())
                except curses.error:
                    continue
                finally:
                    if received_input:
                        # Display keystrokes before the frame-rate limiter waits.
                        self.render(screen)
                        last_cursor_phase = self.cursor_blink_phase()
                        repaint_native = False
                    screen.timeout(100)
                    # Input may repaint at 60 Hz; idle/errors stay capped at 10 Hz.
                    # Keep the error backoff separate so responsiveness cannot revive
                    # the disconnected-terminal busy loop.
                    interval = 1 / 60 if received_input else 0.1
                    remaining = max(0, interval - (time.monotonic() - frame_started))
                    if received_input:
                        # Wake immediately for the next keystroke instead of
                        # sleeping through it to enforce a rendering interval.
                        select.select([sys.stdin], [], [], remaining)
                    else:
                        self.stopped.wait(remaining)
        finally:
            self.stopped.set()
            for sig, previous in previous_handlers.items():
                signal.signal(sig, previous)
            curses.mousemask(0)
            sys.stdout.write("\x1b[<u\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l")
            if self.pointer_supported:
                sys.stdout.write("\x1b]22;text\x1b\\")
            sys.stdout.flush()
