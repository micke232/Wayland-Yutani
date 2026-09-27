"""Trading form using Tyrell's focus, row, mouse and prompt-editing conventions."""

CONNECTION_SECTIONS = {
    "Codex model": ["openai_model", "analysis_timeout_seconds", "analysis_cooldown_seconds"],
    "IBKR paper connection": [
        "ibkr_account",
        "account_allowlist",
        "ibkr_host",
        "ibkr_port",
        "ibkr_client_id",
        "ibkr_paper_orders",
    ],
}
SECTIONS = {
    "Entry limits": ["max_position_sek", "max_daily_loss_sek", "max_trades_per_day", "max_open_positions"],
    "Data quality": [
        "max_market_data_age_seconds",
        "max_fx_age_seconds",
        "max_bid_ask_fraction",
        "min_volume",
        "min_open_interest",
        "min_dte",
        "max_dte",
        "fee_per_contract_sek",
    ],
    "Strategy": ["allowed_symbols", "rebound_fraction", "reversal_fraction"],
}
HELP = {
    "ibkr_paper_orders": "true enables native paper LONG PUT limit orders only, after reconciliation. Spreads remain blocked.",
    "openai_model": "Optional Codex model ID. Leave blank to use the CLI default.",
    "account_allowlist": "Comma-separated paper account IDs. Never add a live account.",
    "ibkr_account": "Paper account ID reported by your local Gateway.",
    "fee_per_contract_sek": "Conservative fee reserve per contract; calibrate before paper execution.",
    "max_daily_loss_sek": "Blocks new entries at the threshold; it does not guarantee a maximum daily loss.",
}


def preview_rows(text, width, wrap):
    return [{"text": part, "heading": False} for part in wrap(text, width)]


class SetupForm:
    def __init__(self):
        self.index = self.scroll = 0
        self.rows, self.hits = [], {}
        self.expanded = {"Entry limits"}
        self.sections = SECTIONS
        self.pending = False
        self.folder = self.branch_picker = None
        self.scope = "defaults"

    def view_data(self, data):
        return data

    def close_folder(self):
        self.folder = None

    def saved(self, result):
        self.pending = False

    def visible(self, ui, height):
        values = ui.data.get("tradingSettings", {})
        self.rows = []

        def add(text, action=None, tone="base", help=""):
            self.rows.append(
                {
                    "text": text,
                    "action": action,
                    "tone": tone,
                    "help": help,
                    "header": tone == "accent",
                    "inset": 0,
                    "copy_text": None,
                }
            )

        add("GLOBAL TRADING SETTINGS · PAPER ONLY · LIVE LOCKED", tone="warning")
        add(
            "Applies to new analysis and the next monitor start; never changes an active order.", tone="muted"
        )
        for title, keys in self.sections.items():
            add(("▾ " if title in self.expanded else "▸ ") + title, "section:" + title, "accent")
            if title in self.expanded:
                for key in keys:
                    value = values.get(key, "")
                    if isinstance(value, list):
                        value = ", ".join(value)
                    add(
                        "    "
                        + (
                            "Codex model (optional)"
                            if key == "openai_model"
                            else key.replace("_", " ").title()
                        )
                        + ": "
                        + (str(value) or "Not configured"),
                        key,
                        help=HELP.get(key, "Enter edits this value. Settings are validated before saving."),
                    )
        self.index = min(self.index, max(0, len(self.rows) - 1))
        self.scroll = min(self.scroll, max(0, len(self.rows) - height))
        return [
            {**line, "source_index": i, "setup_selected": i == self.index}
            for i, line in enumerate(self.rows[self.scroll : self.scroll + height], self.scroll)
        ]

    def move(self, ui, delta, height):
        self.index = max(0, min(len(self.rows) - 1, self.index + delta))
        self.scroll = max(0, min(self.scroll, self.index))
        if self.index >= self.scroll + height:
            self.scroll = self.index - height + 1

    def activate(self, ui, index=None, direction=1):
        if index is not None:
            self.index = index
        action = self.rows[self.index]["action"]
        if not action:
            return
        if action.startswith("section:"):
            name = action[8:]
            self.expanded.symmetric_difference_update({name})
        else:
            value = ui.data.get("tradingSettings", {}).get(action, "")
            ui.drafts[ui.selected] = ui.buffer
            ui.wizard = {"kind": "setup", "field": action, "label": action.replace("_", " ").title()}
            ui.buffer = ", ".join(value) if isinstance(value, list) else str(value)
            ui.cursor, ui.focus = len(ui.buffer), "chat"
