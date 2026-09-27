from .connections import connections_text


def settings_text(data, directory):
    enabled = data.get("settings", {}).get("mouseEnabled", True)
    return (
        "# Wayland · Settings\n\n## Interface\n"
        "  [M] " + ("[x]" if enabled else "[ ]") + " Mouse navigation\n"
        "  [C] Appearance · colours, preview, import, defaults\n\n"
        "## Trading\n  [G] Trading setup · model, paper account, data and risk limits\n"
        "  [H] Connections · OpenAI API and IBKR\n"
        "  [B] "
        + ("Disconnect" if data.get("trading", {}).get("broker_enabled") else "Connect")
        + " IBKR paper (read-only)\n"
        "  [K] Entry kill switch: "
        + ("ON" if data.get("trading", {}).get("kill_switch") else "OFF")
        + "\n\n## Runtime\n  [D] Diagnostics\n\n"
        "  Independent state: " + str(directory) + "\n"
        "  Closing the view leaves analysis running in Wayland's own service."
    )


def provider_guide(*args):
    return "Use Setup to configure the OpenAI API model and paper Gateway."


def diagnostics_text(data):
    return (
        "# Wayland · Diagnostics\n\n"
        + connections_text(data)
        + "\n\nNo development CLI sessions are discovered."
    )
