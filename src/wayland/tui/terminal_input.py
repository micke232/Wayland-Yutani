"""Keyboard escape sequences when curses keypad decoding is disabled.

One decoder owns both keyboard and mouse reports. Some macOS terminfo entries
otherwise let curses consume just the mouse prefix, leaving coordinates as text.
"""

import curses
import re


def enhanced_key(sequence):
    """Decode CSI-u keys from terminals using Kitty's disambiguation flag."""
    match = re.fullmatch(r"\x1b\[(\d+)(?::[\d:]*)?(?:;(\d+)(?::(\d+))?)?u", sequence)
    if not match:
        return None
    code, modifiers = int(match[1]), (int(match[2] or 1) - 1) & 63
    event = int(match[3] or 1)
    if event == 3:  # Releases must never trigger actions or insert text.
        return None
    if code in (67, 99) and modifiers == 8:
        return "copy" if event == 1 else None
    if code == 13 and modifiers == 8:
        return "intervene" if event == 1 else None
    if code == 13 and modifiers in (1, 2):
        return "newline"
    if modifiers == 4 and 64 <= code <= 127:
        return chr(code & 31)
    if modifiers in (0, 1) and 0 <= code <= 0x10FFFF:
        return chr(code)
    return None


def key_sequences():
    keys = {}
    for suffix, key in {
        "A": curses.KEY_UP,
        "B": curses.KEY_DOWN,
        "C": curses.KEY_RIGHT,
        "D": curses.KEY_LEFT,
        "H": curses.KEY_HOME,
        "F": curses.KEY_END,
    }.items():
        keys["\x1b[" + suffix] = keys["\x1bO" + suffix] = key
    for number, key in {
        1: curses.KEY_HOME,
        3: curses.KEY_DC,
        4: curses.KEY_END,
        5: curses.KEY_PPAGE,
        6: curses.KEY_NPAGE,
        7: curses.KEY_HOME,
        8: curses.KEY_END,
        11: curses.KEY_F1,
        12: curses.KEY_F2,
        13: curses.KEY_F3,
        14: curses.KEY_F4,
        15: curses.KEY_F5,
        17: curses.KEY_F6,
        18: curses.KEY_F7,
        19: curses.KEY_F8,
        20: curses.KEY_F9,
        21: curses.KEY_F10,
        23: curses.KEY_F11,
        24: curses.KEY_F12,
    }.items():
        keys["\x1b[%d~" % number] = key
    for suffix, key in zip("PQRS", (curses.KEY_F1, curses.KEY_F2, curses.KEY_F3, curses.KEY_F4)):
        keys["\x1bO" + suffix] = keys["\x1b[" + suffix] = key
    for cap, key in {
        "kcuu1": curses.KEY_UP,
        "kcud1": curses.KEY_DOWN,
        "kcub1": curses.KEY_LEFT,
        "kcuf1": curses.KEY_RIGHT,
        "khome": curses.KEY_HOME,
        "kend": curses.KEY_END,
        "kpp": curses.KEY_PPAGE,
        "knp": curses.KEY_NPAGE,
        "kdch1": curses.KEY_DC,
        "kf1": curses.KEY_F1,
        "kf2": curses.KEY_F2,
        "kf3": curses.KEY_F3,
        "kf4": curses.KEY_F4,
        "kf5": curses.KEY_F5,
        **{"kf%d" % n: curses.KEY_F0 + n for n in range(6, 13)},
    }.items():
        try:
            sequence = curses.tigetstr(cap)
        except curses.error:
            sequence = None
        if sequence:
            keys[sequence.decode("ascii")] = key
    return keys
