from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "monitor.db"
SETTINGS_PATH = DATA_DIR / "settings.json"


@dataclass(slots=True)
class Settings:
    retention_days: int = 30
    cleanup_interval_hours: int = 24
    dashboard_range_key: str = "24h"
    speedtest_enabled: bool = True
    speedtest_interval_minutes: int = 30
    speedtest_server_id: str = ""
    speedtest_timeout_seconds: int = 240
    speedtest_start_delay_hours: int = 0
    log_max_mb: int = 10
    log_backup_count: int = 5
    pinned_monitors: list[str] = None
    manual_tests_run: int = 0
    automatic_tests_run: int = 0

    def __post_init__(self):
        if self.pinned_monitors is None:
            self.pinned_monitors = []

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        payload = {}
        for field_name in cls.__dataclass_fields__:
            if field_name in raw:
                payload[field_name] = raw[field_name]
        return cls(**payload).normalized()

    def normalized(self) -> "Settings":
        self.retention_days = max(1, int(self.retention_days))
        self.cleanup_interval_hours = max(1, int(self.cleanup_interval_hours))
        self.dashboard_range_key = str(self.dashboard_range_key or "24h").strip() or "24h"
        self.speedtest_interval_minutes = max(1, int(self.speedtest_interval_minutes))
        self.speedtest_timeout_seconds = max(30, int(self.speedtest_timeout_seconds))
        self.speedtest_start_delay_hours = max(0, min(23, int(self.speedtest_start_delay_hours)))
        self.log_max_mb = max(1, int(self.log_max_mb))
        self.log_backup_count = max(1, int(self.log_backup_count))
        self.speedtest_server_id = str(self.speedtest_server_id or "").strip()
        self.speedtest_enabled = bool(self.speedtest_enabled)
        if not isinstance(self.pinned_monitors, list):
            self.pinned_monitors = []
        return self

    def to_dict(self) -> dict:
        return asdict(self)


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Settings:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            settings = Settings()
            self.save(settings)
            return settings
        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        return Settings.from_dict(raw)

    def save(self, settings: Settings) -> Settings:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        normalized = settings.normalized()
        with self._lock:
            self.path.write_text(
                json.dumps(normalized.to_dict(), indent=2),
                encoding="utf-8",
            )
        return normalized
