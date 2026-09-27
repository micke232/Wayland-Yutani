"""Official-client login and status. No HTTP server or copied credentials."""

import subprocess
import threading
from pathlib import Path

from .providers.cli_transport import environment, executable

IBKR_DOWNLOAD = "https://www.interactivebrokers.com/en/trading/ibgateway-latest.php"
CODEX_DOWNLOAD = "https://developers.openai.com/codex/cli/"


def ibkr_application():
    roots = (Path("/Applications"), Path.home() / "Applications", Path.home() / "Jts")
    for root in roots:
        for pattern in (
            "IB Gateway*.app",
            "IB Gateway*/*.app",
            "Trader Workstation*.app",
            "Trader Workstation*/*.app",
            "ibgateway/*/*.app",
        ):
            for path in sorted(root.glob(pattern), reverse=True):
                if path.is_dir() and "installer" not in path.name.lower():
                    return path
    return None


class Connections:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.state = {"connected": False, "status": "Checking Codex CLI login"}
        self.thread = None
        self.login = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def publish(self, connected, status):
        with self.lock:
            self.state = {"connected": connected, "status": status}

    def refresh(self):
        try:
            result = subprocess.run(
                [executable(), "login", "status"],
                env=environment(),
                capture_output=True,
                timeout=5,
                check=False,
            )
            connected = result.returncode == 0
            self.publish(
                connected, "Connected · Codex CLI" if connected else "Not signed in · press O to sign in"
            )
        except RuntimeError:
            self.publish(False, "Codex CLI not installed · press O to install")
        except (OSError, subprocess.TimeoutExpired):
            self.publish(False, "Codex CLI unavailable · retry Connect")
        return self.snapshot()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            if self.login is None or self.login.poll() is not None:
                self.refresh()
            else:
                self.publish(False, "Waiting for OpenAI browser login")
            self.stop.wait(3 if self.login else 15)

    def connect(self, provider):
        if provider == "codex":
            try:
                command = executable()
            except RuntimeError:
                return {
                    "url": CODEX_DOWNLOAD,
                    "message": "Install the official Codex CLI, then press Connect again.",
                }
            if self.refresh()["connected"]:
                return {"message": "Connected using your existing Codex CLI login"}
            if self.login is None or self.login.poll() is not None:
                self.login = subprocess.Popen(
                    [command, "login"],
                    env=environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self.publish(False, "Waiting for OpenAI browser login")
            self.start()
            return {"message": "Complete OpenAI's login in your browser"}
        if provider == "ibkr":
            application = ibkr_application()
            if application is None:
                return {
                    "url": IBKR_DOWNLOAD,
                    "message": "Install IBKR's official Gateway, then press Open IBKR. No Wayland proxy is used.",
                }
            subprocess.run(["open", "-a", str(application)], check=True, capture_output=True, timeout=5)
            self.runtime.dispatch("broker_connection", enabled=True)
            return {"message": "Log in to Paper Trading in IBKR. Wayland detects the account automatically."}
        raise ValueError("Unknown connection provider")

    def close(self):
        self.stop.set()
        if self.login and self.login.poll() is None:
            self.login.terminate()
            try:
                self.login.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.login.kill()
                self.login.wait(timeout=3)
