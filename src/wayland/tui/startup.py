"""Animated terminal startup with real status and explicit keyboard continuation."""

import os
import select
import sys
import termios
import time
import tty


class Startup:
    def __init__(self, stream=None, animate=None):
        self.stream = stream if stream is not None else sys.stdout
        self.interactive = self.stream.isatty() and sys.stdin.isatty()
        self.animate = self.interactive if animate is None else animate
        self.color = (
            self.stream.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
        )

    def write(self, text, bright=False):
        if self.color:
            self.stream.write("\x1b[1;38;5;157m" if bright else "\x1b[38;5;108m")
        try:
            if self.animate:
                for char in text:
                    self.stream.write(char)
                    self.stream.flush()
                    time.sleep(0.008)
            else:
                self.stream.write(text)
        finally:
            if self.color:
                self.stream.write("\x1b[0m")
            self.stream.flush()

    def begin(self):
        self.write("\nWAYLAND TRADING\nSYSTEM INITIALIZATION\n")

    def step(self, title, action, status=None):
        try:
            result = action()
            label = status(result) if status else "OK"
        except BaseException:
            self.write(
                title + ": " + ("INTERRUPTED\n" if sys.exc_info()[0] is KeyboardInterrupt else "FAILED\n")
            )
            raise
        self.write(title + (" — " + label if label != "OK" else "") + "\n")
        return result

    def finish(self):
        self.write("\nINTERFACE 2037\nREADY FOR INQUIRY\n\n")
        if self.interactive:
            self.write("Models reason. Code controls execution.\n\n", bright=True)
            fd = sys.stdin.fileno()
            previous = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                self.write("Press any key to continue")
                key = os.read(fd, 1)
                if not key:
                    raise RuntimeError("Terminal input closed during startup")
                if key in (b"\x03", b"\x11"):
                    raise KeyboardInterrupt
                # Consume the rest of an arrow/function key sequence so it cannot
                # accidentally navigate or type into the freshly opened dashboard.
                while select.select([fd], [], [], 0.02)[0]:
                    if not os.read(fd, 1024):
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, previous)
            self.write("\n")


def provider_status(snapshot):
    providers = snapshot.get("providers", {})
    connected = sum(bool(provider.get("connected")) for provider in providers.values())
    if providers and connected == len(providers):
        return "OK"
    return (
        "%d/%d CONNECTED - see Settings" % (connected, len(providers))
        if connected
        else "WAITING - see Settings"
    )
