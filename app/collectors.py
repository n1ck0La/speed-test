from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


PING_SAMPLE_RE = re.compile(r"time=([\d.]+)\s*ms")
PING_STATS_RE = re.compile(
    r"(?P<sent>\d+)\s+packets transmitted,\s+"
    r"(?P<received>\d+)\s+(?:packets )?received.*?"
    r"(?P<loss>[\d.]+)%\s+packet loss",
    re.DOTALL,
)
REPLY_RE = re.compile(r"bytes from ([^:\s]+)")
TTL_RE = re.compile(r"From ([^ ]+) icmp_seq=\d+ Time to live exceeded", re.IGNORECASE)
UNREACHABLE_RE = re.compile(r"From ([^ ]+) icmp_seq=\d+ .*Unreachable", re.IGNORECASE)
SERVER_LINE_RE = re.compile(
    r"^\s*(?P<id>\d+)\)\s+(?P<sponsor>.+?)\s+\((?P<city>.+?),\s+(?P<country>.+?)\)\s+\[(?P<distance>[^\]]+)\]$"
)

_SERVER_CACHE: dict[str, object] = {
    "expires_at": datetime.min.replace(tzinfo=timezone.utc),
    "servers": [],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_jitter(samples: list[float]) -> float | None:
    if len(samples) < 2:
        return 0.0 if samples else None
    deltas = [abs(curr - prev) for prev, curr in zip(samples, samples[1:])]
    return round(sum(deltas) / len(deltas), 3)


def _ping_binary() -> str:
    return shutil.which("ping") or "ping"


def _speedtest_binary() -> str:
    candidate = Path(sys.executable).with_name("speedtest-cli")
    if candidate.exists():
        return str(candidate)
    return shutil.which("speedtest-cli") or "speedtest-cli"


def list_speedtest_servers(limit: int = 25, ttl_minutes: int = 60) -> list[dict]:
    now = datetime.now(timezone.utc)
    expires_at = _SERVER_CACHE["expires_at"]
    if isinstance(expires_at, datetime) and now < expires_at:
        return list(_SERVER_CACHE["servers"])[:limit]

    try:
        completed = subprocess.run(
            [_speedtest_binary(), "--secure", "--list"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception:
        return []

    servers: list[dict] = []
    for line in completed.stdout.splitlines():
        match = SERVER_LINE_RE.match(line.strip())
        if not match:
            continue
        sponsor = match.group("sponsor").strip()
        city = match.group("city").strip()
        country = match.group("country").strip()
        server_id = match.group("id").strip()
        distance = match.group("distance").strip()
        servers.append(
            {
                "id": server_id,
                "label": f"{sponsor} - {city}, {country} [{distance}]",
                "sponsor": sponsor,
                "city": city,
                "country": country,
                "distance": distance,
            }
        )
        if len(servers) >= limit:
            break

    _SERVER_CACHE["servers"] = servers
    _SERVER_CACHE["expires_at"] = now + timedelta(minutes=ttl_minutes)
    return servers


def run_speedtest(server_id: str = "", timeout_seconds: int = 240) -> dict:
    command = [_speedtest_binary(), "--secure", "--json"]
    if server_id:
        command.extend(["--server", server_id])
    if timeout_seconds:
        command.extend(["--timeout", str(timeout_seconds)])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds + 30, 60),
            check=False,
        )
    except Exception as exc:
        return {
            "recorded_at": utcnow(),
            "success": False,
            "error": str(exc),
            "server_id": None,
            "server_name": None,
            "server_sponsor": None,
            "server_location": None,
            "download_bps": None,
            "upload_bps": None,
            "ping_ms": None,
            "bytes_sent": None,
            "bytes_received": None,
            "external_ip": None,
            "raw_json": None,
        }

    raw = (completed.stdout or completed.stderr or "").strip()
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "recorded_at": utcnow(),
            "success": False,
            "error": raw or f"speedtest-cli exited with code {completed.returncode}",
            "server_id": None,
            "server_name": None,
            "server_sponsor": None,
            "server_location": None,
            "download_bps": None,
            "upload_bps": None,
            "ping_ms": None,
            "bytes_sent": None,
            "bytes_received": None,
            "external_ip": None,
            "raw_json": raw,
        }

    server = payload.get("server", {})
    return {
        "recorded_at": utcnow(),
        "success": completed.returncode == 0,
        "error": None if completed.returncode == 0 else raw,
        "server_id": str(server.get("id") or ""),
        "server_name": server.get("name"),
        "server_sponsor": server.get("sponsor"),
        "server_location": ", ".join(
            [part for part in [server.get("name"), server.get("country")] if part]
        ),
        "download_bps": payload.get("download"),
        "upload_bps": payload.get("upload"),
        "ping_ms": payload.get("ping"),
        "bytes_sent": payload.get("bytes_sent"),
        "bytes_received": payload.get("bytes_received"),
        "external_ip": payload.get("client", {}).get("ip"),
        "raw_json": json.dumps(payload),
    }


