import curses
import unittest
from unittest.mock import patch

from wayland.tui.composer import DraftLayout
from wayland.tui.ui import Dashboard


class ComposerTests(unittest.TestCase):
    def ui(self):
        data = {
            "threads": {
                "wayland:test": {
                    "id": "wayland:test",
                    "name": "Test",
                    "status": {"type": "idle"},
                    "items": [],
                    "plan": [],
                }
            },
            "tasks": [],
            "connected": True,
        }
        ui = Dashboard("/tmp", data)
        ui.update()
        ui.switch("thread:" + next(iter(data["threads"])))
        ui.focus = "chat"
        return ui

    def test_wrap_retains_spaces_and_explicit_newlines(self):
        layout = DraftLayout("abcd\nxy  z", 4)
        self.assertEqual(layout.lines, ["abcd", "xy  ", "z"])
        self.assertEqual(layout.position(5), (1, 0))
        self.assertEqual(layout.position(len(layout.text)), (2, 1))
        self.assertEqual(DraftLayout("abcd", 4).position(4), (1, 0))

    def test_cursor_mapping_handles_wide_characters_and_tabs(self):
        layout = DraftLayout("猫\tå", 8)
        self.assertEqual(layout.lines, ["猫  å"])
        self.assertEqual(layout.position(2), (0, 4))
        self.assertEqual(layout.index_at(0, 4), 2)

    def test_shift_and_alt_enter_add_newlines_but_control_j_does_not(self):
        ui = self.ui()
        with patch.object(ui, "submit") as submit:
            for key in "first\n\x1b\rsecond\x1b[13;2uthird":
                ui.input_key(key)
            self.assertEqual(ui.buffer, "first\nsecond\nthird")
            submit.assert_not_called()
            ui.key("\r")
            submit.assert_called_once_with("send", threadId=ui.selected[7:], text="first\nsecond\nthird")
            self.assertEqual(ui.buffer, "")

    def test_up_down_edits_the_draft_without_scrolling_history(self):
        ui = self.ui()
        ui.buffer = "one\ntwo\nthree"
        ui.cursor = len(ui.buffer)
        ui.key(curses.KEY_UP)
        ui.key("!")
        self.assertEqual(ui.buffer, "one\ntwo!\nthree")
        self.assertEqual(ui.scroll, 0)

    def test_enhanced_keyboard_preserves_editing_navigation_and_exit(self):
        ui = self.ui()
        for key in "discard\x1b[117;5unew\x1b[13;3uline":
            ui.input_key(key)
        self.assertEqual(ui.buffer, "new\nline")
        for key in "\x1b[99;9u":  # Cmd+C with no selection is harmless.
            ui.input_key(key)
        self.assertEqual(ui.buffer, "new\nline")
        self.assertFalse(ui.stopped.is_set())
        ui.view = "tools"
        for key in "\x1b[27u":
            ui.input_key(key)
        self.assertEqual((ui.view, ui.focus), ("chat", "chat"))
        for key in "\x1b[113;5u":
            ui.input_key(key)
        self.assertTrue(ui.stopped.is_set())

    def test_multiline_paste_preserves_windows_linebreaks_and_does_not_send(self):
        ui = self.ui()
        for key in "\x1b[200~first\r\nsecond\rthird\x1b[201~":
            ui.input_key(key)
        self.assertEqual(ui.buffer, "first\nsecond\nthird")
        self.assertTrue(ui.commands.empty())

    def test_click_moves_cursor_and_resize_keeps_draft_intact(self):
        ui = self.ui()
        ui.buffer, ui.cursor = "first\nsecond", 12
        ui.draft_top, ui.draft_left, ui.draft_height = 15, 30, 3
        ui.mouse(0, 33, 16)
        ui.key("!")
        self.assertEqual(ui.buffer, "first\nsec!ond")
        before = ui.buffer
        for width in (10, 4, 40):
            ui.draft_width = width
            layout = ui.draft_layout()
            self.assertLess(layout.position(ui.cursor)[0], len(layout.lines))
        self.assertEqual(ui.buffer, before)
