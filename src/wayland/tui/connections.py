def connection_badges(data, width, demo=False):
    runtime = data.get("trading", {})
    return [(runtime.get("operating_state", "RECONCILING"), "warning"), ("PAPER · LIVE LOCKED", "success")]


def connections_text(data, demo=False):
    p = data.get("providers", {}).get("openai", {})
    t = data.get("trading", {})
    return (
        "CONNECTIONS\n\nOpenAI API: "
        + p.get("status", "not configured")
        + "\nModel: "
        + (p.get("model") or "Not configured")
        + "\nIBKR: "
        + t.get("broker_status", "not verified")
        + "\n\nConfigure the model and paper Gateway in Setup.\n"
        "API credentials come from OPENAI_API_KEY, not a CLI subscription.\n"
        "After changing the environment, run wayland service-restart in your shell.\n"
        "Chat is analytical only. It cannot submit orders.\n"
        "Paper execution through IBKR remains disabled."
    )


def normalize_host(value):
    return value
