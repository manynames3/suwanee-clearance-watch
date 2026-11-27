"""Remember what was already sent so a deal buzzes once, not every scan."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hd.notifiers.base import Notification


class SentLog:
    def __init__(self, path: str | Path, window_hours: float = 24.0) -> None:
        self.path = Path(path)
        self.window_s = window_hours * 3600
        self._sent: dict[str, float] = {}
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                self._sent = {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError):
            self._sent = {}

    def _fresh(self, ts: float, now: float) -> bool:
        return now - ts < self.window_s

    def unseen(self, items: list[Notification], now: float | None = None) -> list[Notification]:
        now = time.time() if now is None else now
        seen: set[str] = set()
        out = []
        for n in items:
            ts = self._sent.get(n.key)
            if (ts is not None and self._fresh(ts, now)) or n.key in seen:
                continue
            seen.add(n.key)
            out.append(n)
        return out

    def mark(self, items: list[Notification], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for n in items:
            self._sent[n.key] = now
        self._sent = {k: v for k, v in self._sent.items() if self._fresh(v, now)}
        try:
            self.path.write_text(json.dumps(self._sent))
        except OSError:
            pass
