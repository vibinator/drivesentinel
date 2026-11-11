"""Feature extraction shared by the Backblaze trainer and live inference.

Both sides must produce identical vectors, so all transforms live here.
"""
import math
import re

# SMART attribute ids used by the model (Backblaze column smart_<id>_raw)
ATTRS = (5, 187, 188, 197, 198, 10, 184, 199, 9, 194)

FEATURES = [
    "log_realloc",        # 5
    "log_reported_unc",   # 187
    "log_cmd_timeout",    # 188
    "log_pending",        # 197
    "log_offline_unc",    # 198
    "log_spin_retry",     # 10
    "log_e2e",            # 184
    "log_crc",            # 199
    "any_sector_error",   # 5/187/197/198 > 0
    "age_years",          # 9
    "age_sq",
    "temp",               # 194
    "log_model_afr",      # make/model historical failure rate
]

LABELS = {
    "log_realloc": "Reallocated sectors", "log_reported_unc": "Reported uncorrectable errors",
    "log_cmd_timeout": "Command timeouts", "log_pending": "Pending sectors",
    "log_offline_unc": "Offline uncorrectable sectors", "log_spin_retry": "Spin retries",
    "log_e2e": "End-to-end errors", "log_crc": "Interface CRC errors",
    "any_sector_error": "Presence of sector errors", "age_years": "Drive age", "age_sq": "Drive age",
    "temp": "Temperature", "log_model_afr": "Model's historical failure rate",
}

SSD_MODEL_RE = re.compile(r"SSD|^CT\d|^ZA\d|^WDS|^MTFD|^Micron|DELLBOSS|^SSDSC|^MZ|^SAMSUNG MZ|NVMe", re.I)


def is_ssd_model(model):
    return bool(SSD_MODEL_RE.search(model or ""))


def _l(x):
    return math.log1p(max(0.0, float(x or 0)))


def vector(raw, model_afr):
    """raw: {attr_id: raw value or None}; model_afr: annualized failure rate in %."""
    g = lambda k: raw.get(k) or 0   # noqa: E731 - missing attribute == 0
    timeouts = g(188)
    if timeouts > 0xFFFF:            # Seagate packs three 16-bit counters
        timeouts &= 0xFFFF
    sector = g(5) + g(187) + g(197) + g(198)
    age = min(15.0, max(0.0, g(9) / 8766.0))
    temp = raw.get(194)
    temp = 35.0 if not temp or temp > 90 else min(70.0, max(15.0, float(temp)))
    return [
        _l(g(5)), _l(g(187)), _l(timeouts), _l(g(197)), _l(g(198)), _l(g(10)), _l(g(184)), _l(g(199)),
        1.0 if sector > 0 else 0.0,
        age, age * age, temp,
        math.log(max(0.1, float(model_afr or 1.5))),
    ]


def normalize_model(model):
    return re.sub(r"\s+", " ", (model or "").strip())
