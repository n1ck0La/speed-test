# Speedtest Monitor

Local network monitoring app with a web interface for:

- scheduled internet speed tests against public Speedtest.net-compatible servers
- continuous ping checks for user-defined hosts
- MTR-style hop-by-hop path measurements
- latency, jitter, and packet loss tracking
- configurable retention and rotating application logs

## Stack

- Python 3.12
- FastAPI
- SQLite
- APScheduler
- `speedtest-cli`
- Linux `ping`

## Notes

- Speed tests use `speedtest-cli --secure --json`, which selects the best public server by default or a configured server ID.
- Because this WSL environment does not allow raw ICMP sockets or package installs without sudo, the MTR feature is implemented with repeated TTL-limited `ping` probes. It gives per-hop latency and loss data without requiring root.
- Historical measurement data is stored in SQLite and purged according to the configured retention period. Runtime logs are rotated with a configurable size and backup count.

## Run

```bash
cd /home/nick/speedtest
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open [http://localhost:8000](http://localhost:8000).

## Data paths

- SQLite DB: `data/monitor.db`
- Settings: `data/settings.json`
- Logs: `logs/app.log`

## Maintenance

Reset stored measurement data, counters, pins, and runtime logs:

```bash
cd /home/nick/speedtest
.venv/bin/python scripts/reset_runtime_state.py
```

## Autostart

Install and enable the user-level `systemd` service:

```bash
cd /home/nick/speedtest
./scripts/install_user_service.sh
```

Check service status:

```bash
systemctl --user status speedtest-monitor.service
```
