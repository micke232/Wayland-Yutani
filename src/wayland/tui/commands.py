"""Tyrell completion behavior with Wayland operations."""

COMMANDS = [
    ("/help", "Help"),
    ("/new", "New analytical role"),
    ("/agents", "Show roles"),
    ("/chat", "Conversation"),
    ("/plan", "Analysis plan"),
    ("/market", "Market and setup"),
    ("/positions", "Broker positions"),
    ("/orders", "Order intentions"),
    ("/audit", "Audit events"),
    ("/setup", "Trading setup"),
    ("/settings", "Application settings"),
    ("/connections", "Connections"),
    ("/rename", "Rename role"),
    ("/archive", "Archive role"),
    ("/archives", "Browse archive"),
    ("/remove", "Hide role"),
    ("/hidden", "Hidden roles"),
    ("/restore", "Restore role"),
    ("/interrupt", "Cancel analytical request"),
    ("/kill on", "Block new entries"),
    ("/kill off", "Remove entry kill gate; all other checks remain"),
    ("/mouse", "Toggle mouse"),
    ("/quit", "Close view; service continues"),
]


def choices(text, skills=()):
    if not text.startswith("/"):
        return []
    return [(name, description) for name, description in COMMANDS if name.startswith(text.lower())]
