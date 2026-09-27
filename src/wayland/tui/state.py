"""Presentation-only status; never discovers provider sessions."""


def status_label(thread, connected=True, now=None):
    if not connected:
        return "offline"
    kind = thread.get("status", {}).get("type", "idle")
    return {"active": "working", "idle": "idle", "systemError": "error", "waiting": "waiting"}.get(kind, kind)
