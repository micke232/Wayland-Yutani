"""Launch the reused Tyrell interface against Wayland's independent service."""

import curses
import sys

from .client import ensure_service
from .ui import Dashboard


def run(root):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("The dashboard needs an interactive terminal. Use wayland status for JSON.")
    from .startup import Startup

    startup = Startup()
    startup.begin()
    snapshot = startup.step("WAYLAND RUNTIME ONLINE", lambda: ensure_service(root))
    startup.write("PAPER MODE · LIVE LOCKED\n")
    startup.finish()
    dashboard = Dashboard(str(root))
    dashboard.data = snapshot
    from ..agents import COORDINATOR_ID

    if COORDINATOR_ID in snapshot.get("threads", {}):
        dashboard.selected = "thread:" + COORDINATOR_ID
        dashboard.focus = "chat"
    try:
        curses.wrapper(dashboard.run)
    except KeyboardInterrupt:
        pass
