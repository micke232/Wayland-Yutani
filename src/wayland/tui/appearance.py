"""Per-user interface colors; startup and status colors are deliberately fixed."""

import curses
import json
import os
import re
import tempfile
from pathlib import Path

from .presentation import PALETTE, theme

FIELDS = {
    "base": "General text / background",
    "surface": "Header and panels",
    "accent": "Focus and accents",
    "agentname": "Agent names",
    "muted": "Secondary text",
    "historyfocus": "History focus line",
    "divider": "Top divider",
    "scrollthumb": "Scrollbar thumb",
    "scrolltrack": "Scrollbar track",
    "selected": "Selected text",
    "user": "Your messages",
    "agent": "Agent replies",
    "strong": "Emphasized reply text",
    "input": "Prompt",
    "inputmuted": "Prompt secondary text",
    "tool": "Tool output",
    "code": "Code text",
    "inlinecode": "Inline code",
    "keyword": "Code keywords",
    "string": "Code strings",
    "number": "Code numbers",
    "comment": "Code comments",
}


def validate(values):
    if not isinstance(values, dict) or set(values) - FIELDS.keys():
        raise ValueError("Unknown or protected color setting")
    if any(
        not isinstance(pair, (list, tuple))
        or len(pair) != 2
        or any(type(c) is not int or not 0 <= c <= 255 for c in pair)
        for pair in values.values()
    ):
        raise ValueError("Choose colors from the 256-color palette")
    return {name: list(pair) for name, pair in values.items()}


SWATCHES = [
    ("Black", 16),
    ("Charcoal", 235),
    ("Slate", 60),
    ("Gray", 245),
    ("Silver", 250),
    ("White", 231),
    ("Red", 196),
    ("Coral", 203),
    ("Orange", 208),
    ("Amber", 214),
    ("Yellow", 226),
    ("Cream", 230),
    ("Green", 40),
    ("Lime", 118),
    ("Mint", 121),
    ("Teal", 37),
    ("Cyan", 51),
    ("Sky", 117),
    ("Blue", 33),
    ("Navy", 18),
    ("Purple", 141),
    ("Violet", 99),
    ("Pink", 212),
    ("Rose", 218),
]


