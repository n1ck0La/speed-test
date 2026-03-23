from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR, DB_PATH, Settings, SettingsStore
from app.db import Database
from app.logging_utils import configure_logging
from app.scheduler import MonitorScheduler


KYIV = ZoneInfo("Europe/Kyiv")

# Speedtest range options (longer time periods)
SPEEDTEST_RANGE_OPTIONS = [
    {"key": "1h", "label": "1 hour", "delta": timedelta(hours=1)},
    {"key": "6h", "label": "6 hours", "delta": timedelta(hours=6)},
    {"key": "24h", "label": "24 hours", "delta": timedelta(hours=24)},
    {"key": "1w", "label": "1 week", "delta": timedelta(days=7)},
    {"key": "2w", "label": "2 weeks", "delta": timedelta(days=14)},
    {"key": "1m", "label": "1 month", "delta": timedelta(days=30)},
]

# Ping/MTR range options (shorter time periods)
PING_RANGE_OPTIONS = [
    {"key": "5m", "label": "5 minutes", "delta": timedelta(minutes=5)},
    {"key": "10m", "label": "10 minutes", "delta": timedelta(minutes=10)},
    {"key": "30m", "label": "30 minutes", "delta": timedelta(minutes=30)},
    {"key": "1h", "label": "1 hour", "delta": timedelta(hours=1)},
    {"key": "6h", "label": "6 hours", "delta": timedelta(hours=6)},
    {"key": "24h", "label": "24 hours", "delta": timedelta(hours=24)},
]

# Legacy range options (for backward compatibility)
RANGE_OPTIONS = SPEEDTEST_RANGE_OPTIONS

SPEEDTEST_RANGE_BY_KEY = {item["key"]: item for item in SPEEDTEST_RANGE_OPTIONS}
PING_RANGE_BY_KEY = {item["key"]: item for item in PING_RANGE_OPTIONS}
RANGE_BY_KEY = SPEEDTEST_RANGE_BY_KEY
SPEEDTEST_INTERVAL_OPTIONS = [1, 5, 10, 15, 30, 60, 120, 240]


def format_dt(value: str | None) -> str:
    if not value:
        return "Never"
    dt = datetime.fromisoformat(value)
    return dt.astimezone(KYIV).strftime("%Y-%m-%d %H:%M:%S")


def mbps(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / 1_000_000:.2f}"


def ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def loss_text(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def resolve_range(
    range_key: str | None, 
    settings: Settings,
    range_options: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    if range_options is None:
        range_options = RANGE_BY_KEY
    key = (range_key or settings.dashboard_range_key or "24h").strip()
    return range_options.get(key, list(range_options.values())[0])


def speedtest_chart(points: list[dict]) -> dict[str, Any]:
    return {
        "x_label": "Time",
        "y_label": "Mbit/s",
        "unit": "Mbit/s",
        "labels": [format_dt(point["recorded_at"]) for point in points],
        "series": [
            {
                "key": "download_mbps",
                "label": "Download",
                "unit": "Mbit/s",
                "values": [
                    round(point["download_bps"] / 1_000_000, 2) if point["download_bps"] else None
                    for point in points
                ],
            },
            {
                "key": "upload_mbps",
                "label": "Upload",
                "unit": "Mbit/s",
                "values": [
                    round(point["upload_bps"] / 1_000_000, 2) if point["upload_bps"] else None
                    for point in points
                ],
            },
            {
                "key": "ping_ms",
                "label": "Latency",
                "unit": "ms",
                "values": [point.get("ping_ms") for point in points],
            },
        ],
    }


def ping_chart(points: list[dict]) -> dict[str, Any]:
    return {
        "x_label": "Time",
        "y_label": "ms / %",
        "unit": "ms / %",
        "labels": [format_dt(point["recorded_at"]) for point in points],
        "series": [
            {
                "key": "avg_ms",
                "label": "Latency",
                "values": [point.get("avg_ms") for point in points],
            },
            {
                "key": "jitter_ms",
                "label": "Jitter",
                "values": [point.get("jitter_ms") for point in points],
            },
            {
                "key": "packet_loss",
                "label": "Packet Loss %",
                "values": [point.get("packet_loss") for point in points],
            },
        ],
    }


def serialize_latest_speedtest(latest_speedtest: dict | None) -> dict[str, Any]:
    if not latest_speedtest:
        return {
            "download_text": "n/a",
            "upload_text": "n/a",
            "latency_text": "n/a",
            "server_text": "No speedtest data yet",
            "location_text": "Server n/a",
            "last_text": "Never",
            "error": None,
        }
    return {
        "download_text": mbps(latest_speedtest.get("download_bps")),
        "upload_text": mbps(latest_speedtest.get("upload_bps")),
        "latency_text": ms(latest_speedtest.get("ping_ms")),
        "server_text": latest_speedtest.get("server_sponsor") or "Auto-selected",
        "location_text": latest_speedtest.get("server_location") or "Server n/a",
        "last_text": format_dt(latest_speedtest.get("recorded_at")),
        "error": latest_speedtest.get("error"),
    }


def serialize_ping_target(target: dict, latest: dict | None, history: list[dict]) -> dict[str, Any]:
    return {
        "id": int(target["id"]),
        "active": bool(target["active"]),
        "latest": {
            "avg_text": ms(latest.get("avg_ms") if latest else None),
            "jitter_text": ms(latest.get("jitter_ms") if latest else None),
            "loss_text": loss_text(latest.get("packet_loss") if latest else None),
            "last_text": format_dt(latest.get("recorded_at")) if latest else "Never",
            "error": latest.get("error") if latest else None,
        },
        "chart": ping_chart(history),
    }


def serialize_mtr_target(target: dict, latest_run: dict | None, hops: list[dict]) -> dict[str, Any]:
    return {
        "id": int(target["id"]),
        "active": bool(target["active"]),
        "interval_text": f"{target['interval_seconds']}s",
        "probes_text": str(target["probe_count"]),
        "hops_text": str(latest_run["hop_count"]) if latest_run else "0",
        "last_text": format_dt(latest_run.get("recorded_at")) if latest_run else "Never",
        "error": latest_run.get("error") if latest_run else None,
        "hops": [
            {
                "hop_index": hop["hop_index"],
                "address": hop["address"],
                "loss_text": loss_text(hop.get("packet_loss")),
                "avg_text": ms(hop.get("avg_ms")),
                "jitter_text": ms(hop.get("jitter_ms")),
            }
            for hop in hops
        ],
    }


def build_dashboard_data(db: Database, settings: Settings, range_key: str | None) -> dict[str, Any]:
    range_option = resolve_range(range_key, settings)
    since_utc = datetime.now(timezone.utc) - range_option["delta"]
    since_iso = since_utc.isoformat()

    latest_speedtest = db.latest_speedtest()
    speedtest_points = db.speedtest_history(since_iso)

    ping_targets: list[dict[str, Any]] = []
    mtr_targets: list[dict[str, Any]] = []

    for target in db.list_ping_targets():
        latest = db.latest_ping_result(int(target["id"]))
        history = db.ping_history(int(target["id"]), since_iso)
        ping_targets.append(
            {
                "target": target,
                "latest": latest,
                "history": history,
                "live": serialize_ping_target(target, latest, history),
            }
        )

    for target in db.list_mtr_targets():
        latest_run = db.latest_mtr_run(int(target["id"]))
        hops = db.mtr_hops_for_run(int(latest_run["id"])) if latest_run else []
        mtr_targets.append(
            {
                "target": target,
                "latest_run": latest_run,
                "hops": hops,
                "live": serialize_mtr_target(target, latest_run, hops),
            }
        )

    speedtest_live = serialize_latest_speedtest(latest_speedtest)
    payload = {
        "range_key": range_option["key"],
        "range_label": range_option["label"],
        "overview": {
            "download_text": speedtest_live["download_text"],
            "upload_text": speedtest_live["upload_text"],
            "latency_text": speedtest_live["latency_text"],
            "monitors_text": f"{sum(1 for item in ping_targets if item['target']['active']) + sum(1 for item in mtr_targets if item['target']['active'])}",
            "monitors_meta": f"{sum(1 for item in ping_targets if item['target']['active'])} ping, {sum(1 for item in mtr_targets if item['target']['active'])} MTR",
        },
        "speedtest": {
            **speedtest_live,
            "chart": speedtest_chart(speedtest_points),
            "points_count": len(speedtest_points),
        },
        "ping_targets": [item["live"] for item in ping_targets],
        "mtr_targets": [item["live"] for item in mtr_targets],
    }

    return {
        "range_option": range_option,
        "payload": payload,
        "latest_speedtest": latest_speedtest,
        "speedtest_points": speedtest_points,
        "ping_targets": ping_targets,
        "mtr_targets": mtr_targets,
    }


def create_app() -> FastAPI:
    settings_store = SettingsStore()
    db = Database(DB_PATH)
    scheduler = MonitorScheduler(db, settings_store)
    templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
    templates.env.filters["format_dt"] = format_dt
    templates.env.filters["mbps"] = mbps
    templates.env.filters["ms"] = ms

    def render_template(request: Request, name: str, context: dict[str, Any]):
        context = {**context, "request": request}
        try:
            return templates.TemplateResponse(request=request, name=name, context=context)
        except TypeError:
            return templates.TemplateResponse(name, context)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.initialize()
        configure_logging(settings_store.load())
        scheduler.start()
        scheduler.sync_jobs()
        app.state.db = db
        app.state.settings_store = settings_store
        app.state.scheduler = scheduler
        yield
        scheduler.shutdown()

    app = FastAPI(title="Speedtest Monitor", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

    @app.get("/")
    async def dashboard(request: Request):
        return RedirectResponse("/dashboard", status_code=302)

    @app.get("/speedtest")
    async def speedtest_config_page(request: Request):
        settings = settings_store.load()

        context = {
            "request": request,
            "settings": settings,
            "page": "speedtest_config",
            "range_key": None,
            "range_label": None,
            "range_options": SPEEDTEST_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/speedtest/configure")
    async def speedtest_config_redirect():
        return RedirectResponse("/speedtest", status_code=303)

    @app.get("/ping")
    async def ping_config_page(request: Request):
        settings = settings_store.load()

        context = {
            "request": request,
            "settings": settings,
            "page": "ping_config",
            "range_key": None,
            "range_label": None,
            "range_options": PING_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/mtr")
    async def mtr_config_page(request: Request):
        settings = settings_store.load()

        context = {
            "request": request,
            "settings": settings,
            "page": "mtr_config",
            "range_key": None,
            "range_label": None,
            "range_options": PING_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/monitors")
    async def monitors_page(request: Request):
        settings = settings_store.load()

        # Get ping targets for monitors display
        ping_targets: list[dict[str, Any]] = []
        for target in db.list_ping_targets():
            latest = db.latest_ping_result(int(target["id"]))
            ping_targets.append(
                {
                    "target": target,
                    "latest": latest,
                }
            )

        # Get MTR targets for monitors display
        mtr_targets: list[dict[str, Any]] = []
        for target in db.list_mtr_targets():
            latest_run = db.latest_mtr_run(int(target["id"]))
            mtr_targets.append(
                {
                    "target": target,
                    "latest_run": latest_run,
                }
            )

        active_ping_count = sum(1 for item in ping_targets if item["target"]["active"])
        active_mtr_count = sum(1 for item in mtr_targets if item["target"]["active"])
        active_speedtest_count = 1 if settings.speedtest_enabled else 0

        context = {
            "request": request,
            "settings": settings,
            "page": "monitors",
            "range_key": None,
            "range_label": None,
            "range_options": SPEEDTEST_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": ping_targets,
            "mtr_targets": mtr_targets,
            "active_ping_count": active_ping_count,
            "active_mtr_count": active_mtr_count,
            "active_speedtest_count": active_speedtest_count,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/dashboard")
    async def dashboard_page(request: Request):
        settings = settings_store.load()
        
        # Check for custom date range
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        
        if from_param and to_param:
            # Custom date range provided
            range_key = "custom"
            since_iso = from_param
            until_iso = to_param
            range_label = f"Custom: {from_param[:16]} to {to_param[:16]}"
        else:
            # Use standard range
            range_key = request.query_params.get("range") or settings.dashboard_range_key
            range_option = resolve_range(range_key, settings, SPEEDTEST_RANGE_BY_KEY)
            since_iso = (datetime.now(timezone.utc) - range_option["delta"]).isoformat()
            until_iso = None
            range_label = range_option["label"]

        # Always load speedtest data
        earliest_speedtest = db.earliest_speedtest()
        latest_speedtest = db.latest_speedtest()
        speedtest_points = db.speedtest_history(since_iso, until_iso)
        speedtest_live = serialize_latest_speedtest(latest_speedtest)
        speedtest_history_json = json.dumps(speedtest_chart(speedtest_points))

        # Get pinned ping and mtr targets
        pinned_list = settings.pinned_monitors or []
        pinned_ping_ids = [int(pid.split("_")[1]) for pid in pinned_list if pid.startswith("ping_")]
        pinned_mtr_ids = [int(pid.split("_")[1]) for pid in pinned_list if pid.startswith("mtr_")]

        # Get ping targets if pinned
        ping_targets: list[dict[str, Any]] = []
        for target in db.list_ping_targets():
            if int(target["id"]) in pinned_ping_ids:
                latest = db.latest_ping_result(int(target["id"]))
                history = db.ping_history(int(target["id"]), since_iso, until_iso)
                ping_targets.append({
                    "target": target,
                    "latest": latest,
                    "history": history,
                    "live": serialize_ping_target(target, latest, history),
                })

        # Get mtr targets if pinned
        mtr_targets: list[dict[str, Any]] = []
        for target in db.list_mtr_targets():
            if int(target["id"]) in pinned_mtr_ids:
                latest_run = db.latest_mtr_run(int(target["id"]))
                hops = db.mtr_hops_for_run(int(latest_run["id"])) if latest_run else []
                mtr_targets.append({
                    "target": target,
                    "latest_run": latest_run,
                    "hops": hops,
                    "live": serialize_mtr_target(target, latest_run, hops),
                })

        context = {
            "request": request,
            "settings": settings,
            "page": "dashboard",
            "range_key": range_key,
            "range_label": range_label,
            "range_options": SPEEDTEST_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": latest_speedtest,
            "earliest_speedtest_date": earliest_speedtest.get("recorded_at") if earliest_speedtest else None,
            "today_date": datetime.now().date().isoformat(),
            "speedtest_history_json": speedtest_history_json,
            "speedtest_points_count": len(speedtest_points),
            "speedtest_live": speedtest_live,
            "ping_targets": ping_targets,
            "mtr_targets": mtr_targets,
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/monitor/speedtest")
    async def speedtest_detail_page(request: Request):
        settings = settings_store.load()
        
        # Check for custom date range
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        
        if from_param and to_param:
            # Custom date range provided
            range_key = "custom"
            since_iso = from_param
            until_iso = to_param
            range_label = f"Custom: {from_param[:16]} to {to_param[:16]}"
        else:
            # Use standard range
            range_key = request.query_params.get("range") or settings.dashboard_range_key
            range_option = resolve_range(range_key, settings, SPEEDTEST_RANGE_BY_KEY)
            since_iso = (datetime.now(timezone.utc) - range_option["delta"]).isoformat()
            until_iso = None
            range_label = range_option["label"]

        earliest_speedtest = db.earliest_speedtest()
        latest_speedtest = db.latest_speedtest()
        speedtest_points = db.speedtest_history(since_iso, until_iso)
        speedtest_live = serialize_latest_speedtest(latest_speedtest)
        is_pinned = "speedtest" in (settings.pinned_monitors or [])

        context = {
            "request": request,
            "settings": settings,
            "page": "monitor_speedtest",
            "range_key": range_key,
            "range_label": range_label,
            "range_options": SPEEDTEST_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": latest_speedtest,
            "earliest_speedtest_date": earliest_speedtest.get("recorded_at") if earliest_speedtest else None,
            "today_date": datetime.now().date().isoformat(),
            "speedtest_history_json": json.dumps(speedtest_chart(speedtest_points)),
            "speedtest_points_count": len(speedtest_points),
            "speedtest_live": speedtest_live,
            "ping_targets": [],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "is_pinned": is_pinned,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/monitor/ping/{target_id}")
    async def ping_detail_page(request: Request, target_id: int):
        settings = settings_store.load()
        target = db.get_ping_target(target_id)
        if not target:
            return RedirectResponse("/monitors", status_code=303)

        range_key = request.query_params.get("range") or settings.dashboard_range_key
        range_option = resolve_range(range_key, settings, PING_RANGE_BY_KEY)
        since_utc = datetime.now(timezone.utc) - range_option["delta"]
        since_iso = since_utc.isoformat()

        latest = db.latest_ping_result(target_id)
        history = db.ping_history(target_id, since_iso)
        ping_live = serialize_ping_target(target, latest, history)
        is_pinned = f"ping_{target_id}" in (settings.pinned_monitors or [])

        context = {
            "request": request,
            "settings": settings,
            "page": "monitor_ping",
            "monitor_id": target_id,
            "monitor_name": target.get("name", "Ping"),
            "range_key": range_option["key"],
            "range_label": range_option["label"],
            "range_options": PING_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [{"target": target, "latest": latest, "history": history, "live": ping_live}],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "is_pinned": is_pinned,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.get("/monitor/mtr/{target_id}")
    async def mtr_detail_page(request: Request, target_id: int):
        settings = settings_store.load()
        target = db.get_mtr_target(target_id)
        if not target:
            return RedirectResponse("/monitors", status_code=303)

        range_key = request.query_params.get("range") or settings.dashboard_range_key
        range_option = resolve_range(range_key, settings, PING_RANGE_BY_KEY)
        
        latest_run = db.latest_mtr_run(target_id)
        hops = db.mtr_hops_for_run(int(latest_run["id"])) if latest_run else []
        mtr_live = serialize_mtr_target(target, latest_run, hops)
        is_pinned = f"mtr_{target_id}" in (settings.pinned_monitors or [])

        context = {
            "request": request,
            "settings": settings,
            "page": "monitor_mtr",
            "monitor_id": target_id,
            "monitor_name": target.get("name", "MTR"),
            "range_key": range_option["key"],
            "range_label": range_option["label"],
            "range_options": PING_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [],
            "mtr_targets": [{"target": target, "latest_run": latest_run, "hops": hops, "live": mtr_live}],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "is_pinned": is_pinned,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.post("/monitor/speedtest/pin")
    async def toggle_speedtest_pin():
        settings = settings_store.load()
        if settings.pinned_monitors is None:
            settings.pinned_monitors = []
        
        if "speedtest" in settings.pinned_monitors:
            settings.pinned_monitors.remove("speedtest")
        else:
            settings.pinned_monitors.append("speedtest")
        
        settings_store.save(settings)
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/monitor/ping/{target_id}/pin")
    async def toggle_ping_pin(target_id: int):
        settings = settings_store.load()
        if settings.pinned_monitors is None:
            settings.pinned_monitors = []
        
        pin_id = f"ping_{target_id}"
        if pin_id in settings.pinned_monitors:
            settings.pinned_monitors.remove(pin_id)
        else:
            settings.pinned_monitors.append(pin_id)
        
        settings_store.save(settings)
        return RedirectResponse(f"/monitor/ping/{target_id}", status_code=303)

    @app.post("/monitor/mtr/{target_id}/pin")
    async def toggle_mtr_pin(target_id: int):
        settings = settings_store.load()
        if settings.pinned_monitors is None:
            settings.pinned_monitors = []
        
        pin_id = f"mtr_{target_id}"
        if pin_id in settings.pinned_monitors:
            settings.pinned_monitors.remove(pin_id)
        else:
            settings.pinned_monitors.append(pin_id)
        
        settings_store.save(settings)
        return RedirectResponse(f"/monitor/mtr/{target_id}", status_code=303)

    @app.get("/api/dashboard")
    async def dashboard_api(request: Request):
        settings = settings_store.load()
        range_key = request.query_params.get("range")
        dashboard_data = build_dashboard_data(db, settings, range_key)
        return JSONResponse(dashboard_data["payload"])

    @app.get("/settings")
    async def settings_page(request: Request):
        settings = settings_store.load()

        context = {
            "request": request,
            "settings": settings,
            "page": "settings",
            "range_key": "24h",
            "range_label": "24 hours",
            "range_options": SPEEDTEST_RANGE_OPTIONS,
            "speedtest_interval_options": sorted(
                set(SPEEDTEST_INTERVAL_OPTIONS + [settings.speedtest_interval_minutes])
            ),
            "speedtest_servers": [],
            "latest_speedtest": None,
            "speedtest_history_json": json.dumps({"labels": [], "series": []}),
            "speedtest_points_count": 0,
            "speedtest_live": {},
            "ping_targets": [],
            "mtr_targets": [],
            "active_ping_count": 0,
            "active_mtr_count": 0,
            "retention_cutoff": format_dt(
                (datetime.now().astimezone() - timedelta(days=settings.retention_days)).isoformat()
            ),
        }
        return render_template(request, "dashboard.html", context)

    @app.post("/settings")
    async def update_settings(
        retention_days: int = Form(...),
        cleanup_interval_hours: int = Form(...),
        dashboard_range_key: str = Form(...),
        log_max_mb: int = Form(...),
        log_backup_count: int = Form(...),
    ):
        # Load existing settings and preserve speedtest config and pinned monitors
        existing = settings_store.load()
        settings = Settings(
            retention_days=retention_days,
            cleanup_interval_hours=cleanup_interval_hours,
            dashboard_range_key=dashboard_range_key,
            speedtest_enabled=existing.speedtest_enabled,
            speedtest_interval_minutes=existing.speedtest_interval_minutes,
            speedtest_server_id="",
            speedtest_timeout_seconds=existing.speedtest_timeout_seconds,
            speedtest_start_delay_hours=existing.speedtest_start_delay_hours,
            log_max_mb=log_max_mb,
            log_backup_count=log_backup_count,
            pinned_monitors=existing.pinned_monitors,
            manual_tests_run=existing.manual_tests_run,
            automatic_tests_run=existing.automatic_tests_run,
        )
        settings_store.save(settings)
        configure_logging(settings)
        scheduler.sync_jobs()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/speedtest/configure")
    async def configure_speedtest(
        speedtest_enabled: str | None = Form(default=None),
        speedtest_interval_minutes: int = Form(...),
        speedtest_timeout_seconds: int = Form(...),
        speedtest_start_delay_hours: int = Form(default=0),
    ):
        # Load existing settings and preserve non-speedtest config and pinned monitors
        existing = settings_store.load()
        settings = Settings(
            retention_days=existing.retention_days,
            cleanup_interval_hours=existing.cleanup_interval_hours,
            dashboard_range_key=existing.dashboard_range_key,
            speedtest_enabled=speedtest_enabled == "on",
            speedtest_interval_minutes=speedtest_interval_minutes,
            speedtest_server_id="",
            speedtest_timeout_seconds=speedtest_timeout_seconds,
            speedtest_start_delay_hours=speedtest_start_delay_hours,
            log_max_mb=existing.log_max_mb,
            log_backup_count=existing.log_backup_count,
            pinned_monitors=existing.pinned_monitors,
            manual_tests_run=existing.manual_tests_run,
            automatic_tests_run=existing.automatic_tests_run,
        )
        settings_store.save(settings)
        configure_logging(settings)
        scheduler.sync_jobs()
        return RedirectResponse("/speedtest", status_code=303)

    @app.post("/speedtest/run")
    async def run_speedtest_now():
        scheduler.enqueue_speedtest_now()
        settings = settings_store.load()
        settings.manual_tests_run += 1
        settings_store.save(settings)
        return JSONResponse({"status": "running", "message": "Speedtest started"})

    @app.post("/ping-targets")
    async def create_ping_target(
        name: str = Form(...),
        host: str = Form(...),
        interval_seconds: int = Form(...),
        probe_count: int = Form(...),
        timeout_seconds: float = Form(...),
        active: str | None = Form(default=None),
        redirect_to: str | None = Form(default=None),
    ):
        target_id = db.create_ping_target(
            name=name.strip(),
            host=host.strip(),
            interval_seconds=max(5, interval_seconds),
            probe_count=max(1, probe_count),
            timeout_seconds=max(1.0, timeout_seconds),
            active=active == "on",
        )
        scheduler.sync_jobs()
        scheduler.enqueue_ping_now(target_id)
        return_to = redirect_to or "/ping"
        return RedirectResponse(return_to, status_code=303)

    @app.post("/ping-targets/{target_id}/toggle")
    async def toggle_ping_target(target_id: int):
        db.toggle_ping_target(target_id)
        scheduler.sync_jobs()
        return RedirectResponse("/", status_code=303)

    @app.post("/ping-targets/{target_id}/delete")
    async def delete_ping_target(target_id: int, redirect_to: str | None = Form(default=None)):
        db.delete_ping_target(target_id)
        scheduler.sync_jobs()
        return_url = redirect_to or "/monitors"
        return RedirectResponse(return_url, status_code=303)

    @app.post("/ping-targets/{target_id}/run")
    async def run_ping_target(target_id: int):
        scheduler.enqueue_ping_now(target_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/mtr-targets")
    async def create_mtr_target(
        name: str = Form(...),
        host: str = Form(...),
        interval_seconds: int = Form(...),
        probe_count: int = Form(...),
        timeout_seconds: float = Form(...),
        max_hops: int = Form(...),
        active: str | None = Form(default=None),
        redirect_to: str | None = Form(default=None),
    ):
        target_id = db.create_mtr_target(
            name=name.strip(),
            host=host.strip(),
            interval_seconds=max(10, interval_seconds),
            probe_count=max(1, probe_count),
            timeout_seconds=max(1.0, timeout_seconds),
            max_hops=max(1, max_hops),
            active=active == "on",
        )
        scheduler.sync_jobs()
        scheduler.enqueue_mtr_now(target_id)
        return_to = redirect_to or "/mtr"
        return RedirectResponse(return_to, status_code=303)

    @app.post("/mtr-targets/{target_id}/toggle")
    async def toggle_mtr_target(target_id: int):
        db.toggle_mtr_target(target_id)
        scheduler.sync_jobs()
        return RedirectResponse("/", status_code=303)

    @app.post("/mtr-targets/{target_id}/delete")
    async def delete_mtr_target(target_id: int, redirect_to: str | None = Form(default=None)):
        db.delete_mtr_target(target_id)
        scheduler.sync_jobs()
        return_url = redirect_to or "/monitors"
        return RedirectResponse(return_url, status_code=303)

    @app.post("/mtr-targets/{target_id}/run")
    async def run_mtr_target(target_id: int):
        scheduler.enqueue_mtr_now(target_id)
        return RedirectResponse("/", status_code=303)

    @app.get("/api/dashboard-data")
    async def api_dashboard_data(request: Request):
        """API endpoint to fetch fresh dashboard data without reloading the page"""
        settings = settings_store.load()
        
        # Check for custom date range
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        
        if from_param and to_param:
            # Custom date range provided
            since_iso = from_param
            until_iso = to_param
        else:
            # Use standard range
            range_key = request.query_params.get("range") or settings.dashboard_range_key
            range_option = resolve_range(range_key, settings, SPEEDTEST_RANGE_BY_KEY)
            since_iso = (datetime.now(timezone.utc) - range_option["delta"]).isoformat()
            until_iso = None

        # Load speedtest data
        latest_speedtest = db.latest_speedtest()
        speedtest_points = db.speedtest_history(since_iso, until_iso)
        speedtest_live = serialize_latest_speedtest(latest_speedtest)

        # Get pinned ping targets
        pinned_list = settings.pinned_monitors or []
        pinned_ping_ids = [int(pid.split("_")[1]) for pid in pinned_list if pid.startswith("ping_")]
        
        ping_data = {}
        for target in db.list_ping_targets():
            if int(target["id"]) in pinned_ping_ids:
                latest = db.latest_ping_result(int(target["id"]))
                history = db.ping_history(int(target["id"]), since_iso, until_iso)
                ping_live = serialize_ping_target(target, latest, history)
                ping_data[str(target["id"])] = {
                    "target": target,
                    "latest": latest,
                    "live": ping_live,
                }

        # Get pinned MTR targets
        pinned_mtr_ids = [int(pid.split("_")[1]) for pid in pinned_list if pid.startswith("mtr_")]
        
        mtr_data = {}
        for target in db.list_mtr_targets():
            if int(target["id"]) in pinned_mtr_ids:
                latest_run = db.latest_mtr_run(int(target["id"]))
                hops = db.mtr_hops_for_run(int(latest_run["id"])) if latest_run else []
                mtr_live = serialize_mtr_target(target, latest_run, hops)
                mtr_data[str(target["id"])] = {
                    "target": target,
                    "latest_run": latest_run,
                    "live": mtr_live,
                }

        return JSONResponse({
            "speedtest": {
                "live": speedtest_live,
                "chart": speedtest_chart(speedtest_points),
            },
            "ping": ping_data,
            "mtr": mtr_data,
        })

    @app.get("/api/monitor/ping/{target_id}")
    async def api_ping_monitor(request: Request, target_id: int):
        """API endpoint to fetch fresh ping monitor data"""
        settings = settings_store.load()
        target = db.get_ping_target(target_id)
        if not target:
            return JSONResponse({"error": "Target not found"}, status_code=404)

        # Check for custom date range
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        
        if from_param and to_param:
            since_iso = from_param
            until_iso = to_param
        else:
            range_key = request.query_params.get("range") or settings.dashboard_range_key
            range_option = resolve_range(range_key, settings, PING_RANGE_BY_KEY)
            since_iso = (datetime.now(timezone.utc) - range_option["delta"]).isoformat()
            until_iso = None

        latest = db.latest_ping_result(target_id)
        history = db.ping_history(target_id, since_iso, until_iso)
        ping_live = serialize_ping_target(target, latest, history)

        return JSONResponse({
            "target": target,
            "latest": latest,
            "live": ping_live,
        })

    @app.get("/api/monitor/speedtest")
    async def api_speedtest_monitor(request: Request):
        """API endpoint to fetch fresh speedtest monitor data"""
        settings = settings_store.load()
        
        # Check for custom date range
        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        
        if from_param and to_param:
            since_iso = from_param
            until_iso = to_param
        else:
            range_key = request.query_params.get("range") or settings.dashboard_range_key
            range_option = resolve_range(range_key, settings, SPEEDTEST_RANGE_BY_KEY)
            since_iso = (datetime.now(timezone.utc) - range_option["delta"]).isoformat()
            until_iso = None

        latest_speedtest = db.latest_speedtest()
        speedtest_points = db.speedtest_history(since_iso, until_iso)
        speedtest_live = serialize_latest_speedtest(latest_speedtest)
        
        # Merge the serialized data with chart data
        return JSONResponse({
            **speedtest_live,
            "chart": speedtest_chart(speedtest_points),
        })

    @app.get("/api/monitor/mtr/{target_id}")
    async def api_mtr_monitor(request: Request, target_id: int):
        """API endpoint to fetch fresh MTR monitor data"""
        settings = settings_store.load()
        target = db.get_mtr_target(target_id)
        if not target:
            return JSONResponse({"error": "Target not found"}, status_code=404)

        # MTR doesn't need date range filtering - just get latest run
        latest_run = db.latest_mtr_run(target_id)
        hops = db.mtr_hops_for_run(int(latest_run["id"])) if latest_run else []
        mtr_live = serialize_mtr_target(target, latest_run, hops)

        return JSONResponse({
            "target": target,
            "latest_run": latest_run,
            "live": mtr_live,
        })

    return app
