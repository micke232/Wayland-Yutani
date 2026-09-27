"""Terminal presentation roles: keep conversation and tool activity separate."""

import curses
import re

from .links import link_ranges, slice_links, web_url
from .progress import conversation_text

PALETTE = {
    "base": (252, 234, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "muted": (245, 234, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "statusmuted": (245, 234, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "accent": (81, 234, curses.COLOR_CYAN, curses.COLOR_BLACK),
    "agentname": (221, 234, curses.COLOR_YELLOW, curses.COLOR_BLACK),
    "historyfocus": (153, 234, curses.COLOR_CYAN, curses.COLOR_BLACK),
    "working": (75, 234, curses.COLOR_BLUE, curses.COLOR_BLACK),
    "divider": (24, 24, curses.COLOR_BLUE, curses.COLOR_BLUE),
    "scrollthumb": (60, 234, curses.COLOR_BLUE, curses.COLOR_BLACK),
    "scrolltrack": (238, 234, curses.COLOR_BLACK, curses.COLOR_BLACK),
    "success": (114, 234, curses.COLOR_GREEN, curses.COLOR_BLACK),
    "warning": (221, 234, curses.COLOR_YELLOW, curses.COLOR_BLACK),
    "error": (203, 234, curses.COLOR_RED, curses.COLOR_BLACK),
    "surface": (252, 236, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "selected": (117, 24, curses.COLOR_WHITE, curses.COLOR_BLUE),
    "user": (117, 235, curses.COLOR_CYAN, curses.COLOR_BLACK),
    "agent": (157, 235, curses.COLOR_GREEN, curses.COLOR_BLACK),
    "strong": (157, 235, curses.COLOR_GREEN, curses.COLOR_BLACK),
    "inlinecode": (215, 235, curses.COLOR_YELLOW, curses.COLOR_BLACK),
    "tool": (183, 235, curses.COLOR_MAGENTA, curses.COLOR_BLACK),
    "input": (255, 236, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "inputmuted": (245, 236, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "code": (252, 235, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "keyword": (117, 235, curses.COLOR_CYAN, curses.COLOR_BLACK),
    "string": (150, 235, curses.COLOR_GREEN, curses.COLOR_BLACK),
    "number": (215, 235, curses.COLOR_YELLOW, curses.COLOR_BLACK),
    "comment": (245, 235, curses.COLOR_WHITE, curses.COLOR_BLACK),
    "logerror": (203, 235, curses.COLOR_RED, curses.COLOR_BLACK),
    "logsuccess": (114, 235, curses.COLOR_GREEN, curses.COLOR_BLACK),
}

TOKENS = re.compile(
    r"""(?P<comment>\#.*|//.*)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<keyword>^\$\s+\S+|\b(?:def|class|import|from|return|if|else|elif|for|while|try|except|with|as|async|await|function|const|let|var|export|true|false|null|None|True|False|SELECT|FROM|WHERE)\b)|(?P<number>\b\d+(?:\.\d+)?\b)"""
)


def syntax_spans(text):
    if re.search(r"\b(error|failed|failure|traceback|exception)\b", text, re.IGNORECASE) or text.startswith(
        "- "
    ):
        return [(text, "logerror")]
    if re.search(r"\b(passed|PASS|success)\b", text) or text.startswith("+ "):
        return [(text, "logsuccess")]
    spans, end = [], 0
    for match in TOKENS.finditer(text):
        if match.start() > end:
            spans.append((text[end : match.start()], "code"))
        spans.append((match.group(), match.lastgroup or "base"))
        end = match.end()
    if end < len(text):
        spans.append((text[end:], "code"))
    return spans


INLINE_MARKDOWN = re.compile(
    r"(?<!\\)(?:\*\*(?P<strong>.+?)\*\*|__(?P<bold>.+?)__|(?P<ticks>`+)(?P<code>.+?)(?P=ticks)|\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\))"
)


def markdown_rows(text, width, wrap, crop, with_links=False):
    """Render common Markdown as terminal spans; keep fenced code literal."""
    fence = None
    for raw in text.splitlines() or [""]:
        line = wrap(raw, max(8, len(raw) * 2 + 1))[0]
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker and fence is None:
            fence = marker[1]
            continue
        if (
            marker
            and fence
            and marker[1][0] == fence[0]
            and len(marker[1]) >= len(fence)
            and not marker[2].strip()
        ):
            fence = None
            continue
        if fence:
            while len(crop(line, width)) < len(line):
                part = crop(line, width)
                yield (part, syntax_spans(part), []) if with_links else (part, syntax_spans(part))
                line = line[len(part) :]
            yield (line, syntax_spans(line), []) if with_links else (line, syntax_spans(line))
            continue
        heading = re.match(r"^#{1,6}\s+", line)
        tone = "strong" if heading else "agent"
        if heading:
            line = line[heading.end() :]
        line = re.sub(r"^(\s*)[-*] ", r"\1• ", line)
        spans, end = [], 0
        named_links = []
        for match in INLINE_MARKDOWN.finditer(line):
            if match.start() > end:
                spans.append((line[end : match.start()], tone))
            if match["code"] is not None:
                spans.append((match["code"], "inlinecode"))
            elif match["label"] is not None:
                start = sum(len(value) for value, _ in spans)
                rendered = match["label"] + " (" + match["url"] + ")"
                spans.append((rendered, tone))
                if web_url(match["url"]):
                    named_links.append((start, start + len(rendered), match["url"]))
            else:
                spans.append((match["strong"] or match["bold"], "strong"))
            end = match.end()
        spans.append((line[end:], tone))
        plain = "".join(value for value, _ in spans)
        links = named_links + [
            (left, right, url)
            for left, right, url in link_ranges(plain)
            if not any(a <= left < b for a, b, _ in named_links)
        ]
        offset = 0
        for part in wrap(plain, width):
            start = plain.find(part, offset) if part else offset
            finish = start + len(part)
            styled, position = [], 0
            for value, style in spans:
                left, right = max(0, start - position), min(len(value), finish - position)
                if left < right:
                    styled.append((value[left:right], style))
                position += len(value)
            yield (part, styled, slice_links(links, start, finish)) if with_links else (part, styled)
            offset = finish


def theme(overrides=None):
    styles = {name: 0 for name in PALETTE}
    if curses.has_colors():
        for i, (name, (fg, bg, fallback_fg, fallback_bg)) in enumerate(PALETTE.items(), 1):
            if i >= curses.COLOR_PAIRS:
                continue
            if overrides and name not in (
                "working",
                "success",
                "warning",
                "error",
                "statusmuted",
                "logerror",
                "logsuccess",
            ):
                value = overrides.get(name)
                if (
                    isinstance(value, (list, tuple))
                    and len(value) == 2
                    and all(type(c) is int and 0 <= c <= 255 for c in value)
                ):
                    fg, bg = value
            curses.init_pair(
                i, fg if curses.COLORS >= 256 else fallback_fg, bg if curses.COLORS >= 256 else fallback_bg
            )
            styles[name] = curses.color_pair(i)
    else:
        styles["selected"] = curses.A_REVERSE
        styles["divider"] = curses.A_REVERSE
    styles["strong"] |= curses.A_BOLD
    return styles


STATUS = {
    "working": "Working",
    "quiet (active)": "Working",
    "approval": "Waiting",
    "question": "Waiting",
    "waiting": "Waiting",
    "idle": "Ready",
    "saved": "Ready",
    "error": "Error",
    "offline": "Disconnected",
    "planned": "Planned",
    "starting": "Starting",
    "setupFailed": "Start failed",
    "needsReview": "Needs review",
    "completed": "Complete",
}


def status_tone(label):
    if label in ("approval", "question", "waiting", "needsReview"):
        return "warning"
    if label in ("working", "quiet (active)", "starting"):
        return "working"
    if label in ("error", "setupFailed", "offline"):
        return "error"
    return "success" if label in ("idle", "saved", "completed") else "statusmuted"


def activity_indicator(label, now, thinking=False):
    if label != "working":
        return "● " + STATUS.get(label, label)
    tick = int(now / 0.35)
    position = tick % 12
    position = position if position < 7 else 12 - position
    bar = "".join("━" if position <= i < position + 3 else "·" for i in range(9))
    return "[" + bar + "] " + ("Agent thinking" if thinking else "Agent working")


def timeline(items, width, view, wrap, crop):
    """Rows carry their speaker title so a clipped message never loses attribution."""
    rows = []
    tools = {
        "commandExecution": "Terminal",
        "fileChange": "File changes",
        "reasoning": "Reasoning",
        "plan": "Plan",
        "webSearch": "Web search",
        "mcpToolCall": "Tool call",
        "dynamicToolCall": "Tool call",
        "collabAgentToolCall": "Agents",
    }
    for item in items:
        kind = item.get("type", "")
        conversation = kind in ("userMessage", "agentMessage", "appNotice")
        if (view == "chat") != conversation:
            continue
        text = conversation_text(item) if view == "chat" else item.get("text", "")
        if not text.strip():
            continue
        tone = "user" if kind == "userMessage" else "agent" if kind == "agentMessage" else "tool"
        delivery = {"sending": "Sending…", "unconfirmed": "Delivery unconfirmed"}.get(
            item.get("delivery"), "Delivered"
        )
        title = (
            "YOU · " + delivery
            if tone == "user"
            else "AGENT · Reply"
            if tone == "agent"
            else "AUDIT · " + tools.get(kind, kind)
        )
        if kind == "appNotice":
            tone, title = "warning", "WAYLAND · Analysis update"
        inset = 3 if tone == "user" and width >= 40 else 0

        def row(text, header=False, syntax=False, copy_text=None, tone=tone, title=title, inset=inset):
            return {
                "text": text,
                "tone": tone,
                "title": title,
                "inset": inset,
                "header": header,
                "copy_text": copy_text,
                "copy_offset": 2,
                "spans": [("│ ", "tool")] + syntax_spans(text[2:]) if syntax else [],
            }

        rows.append(row("╭─ " + title, True))
        body_width = max(8, width - inset - 4)
        if kind == "agentMessage":
            for part, spans, links in markdown_rows(text, body_width, wrap, crop, with_links=True):
                rows.append(
                    {**row("│ " + part, copy_text=part), "spans": [("│ ", tone)] + spans, "links": links}
                )
        else:
            offset = 0
            links = link_ranges(text)
            for part in wrap(text, body_width):
                start = text.find(part, offset)
                start = max(offset, start)
                rows.append(
                    {
                        **row("│ " + part, syntax=kind in ("commandExecution", "fileChange"), copy_text=part),
                        "links": slice_links(links, start, start + len(part)),
                    }
                )
                offset = start + len(part)
        rows.append({**row(" " * max(0, width - inset)), "footer": True})
        rows.append({"text": "", "tone": "base", "title": "", "inset": 0, "header": False, "copy_text": ""})
    return rows


class TimelineCache:
    """Keep only the current rendering of each visible-history item, not every token version."""

    def __init__(self):
        self.context = None
        self.entries = {}

    def render(self, items, width, view, wrap, crop, owner=None):
        context = (owner, width, view)
        if context != self.context:
            self.entries.clear()
            self.context = context
        current, rows = {}, []
        for index, item in enumerate(items):
            key = item.get("id") or ("position", index)
            signature = (item.get("type"), item.get("text"), item.get("delivery"))
            cached = self.entries.get(key)
            if cached is None or cached[0] != signature:
                cached = (signature, timeline([item], width, view, wrap, crop))
            current[key] = cached
            rows.extend(cached[1])
        self.entries = current
        return rows


def viewport(rows, height, scroll):
    """Scroll every row, including sender headers, naturally through the viewport."""
    scroll = min(max(0, scroll), max(0, len(rows) - height))
    end = max(0, len(rows) - scroll)
    start = max(0, end - height)
    visible = [{**row, "source_index": index} for index, row in enumerate(rows[start:end], start)]
    return visible, scroll


def plan_rows(steps, width, wrap):
    """A live checklist with wrapped labels and explicit step states."""
    rows = []
    for i, step in enumerate(steps, 1):
        mark, label, tone = {
            "completed": ("[✓]", "Done", "success"),
            "inProgress": ("[▶]", "In progress", "working"),
        }.get(step["status"], ("[ ]", "Pending", "muted"))
        text = "%s %d. %s · %s" % (mark, i, step["step"], label)
        for line in wrap(text, max(8, width - 2)):
            rows.append(
                {"text": line, "tone": tone, "title": "", "inset": 0, "header": False, "copy_text": line}
            )
        rows.append({"text": "", "tone": "base", "title": "", "inset": 0, "header": False, "copy_text": ""})
    return rows


def scrollbar_geometry(total, height, offset):
    maximum = max(0, total - height)
    size = height if maximum == 0 else max(1, min(height - 1, round(height * height / total)))
    top = round((height - size) * min(maximum, max(0, offset)) / maximum) if maximum else 0
    return top, size, maximum
