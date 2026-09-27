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
        + "\n\nConfigure the model and paper Gateway in F10 → Connections.\n"
        "API credentials come from OPENAI_API_KEY, not a CLI subscription.\n"
        "After changing the environment, run wayland service-restart in your shell.\n"
        "Chat is analytical only. It cannot submit orders.\n"
        "Paper execution through IBKR remains disabled."
    )


def normalize_host(value):
    return value


from .setup_view import CONNECTION_SECTIONS, SetupForm


class ConnectionsForm(SetupForm):
    """Application-wide connection editor, independent of the selected role."""

    def __init__(self):
        super().__init__()
        self.sections = CONNECTION_SECTIONS
        self.expanded = set(CONNECTION_SECTIONS)

    def visible(self, ui, height):
        index, scroll = self.index, self.scroll
        super().visible(ui, height)
        self.rows[0]["text"] = "CONNECTIONS · SHARED BY ALL WAYLAND ROLES"
        self.rows[1]["text"] = "IBKR login belongs to Gateway/TWS. OpenAI uses OPENAI_API_KEY."
        trading = ui.data.get("trading", {})

        def line(text, action=None):
            return {
                "text": text,
                "action": action,
                "tone": "accent" if action else "muted",
                "help": "",
                "header": False,
                "inset": 0,
                "copy_text": None,
            }

        self.rows[2:2] = [
            line("OpenAI: " + ui.data.get("providers", {}).get("openai", {}).get("status", "Not configured")),
            line("IBKR: " + trading.get("broker_status", "Not connected")),
            line(
                ("Disconnect" if trading.get("broker_enabled") else "Connect") + " IBKR paper · read-only",
                "broker_toggle",
            ),
        ]
        self.index = min(index, len(self.rows) - 1)
        self.scroll = max(0, min(scroll, max(0, len(self.rows) - height)))
        return [
            {**line, "source_index": i, "setup_selected": i == self.index}
            for i, line in enumerate(self.rows[self.scroll : self.scroll + height], self.scroll)
        ]

    def activate(self, ui, index=None, direction=1):
        if index is not None:
            self.index = index
        if self.rows[self.index]["action"] == "broker_toggle":
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
            "↑↓ Select · Enter Edit · Click to select · Esc Settings",
            width - 8,
            ui.styles["muted"],
        )

    def key(self, ui, key):
        import curses

        if key == "\x1b":
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
