"""Runtime helpers for desktop sidecar."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from nanobot.utils.helpers import ensure_dir, get_data_path


def get_desktop_data_dir() -> Path:
    return ensure_dir(get_data_path() / "desktop")


def load_or_create_auth_token(config_token: str = "") -> str:
    token = (config_token or "").strip()
    if token:
        return token

    token_path = get_desktop_data_dir() / "token.json"
    if token_path.exists():
        try:
            raw = json.loads(token_path.read_text(encoding="utf-8"))
            existing = str(raw.get("token") or "").strip()
            if existing:
                return existing
        except Exception:
            pass

    generated = secrets.token_urlsafe(32)
    token_path.write_text(json.dumps({"token": generated}, ensure_ascii=False, indent=2), encoding="utf-8")
    return generated


class RuntimeLock:
    """Process-level lock to enforce single sidecar instance."""

    def __init__(self, path: Path):
        self.path = path
        self._fp = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fp = open(self.path, "a+", encoding="utf-8")
        fp.seek(0)
        fp.write(str(os.getpid()))
        fp.truncate()
        fp.flush()
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            fp.close()
            raise RuntimeError("desktop sidecar is already running")
        self._fp = fp

    def release(self) -> None:
        if not self._fp:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fp.close()
        finally:
            self._fp = None
