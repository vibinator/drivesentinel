"""Failure-probability inference with the logistic-regression model trained on Backblaze data."""
import json
import math
import os

import features
from config import DATA_DIR

_BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ml_model.json")
_USER = os.path.join(DATA_DIR, "ml_model.json")
_cache = {"key": None, "model": None}


def load():
    paths = [p for p in (_USER, _BUILTIN) if os.path.exists(p)]
    key = tuple((p, os.path.getmtime(p)) for p in paths)
    if _cache["key"] != key:
        model = None
        for path in paths:      # an imported model wins, unless it is unreadable or incompatible
            try:
                with open(path, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if m.get("features") == features.FEATURES and len(m.get("weights", [])) == len(features.FEATURES):
                m["_custom"] = path == _USER
                model = m
                break
        _cache.update(key=key, model=model)
    return _cache["model"]


def info():
    m = load()
    if not m:
        return None
    keys = ("trained_on", "trained_at", "auc", "positives", "negatives", "horizon_days", "holdout",
            "recall_at_1pct", "false_alarm_rate_at_1pct")
    return {k: m.get(k) for k in keys} | {"custom": bool(m.get("_custom"))}


def applicable(summary):
    """The model is trained on SATA HDD SMART attributes only."""
    return (summary.get("protocol") == "ATA" and not summary.get("is_ssd")
            and bool(summary.get("attributes")))


def predict(summary, model_afr):
    m = load()
    if not m or not applicable(summary):
        return None
    raw = {a["id"]: a["raw"] for a in summary["attributes"] if a["id"] in features.ATTRS}
    if 194 not in raw and summary.get("temperature") is not None:
        raw[194] = summary["temperature"]
    if 9 not in raw and summary.get("power_on_hours") is not None:
        raw[9] = summary["power_on_hours"]
    x = features.vector(raw, model_afr)
    z = [(v - mu) / sd for v, mu, sd in zip(x, m["mean"], m["std"])]
    contrib = [w * zi for w, zi in zip(m["weights"], z)]
    logit = m["intercept"] + sum(contrib)
    p = 1 / (1 + math.exp(-max(-35, min(35, logit))))
    horizon = m.get("horizon_days", 30)
    p_year = 1 - (1 - p) ** (365 / horizon)

    # group contributions by human label (age + age² are one factor)
    grouped = {}
    for name, c in zip(m["features"], contrib):
        label = features.LABELS[name]
        grouped[label] = grouped.get(label, 0) + c
    drivers = sorted(({"factor": k, "weight": round(v, 3)} for k, v in grouped.items() if v > 0.15),
                     key=lambda d: -d["weight"])[:4]
    return {
        "p_horizon": p, "p_year": p_year, "horizon_days": horizon,
        "drivers": drivers, "trained_on": m.get("trained_on"), "auc": m.get("auc"),
    }
