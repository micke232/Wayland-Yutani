"""Exercise the real curses event loop, socket service and PTY without broker/API access."""

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

from wayland.config import app_root
from wayland.tui.client import request, socket_path


def test_real_terminal_start_settings_navigation_and_detach():
    with tempfile.TemporaryDirectory(prefix="wayland-ui-", dir="/tmp") as directory:
        root = app_root(Path(directory))
        env = dict(
            os.environ,
            TERM="xterm-256color",
            TERM_PROGRAM="test",
            PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
        )
        env.pop("OPENAI_API_KEY", None)
        installed = os.environ.get("WAYLAND_TEST_COMMAND")
        command = [installed] if installed else [sys.executable, "-m", "wayland"]
        if installed:
            env.pop("PYTHONPATH", None)
        service = subprocess.Popen(
            [*command, "--root", str(root), "ui-service"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        app = None
        output = bytearray()

        def wait_text(text, seconds=8):
            deadline = time.monotonic() + seconds
            while text.encode() not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    output.extend(os.read(master, 65536))
            assert text.encode() in output, output[-4000:].decode(errors="replace")

        try:
            deadline = time.monotonic() + 5
            while not socket_path(root).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert service.poll() is None
            app = subprocess.Popen(
                [*command, "--root", str(root)],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
            )
            wait_text("Press any key to continue")
            os.write(master, b" ")
            wait_text("Market Analyst")
            wait_text("Strategist")
            os.write(master, b"\x1b[21~")  # F10
            wait_text("Mouse navigation")
            os.write(master, b"c")
            wait_text("Custom colors")
            os.write(master, b"\x1b")
            time.sleep(0.2)
            os.write(master, b"\x1b")
            time.sleep(0.2)
            os.write(master, b"\x11")  # Ctrl+Q
            deadline = time.monotonic() + 5
            while app.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
            app.wait(timeout=1)
            assert app.returncode == 0, output.decode(errors="replace")
            assert service.poll() is None  # Detaching does not terminate runtime.
            assert len(request(root, "snapshot")["threads"]) == 5
            assert "Traceback" not in output.decode(errors="replace")
        finally:
            if app and app.poll() is None:
                app.kill()
                app.wait(timeout=5)
            service.terminate()
            service.wait(timeout=5)
            os.close(master)
            os.close(slave)
