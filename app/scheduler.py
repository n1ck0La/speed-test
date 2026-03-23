from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.collectors import run_mtr_check, run_ping_check, run_speedtest
from app.config import SettingsStore
from app.db import Database


class MonitorScheduler:
    def __init__(self, db: Database, settings_store: SettingsStore) -> None:
        self.db = db
        self.settings_store = settings_store
        self.logger = logging.getLogger("speedtest.scheduler")
        self.scheduler = BackgroundScheduler(
            timezone=datetime.now().astimezone().tzinfo or timezone.utc
        )

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync_jobs(self) -> None:
        settings = self.settings_store.load()

        for job in self.scheduler.get_jobs():
            self.scheduler.remove_job(job.id)

        self.scheduler.add_job(
            self.cleanup_old_data,
            "interval",
            hours=settings.cleanup_interval_hours,
            id="cleanup",
            next_run_time=datetime.now() + timedelta(minutes=5),
            max_instances=1,
            replace_existing=True,
            coalesce=True,
        )

        if settings.speedtest_enabled:
            self.scheduler.add_job(
                self.collect_speedtest,
                "interval",
                minutes=settings.speedtest_interval_minutes,
                id="speedtest",
                next_run_time=datetime.now() + timedelta(seconds=5),
                max_instances=1,
                replace_existing=True,
                coalesce=True,
            )

        for target in self.db.list_ping_targets():
            if not target["active"]:
                continue
            self.scheduler.add_job(
                self.collect_ping_target,
                "interval",
                seconds=max(5, int(target["interval_seconds"])),
                args=[int(target["id"])],
                id=f"ping-{target['id']}",
                next_run_time=datetime.now() + timedelta(seconds=3),
                max_instances=1,
                replace_existing=True,
                coalesce=True,
            )

        for target in self.db.list_mtr_targets():
            if not target["active"]:
                continue
            self.scheduler.add_job(
                self.collect_mtr_target,
                "interval",
                seconds=max(10, int(target["interval_seconds"])),
                args=[int(target["id"])],
                id=f"mtr-{target['id']}",
                next_run_time=datetime.now() + timedelta(seconds=8),
                max_instances=1,
                replace_existing=True,
                coalesce=True,
            )

    def enqueue_speedtest_now(self) -> None:
        self.scheduler.add_job(
            self.collect_speedtest,
            id=f"speedtest-now-{datetime.now().timestamp()}",
            trigger="date",
            next_run_time=datetime.now(),
            replace_existing=False,
        )

    def enqueue_ping_now(self, target_id: int) -> None:
        self.scheduler.add_job(
            self.collect_ping_target,
            args=[target_id],
            id=f"ping-now-{target_id}-{datetime.now().timestamp()}",
            trigger="date",
            next_run_time=datetime.now(),
            replace_existing=False,
        )

    def enqueue_mtr_now(self, target_id: int) -> None:
        self.scheduler.add_job(
            self.collect_mtr_target,
            args=[target_id],
            id=f"mtr-now-{target_id}-{datetime.now().timestamp()}",
            trigger="date",
            next_run_time=datetime.now(),
            replace_existing=False,
        )

    def cleanup_old_data(self) -> None:
        settings = self.settings_store.load()
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_days)
        cutoff_iso = cutoff.replace(microsecond=0).isoformat()
        self.db.purge_older_than(cutoff_iso)
        self.logger.info("Purged samples older than %s", cutoff_iso)

    def collect_speedtest(self) -> None:
        settings = self.settings_store.load()
        self.logger.info("Running speedtest")
        result = run_speedtest(settings.speedtest_server_id, settings.speedtest_timeout_seconds)
        self.db.record_speedtest(result)
        if result["success"]:
            self.logger.info(
                "Speedtest complete: %.2f Mbps down / %.2f Mbps up",
                (result["download_bps"] or 0) / 1_000_000,
                (result["upload_bps"] or 0) / 1_000_000,
            )
            # Increment automatic tests counter
            settings.automatic_tests_run += 1
            self.settings_store.save(settings)
        else:
            self.logger.warning("Speedtest failed: %s", result["error"])

    def collect_ping_target(self, target_id: int) -> None:
        target = self.db.get_ping_target(target_id)
        if not target or not target["active"]:
            return
        self.logger.info("Running ping target %s (%s)", target["name"], target["host"])
        result = run_ping_check(
            target["host"],
            int(target["probe_count"]),
            float(target["timeout_seconds"]),
        )
        self.db.record_ping_result(target_id, result)

    def collect_mtr_target(self, target_id: int) -> None:
        target = self.db.get_mtr_target(target_id)
        if not target or not target["active"]:
            return
        self.logger.info("Running mtr target %s (%s)", target["name"], target["host"])
        result = run_mtr_check(
            target["host"],
            int(target["probe_count"]),
            float(target["timeout_seconds"]),
            int(target["max_hops"]),
        )
        self.db.record_mtr_run(target_id, result)
