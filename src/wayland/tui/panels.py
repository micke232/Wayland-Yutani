"""Trading content rendered using the original Tyrell row and scrolling protocol."""

import json


def row(text, tone="base", header=False):
    return {"text": text, "tone": tone, "header": header, "inset": 0, "copy_text": text, "action": None}


def trading_rows(data, view, width, wrap):
    t = data.get("trading", {})
    p = t.get("portfolio") or {}
    lines = []
    if view == "files":
        lines = [
            ("POSITIONS · Broker snapshot", "accent"),
            ("State: " + t.get("operating_state", "RECONCILING"), "warning"),
            ("Snapshot: " + str(p.get("timestamp") or "Not verified"), "muted"),
            ("Daily realized P&L: " + str(p.get("daily_realized_pnl_sek", "Unknown")) + " SEK", "base"),
            ("Daily unrealized P&L: " + str(p.get("daily_unrealized_pnl_sek", "Unknown")) + " SEK", "base"),
        ]
        for position in p.get("positions", []):
            lines += [
                ("", "base"),
                (position["symbol"] + " · " + position["candidate"]["instrument"], "accent"),
                (
                    "Quantity: "
                    + str(position["quantity"])
                    + " · Entry: "
                    + str(position["average_entry_usd"])
                    + " USD",
                    "base",
                ),
                ("Max loss: " + str(position["max_loss_sek"]) + " SEK", "base"),
                ("Invalidation: " + position.get("invalidation", ""), "muted"),
            ]
        if not p:
            lines.append(("Position state unknown. No empty-portfolio assumption is made.", "warning"))
        elif not p.get("positions"):
            lines.append(("No positions in this stored snapshot.", "muted"))
    elif view == "processes":
        lines = [("ORDERS · Durable intentions", "accent")]
        for intent in t.get("order_intents", []):
            command = json.loads(intent["command"])
            lines += [
                (command["symbol"] + " · " + command["action"] + " · " + intent["status"], "accent"),
                ("Intent: " + intent["intent_id"], "muted"),
                (
                    "Quantity: "
                    + str(command["quantity"])
                    + " · Limit: "
                    + str(command["limit_usd"])
                    + " USD",
                    "base",
                ),
                ("Broker ID: " + str(intent.get("broker_id") or "Not acknowledged"), "base"),
                ("", "base"),
            ]
        if len(lines) == 1:
            lines.append(("No order intentions recorded.", "muted"))
    else:
        lines = [
            ("MARKET · Supplied observations", "accent"),
            ("Broker: " + t.get("broker_status", "Not verified"), "warning"),
            ("State: " + t.get("operating_state", "RECONCILING"), "base"),
            ("Reason: " + ", ".join(t.get("state_reasons", [])), "muted"),
        ]
        market = t.get("market") or {}
        for key in ("symbol", "price", "timestamp", "usd_sek", "fx_timestamp"):
            lines.append(
                (key.replace("_", " ").title() + ": " + str(market.get(key, "Not available")), "base")
            )
        lines += [
            ("Setup: " + json.dumps(t.get("setup") or {}, ensure_ascii=False), "base"),
            ("No continuous market/news feed is running in this release.", "warning"),
            ("Stored data is not a fresh trading signal.", "muted"),
        ]
    return [row(part, tone, tone == "accent") for text, tone in lines for part in wrap(text, width)]