def run_ping_check(host: str, probe_count: int, timeout_seconds: float) -> dict:
    timeout = max(1, int(math.ceil(timeout_seconds)))
    command = [
        _ping_binary(),
        "-n",
        "-c",
        str(max(1, probe_count)),
        "-i",
        "0.2",
        "-W",
        str(timeout),
        host,
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(timeout * probe_count + 10, 15),
            check=False,
        )
    except Exception as exc:
        return {
            "recorded_at": utcnow(),
            "packets_sent": max(1, probe_count),
            "packets_received": 0,
            "packet_loss": 100.0,
            "min_ms": None,
            "avg_ms": None,
            "max_ms": None,
            "jitter_ms": None,
            "success": False,
            "error": str(exc),
            "raw_output": None,
        }

    output = (completed.stdout or "") + (completed.stderr or "")
    samples = [float(match) for match in PING_SAMPLE_RE.findall(output)]
    stats = PING_STATS_RE.search(output)

    if stats:
        packets_sent = int(stats.group("sent"))
        packets_received = int(stats.group("received"))
        packet_loss = float(stats.group("loss"))
    else:
        packets_sent = max(1, probe_count)
        packets_received = len(samples)
        packet_loss = round((1 - packets_received / packets_sent) * 100, 2)

    return {
        "recorded_at": utcnow(),
        "packets_sent": packets_sent,
        "packets_received": packets_received,
        "packet_loss": packet_loss,
        "min_ms": round(min(samples), 3) if samples else None,
        "avg_ms": round(sum(samples) / len(samples), 3) if samples else None,
        "max_ms": round(max(samples), 3) if samples else None,
        "jitter_ms": compute_jitter(samples),
        "success": packets_received > 0,
        "error": None if packets_received > 0 else output.strip() or "No ping replies received",
        "raw_output": output.strip(),
    }


def _parse_probe_output(output: str) -> tuple[str | None, bool]:
    reply = REPLY_RE.search(output)
    if reply:
        return reply.group(1), True

    ttl = TTL_RE.search(output)
    if ttl:
        return ttl.group(1), False

    unreachable = UNREACHABLE_RE.search(output)
    if unreachable:
        return unreachable.group(1), False

    return None, False


def run_mtr_check(
    host: str,
    probe_count: int,
    timeout_seconds: float,
    max_hops: int,
) -> dict:
    timeout = max(1, int(math.ceil(timeout_seconds)))
    hops: list[dict] = []
    raw_sections: list[str] = []
    reached_target = False

    for ttl in range(1, max(1, max_hops) + 1):
        samples: list[float] = []
        addresses: Counter[str] = Counter()
        hop_reached_target = False

        for probe_number in range(max(1, probe_count)):
            command = [
                _ping_binary(),
                "-n",
                "-c",
                "1",
                "-W",
                str(timeout),
                "-t",
                str(ttl),
                host,
            ]
            started = time.perf_counter()
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            output = (completed.stdout or "") + (completed.stderr or "")
            raw_sections.append(f"hop {ttl} probe {probe_number + 1}\n{output.strip()}")

            address, target_hit = _parse_probe_output(output)
            if address:
                addresses[address] += 1
                samples.append(elapsed_ms)
            if target_hit:
                hop_reached_target = True

        packets_sent = max(1, probe_count)
        packets_received = len(samples)
        packet_loss = round((1 - packets_received / packets_sent) * 100, 2)
        hops.append(
            {
                "hop_index": ttl,
                "address": addresses.most_common(1)[0][0] if addresses else "*",
                "packets_sent": packets_sent,
                "packets_received": packets_received,
                "packet_loss": packet_loss,
                "min_ms": round(min(samples), 3) if samples else None,
                "avg_ms": round(sum(samples) / len(samples), 3) if samples else None,
                "max_ms": round(max(samples), 3) if samples else None,
                "jitter_ms": compute_jitter(samples),
                "reached_target": hop_reached_target,
            }
        )
        if hop_reached_target:
            reached_target = True
            break

    return {
        "recorded_at": utcnow(),
        "destination": host,
        "hop_count": len(hops),
        "success": reached_target,
        "error": None if reached_target else f"Target was not reached within {max_hops} hops",
        "raw_output": "\n\n".join(raw_sections),
        "hops": hops,
    }
