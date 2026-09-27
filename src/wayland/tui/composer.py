"""Visual draft rows with a lossless mapping back to the text being edited."""

import unicodedata


def character_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class DraftLayout:
    def __init__(self, text, width):
        self.text, self.width = text, max(2, width)
        self.lines = [""]
        self.positions = []
        row, column = 0, 0
        for char in text:
            if char == "\n":
                self.positions.append((row, column))
                self.lines.append("")
                row, column = row + 1, 0
                continue
            display = (
                " " * min(self.width, 4 - column % 4) if char == "\t" else char if char.isprintable() else " "
            )
            size = sum(character_width(c) for c in display)
            if column + size > self.width:
                self.lines.append("")
                row, column = row + 1, 0
                if char == "\t":
                    display, size = " " * min(4, self.width), min(4, self.width)
            self.positions.append((row, column))
            self.lines[row] += display
            column += size
        if column == self.width:
            self.lines.append("")
            row, column = row + 1, 0
        self.positions.append((row, column))

    def position(self, index):
        return self.positions[max(0, min(index, len(self.text)))]

    def index_at(self, row, column):
        row = max(0, min(row, len(self.lines) - 1))
        candidates = [(abs(col - column), i) for i, (r, col) in enumerate(self.positions) if r == row]
        if row + 1 < len(self.lines):
            following = next((i for i, (r, _) in enumerate(self.positions) if r == row + 1), None)
            if following is not None:
                end_column = sum(character_width(c) for c in self.lines[row])
                candidates.append((abs(end_column - column), following))
        return min(candidates)[1] if candidates else len(self.text)
