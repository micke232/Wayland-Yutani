"""Private application credentials, never part of settings, chat or audit state."""

import os
from pathlib import Path


def api_key(root: Path) -> str | None:
    path = root / "secrets/openai.key"
    if path.is_file():
        return path.read_text().strip() or None
    return os.environ.get("OPENAI_API_KEY") or None


def save_api_key(root: Path, value: str) -> None:
    folder = root / "secrets"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    temporary = folder / "openai.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
        temporary.replace(folder / "openai.key")
    finally:
        temporary.unlink(missing_ok=True)
