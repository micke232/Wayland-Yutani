"""Wayland-only local socket. Never contacts Tyrell or development CLI services."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def socket_path(directory):
    path = Path(directory) / "state/ui.sock"
    if len(os.fsencode(path)) >= 104:
        raise ValueError("Wayland root is too long for a local socket. Choose a shorter --root.")
    return path


def request(directory, action, **params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(socket_path(directory)))
        sock.sendall((json.dumps({"action": action, **params}) + "\n").encode())
        with sock.makefile("rb") as reader:
            payload = reader.readline(8 * 1024 * 1024)
    if not payload:
        raise RuntimeError("Wayland service disconnected")
    response = json.loads(payload)
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


def ensure_service(root):
    try:
        return request(root, "snapshot")
    except (OSError, RuntimeError):
        pass
    with (Path(root) / "logs/ui-service.log").open("a") as log:
        os.chmod(log.name, 0o600)
        subprocess.Popen(
            [sys.executable, "-m", "wayland", "--root", str(root), "ui-service"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    for _ in range(100):
        time.sleep(0.05)
        try:
            return request(root, "snapshot")
        except (OSError, RuntimeError):
            pass
    raise RuntimeError("Wayland service did not start; inspect logs/ui-service.log")
