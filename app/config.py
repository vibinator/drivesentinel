"""Configuration: defaults <- environment variables <- /data/config.json (UI-saved)."""
import json
import os
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    # General
    "scan_interval_min": 60,          # how often SMART data is collected
    "devices": "",                    # "" = auto-detect, or "/dev/sda:sat,/dev/nvme0"
    "skip_standby": True,             # do not wake sleeping disks
    "retention_days": 365,            # history kept in the database
    # Thresholds
    "hdd_temp_warn": 50,
    "hdd_temp_crit": 58,
    "ssd_temp_warn": 70,
    "ssd_temp_crit": 80,
    # Scheduled self-tests (days between runs, 0 = disabled)
    "short_test_interval_days": 7,
    "long_test_interval_days": 30,
    # Benchmarks (read-only)
    "bench_interval_days": 7,         # 0 = disabled
    "bench_seq_mb": 256,              # MB read at each of 3 positions
    # AI / datasets
    "ml_enabled": True,
    "backblaze_quarters": 2,          # quarters used when rebuilding from Backblaze data
    # Notifications
    "alert_min_level": "warning",     # info | warning | critical
    "temp_alert_cooldown_h": 6,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_security": "starttls",      # starttls | ssl | none
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "smtp_to": "",
    "discord_webhook": "",
    "slack_webhook": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "ntfy_url": "",                   # e.g. https://ntfy.sh/my-nas-alerts
    "ntfy_token": "",
    "webhook_url": "",                # generic JSON POST
}

SECRET_KEYS = {"smtp_pass", "telegram_token", "discord_webhook", "slack_webhook",
               "webhook_url", "ntfy_token"}
MASK = "********"


def _cast(key, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    return "" if value is None else str(value).strip()


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict(DEFAULTS)
        for key in DEFAULTS:
            env = os.environ.get(key.upper())
            if env is not None:
                self._values[key] = _cast(key, env)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for key, value in saved.items():
                    if key in DEFAULTS:
                        self._values[key] = _cast(key, value)
            except (OSError, ValueError):
                pass

    def get(self, key):
        with self._lock:
            return self._values[key]

    def __getitem__(self, key):
        return self.get(key)

    def public(self):
        """Settings for the UI, with secrets masked."""
        with self._lock:
            out = dict(self._values)
        for key in SECRET_KEYS:
            if out.get(key):
                out[key] = MASK
        return out

    def update(self, changes):
        with self._lock:
            for key, value in changes.items():
                if key not in DEFAULTS:
                    continue
                if key in SECRET_KEYS and value == MASK:
                    continue  # unchanged secret
                self._values[key] = _cast(key, value)
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._values, f, indent=2)
            os.replace(tmp, CONFIG_FILE)
