# API reference

The base URL is `http://<NAS-IP>:8090`. All bodies are JSON. If `AUTH_USER` is set, every
request needs HTTP basic auth.

Every `POST` must send `Content-Type: application/json` (an empty `{}` body is fine), or it
is rejected with `415`. This stops other websites from driving the API through your browser.
Unexpected server errors return `500 {"error": "..."}`.

## Status and diagnostics

### `GET /api/status`
An overview for the UI header and dashboard.
```json
{
  "version": "1.2.0", "mock": false, "scanning": false,
  "last_scan": 1794363286, "last_scan_duration": 4.2, "next_scan": 1794366886,
  "errors": [], "skipped": ["/dev/sdi: No SMART data / serial number"],
  "counts": {"good": 9, "warning": 1}, "drive_count": 10,
  "channels": ["email", "ntfy"], "now": 1794363295,
  "ml": {"trained_on": "...", "auc": 0.8777, "positives": 47263, "custom": false, ...},
  "dataset": {"as_of": "2025-10-01 to 2026-03-31", "models": 43, "custom": false},
  "benching": null,
  "backblaze": {"state": "idle"},
  "arrays": [{"name": "md1", "status": "healthy", "role": "data"}]
}
```

### `GET /api/diagnostics`
Raw troubleshooting data: `smartctl_version`, `smartctl_scan` (the `--scan-open` JSON),
`devices` (what will be scanned), `pools` (`mdstat` text and the ZFS kstat listing),
`skipped`, `errors`, and `drives` (device, dev_type, protocol, state, attribute count).

### `GET /healthz`
Returns `{"ok": true}`. Used by the container healthcheck.

### `GET /metrics`
Prometheus text format. Per drive (labels `serial`, `model`, `device`):
`drivesentinel_health_percent`, `drivesentinel_failure_risk_percent`,
`drivesentinel_temperature_celsius`, `drivesentinel_power_on_hours`,
`drivesentinel_status` (0 good, 1 warning, 2 critical, 3 failing),
`drivesentinel_performance_score_percent`, `drivesentinel_failure_probability_30d`, and
`drivesentinel_smart_attribute_raw{id,name}`.

## Drives

### `GET /api/drives`
A list of drive cards:
```json
[{"serial": "ZA1...", "model": "ST8000DM004-2CX188", "family": "...", "device": "/dev/sda",
  "protocol": "ATA", "state": "online", "capacity": 8001563222016, "is_ssd": false,
  "temperature": 34, "power_on_hours": 20000, "health": 97, "risk": 3.1, "status": "good",
  "verdict": "...", "top_issue": "...", "issue_count": 2, "eta_days": null, "life_left": null,
  "last_seen": 1794363286, "selftest_running": false, "selftest_remaining": null,
  "perf_score": 100, "ml_p": 0.0005, "known_issues": 1, "arrays": ["md1"]}]
```

### `GET /api/drives/{serial}`
Full detail: `card`, `summary` (normalized SMART data, including `attributes`, `nvme`,
`scsi`, `selftest`, `raid` and `perf`), `analysis` (`health`, `risk`, `status`, `issues[]`,
`predictions[]`, `eta_days`, `verdict`, `reliability`, `ml`, `known_issues[]`, `perf`, `raid[]`),
`first_seen`, `events[]` (last 50), `peers` (`group`, `insights[]`, `peers[]`), `benchmarks[]`,
`bench_eval` and `benching`.

### `GET /api/drives/{serial}/history?days=30`
Column arrays for charts (1–3650 days):
`{"ts": [...], "temp": [...], "health": [...], "realloc": [...], "pending": [...], "uncorrectable": [...], "wear": [...]}`.
`realloc` falls back to SAS grown defects, `uncorrectable` to NVMe media errors, and `wear` is
the percentage used.

### `GET /api/drives/{serial}/raw`
The latest unmodified `smartctl -j -a` output.

### `POST /api/drives/{serial}/selftest`
Body: `{"type": "short" | "long" | "conveyance" | "abort"}`. Returns `{"ok": true, "message": "..."}`.

### `POST /api/drives/{serial}/benchmark`
Starts a read-only benchmark in the background. Returns `202 {"ok": true, "message": "..."}`,
or `409` if one is already running.

### `POST /api/drives/{serial}/label`
Body: `{"label": "Bay 3"}` (up to 60 characters; empty clears it). The label appears on the
drive's card.

### `POST /api/drives/{serial}/forget`
Deletes the drive, its snapshots and its benchmarks.

## Pools

### `GET /api/arrays`
`{"arrays": [...], "drives": {"sda": {"serial", "model", "status", "state"}}}`. Each array has
`name`, `level`, `role` (`data`, `system` or `swap`), `status`, `state`, `members[]`
(`part`, `disk`, `slot`, `failed`, `spare`), `size_bytes`, `slots`, `active`, `map`, `missing`,
`failed_members`, `redundancy`, `redundancy_left`, `sync`, `sync_pct`, `sync_eta_min`,
`sync_speed`, and `zfs` (true for ZFS pools).

## Actions and settings

| Method | Path | Body | Result |
|---|---|---|---|
| `POST` | `/api/scan` | – | `202`, or `409` if a scan is already running |
| `GET` | `/api/events?limit=300&serial=…` | – | Newest first: `{id, ts, serial, level, title, message}` |
| `GET` | `/api/settings` | – | All settings, secrets masked |
| `POST` | `/api/settings` | Partial `{key: value}` | Saves the changes, triggers a scan, returns the new settings |
| `POST` | `/api/alerts/test` | – | `{"results": {"email": "ok", "ntfy": "error: ..."}}` |
| `GET` | `/api/backblaze` | – | Job state: `state` (idle, running, done or error), `step`, `progress`, `log[]`, `result`, `error` |
| `POST` | `/api/backblaze` | `{"quarters": 2}` | `202` (started), or `409` if already running |

## Generic webhook payload

```json
{"level": "critical", "title": "RAID md1 (RAID5) is DEGRADED", "message": "...", "serial": null}
```
