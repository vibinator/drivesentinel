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

## Testing

```sh
python -m unittest discover -s tests        # ~10 seconds, standard library only
```

`tests/test_all.py` covers SMART normalization, the scoring rules, reliability and
known-issue matching, the AI model, the RAID and ZFS parsers, benchmark evaluation, the
schedulers, and the whole app end to end over HTTP in demo mode. GitHub Actions runs it before
every image build, and a failing test blocks the build.

When you fix a bug, add a test that fails without the fix. For parser changes, feed
`raid.parse()` or `smart.normalize()` captured real output (see `/api/diagnostics` and
`/api/drives/{serial}/raw` on a live install).

Still checked by hand: the UI (dashboard, a drive page, Events and Settings, in both themes
and at phone width), and `MOCK_RAID=degraded` for the pool alerts.

## Releasing

1. Bump `VERSION` in `app/server.py`.
2. Commit and push to `main`. GitHub Actions builds and publishes
   `ghcr.io/vibinator/drivesentinel:latest` for amd64 and arm64, in about 5 minutes.
3. Optionally tag `vX.Y.Z` to publish a versioned image tag as well.
4. On the NAS, recreate the Container Station application to pull the new image.

## Changelog

- **1.4.0:** ZFS pool monitoring for QuTS hero through the NAS's `zpool` tool (needs
  `pid: host`): pool health, member disks, resilver/scrub progress, and per-disk ZFS error
  counts feeding the risk score. Compose file now pulls the newest image on recreate.

- **1.3.0:** Extended self-tests now run one drive at a time (they used to start on every
  drive at once) and respect a new maintenance window. Fixed false failure predictions from
  noisy attributes, from 1% SSD wear steps and from past overheating. Benchmarks confirm a
  slow result before alerting. Repeat counter alerts are rate-limited. The API rejects
  cross-site POSTs. Added workload and head-load-cycle tracking, drive labels, a "limited
  data" flag, history thinning, more drive families, a test suite, and a built-in dataset
  and model built from four quarters of Backblaze data.

- **1.2.0:** SATA disks that QNAP reports as SCSI are now read with SAT passthrough, giving full
  SMART attributes. Added ZFS pool health (QuTS hero), the `/api/diagnostics` endpoint, a WD
  white-label model family, and documentation.
- **1.1.0:** Backblaze importer and AI failure prediction, md-RAID pool monitoring, read-only
  benchmarks, known firmware issues, peer comparison.
- **1.0.0:** SMART monitoring, risk scoring and trend prediction, model reliability baselines,
  self-tests, alerts, dashboard, Prometheus metrics.
