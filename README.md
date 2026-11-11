# DriveSentinel

A self-hosted drive health monitor for QNAP Container Station (or any Docker host), in the
spirit of QNAP's DA Drive Analyzer. It runs on your NAS, needs no cloud account, and has no
Python dependencies beyond the standard library plus `smartmontools`.

**Full documentation:** [docs/](docs/README.md): [operations](docs/operations.md), [configuration](docs/configuration.md), [health model](docs/health-model.md), [architecture](docs/architecture.md), [API](docs/api.md), [development](docs/development.md).

## Features

- **Automatic discovery** of every HDD, SATA SSD, NVMe and SAS drive. It uses `smartctl --scan-open`
  plus a fallback that probes every `/dev/sd*` and `/dev/nvme*`, retrying with SAT/SCSI passthrough.
  If the same disk shows up under several device paths, it is collapsed by serial number.
- **SMART data collection** on a schedule: ATA attributes, the NVMe health log, SAS error counters,
  the self-test log and the error log. Sleeping disks are not woken up.
- **Failure prediction**
  - A risk model weights the SMART indicators that large-fleet studies (Backblaze, Google) correlate
    with failure: reallocated, pending and uncorrectable sectors, end-to-end errors, spin retries,
    command timeouts, NVMe critical warnings and media errors, SAS grown defects.
  - **Trend analysis**: linear regression over the stored history detects deterioration and
    projects when attributes, SSD endurance or NVMe wear will cross their failure thresholds.
  - **Make/model history**: a baseline annualized failure rate per drive model, computed from
    Backblaze's public Drive Stats, falling back to product family and then drive type. It is
    adjusted for age along the "bathtub curve" (infant mortality, useful life, wear-out).
  - **AI failure prediction**: a logistic-regression model trained on Backblaze's daily SMART
    records gives a calibrated *probability of failure within 30 days* for each hard drive, and
    shows which factors drive the estimate. The built-in model was trained on a year of data
    (Oct 2025 – Sep 2026: 382k drives, 127M drive-days), with a held-out AUC of 0.88.
- **One-click Backblaze update**: in Settings, *Update from Backblaze* downloads the latest
  quarters, recomputes failure rates per model and retrains the model on your NAS. You can also
  run it by hand: `python backblaze.py --quarters 2 --out /data`.
- **RAID / storage pool monitoring**: reads QNAP's md-RAID arrays from `/proc/mdstat` (QTS)
  and ZFS pools through the NAS's `zpool` tool (QuTS hero, needs `pid: host`). It tracks
  degraded, rebuilding and resyncing states, rebuild progress and ETA, and the redundancy left.
  A drive in an array with no redundancy left is escalated, and the pool's member disks link to
  their drive pages.
- **Read-only performance benchmarks**: measures sequential throughput at the outer, middle and
  inner disk positions, plus random 4K latency and IOPS. Runs on a schedule (weekly by default)
  and is skipped during RAID rebuilds. Each result is compared with the drive's own baseline to
  catch slowdowns, which often come before SMART errors.
- **Known firmware bugs and caveats**: flags known issues such as the Samsung 980/990 PRO health
  bugs, the IronWolf 10TB SC60 firmware and the Crucial M4 5184-hour bug. It also flags SMR
  drives in RAID (WD Red EFAX, BarraCuda, P300) and desktop-class drives. Extend the list with
  `/data/known_issues.json`.
- **Peer comparison**: compares each drive with identical drives (or the same drive type) in your
  NAS. It flags drives that run hotter (bay airflow), are the only ones with sector errors, read
  slower than their twins, or are the oldest.
- **Dashboard** in light or dark mode: fleet overview, per-drive health and risk, findings,
  predictions, history charts (health, temperature, error counters, wear), the full SMART table
  and the self-test log.
- **Self-tests**: run short or extended tests from the UI, or schedule them automatically
  (short weekly and long monthly by default).
- **Alerts** for status changes, growing error counters, failed self-tests, overheating and
  missing drives. Channels: Email (SMTP), Discord, Slack, Telegram, ntfy and a generic JSON webhook.
- **Prometheus endpoint** at `/metrics`, for Grafana.
- Optional HTTP basic auth.

## Install on QNAP Container Station

### Easiest: paste YAML into Container Station

A pre-built image for Intel/AMD and ARM QNAPs is published at `ghcr.io/vibinator/drivesentinel`.

