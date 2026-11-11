# Operations guide

## Installing on QNAP Container Station

1. In Container Station, go to **Applications → Create** and name it `drivesentinel`.
2. Paste [`docker-compose.qnap.yml`](../docker-compose.qnap.yml):

   ```yaml
   services:
     drivesentinel:
       image: ghcr.io/vibinator/drivesentinel:latest
       container_name: drivesentinel
       restart: unless-stopped
       privileged: true
       pid: host
       pull_policy: always
       ports:
         - "8090:8090"
       volumes:
         - /share/Container/drivesentinel/data:/data
         - /dev:/dev
         - /run/udev:/run/udev:ro
       environment:
         - TZ=Europe/London
   ```
3. Click **Create**, and confirm privileged mode if asked.
4. Open `http://<NAS-IP>:8090`. This is plain HTTP; QNAP's own admin page is the `https://` address.

**Why each option matters**

| Option | Reason |
|---|---|
| `privileged: true` | SMART and NVMe passthrough need raw device ioctls. Without it no drives are found. |
| `pid: host` | QuTS hero only: lets the app run the NAS's own `zpool status` (read-only) to see ZFS pools, their disks, rebuild progress and error counts. Not needed on QTS. |
| `pull_policy: always` | Checks for a newer image every time the application is recreated. |
| `/dev:/dev` | Exposes every disk, including hot-swapped ones, to `smartctl`. |
| `/run/udev` (read-only) | Helps `smartctl` identify devices. |
| `/data` volume | Holds the database, settings and imported datasets. Without it, history is lost on recreate. |
| `TZ` | Sets the timestamps in alerts and logs. |

### Other install methods

- **SSH + compose:** copy the repo to the NAS, then run `docker compose up -d --build`
  (uses [`docker-compose.yml`](../docker-compose.yml)).
- **Offline import:** `docker build` and `docker save` on a PC, then **Images → Import** in Container Station.

## Updating

Every push to `main` publishes a new `latest` image. To update:

1. In Container Station, open the application and choose **Recreate**, or pull the
   `ghcr.io/vibinator/drivesentinel:latest` image again and restart the container.
2. Data in `/data` is kept. The database schema is created and updated automatically on start.

Check the running version in the API (`GET /api/status` → `version`) or on the diagnostics endpoint.

## Backing up

Copy the `/data` folder. On QNAP it is `/share/Container/drivesentinel/data`, which is
`\\<NAS>\Container\drivesentinel\data` from Windows.

| File | Contents |
|---|---|
| `drivesentinel.db` (+ `-wal`, `-shm`) | Drives, SMART history, events, benchmarks, scheduler state |
| `config.json` | Settings saved from the UI |
| `backblaze_stats.json`, `ml_model.json` | Present only after an in-app Backblaze update |
| `model_stats.json`, `known_issues.json` | Your optional overrides |

To get a consistent copy of the database, stop the container first, or copy all three
`drivesentinel.db*` files together.

## Warm-up period: how long until results are accurate?

Some checks work from the first scan. Others need history to build up.

| Capability | Ready | Why |
|---|---|---|
| SMART pass/fail, attribute thresholds, error counters | First scan | Read directly from the drive |
| Make/model reliability baseline | First scan | Looked up from the dataset |
| AI 30-day failure probability | First scan | Uses the drive's current SMART values |
| Known firmware issues | First scan | Matched on model and firmware |
| Peer comparison | First scan, robust after **7 days** | Temperatures are compared as 7-day averages |
| "Actively deteriorating" detection | **7 days** | Compares counters across a 7-day window |
| Trend projections ("reaches threshold in ~N days") | Starts after **1 day** (≥3 scans), reliable after **2–4 weeks** | Linear regression over up to 30 days of history |
| Performance baseline | First benchmark ~1 h after discovery, baseline after **3 runs** (~3 weeks at the default weekly schedule) | The baseline is the median of the first 3 runs |
| SSD/NVMe wear projections | **Several weeks to months** | Wear counters move in whole 1% steps |

**In short:** it's useful from day one, fully calibrated after about a month, and should then be
left running permanently. Prediction improves as history accumulates, and a sudden change
against a long baseline is the strongest warning sign.

To speed up the performance baseline, run three benchmarks manually from each drive's page.

## Day-to-day use

- **Dashboard:** shows overall status, storage pools, and a card for every drive. Filter by
  *Needs attention*, HDD or SSD.
- **Drive page:** verdict, findings, predictions, AI probability, model reliability, peers,
  known issues, performance, history charts, self-tests, the SMART table and events.
- **Events:** the full log of everything that has been detected, filterable by level.
- **Scan now:** forces an immediate SMART collection. It runs on a schedule otherwise.
- **Forget drive:** removes a drive you've physically removed, along with its history. It is
  re-added if it's detected again.

## Notifications

Configure channels in **Settings → Notifications / Email / Push & chat**, then click
**Send test notification**. Alerts are sent when:

- a drive's status worsens (warning, critical, failing) or improves
- critical counters grow (reallocated, pending or uncorrectable sectors, NVMe media errors)
- a self-test fails or completes
- a drive overheats (limited by the configured cooldown)
- a drive goes missing or comes back
- a RAID pool degrades, rebuilds, recovers or disappears
- a benchmark shows a performance drop
- a drive becomes an outlier against its peers

Only events at or above **Minimum level to notify** are sent. Everything is still recorded in
the event log.

## Refreshing the reliability data and AI model

**Settings → AI & datasets → Update from Backblaze** downloads the newest Backblaze Drive Stats
quarters, recomputes failure rates per model and retrains the AI model, all on the NAS.

- Each quarter is a 1–2 GB download, which is deleted afterwards. Allow 10–60 minutes per
  quarter on NAS hardware.
- The results are written to `/data/backblaze_stats.json` and `/data/ml_model.json` and take
  precedence over the built-in files.
- To go back to the built-in data, delete those two files.

Backblaze publishes a new quarter roughly every three months, so updating twice a year is plenty.

## Troubleshooting

Start with **`http://<NAS-IP>:8090/api/diagnostics`**. It shows the smartctl version, what
`smartctl --scan-open` reports, the device list, the raw pool sources (`/proc/mdstat` and ZFS
kstat), skipped devices, scan errors, and how many SMART attributes each drive returned.

| Symptom | Likely cause and fix |
|---|---|
| "No devices found" banner | The container isn't privileged, or `/dev` isn't mounted. Check the YAML. |
| HDDs show 0 attributes, no temperature or age | The disk is being read as `-d scsi`. Since v1.2.0, DriveSentinel retries with `-d sat` automatically. If it persists, set **Settings → Devices** to e.g. `/dev/sda:sat,/dev/sdb:sat`. |
| A drive is listed under "skipped" | It has no SMART data (QNAP's boot flash/DOM, card readers). This is normal. |
| Drives show "Sleeping" | They are in standby and **Don't wake sleeping disks** is on. They update when they spin up. |
| Storage pools section is empty or only shows system arrays | QuTS hero uses ZFS, which a container can't see directly. Add `pid: host` to the YAML and recreate. Then check `pools.zpool` in `/api/diagnostics`: `method` shows how the NAS was queried, `error` why it failed, and `zpool_status` the raw output. |
| Benchmark failed | The drive is busy, or isn't a block device (e.g. behind a RAID controller). Check the drive's events for the error. |
| Backblaze update failed | Not enough free space in `/data`, or no internet access. The error is shown in Settings. |
| UI asks for a password | `AUTH_USER` and `AUTH_PASS` are set in the YAML. |

Container logs are under **Container Station → drivesentinel → Logs**. Every event is also logged there.
