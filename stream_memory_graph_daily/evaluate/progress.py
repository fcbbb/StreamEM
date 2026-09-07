from __future__ import annotations

import sys
import time
from typing import TextIO


class ConsoleProgress:
    """Dependency-free progress display that also works with redirected output."""

    def __init__(
        self,
        total: int,
        label: str,
        *,
        stream: TextIO | None = None,
        width: int = 28,
    ) -> None:
        self.total = max(0, int(total))
        self.label = label
        self.stream = stream or sys.stderr
        self.width = max(10, int(width))
        self.started_at = time.monotonic()
        self.completed = 0
        self._last_length = 0
        self._interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.update(0, "准备开始")

    def _line(self, status: str = "") -> str:
        ratio = self.completed / self.total if self.total else 1.0
        filled = min(self.width, int(self.width * ratio))
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = max(0.0, time.monotonic() - self.started_at)
        rate = self.completed / elapsed if elapsed > 0 and self.completed else 0.0
        eta = (self.total - self.completed) / rate if rate > 0 else None
        eta_text = f"ETA {eta:6.1f}s" if eta is not None else "ETA    --.-s"
        suffix = f" | {status}" if status else ""
        return (
            f"{self.label} [{bar}] {self.completed:>{len(str(self.total))}}/"
            f"{self.total} {ratio * 100:6.2f}% | {elapsed:6.1f}s | {eta_text}{suffix}"
        )

    def update(self, completed: int, status: str = "") -> None:
        self.completed = min(self.total, max(0, int(completed)))
        line = self._line(status)
        if self._interactive:
            padded = line.ljust(self._last_length)
            print(f"\r{padded}", end="", file=self.stream, flush=True)
            self._last_length = len(line)
        else:
            print(line, file=self.stream, flush=True)

    def write(self, message: str) -> None:
        if self._interactive:
            print(f"\r{' ' * self._last_length}\r", end="", file=self.stream)
        print(message, file=self.stream, flush=True)
        if self._interactive:
            self.update(self.completed)

    def finish(self, status: str = "完成") -> None:
        self.update(self.total, status)
        if self._interactive:
            print(file=self.stream, flush=True)


__all__ = ["ConsoleProgress"]