1. In Container Station, go to **Applications → Create** and name it `drivesentinel`.
2. Paste the contents of [`docker-compose.qnap.yml`](docker-compose.qnap.yml) and click **Create**.
3. Open `http://NAS_IP:8090`.

To update, recreate the application, or pull the `latest` image and restart the container.
Your data stays in `/share/Container/drivesentinel/data`.

### Option A: build on the NAS with SSH

1. Enable SSH: **Control Panel → Network & File Services → Telnet/SSH**.
2. Copy this folder to the NAS, for example to `/share/Container/drivesentinel`. You can use
   File Station, or run `scp -r DADrive admin@NAS_IP:/share/Container/drivesentinel` from your PC.
3. SSH in and start the container:
   ```sh
   cd /share/Container/drivesentinel
   docker compose up -d --build      # older firmware: docker-compose up -d --build
   ```
4. Open `http://NAS_IP:8090`.

When the container exists, it also appears in Container Station, where you can start, stop and view logs.

### Option B: build on a PC, then import

```sh
docker build -t drivesentinel:latest .
docker save drivesentinel:latest -o drivesentinel.tar
```
In Container Station, go to **Images → Import** and select `drivesentinel.tar`. Then go to
**Applications → Create**, paste `docker-compose.yml`, delete the `build: .` line, and change
`./data` to an absolute path such as `/share/Container/drivesentinel/data`.

### Required container options

| Setting | Why |
|---|---|
| `privileged: true` | SMART passthrough ioctls need raw device access |
| `/dev:/dev` | Lets the container see every disk, including hot-swapped ones |
| `/data` volume | Database, settings and the optional model-stats override |

### Demo mode

To preview the UI with simulated drives (including one that is failing), set `MOCK=1`. To also
simulate a degraded RAID 5, add `MOCK_RAID=degraded`. You can
also run it locally without Docker: `cd app && MOCK=1 DATA_DIR=./demo-data python main.py`.

## Configuration

Everything can be changed in the **Settings** page, which saves to `/data/config.json`. You can
also preset any setting with an upper-case environment variable, such as `SCAN_INTERVAL_MIN=30`,
`NTFY_URL=…` or `SMTP_HOST=…`.

Other environment variables: `AUTH_USER` / `AUTH_PASS` (basic auth), `PORT` (default 8090),
`TZ`, `MOCK`.

### Data files

| File | Purpose | Override in `/data` |
|---|---|---|
| `app/data/backblaze_stats.json` | Failure rates per model, computed from Backblaze | written by *Update from Backblaze* |
| `app/data/ml_model.json` | Trained failure-prediction model | written by *Update from Backblaze* |
| `app/data/model_stats.json` | Curated fallbacks: families, NAS drives, age curve | `model_stats.json` (merged, takes priority) |
| `app/data/known_issues.json` | Firmware bugs and model caveats | `known_issues.json` (added to the built-in list) |

To add drives or adjust failure rates by hand, create `/data/model_stats.json`:

```json
{
  "models": [
    {"match": "^WDC WD120EFBX", "name": "WD Red Plus 12TB", "afr": 0.9, "source": "My data"}
  ]
}
```

## How health is calculated

Each indicator contributes an independent failure probability *pᵢ*. Overall risk is
`1 − Π(1 − pᵢ)` and health is `100 − risk`.

| Status | Meaning |
|---|---|
| **Good** | Risk under 15% and no critical findings |
| **Warning** | Early signs, such as a few reallocated sectors, high temperature or an old drive |
| **Critical** | Risk of 50% or more, active deterioration, or a projected threshold crossing within ~90 days |
| **Failing** | SMART reports FAILED, an attribute is below its threshold, or NVMe is read-only or degraded |

Treat these as estimates. They are not guarantees: drives can fail without warning. Keep backups.

## API

`GET /api/drives`, `GET /api/drives/{serial}`, `GET /api/drives/{serial}/history?days=30`,
`GET /api/drives/{serial}/raw`, `POST /api/drives/{serial}/selftest {"type":"short|long|abort"}`,
`POST /api/drives/{serial}/benchmark`, `GET /api/arrays`, `GET|POST /api/backblaze {"quarters":2}`,
`POST /api/scan`, `GET /api/events`, `GET|POST /api/settings`, `POST /api/alerts/test`,
`GET /metrics`, `GET /healthz`.
