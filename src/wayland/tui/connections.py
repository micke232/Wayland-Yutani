def connection_badges(data, width, demo=False):
    runtime = data.get("trading", {})
    return [(runtime.get("operating_state", "RECONCILING"), "warning"), ("PAPER · LIVE LOCKED", "success")]


def connections_text(data, demo=False):
    p = data.get("providers", {}).get("openai", {})
    t = data.get("trading", {})
    return (
        "CONNECTIONS\n\nCodex CLI: "
        + p.get("status", "not configured")
        + "\nModel: "
        + (p.get("model") or "Not configured")
        + "\nIBKR: "
        + t.get("broker_status", "not verified")
        + "\n\nConfigure the model and paper Gateway in F10 → Connections.\n"
        "Codex uses your existing CLI login; Connect starts official login if needed.\n"
        "Open IBKR starts the official IB Gateway or TWS application.\n"
        "Chat is analytical only. It cannot submit orders.\n"
        "Native paper long puts require enabled routing and verified state. Spreads remain blocked."
    )


def normalize_host(value):
    return value


from .setup_view import CONNECTION_SECTIONS, SetupForm


class ConnectionsForm(SetupForm):
    """Application-wide connection editor, independent of the selected role."""

    def __init__(self):
        super().__init__()
        self.sections = CONNECTION_SECTIONS
        self.expanded = set()
        self.index = 3

    def visible(self, ui, height):
        index, scroll = self.index, self.scroll
        super().visible(ui, height)
        advanced = self.rows[2:]
        providers = ui.data.get("providers", {})
        trading = ui.data.get("trading", {})

        def line(text, action=None, tone="muted"):
            return {
                "text": text,
                "action": action,
                "tone": "accent" if action else tone,
                "help": "",
                "header": False,
                "inset": 0,
                "copy_text": None,
            }

        self.rows = [
            line("CONNECT WAYLAND", tone="accent"),
            line("Use your existing logins. No Wayland login server or proxy."),
            line("IBKR · Paper trading account", tone="base"),
            line("    [I] Open IBKR · Paper Trading", "login:ibkr"),
            line(
                "    " + trading.get("broker_status", "Not connected"),
                tone="success" if trading.get("brokerObservation", {}).get("connected") else "warning",
            ),
            line(
                "    Paper orders: "
                + (
                    "enabled · LONG PUT only"
                    if ui.data.get("tradingSettings", {}).get("ibkr_paper_orders")
                    else "disabled · enable in IBKR settings"
                )
            ),
            line("    " + trading.get("brokerObservation", {}).get("data_status", "Waiting for market data")),
            line("    " + trading.get("brokerObservation", {}).get("inspection", {}).get("reason", "")),
            line(""),
            line("Codex CLI · Shared by all analytical roles", tone="base"),
            line("    [O] Connect Codex CLI", "login:codex"),
            line("    " + providers.get("openai", {}).get("status", "Not connected")),
            line("    Existing CLI login is detected automatically. No API key needed."),
            line(""),
            line("[S] Other settings · appearance, data and risk limits", "settings"),
            line(""),
            line("ADVANCED CONNECTION SETTINGS", tone="base"),
            *advanced,
        ]
        for account in trading.get("brokerObservation", {}).get("accounts", []):
            self.rows.insert(5, line("    Use paper account " + account, "account:" + account))
        if "IBKR paper connection" in self.expanded:
            self.rows.append(line("TWS adapter: " + trading.get("broker_status", "Not connected")))
            self.rows.append(
                line(
                    ("Disconnect" if trading.get("broker_enabled") else "Connect")
                    + " existing TWS / socket Gateway",
                    "broker_toggle",
                )
            )
        self.index = min(index, len(self.rows) - 1)
        self.scroll = max(0, min(scroll, max(0, len(self.rows) - height)))
        return [
            {**row, "source_index": i, "setup_selected": i == self.index}
            for i, row in enumerate(self.rows[self.scroll : self.scroll + height], self.scroll)
        ]

    def activate(self, ui, index=None, direction=1):
        if index is not None:
            self.index = index
        action = self.rows[self.index]["action"]
        if action and action.startswith("account:"):
            account = action.split(":", 1)[1]
            ui.submit("trading_settings", patch={"ibkr_account": account, "account_allowlist": account})
            return
        if action == "settings":
            ui.panel = "HUB SETTINGS"
            return
        if action and action.startswith("login:"):
            ui.submit("connect_provider", provider=action.split(":", 1)[1])
            return
        if action == "broker_toggle":
            ui.submit(
                "broker_connection", enabled=not ui.data.get("trading", {}).get("broker_enabled", False)
            )
            return
        super().activate(ui, direction=direction)
        if ui.wizard:
            ui.wizard["return_panel"] = "CONNECTIONS"
            ui.wizard["label"] = "Global · " + ui.wizard["label"]
            ui.panel = None

    def render(self, ui, screen):
        import curses

        height, width = screen.getmaxyx()
        for y in range(2, height - 1):
            ui.band(screen, y, 1, width - 3, "", "surface")
        visible = self.visible(ui, height - 8)
        self.hits = {}
        for y, row in enumerate(visible, 4):
            ui.put(
                screen,
                y,
                4,
                row["text"],
                width - 8,
                ui.styles[row["tone"]] | (curses.A_UNDERLINE if row["setup_selected"] else 0),
            )
            self.hits[y] = row["source_index"]
        ui.put(screen, 2, width - 8, "[Esc]", 5, ui.styles["accent"])
        ui.put(
            screen,
            height - 3,
            4,
            "[I] Open IBKR  [O] Codex  [S] Settings · ↑↓ Select · Enter Open · Esc Back",
            width - 8,
            ui.styles["muted"],
        )

    def key(self, ui, key):
        import curses

        if key in ("i", "I", "o", "O"):
            ui.submit("connect_provider", provider="ibkr" if key.lower() == "i" else "codex")
        elif key in ("s", "S"):
            ui.panel = "HUB SETTINGS"
        elif key == "\x1b":
            ui.panel, ui.panel_scroll = "HUB SETTINGS", 0
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            delta = -1 if key in (curses.KEY_UP, curses.KEY_PPAGE) else 1
            self.move(
                ui,
                delta * (8 if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1),
                max(1, ui.screen_height - 8),
            )
        elif key in ("\r", "\n", curses.KEY_ENTER, " ", curses.KEY_LEFT, curses.KEY_RIGHT):
            self.activate(ui)

    def mouse(self, ui, button, x, y):
        if button in (64, 65):
            self.move(ui, -3 if button == 64 else 3, max(1, ui.screen_height - 8))
        elif button == 0:
            if y == 2:
                ui.panel = "HUB SETTINGS"
            elif y in self.hits:
                self.activate(ui, self.hits[y])
