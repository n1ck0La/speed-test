from __future__ import annotations

import json
import math
import platform
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
    r"(?:(?P<sent>\d+)\s+packets transmitted,\s+"
    r"(?P<received>\d+)\s+(?:packets )?received.*?"
    r"(?P<loss>[\d.]+)%\s+packet loss)|"
    r"(?:Packets:\s+Sent\s+=\s+(?P<sent_win>\d+),\s+Received\s+=\s+(?P<received_win>\d+),\s+Lost\s+=\s+(?P<lost_win>\d+))",
    re.DOTALL | re.IGNORECASE,
)
REPLY_RE = re.compile(r"(?:bytes from ([^:\s]+))|(?:Reply from ([^:\s]+))")
TTL_RE = re.compile(r"(?:From ([^ ]+) icmp_seq=\d+ Time to live exceeded)|(?:Reply from ([^:\s]+): TTL expired in transit)", re.IGNORECASE)
UNREACHABLE_RE = re.compile(r"(?:From ([^ ]+) icmp_seq=\d+ .*Unreachable)|(?:Reply from ([^:\s]+): Destination .* unreachable)", re.IGNORECASE)
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


def run_speedtest(
    server_id: str = "",
    timeout_seconds: int = 240,
    use_secure: bool = False,
) -> dict:
    command = [_speedtest_binary(), "--json"]
    if use_secure:
        command.append("--secure")
    if server_id:
        command.extend(["--server", server_id])
    if timeout_seconds:
        command.extend(["--timeout", str(timeout_seconds)])
    command.append("--no-pre-allocate")

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
    import platform
    timeout = max(1, int(math.ceil(timeout_seconds)))
    
    if platform.system() == "Windows":
        # Windows ping syntax
        command = [
            _ping_binary(),
            "-n", str(max(1, probe_count)),  # count
            "-w", str(timeout * 1000),       # timeout in milliseconds
            host,
        ]
    else:
        # Unix ping syntax
        command = [
            _ping_binary(),
            "-c", str(max(1, probe_count)),  # count
            "-i", "0.2",                     # interval
            "-W", str(timeout),              # timeout in seconds
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
        if stats.group("sent"):  # Unix format
            packets_sent = int(stats.group("sent"))
            packets_received = int(stats.group("received"))
            packet_loss = float(stats.group("loss"))
        else:  # Windows format
            packets_sent = int(stats.group("sent_win"))
            packets_received = int(stats.group("received_win"))
            lost = int(stats.group("lost_win"))
            packet_loss = round((lost / packets_sent) * 100, 2) if packets_sent > 0 else 100.0
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
        # REPLY_RE now has two groups: Unix format (group 1) and Windows format (group 2)
        address = reply.group(1) or reply.group(2)
        return address, True

    ttl = TTL_RE.search(output)
    if ttl:
        # TTL_RE now has two groups: Unix format (group 1) and Windows format (group 2)
        address = ttl.group(1) or ttl.group(2)
        return address, False

    unreachable = UNREACHABLE_RE.search(output)
    if unreachable:
        # UNREACHABLE_RE now has two groups: Unix format (group 1) and Windows format (group 2)
        address = unreachable.group(1) or unreachable.group(2)
        return address, False

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
            if platform.system() == "Windows":
                # Windows ping syntax
                command = [
                    _ping_binary(),
                    "-n", "1",                    # count
                    "-w", str(timeout * 1000),   # timeout in milliseconds
                    "-i", str(ttl),              # TTL
                    host,
                ]
            else:
                # Unix ping syntax
                command = [
                    _ping_binary(),
                    "-c", "1",                   # count
                    "-W", str(timeout),          # timeout in seconds
                    "-t", str(ttl),              # TTL
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
