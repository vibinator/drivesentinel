# Development guide

## Running locally (demo mode)

No Docker or real disks are needed. This requires Python 3.10 or newer.

```sh
cd app
MOCK=1 DATA_DIR=./demo-data PORT=8090 python main.py
# optional: MOCK_RAID=degraded to simulate a failed RAID member
```

On Windows PowerShell:
`$env:MOCK=1; $env:DATA_DIR="demo-data"; python main.py`

On the first start the mock backfills 30 days of history: a scan every 3 hours and a benchmark
weekly. The simulated fleet covers:

| Device | Scenario |
|---|---|
| `/dev/sda` | Healthy WD Red |
| `/dev/sdb` | Seagate IronWolf with accelerating sector failures, a slowing benchmark, and a high AI probability |
| `/dev/sdc` | 6-year-old Toshiba with UDMA CRC errors (cable) |
| `/dev/sdd` | Samsung 870 EVO SATA SSD wearing out |
| `/dev/nvme0` | Samsung 980 PRO on the buggy 3B2QGXA7 firmware (known issue) |
| `/dev/sde` | WD Red identical to sda but 11°C hotter (peer outlier) |

Delete the data directory to start fresh.

## Running against real disks (Linux)

```sh
sudo apt install smartmontools      # 7.0+ for JSON output
cd app && sudo DATA_DIR=./data python main.py
```

## Code conventions

- Standard library only. Don't add pip dependencies; the image stays at about 60 MB and works offline.
- All smartctl JSON handling belongs in `smart.normalize()`. Everything downstream uses the
  normalized summary.
- Any change to the ML features must go in `features.py`, since both training and inference
  import it. `ml.py` refuses a model whose `features` list doesn't match.
- New scoring signals go in `analyzer.analyze()` via `add(p, level, text)`.
- New scheduled work goes in `Monitor.loop()` and must not block scans for long; use a thread.

## Retraining the model and rebuilding the datasets

```sh
cd app
# download the N most recent quarters automatically
python backblaze.py --quarters 2 --out ./out
# or reuse zips you already have
python backblaze.py --zips data_Q4_2025.zip data_Q1_2026.zip --out ./out
```

This writes `out/backblaze_stats.json` and `out/ml_model.json`. To ship them as the new
built-in defaults, copy them into `app/data/` and commit. On a desktop PC, two quarters take
about 7 minutes and a peak of a few hundred MB of RAM. Each zip is 1–2 GB, with roughly 12 GB
of CSV inside.

Options: `--neg-rate` (negative sampling rate, default 0.004), `--keep` (keep the downloaded
zips), `--cache` (download folder).

Data source: https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data. The zip
URL pattern is `https://f001.backblazeb2.com/file/Backblaze-Hard-Drive-Data/data_Q{n}_{year}.zip`.

## Adding a known issue

Add an entry to `app/data/known_issues.json` (see the [format](configuration.md#known_issuesjson-format)),
then check it matches:

```sh
cd app && python -c "import issues; print(issues.lookup({'model': 'Samsung SSD 980 PRO 2TB', 'firmware': '3B2QGXA7'}))"
```

## Testing checklist

There's no test framework; checks are done by hand:

1. `python -m py_compile app/*.py` and `node -e "new Function(require('fs').readFileSync('app/static/app.js','utf8'))"`
2. Start in mock mode and confirm `GET /api/status` has no `errors` and the log has no tracebacks.
3. In the UI, check the dashboard, a drive page, Events and Settings, in both themes and at phone width.
4. Repeat with `MOCK_RAID=degraded` to check the pool alerts and the hero headline.
5. For parser changes, feed `raid.parse()` or `smart.normalize()` captured real output
   (see `/api/diagnostics` and `/api/drives/{serial}/raw` on a live install).

## Releasing

1. Bump `VERSION` in `app/server.py`.
2. Commit and push to `main`. GitHub Actions builds and publishes
   `ghcr.io/vibinator/drivesentinel:latest` for amd64 and arm64, in about 5 minutes.
3. Optionally tag `vX.Y.Z` to publish a versioned image tag as well.
4. On the NAS, recreate the Container Station application to pull the new image.

## Changelog

- **1.2.0:** SATA disks that QNAP reports as SCSI are now read with SAT passthrough, giving full
  SMART attributes. Added ZFS pool health (QuTS hero), the `/api/diagnostics` endpoint, a WD
  white-label model family, and documentation.
- **1.1.0:** Backblaze importer and AI failure prediction, md-RAID pool monitoring, read-only
  benchmarks, known firmware issues, peer comparison.
- **1.0.0:** SMART monitoring, risk scoring and trend prediction, model reliability baselines,
  self-tests, alerts, dashboard, Prometheus metrics.
