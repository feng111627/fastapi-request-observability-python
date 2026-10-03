"""Runtime-tunable observability settings."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ObservabilitySettings:
    """Configuration for tracing, sampling and log rotation.

    Instances are immutable; runtime changes swap the whole object so
    readers always see a consistent snapshot.
    """

    slow_request_ms: float = 500.0
    sample_rate: float = 1.0
    log_dir: str = "logs"
    log_file: str = "requests.log"
    rotation_max_bytes: int = 1_048_576
    rotation_backup_count: int = 3


class SettingsHolder:
    """Thread-safe holder allowing runtime updates of settings."""

    def __init__(self, settings: ObservabilitySettings | None = None) -> None:
        self._lock = threading.Lock()
        self._settings = settings or ObservabilitySettings()

    def get(self) -> ObservabilitySettings:
        with self._lock:
            return self._settings

    def update(self, **changes: object) -> ObservabilitySettings:
        """Atomically replace settings; unknown keys raise TypeError."""
        with self._lock:
            self._settings = replace(self._settings, **changes)
            return self._settings


settings_holder = SettingsHolder()
