# Architecture

## Runtime

A single Python 3.12 process (`app/main.py`) with no third-party packages:

| Thread | Started by | Job |
|---|---|---|
| Main | `server.serve()` | `ThreadingHTTPServer` on `$PORT`: UI, API and metrics |
| `monitor` | `Monitor.loop()` | Wakes every 30 s. Scans when the interval elapses or a scan is requested, starts due benchmarks, prunes old data daily. |
| Benchmark worker | `_bench_batch` | Runs due benchmarks one drive at a time |
| Backblaze job | `start_backblaze` | Download, analyze and train (on demand) |
| Alert senders | `Monitor.notify` | One short-lived thread per notification |

`smartctl` runs as a subprocess with JSON output (`-j`). Scans are serialized by a lock, and so
are benchmarks.

## Scan pipeline (`Monitor.scan`)

```
raid.read()  ─────────────────────────────► arrays + membership map
backend.scan_devices()                       (smartctl --scan-open + /dev/sd*, /dev/nvme* probing)
  for each device:
    backend.read_device()                    smartctl -j -a [-n standby] [-d type]
                                             └ retries: SCSI→SAT, then sat/scsi/sat,12 if unidentified
    smart.normalize()                        → protocol-independent summary (or sleeping/skip/error)
    dedupe by serial number
    _process():
      summary.raid  ← membership             summary.perf ← bench.evaluate(history)
      analyzer.analyze(summary, 30-day history)
        ├ reliability.lookup()   ├ ml.predict()   ├ known_issue_db.lookup()
      db.add_snapshot(metrics)   db.upsert_drive(summary, analysis, raw)
      events: new drive, status change, counter growth, self-test result, overheating
  _check_missing()   _check_raid()   _update_peers()   _check_selftests()
```

`backend` is `smart` normally, or `mock` when `MOCK=1`. The mock produces smartctl-shaped JSON,
mdstat text and benchmark results, so the same code paths run in both.

## Module map (`app/`)

| Module | Responsibility |
|---|---|
| `main.py` | Entry point. Wires config, DB, monitor and server; backfills 30 days of history in mock mode. |
| `config.py` | Settings: defaults, env vars, `/data/config.json`. Masks secrets. |
| `db.py` | SQLite access (WAL mode, a single connection behind a lock) |
| `smart.py` | smartctl wrapper, device discovery, normalization, `metrics_from()` snapshots |
| `analyzer.py` | Risk scoring, status, predictions, verdict ([health model](health-model.md)) |
| `reliability.py` | Model/family AFR lookup with the age multiplier; layered data files |
| `features.py` | Feature vector shared by training and inference |
| `ml.py` | Loads the model JSON and computes the probability and drivers |
| `backblaze.py` | Backblaze downloader, AFR builder, logistic-regression trainer (also a CLI) |
| `raid.py` | `/proc/mdstat` parser, ZFS kstat reader, membership, diagnostics |
| `bench.py` | O_DIRECT read benchmark and baseline evaluation |
| `issues.py` | Known-issue matching |
| `peers.py` | Fleet comparison |
| `monitor.py` | Scheduler, event/alert logic, self-test/benchmark scheduling, Backblaze job |
| `alerts.py` | Email, Discord, Slack, Telegram, ntfy and webhook senders |
| `server.py` | HTTP routing, JSON API, Prometheus output, static files, basic auth |
| `mock.py` | Simulated drives, pools and benchmarks for demo and testing |
| `static/` | Single-page UI (`index.html`, `app.js`, `style.css`), with hash routing and hand-written SVG charts |
| `data/` | Built-in datasets: `backblaze_stats.json`, `ml_model.json`, `model_stats.json`, `known_issues.json` |

## Database schema (`/data/drivesentinel.db`)

```sql
drives     (serial PK, model, device, dev_type, protocol, first_seen, last_seen,
            state,               -- online | standby | missing
            summary JSON,        -- normalized SMART data (+ raid, perf)
            analysis JSON,       -- latest analyzer output
            raw JSON)            -- latest raw smartctl output
snapshots  (id, serial, ts, metrics JSON)   -- one per drive per scan
benchmarks (id, serial, ts, result JSON)
events     (id, ts, serial, level, title, message)
kv         (k PK, v JSON)       -- scheduler and alert state
```

**Snapshot `metrics`** are kept compact (about 1 KB):
`{temp, poh, ok, health, risk, attrs: {"<id>": [value, worst, thresh, raw]}, nvme: {…}, scsi: {…}}`.

**`kv` keys:** `last_scan`, `selftest_schedule` (`serial:kind → ts`), `bench_last`,
`raid_state` (`array → status`), `peers` (latest reports), `peer_alerted`, `mock_t0`.

Retention: snapshots and events older than `retention_days` are deleted daily.

## Container image

`Dockerfile`: `python:3.12-alpine`, plus `smartmontools`, `tzdata`, and a best-effort
`update-smart-drivedb`. The app lives in `/app` and data in the `/data` volume. Port 8090 is
exposed, with a `HEALTHCHECK` on `/healthz`.

`.github/workflows/docker.yml` builds `linux/amd64` and `linux/arm64` with Buildx and QEMU, and
pushes to `ghcr.io/<owner>/drivesentinel` with the tags `latest`, the short SHA and semver
(on `v*` tags).

## Security notes

- The container is **privileged**, which it needs for SMART passthrough. Only run images you trust.
- The UI has no authentication unless `AUTH_USER` and `AUTH_PASS` are set. Keep port 8090 on
  your LAN, not exposed to the internet.
- Static file serving is path-normalized and confined to `static/`.
- Secrets are stored in plain text in `/data/config.json`, which the container can read; they
  are masked in API responses.
