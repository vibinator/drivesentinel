"""Make/model reliability baselines (annualized failure rate) adjusted for drive age."""
import json
import os
import re

from config import DATA_DIR

_BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "model_stats.json")
_BB_BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backblaze_stats.json")
_BB_USER = os.path.join(DATA_DIR, "backblaze_stats.json")       # written by the in-app importer
_OVERRIDE = os.path.join(DATA_DIR, "model_stats.json")           # hand-written user overrides
_cache = {"key": None, "data": None}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load():
    paths = (_BUILTIN, _BB_BUILTIN, _BB_USER, _OVERRIDE)
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in paths)
    if _cache["data"] is not None and _cache["key"] == key:
        return _cache["data"]
    data = _read(_BUILTIN)
    # Priority: user overrides > computed Backblaze stats > curated table > families > defaults
    bb = _read(_BB_USER) if os.path.exists(_BB_USER) else _read(_BB_BUILTIN)
    data["dataset"] = None
    if bb:
        data["models"] = bb.get("models", []) + data["models"]
        data["fleet_average_afr"] = bb.get("fleet_average_afr", data.get("fleet_average_afr"))
        data["as_of"] = bb.get("as_of", data.get("as_of"))
        data["dataset"] = {"as_of": bb.get("as_of"), "models": len(bb.get("models", [])),
                           "custom": os.path.exists(_BB_USER)}
    user = _read(_OVERRIDE) if os.path.exists(_OVERRIDE) else None
    if user:
        data["models"] = user.get("models", []) + data["models"]
        data["families"] = user.get("families", []) + data["families"]
        data["defaults"].update(user.get("defaults", {}))
        if "age_multipliers" in user:
            data["age_multipliers"] = user["age_multipliers"]
    _cache.update(key=key, data=data)
    return data


def dataset_info():
    return _load().get("dataset")


def lookup(summary):
    """Return the reliability baseline for a drive, including an age-adjusted 12-month risk."""
    data = _load()
    model = re.sub(r"\s+", " ", summary.get("model") or "").strip()
    family = summary.get("family") or ""
    match, kind = None, None
    for entry in data["models"]:
        if _safe_search(entry["match"], model):
            match, kind = entry, "model"
            break
    if not match:
        for entry in data["families"]:
            if _safe_search(entry["match"], model) or (family and _safe_search(entry["match"], family)):
                match, kind = entry, "family"
                break
    if not match:
        key = "nvme" if summary.get("protocol") == "NVMe" else "ssd" if summary.get("is_ssd") else "hdd"
        match, kind = dict(data["defaults"][key], name=f"Generic {key.upper()}"), "default"

    poh = summary.get("power_on_hours")
    years = (poh or 0) / 8766
    mult, phase = 1.0, "age unknown"
    for band in (data["age_multipliers"] if poh is not None else ()):
        if years < band["max_years"]:
            mult, phase = band["mult"], band.get("note", "")
            break
    if summary.get("is_ssd"):
        mult, phase = min(mult, 1.4), phase if years < 1 else "SSD (wear tracked separately)"
    afr = float(match["afr"])
    adjusted = min(60.0, afr * mult)
    fleet = data.get("fleet_average_afr", 1.5)
    return {
        "afr": afr,
        "adjusted_afr": round(adjusted, 2),
        "age_years": round(years, 1),
        "age_multiplier": mult,
        "age_phase": phase,
        "matched": match.get("name"),
        "match_type": kind,              # model | family | default
        "source": match.get("source"),
        "fleet_average": fleet,
        "vs_fleet": round(afr / fleet, 2) if fleet else None,
        "as_of": data.get("as_of"),
        "failures": match.get("failures"),
        "drive_days": match.get("drive_days"),
        "drives": match.get("drives"),
    }


def _safe_search(pattern, text):
    try:
        return re.search(pattern, text, re.I) is not None
    except re.error:
        return False


CLASSES = {
    "desktop":    {"name": "desktop", "workload_tb_yr": 55, "load_cycles": 300000},
    "nas":        {"name": "NAS", "workload_tb_yr": 180, "load_cycles": 600000},
    "nas_pro":    {"name": "NAS Pro", "workload_tb_yr": 300, "load_cycles": 600000},
    "enterprise": {"name": "enterprise", "workload_tb_yr": 550, "load_cycles": 600000},
    "ssd":        {"name": "SSD", "workload_tb_yr": None, "load_cycles": 600000},
}


def drive_class(summary):
    """Workload / load-cycle ratings for the drive's class (from the family table)."""
    if summary.get("is_ssd"):
        return CLASSES["ssd"]
    model = re.sub(r"\s+", " ", summary.get("model") or "").strip()
    family = summary.get("family") or ""
    for entry in _load()["families"]:
        if entry.get("class") and (_safe_search(entry["match"], model)
                                   or (family and _safe_search(entry["match"], family))):
            return CLASSES.get(entry["class"], CLASSES["nas"])
    return CLASSES["nas"]
