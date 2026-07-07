"""A small per-host circuit breaker for outbound HTTP.

Wraps external calls (which already carry timeouts) so that a provider that is
down or slow stops being hammered: after ``threshold`` consecutive failures the
circuit opens for ``cooldown`` seconds and calls fail fast with ``CircuitOpen``;
one trial call is allowed after the cooldown (half-open) and a success closes it.

State is process-local (per worker) — good enough to shed load from one process;
a shared store would be needed for cluster-wide coordination.
"""

from __future__ import annotations

import contextlib
import threading
import time

DEFAULT_THRESHOLD = 5
DEFAULT_COOLDOWN = 30.0


class CircuitOpen(Exception):
    """Raised instead of making a call while the circuit for a host is open."""


class _Breaker:
    def __init__(self, threshold: int, cooldown: float):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self.lock = threading.Lock()

    def allow(self, now: float) -> bool:
        with self.lock:
            if self.opened_at is None:
                return True
            if now - self.opened_at >= self.cooldown:
                return True  # half-open: permit a single trial
            return False

    def record(self, ok: bool, now: float) -> None:
        with self.lock:
            if ok:
                self.failures = 0
                self.opened_at = None
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = now


_registry: dict[str, _Breaker] = {}
_registry_lock = threading.Lock()


def _breaker(host: str, threshold: int, cooldown: float) -> _Breaker:
    with _registry_lock:
        breaker = _registry.get(host)
        if breaker is None:
            breaker = _Breaker(threshold, cooldown)
            _registry[host] = breaker
        return breaker


@contextlib.contextmanager
def guard(host: str, *, threshold: int = DEFAULT_THRESHOLD, cooldown: float = DEFAULT_COOLDOWN):
    """Context manager guarding a call to ``host``. Raises CircuitOpen if open."""
    breaker = _breaker(host or "unknown", threshold, cooldown)
    if not breaker.allow(time.monotonic()):
        raise CircuitOpen(f"circuit open for {host}")
    ok = False
    try:
        yield
        ok = True
    finally:
        breaker.record(ok, time.monotonic())


def reset() -> None:
    """Clear all breaker state (used by tests)."""
    with _registry_lock:
        _registry.clear()
