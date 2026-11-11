# Configuration reference

Settings are resolved in this order. Later sources win.

1. Built-in defaults (`app/config.py` → `DEFAULTS`)
2. Environment variables: the setting name in upper case, e.g. `SCAN_INTERVAL_MIN=30`
3. `/data/config.json`, written by the **Settings** page

Secrets (`smtp_pass`, tokens, webhook URLs) are masked as `********` when the UI reads them.
Saving with the mask unchanged keeps the stored value.

## Settings

### Monitoring
| Key | Default | Description |
|---|---|---|
| `scan_interval_min` | `60` | Minutes between SMART collections (minimum 5) |
| `devices` | `""` | Empty = auto-detect. Otherwise a comma-separated list of `device[:type]`, e.g. `/dev/sda:sat,/dev/nvme0` |
| `skip_standby` | `true` | Don't wake disks in standby (`smartctl -n standby`) |
| `retention_days` | `365` | How long snapshots and events are kept |

### Thresholds
| Key | Default | Description |
|---|---|---|
| `hdd_temp_warn` / `hdd_temp_crit` | `50` / `58` | HDD temperature limits (°C) |
| `ssd_temp_warn` / `ssd_temp_crit` | `70` / `80` | SSD/NVMe temperature limits (°C) |

### Scheduled self-tests
| Key | Default | Description |
|---|---|---|
| `short_test_interval_days` | `7` | Days between short self-tests (0 = off) |
| `long_test_interval_days` | `30` | Days between extended self-tests (0 = off). An extended test also resets the short-test timer. |

The first run of each test type is staggered to about 6 hours after a drive is discovered.

### Benchmarks
| Key | Default | Description |
|---|---|---|
| `bench_interval_days` | `7` | Days between read-only benchmarks (0 = off). Skipped while a RAID rebuild or sync is running. |
| `bench_seq_mb` | `256` | MB read at each of the three surface positions |

### AI & datasets
| Key | Default | Description |
|---|---|---|
| `ml_enabled` | `true` | Use the trained failure-prediction model |
| `backblaze_quarters` | `2` | Quarters to download for **Update from Backblaze** (1–8) |

### Notifications
| Key | Default | Description |
|---|---|---|
| `alert_min_level` | `warning` | `info`, `warning` or `critical` |
| `temp_alert_cooldown_h` | `6` | Minimum hours between overheat alerts for the same drive |
| `smtp_host`, `smtp_port`, `smtp_security` | `""`, `587`, `starttls` | `smtp_security`: `starttls`, `ssl` or `none` |
| `smtp_user`, `smtp_pass`, `smtp_from`, `smtp_to` | `""` | `smtp_to` may be comma-separated |
| `discord_webhook`, `slack_webhook` | `""` | Incoming webhook URLs |
| `telegram_token`, `telegram_chat_id` | `""` | Bot token and chat ID |
| `ntfy_url`, `ntfy_token` | `""` | Full topic URL (e.g. `https://ntfy.sh/my-nas`), plus an optional access token |
| `webhook_url` | `""` | Receives JSON `{"level","title","message","serial"}` |

## Environment-only variables

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/data` | Location of the database and data files |
| `PORT` | `8090` | HTTP port |
| `TZ` | system | Timezone, e.g. `Europe/London` |
| `AUTH_USER`, `AUTH_PASS` | unset | Enables HTTP basic auth for the UI and API |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `MOCK` | unset | `1` = demo mode with simulated drives |
| `MOCK_RAID` | unset | `degraded` = simulate a degraded RAID 5 in demo mode |
| `MDSTAT_PATH` | `/proc/mdstat` | md-RAID source (for testing) |
| `ZFS_KSTAT_PATH` | `/proc/spl/kstat/zfs` | ZFS pool-state source (for testing) |

## Data files and overrides

Built-in files ship in the image under `app/data/`. You can override or extend them by placing
files in `/data`:

| Built-in file | Override in `/data` | Merge behaviour |
|---|---|---|
| `backblaze_stats.json`: failure rate per model, computed from Backblaze | `backblaze_stats.json` | Replaces the built-in file (written by the in-app updater) |
| `ml_model.json`: trained logistic-regression model | `ml_model.json` | Replaces the built-in file (written by the in-app updater) |
| `model_stats.json`: curated models, families, defaults, age curve | `model_stats.json` | Your `models` and `families` take priority. `defaults` are merged. `age_multipliers` are replaced. |
| `known_issues.json`: firmware bugs and caveats | `known_issues.json` | Your entries are added. An entry with the same `id` overrides the built-in one. |

All files are re-read automatically when they change. No restart is needed.

**Model lookup priority:** `/data/model_stats.json` models → Backblaze stats → curated models →
families (yours, then built-in) → defaults by drive type.

### `model_stats.json` format
```json
{
  "models":   [{"match": "^WDC WD120EFBX", "name": "WD Red Plus 12TB", "afr": 0.9, "source": "My data"}],
  "families": [{"match": "IronWolf", "name": "Seagate IronWolf", "afr": 1.2, "source": "Estimate"}],
  "defaults": {"hdd": {"afr": 1.5, "source": "..."}, "ssd": {"afr": 0.9, "source": "..."}},
  "age_multipliers": [{"max_years": 1, "mult": 1.4, "note": "infant mortality"}]
}
```
`match` is a case-insensitive regular expression tested against the drive's model name, with
whitespace collapsed to single spaces. `afr` is the annualized failure rate in percent.

### `known_issues.json` format
```json
{
  "issues": [{
    "id": "unique-id",
    "match": "^Samsung SSD 980 PRO",
    "firmware": ["3B2QGXA7"],
    "severity": "critical",
    "title": "Short title",
    "detail": "What it means and what to do",
    "fix": "5B2QGXA7"
  }]
}
```
`firmware` is optional; without it the issue applies to every firmware version. `severity` is
`critical`, `warning` or `info`. Critical and warning issues affect the risk score; info issues
are shown only in the Known issues panel.
