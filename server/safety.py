"""Zero-storage kill-switch, anonymous rolling hush-spike breaker, and loopback health."""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PAUSE_FLAG_PATH = Path(os.environ.get("EARSHOT_PAUSE_FILE", "/etc/earshot.pause"))
HUSH_SPIKE_WINDOW_S = 60.0
HUSH_SPIKE_LIMIT = int(os.environ.get("EARSHOT_HUSH_SPIKE_LIMIT", "120"))
AUTO_PAUSE_COOLDOWN_S = int(os.environ.get("EARSHOT_AUTO_PAUSE_S", "300"))


@dataclass
class SafetyState:
    """Ephemeral in-RAM telemetry only — never stores IDs, IPs, coords, or disk files."""

    pause_flag_path: Path = field(default_factory=lambda: PAUSE_FLAG_PATH)
    hush_spike_window_s: float = HUSH_SPIKE_WINDOW_S
    hush_spike_limit: int = HUSH_SPIKE_LIMIT
    auto_pause_cooldown_s: int = AUTO_PAUSE_COOLDOWN_S
    hush_timestamps: deque[float] = field(default_factory=lambda: deque(maxlen=2048))
    auto_paused_until: float = 0.0
    total_hushes: int = 0
    total_trips: int = 0

    def reset(self) -> None:
        self.hush_timestamps.clear()
        self.auto_paused_until = 0.0
        self.total_hushes = 0
        self.total_trips = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.hush_spike_window_s
        while self.hush_timestamps and self.hush_timestamps[0] < cutoff:
            self.hush_timestamps.popleft()

    def is_manual_paused(self) -> bool:
        return self.pause_flag_path.exists()

    def auto_pause_remaining_s(self) -> int:
        rem = self.auto_paused_until - time.monotonic()
        return max(0, int(rem))

    def pause_reason(self) -> str | None:
        if self.is_manual_paused():
            return "manual_pause"
        if self.auto_pause_remaining_s() > 0:
            return "hush_spike_cooldown"
        return None

    def record_hush(self) -> bool:
        """Record an accepted hush event; returns True if circuit breaker tripped."""
        now = time.monotonic()
        self.total_hushes += 1
        self.hush_timestamps.append(now)
        self._prune(now)
        if len(self.hush_timestamps) >= self.hush_spike_limit and self.auto_pause_remaining_s() == 0:
            self.auto_paused_until = now + self.auto_pause_cooldown_s
            self.total_trips += 1
            return True
        return False

    def snapshot(self, active_sessions: int) -> dict:
        now = time.monotonic()
        self._prune(now)
        reason = self.pause_reason()
        return {
            "status": "paused" if reason else "ok",
            "pause_reason": reason,
            "manual_pause_file": str(self.pause_flag_path),
            "auto_pause_remaining_s": self.auto_pause_remaining_s(),
            "active_sessions": active_sessions,
            "hush_events_60s": len(self.hush_timestamps),
            "hush_spike_limit_60s": self.hush_spike_limit,
            "total_hushes_since_boot": self.total_hushes,
            "circuit_breaker_trips": self.total_trips,
        }


safety = SafetyState()
