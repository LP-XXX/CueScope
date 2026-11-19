"""UI-only preferences with one coalescing background writer."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

VALID_THEMES = frozenset({"system", "light", "dark"})


def load_theme(path: Path) -> str:
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            return "system"
        value = json.loads(data)
        mode = value.get("theme") if isinstance(value, dict) else None
        return mode if isinstance(mode, str) and mode in VALID_THEMES else "system"
    except (OSError, ValueError, TypeError):
        return "system"


def save_theme(path: Path, theme: str) -> None:
    if theme not in VALID_THEMES:
        raise ValueError("Unknown theme")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump({"version": 1, "theme": theme}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PreferenceWriter:
    """At most one write in flight plus one latest pending preference."""

    def __init__(self, path: Path, on_error):
        self.path = Path(path)
        self.on_error = on_error
        self._condition = threading.Condition()
        self._pending: str | None = None
        self._writing = False
        self._closed = False
        self._thread: threading.Thread | None = None

    def submit(self, theme: str) -> None:
        if theme not in VALID_THEMES:
            raise ValueError("Unknown theme")
        with self._condition:
            if self._closed:
                return
            self._pending = theme
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ui-preferences-save", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._pending is None:
                    return
                theme, self._pending = self._pending, None
                self._writing = True
            try:
                save_theme(self.path, theme)
            except Exception as error:  # UI preferences must never stop monitoring.
                self.on_error(error)
            finally:
                with self._condition:
                    self._writing = False
                    self._condition.notify_all()

    def wait_idle(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._pending is not None or self._writing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout: float = 1.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
            return not thread.is_alive()
        return True