def rgb(color):
    if color < 16:
        return [
            (0, 0, 0),
            (128, 0, 0),
            (0, 128, 0),
            (128, 128, 0),
            (0, 0, 128),
            (128, 0, 128),
            (0, 128, 128),
            (192, 192, 192),
            (128, 128, 128),
            (255, 0, 0),
            (0, 255, 0),
            (255, 255, 0),
            (0, 0, 255),
            (255, 0, 255),
            (0, 255, 255),
            (255, 255, 255),
        ][color]
    if color >= 232:
        return (8 + 10 * (color - 232),) * 3
    levels = (0, 95, 135, 175, 215, 255)
    value = color - 16
    return levels[value // 36], levels[(value // 6) % 6], levels[value % 6]


def hex_color(color):
    return "#{:02X}{:02X}{:02X}".format(*rgb(color))


def color_name(color):
    return next((name for name, value in SWATCHES if color == value), hex_color(color))


def distance(first, second):
    return sum((a - b) ** 2 for a, b in zip(first, second))


def parse_color(text):
    text = text.strip().lower()
    names = {name.lower(): color for name, color in SWATCHES}
    names.update(grey=245, **{"light blue": 117, "dark blue": 18})
    if text in names:
        return names[text]
    if re.fullmatch(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?", text):
        value = text[1:]
        if len(value) == 3:
            value = "".join(c * 2 for c in value)
        channels = tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    else:
        if text.startswith("rgb(") and text.endswith(")"):
            text = text[4:-1]
        match = re.fullmatch(r"\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*", text)
        if not match or any(int(c) > 255 for c in match.groups()):
            raise ValueError("Use a color name, #RRGGBB or R,G,B.")
        channels = tuple(map(int, match.groups()))
    # The extended palette is predictable even if a terminal customizes ANSI 0–15.
    return min(range(16, 256), key=lambda color: distance(rgb(color), channels))


class Appearance:
    def __init__(self, directory):
        self.path = Path(directory) / "appearance.json"
        self.values = {}
        self.notice = ""
        try:
            self.values = validate(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.notice = "Saved colors could not be loaded. Using defaults."
        self.index = 0
        self.component = 0
        self.picking = False
        self.color = 0
        self.hits = []
        self.visible_rows = 8
        self.editor_colors_ready = False
        self.method = "swatches"
        self.entry = ""
        self.input_valid = False
        self.pair_cache = {}
        self.preview_scroll = 0
        self.preview_role = None
        self.preview_bounds = None

    @property
    def role(self):
        return list(FIELDS)[self.index]

    def pair(self):
        return list(self.values.get(self.role, PALETTE[self.role][:2]))

    def save(self, values):
        values = validate(values)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, prefix=".appearance-", delete=False
            ) as file:
                temporary = Path(file.name)
                os.chmod(temporary, 0o600)
                json.dump(values, file)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.values = values

    def apply(self, ui, preview=False):
        values = dict(self.values)
        if preview:
            pair = self.pair()
            pair[self.component] = self.color
            values[self.role] = pair
        ui.styles = theme(values)
        ui.draw_context = None

    def cancel(self, ui):
        if self.picking:
            self.picking = False
            self.apply(ui)

    def choose(self, ui):
        if not self.supported():
            self.notice = "Custom colors need a terminal with 256-color support."
            return
        self.color = self.pair()[self.component]
        self.picking = True
        self.method = "swatches"
        self.notice = ""
        self.apply(ui, preview=True)

    @staticmethod
    def supported():
        return (
            curses.has_colors()
            and getattr(curses, "COLORS", 0) >= 256
            and getattr(curses, "COLOR_PAIRS", 0) >= 192
        )

    def commit(self, ui, values):
        try:
            self.save(values)
        except (OSError, ValueError) as error:
            self.notice = "Could not save colors: " + str(error)
            return
        self.picking = False
        self.notice = ""
        self.apply(ui)

    def set_method(self, ui, method):
        self.method = method
        self.notice = ""
        if method == "input":
            self.entry, self.input_valid = "", False

    def preview_input(self, ui):
        try:
            self.color = parse_color(self.entry)
        except ValueError:
            self.input_valid = False
            return
        self.input_valid = True
        self.notice = ""
        self.apply(ui, preview=True)

    def paste(self, ui, text):
        if not self.picking:
            self.choose(ui)
        if self.picking:
            self.set_method(ui, "input")
            self.entry = text.strip()[:64]
            self.preview_input(ui)

    def accept(self, ui):
        if self.method == "input" and not self.input_valid:
            self.notice = "Use a color name, #RRGGBB or R,G,B."
            return
        pair = self.pair()
        pair[self.component] = self.color
        self.commit(ui, {**self.values, self.role: pair})

    def key(self, ui, key):
        if key == "\x1b":
            if self.picking:
                self.cancel(ui)
            else:
                ui.panel = "HUB SETTINGS"
            return
        if self.picking:
            if key in ("\r", "\n", curses.KEY_ENTER):
                self.accept(ui)
            elif key == "\t":
                methods = ["swatches", "palette", "input"]
                self.set_method(ui, methods[(methods.index(self.method) + 1) % 3])
            elif self.method == "input":
                if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                    self.entry = self.entry[:-1]
                elif key == "\x15":
                    self.entry = ""
                elif isinstance(key, str) and key.isprintable() and len(self.entry) < 64:
                    self.entry += key
                self.preview_input(ui)
            elif key in ("h", "H"):
                self.set_method(ui, "input")
            elif key in ("p", "P"):
                self.set_method(ui, "palette")
            elif key in ("c", "C"):
                self.set_method(ui, "swatches")
            else:
                columns = 4 if self.method == "swatches" else 16
                moves = {
                    curses.KEY_LEFT: -1,
                    curses.KEY_RIGHT: 1,
                    curses.KEY_UP: -columns,
                    curses.KEY_DOWN: columns,
                    curses.KEY_PPAGE: -columns * self.visible_rows,
                    curses.KEY_NPAGE: columns * self.visible_rows,
                }
                if key in moves:
                    if self.method == "swatches":
                        index = min(
                            range(len(SWATCHES)), key=lambda i: distance(rgb(SWATCHES[i][1]), rgb(self.color))
                        )
                        index = max(0, min(len(SWATCHES) - 1, index + moves[key]))
                        self.color = SWATCHES[index][1]
                    else:
                        self.color = max(0, min(255, self.color + moves[key]))
                    self.apply(ui, preview=True)
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            self.index = (self.index + (1 if key == curses.KEY_DOWN else -1)) % len(FIELDS)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t"):
            self.component = 1 - self.component
        elif key in ("\r", "\n", curses.KEY_ENTER):
            self.choose(ui)
        elif key in ("r", "R"):
            self.commit(ui, {name: value for name, value in self.values.items() if name != self.role})
        elif key in ("d", "D"):
            self.commit(ui, {})
        elif key == "[":
            self.preview_scroll = max(0, self.preview_scroll - 1)
        elif key == "]":
            self.preview_scroll = min(9, self.preview_scroll + 1)

    def mouse(self, ui, button, x, y):
        if button in (64, 65):
            if self.preview_bounds and self.preview_bounds[0] <= y < self.preview_bounds[1]:
                self.preview_scroll = max(0, min(9, self.preview_scroll + (-1 if button == 64 else 1)))
            else:
                self.key(ui, curses.KEY_UP if button == 64 else curses.KEY_DOWN)
        elif button == 0:
            for left, right, row, action, value in self.hits:
                if row == y and left <= x < right:
                    if action == "role":
                        self.cancel(ui)
                        self.index, self.component = value
                        self.choose(ui)
                    elif action == "color":
                        self.color = value
                        self.apply(ui, preview=True)
                    elif action == "method":
                        self.set_method(ui, value)
                    else:
                        self.key(ui, value)
                    return

    def swatch_style(self, slot, color):
        if not self.supported():
            return 0
        channels = rgb(color)
        foreground = 16 if sum(v * w for v, w in zip(channels, (0.2126, 0.7152, 0.0722))) > 135 else 231
        pair_id = 64 + slot
        value = (foreground, color)
        if self.pair_cache.get(pair_id) != value:
            curses.init_pair(pair_id, *value)
            self.pair_cache[pair_id] = value
        return curses.color_pair(pair_id)

    def preview(self, ui, screen, top, count, plain, accent):
        _, width = screen.getmaxyx()
        left, right = 3, width - 4
        sidebar = min(18, (width - 6) // 4)
        chat = left + sidebar + 2
        # Fictional app fragments, never live agent data or output.
        rows = [
            [("surface", " Tyrell Agent Management · Preview", left, right - left)],
            [("divider", "━" * (right - left), left, right - left)],
            [
                ("base", " AGENTS", left, sidebar),
                ("accent", "Chat  Plan  Tools", chat, 18),
                ("historyfocus", "──────────────", chat + 18, max(1, right - chat - 19)),
            ],
            [
                ("agentname", " Preview Agent", left, sidebar),
                ("user", " YOU · Review this", chat, 21),
                ("selected", " selected text ", chat + 21, max(1, right - chat - 22)),
            ],
            [
                ("success", " ● Ready", left, sidebar),
                ("agent", " AGENT · Changes ", chat, 19),
                ("strong", "look good.", chat + 19, max(1, right - chat - 20)),
            ],
            [
                ("working", " ● Working", left, sidebar),
                ("inlinecode", " npm run dev ", chat, right - chat - 1),
            ],
            [
                ("warning", " ● Waiting", left, sidebar),
                ("tool", " TOOL · Lint completed", chat, right - chat - 1),
            ],
            [
                ("muted", " src / app.py", left, sidebar),
                ("keyword", " const", chat, 6),
                ("code", " port = ", chat + 6, 8),
                ("number", "42001", chat + 14, 6),
                ("string", ' "ready"', chat + 20, max(1, right - chat - 21)),
            ],
            [
                ("base", " Files · 2", left, sidebar),
                ("comment", " // checks complete", chat, right - chat - 1),
            ],
            [
                ("muted", " Workspace", left, sidebar),
                ("input", " Prompt: Your text", chat, 20),
                ("inputmuted", " cursor ", chat + 20, max(1, right - chat - 21)),
            ],
        ]
        if self.preview_role != self.role:
            self.preview_role = self.role
            target = (
                4
                if self.role == "scrollthumb"
                else 2
                if self.role == "scrolltrack"
                else next((i for i, row in enumerate(rows) if any(role == self.role for role, *_ in row)), 0)
            )
            if not self.preview_scroll <= target < self.preview_scroll + count:
                self.preview_scroll = target
        self.preview_scroll = min(self.preview_scroll, max(0, len(rows) - count))
        ui.put(screen, top, 3, "Live preview · click an element to edit", width - 6, accent)
        self.preview_bounds = (top + 1, top + 1 + count)
        for y, index in enumerate(
            range(self.preview_scroll, min(len(rows), self.preview_scroll + count)), top + 1
        ):
            ui.put(screen, y, left, " " * (right - left), right - left, ui.styles["base"])
            for role, text, x, length in rows[index]:
                if length <= 0 or x >= right:
                    continue
                length = min(length, right - x)
                ui.put(screen, y, x, text.ljust(length), length, ui.styles[role])
                if role in FIELDS:
                    self.hits.append((x, x + length, y, "role", (list(FIELDS).index(role), self.component)))
            if 2 <= index <= 8:
                role = "scrollthumb" if index in (4, 5) else "scrolltrack"
                ui.put(screen, y, right, "█" if role == "scrollthumb" else "│", 1, ui.styles[role])
                self.hits.append((right, right + 1, y, "role", (list(FIELDS).index(role), self.component)))
        if count < len(rows):
            ui.put(screen, top, width - 18, "Scroll preview", 14, plain)

    def render(self, ui, screen):
        height, width = screen.getmaxyx()
        plain, accent = 0, curses.A_BOLD
        supported = self.supported()
        if supported:
            if not self.editor_colors_ready:
                curses.init_pair(60, 252, 236)
                curses.init_pair(61, 81, 236)
                self.editor_colors_ready = True
            plain, accent = curses.color_pair(60), curses.color_pair(61) | curses.A_BOLD
        for y in range(2, height - 1):
            ui.put(screen, y, 1, " " * (width - 3), width - 3, plain)
        self.hits = [(width - 8, width - 3, 2, "key", "\x1b")]
        ui.put(screen, 2, 3, "Appearance · Custom colors", width - 14, accent)
        ui.put(screen, 2, width - 8, "[Esc]", 5, accent)
        preview_count = min(10, max(4, (height - 12) // 2))
        preview_top = height - preview_count - 5
        available = max(1, preview_top - 7)
        if self.picking:
            ui.put(
                screen,
                3,
                3,
                FIELDS[self.role] + " · " + ("Text" if self.component == 0 else "Background"),
                width - 6,
                plain,
            )
            for x, label, method in (
                (3, "[Swatches]", "swatches"),
                (17, "[Palette]", "palette"),
                (30, "[Type color]", "input"),
            ):
                ui.put(
                    screen,
                    4,
                    x,
                    label,
                    len(label),
                    accent | (curses.A_UNDERLINE if method == self.method else 0),
                )
                self.hits.append((x, x + len(label), 4, "method", method))
            if self.method == "input":
                ui.put(
                    screen, 5, 3, "Name, #HEX or R,G,B — e.g. coral, #33AACC, 51,170,204", width - 6, plain
                )
                ui.put(screen, 6, 3, "> " + self.entry + "▏", width - 6, accent)
            else:
                label = color_name(self.color)
                ui.put(
                    screen,
                    5,
                    3,
                    label if label.startswith("#") else label + "  " + hex_color(self.color),
                    width - 6,
                    plain,
                )
                columns = 4 if self.method == "swatches" else 16
                values = [value for _, value in SWATCHES] if self.method == "swatches" else list(range(256))
                index = min(range(len(values)), key=lambda i: distance(rgb(values[i]), rgb(self.color)))
                self.visible_rows = min(8, available, (len(values) + columns - 1) // columns)
                start = max(
                    0,
                    min(
                        (len(values) + columns - 1) // columns - self.visible_rows,
                        index // columns - self.visible_rows // 2,
                    ),
                )
                for slot, i in enumerate(
                    range(start * columns, min(len(values), (start + self.visible_rows) * columns))
                ):
                    color = values[i]
                    row, col = divmod(slot, columns)
                    x, y = 3 + col * (16 if columns == 4 else 4), 6 + row
                    style = self.swatch_style(slot, color)
                    ui.put(screen, y, x, " ✓ " if self.color == color else "   ", 3, style)
                    length = 3
                    if columns == 4:
                        ui.put(screen, y, x + 4, SWATCHES[i][0], 11, accent if self.color == color else plain)
                        length = 15
                    self.hits.append((x, x + length, y, "color", color))
            ui.put(
                screen,
                height - 3,
                3,
                self.notice
                or (
                    "Closest match: " + hex_color(self.color)
                    if self.method == "input" and self.input_valid
                    else "Click a color or use arrows to preview. Enter saves."
                ),
                width - 6,
                plain,
            )
            ui.put(screen, height - 2, 3, "[Save]  [Cancel]   Tab method · Esc cancel", width - 6, accent)
            self.hits.extend([(3, 9, height - 2, "key", "\r"), (11, 19, height - 2, "key", "\x1b")])
        else:
            ui.put(screen, 4, 3, "Element", 30, plain)
            ui.put(screen, 4, 36, "Text", 10, plain)
            ui.put(screen, 4, 49, "Background", 12, plain)
            count = available
            start = max(0, min(len(FIELDS) - count, self.index - count // 2))
            for slot, index in enumerate(range(start, min(len(FIELDS), start + count))):
                y = 6 + slot
                role = list(FIELDS)[index]
                pair = self.values.get(role, PALETTE[role][:2])
                ui.put(screen, y, 3, ("› " if index == self.index else "  ") + FIELDS[role], 32, plain)
                for component, x in ((0, 36), (1, 49)):
                    color = pair[component]
                    chosen = index == self.index and component == self.component
                    ui.put(screen, y, x, "  ", 2, self.swatch_style(slot * 2 + component, color))
                    ui.put(
                        screen,
                        y,
                        x + 3,
                        color_name(color),
                        9,
                        accent | curses.A_UNDERLINE if chosen else plain,
                    )
                    self.hits.append((x, x + 12, y, "role", (index, component)))
                self.hits.append((3, 35, y, "role", (index, self.component)))
            if self.notice:
                ui.put(screen, height - 3, 3, self.notice, width - 6, accent)
            else:
                x = 3
                for label, key in (("[R] Default element", "r"), ("[D] Use default colors", "d")):
                    ui.put(screen, height - 3, x, label, len(label), accent)
                    self.hits.append((x, x + len(label), height - 3, "key", key))
                    x += len(label) + 3
            ui.put(screen, height - 2, 3, "↑↓ Element · ←→ Text/background · Enter choose", width - 6, accent)
        self.preview(ui, screen, preview_top, preview_count, plain, accent)
