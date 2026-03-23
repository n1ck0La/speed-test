#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "data" / "monitor.db"
SETTINGS_PATH = BASE_DIR / "data" / "settings.json"
LOG_DIR = BASE_DIR / "logs"


def clear_measurements() -> dict[str, int]:
    summary = {
        "speedtests": 0,
        "ping_results": 0,
        "mtr_runs": 0,
        "mtr_hops": 0,
    }

    if not DB_PATH.exists():
        return summary

    with sqlite3.connect(DB_PATH) as conn:
        for table in summary:
            summary[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute("DELETE FROM speedtests")
        conn.execute("DELETE FROM ping_results")
        conn.execute("DELETE FROM mtr_hops")
        conn.execute("DELETE FROM mtr_runs")
        conn.commit()
        conn.execute("VACUUM")

    return summary


def reset_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}

    payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))

    valid_pins: list[str] = []
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            ping_ids = {f"ping_{row[0]}" for row in conn.execute("SELECT id FROM ping_targets")}
            mtr_ids = {f"mtr_{row[0]}" for row in conn.execute("SELECT id FROM mtr_targets")}
        known_pins = ping_ids | mtr_ids | {"speedtest"}
        valid_pins = [pin for pin in payload.get("pinned_monitors", []) if pin in known_pins]

    payload["manual_tests_run"] = 0
    payload["automatic_tests_run"] = 0
    payload["pinned_monitors"] = valid_pins

    SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def clear_logs() -> list[str]:
    removed: list[str] = []
    if not LOG_DIR.exists():
        return removed

    for path in sorted(LOG_DIR.iterdir()):
        if path.is_file():
            path.unlink()
            removed.append(path.name)
    return removed


def main() -> None:
    summary = clear_measurements()
    settings = reset_settings()
    removed_logs = clear_logs()

    print("Cleared measurement data:")
    for table, count in summary.items():
        print(f"  {table}: {count}")

    if settings:
        print("Reset counters and pins:")
        print(f"  manual_tests_run: {settings.get('manual_tests_run', 0)}")
        print(f"  automatic_tests_run: {settings.get('automatic_tests_run', 0)}")
        print(f"  pinned_monitors: {settings.get('pinned_monitors', [])}")

    if removed_logs:
        print("Removed log files:")
        for name in removed_logs:
            print(f"  {name}")


if __name__ == "__main__":
    main()
