import unittest
from unittest.mock import patch

from wayland.tui.clipboard import copy_text, selection_text


class ClipboardTests(unittest.TestCase):
    def test_selection_preserves_text_and_excludes_message_frame(self):
        rows = [
            {"copy_text": None},
            {"copy_text": "first line"},
            {"copy_text": "猫 second line"},
            {"copy_text": None},
        ]
        self.assertEqual(selection_text(rows, (1, 6), (2, 8)), "line\n猫 second")
        self.assertEqual(selection_text(rows, (2, 8), (1, 6)), "line\n猫 second")
        self.assertEqual(selection_text(rows, (1, 0), (1, 0)), "")

    def test_clipboard_receives_utf8_selection_as_stdin_without_shell(self):
        with (
            patch("wayland.tui.clipboard.shutil.which", return_value="/usr/bin/pbcopy"),
            patch("wayland.tui.clipboard.subprocess.run") as run,
        ):
            copy_text("å猫 $(not a command)")
            self.assertEqual(run.call_args.args[0], ["pbcopy"])
            self.assertEqual(run.call_args.kwargs["input"], "å猫 $(not a command)".encode())
            self.assertNotIn("shell", run.call_args.kwargs)
