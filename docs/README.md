# DriveSentinel documentation

DriveSentinel is a self-hosted drive health monitor and failure predictor for QNAP NAS devices
(and any Docker host). It runs as a single container, reads SMART data with `smartctl`, keeps
its history in SQLite, and serves a web dashboard on port 8090.

| Document | What it covers |
|---|---|
| [Operations guide](operations.md) | Installing, updating, backing up, the warm-up period, troubleshooting |
| [Configuration reference](configuration.md) | Every setting, its default, its environment variable, and the data/override files |
| [How health is assessed](health-model.md) | Risk scoring, the AI model, make/model reliability, trends, benchmarks, peers, RAID, known issues |
| [Architecture](architecture.md) | Components, threads, data flow, module map, database schema |
| [API reference](api.md) | Every HTTP endpoint, with request and response shapes |
| [Development guide](development.md) | Running locally, demo mode, retraining the model, releasing a new version |

## At a glance

```
┌──────────────── QNAP NAS ─────────────────────────────────────────────┐
│  Container "drivesentinel" (privileged, /dev mounted)                 │
│                                                                       │
│   smartctl ──► smart.py ──► analyzer.py ──► SQLite (/data)            │
│   /proc/mdstat, ZFS kstat ──► raid.py ─┘    ▲                         │
│   O_DIRECT reads ──► bench.py ─────────┘     │                        │
│                                              │                        │
│   monitor.py (scheduler: scans, self-tests, benchmarks, alerts)       │
│   server.py  (HTTP API + web UI on :8090, Prometheus /metrics)        │
└───────────────────────────────────────────────────────────────────────┘
        │ alerts: email / Discord / Slack / Telegram / ntfy / webhook
        ▼
   Browser  http://<NAS-IP>:8090
```

## Key facts

- **Image:** `ghcr.io/vibinator/drivesentinel:latest` (amd64 + arm64). It is rebuilt by GitHub
  Actions on every push to `main`.
- **Data:** everything persistent lives in the `/data` volume: database, settings and imported
  datasets.
- **Read-only to disks:** SMART queries, self-tests (run by the drive's own firmware) and
  benchmarks only *read*. DriveSentinel never writes to a disk.
- **No dependencies** beyond the Python 3.12 standard library and `smartmontools`.
