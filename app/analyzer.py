"""Health scoring and failure prediction.

Approach
--------
1. Risk factors: each SMART indicator that correlates with failure (based on public
   large-fleet studies such as Backblaze's drive stats and Google's "Failure Trends in a
   Large Disk Drive Population") contributes an independent failure probability.
   Combined risk = 1 - prod(1 - p_i).  Health % = 100 * (1 - risk).
2. Trend analysis: linear regression over the stored history detects counters that are
   growing (deterioration) and projects when normalized attribute values, SSD wear or
   NVMe spare will cross their failure thresholds.
"""
import math
import time

import issues as known_issue_db
import ml
import reliability

# id -> (label, base probability, per-doubling increment, cap, level)
ATA_COUNTERS = {
    5:   ("Reallocated sectors",          0.12, 0.06, 0.60, "warning"),
    197: ("Current pending sectors",      0.20, 0.07, 0.70, "critical"),
    198: ("Offline uncorrectable sectors", 0.20, 0.07, 0.70, "critical"),
    187: ("Reported uncorrectable errors", 0.15, 0.06, 0.60, "critical"),
    184: ("End-to-end errors",            0.35, 0.05, 0.60, "critical"),
    10:  ("Spin retries",                 0.20, 0.04, 0.45, "warning"),
    188: ("Command timeouts",             0.04, 0.03, 0.30, "warning"),
    196: ("Reallocation events",          0.04, 0.02, 0.20, "warning"),
}

CABLE_ATTR = 199          # UDMA CRC errors: usually cabling/backplane, not the disk
SSD_LIFE_ATTRS = (231, 233, 202, 177, 169)   # normalized value ~= % life remaining
TREND_ATTRS = (5, 197, 198, 187)

LEVEL_RANK = {"info": 0, "warning": 1, "critical": 2}
STATUS_RANK = {"good": 0, "warning": 1, "critical": 2, "failing": 3, "unknown": -1}


def _log_scale(count, base, step, cap):
    return min(cap, base + step * math.log2(1 + count))


def _regress(points):
    """Least squares slope (per day) for [(ts, value)]; None if insufficient data."""
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 3:
        return None
    t0 = pts[0][0]
    xs = [(t - t0) / 86400.0 for t, _ in pts]
    if xs[-1] - xs[0] < 1.0:   # need at least one day of history
        return None
    ys = [float(v) for _, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _hist_series(history, getter):
    out = []
    for ts, m in history:
        try:
            out.append((ts, getter(m)))
        except (KeyError, TypeError, IndexError):
            continue
    return out


def _fmt_days(days):
    if days < 1:
        return "less than a day"
    if days < 60:
        return f"~{int(round(days))} days"
    if days < 730:
        return f"~{days / 30.4:.0f} months"
    return f"~{days / 365:.1f} years"


def analyze(summary, history, cfg):
    """summary: normalized data; history: [(ts, metrics)] oldest->newest incl. current."""
    factors = []      # (probability, level, text)
    predictions = []  # {"text", "days", "level"}
    now = time.time()

    def add(p, level, text):
        factors.append((max(0.0, min(0.99, p)), level, text))

    failing_now = False
    if summary.get("smart_passed") is False:
        failing_now = True
        add(0.95, "critical", "SMART overall-health self-assessment: FAILED")

    attrs = {a["id"]: a for a in summary.get("attributes", [])}

    # ---- ATA attributes -------------------------------------------------
    for a in attrs.values():
        thresh, value, worst = a.get("thresh") or 0, a.get("value"), a.get("worst")
        if thresh > 0 and value is not None and value <= thresh:
            failing_now = True
            add(0.9, "critical", f"{a['name']} (ID {a['id']}) is at/below its failure threshold "
                                 f"({value} <= {thresh})")
        elif thresh > 0 and worst is not None and worst <= thresh:
            add(0.25, "warning", f"{a['name']} (ID {a['id']}) crossed its threshold in the past")

    for aid, (label, base, step, cap, level) in ATA_COUNTERS.items():
        a = attrs.get(aid)
        if not a or a["raw"] <= 0:
            continue
        count = a["raw"]
        if aid == 188 and count > 0xFFFF:      # Seagate packs 3 counters into this raw value
            count &= 0xFFFF
            if count == 0:
                continue
        if aid == 5 and count >= 50:
            level = "critical"
        add(_log_scale(count, base, step, cap), level, f"{label}: {count}")

    crc = attrs.get(CABLE_ATTR)
    if crc and crc["raw"] > 0:
        add(0.02, "info", f"UDMA CRC errors: {crc['raw']} (usually a cable/backplane issue, "
                          "not the disk; only a concern if it keeps growing)")

    # SSD life (SATA SSDs)
    life_left = None
    for aid in SSD_LIFE_ATTRS:
        if aid in attrs and summary.get("is_ssd"):
            life_left = attrs[aid]["value"]
            break
    if life_left is not None:
        if life_left <= 10:
            add(0.4, "critical", f"SSD endurance nearly exhausted ({life_left}% remaining)")
        elif life_left <= 25:
            add(0.1, "warning", f"SSD endurance low ({life_left}% remaining)")

    # ---- NVMe ------------------------------------------------------------
    nv = summary.get("nvme")
    if nv:
        cw = nv.get("critical_warning") or 0
        bits = [(0x01, 0.6, "Available spare below threshold"),
                (0x02, 0.08, "Temperature outside safe range"),
                (0x04, 0.7, "NVM subsystem reliability degraded"),
                (0x08, 0.9, "Media placed in read-only mode"),
                (0x10, 0.3, "Volatile memory backup failed"),
                (0x20, 0.3, "Persistent memory region read-only")]
        for bit, p, text in bits:
            if cw & bit:
                add(p, "critical", f"NVMe critical warning: {text}")
                if bit in (0x04, 0x08):
                    failing_now = True
        used = nv.get("percentage_used")
        if used is not None:
            life_left = max(0, 100 - used)
            if used >= 100:
                add(0.4, "critical", f"Rated endurance exceeded ({used}% used)")
            elif used >= 90:
                add(0.15, "warning", f"Rated endurance almost used ({used}% used)")
        spare, spare_t = nv.get("available_spare"), nv.get("available_spare_threshold")
        if spare is not None and spare_t is not None and spare > spare_t and spare < 30:
            add(0.1, "warning", f"Available spare low ({spare}%, threshold {spare_t}%)")
        if (nv.get("media_errors") or 0) > 0:
            add(_log_scale(nv["media_errors"], 0.2, 0.06, 0.6), "critical",
                f"Media/data integrity errors: {nv['media_errors']}")

    # ---- SCSI / SAS ------------------------------------------------------
    sc = summary.get("scsi")
    if sc:
        if sc.get("grown_defects"):
            add(_log_scale(sc["grown_defects"], 0.12, 0.06, 0.6), "warning",
                f"Grown defect list: {sc['grown_defects']}")
        unc = sum(sc.get(k) or 0 for k in ("read_uncorrected", "write_uncorrected", "verify_uncorrected"))
        if unc:
            add(_log_scale(unc, 0.15, 0.06, 0.6), "critical", f"Uncorrected I/O errors: {unc}")

    # ---- Temperature / age / self-test ---------------------------------
    temp = summary.get("temperature")
    if temp is not None:
        warn = cfg["ssd_temp_warn"] if summary.get("is_ssd") else cfg["hdd_temp_warn"]
        crit = cfg["ssd_temp_crit"] if summary.get("is_ssd") else cfg["hdd_temp_crit"]
        if temp >= crit:
            add(0.08, "critical", f"Temperature critical: {temp}°C (limit {crit}°C)")
        elif temp >= warn:
            add(0.02, "warning", f"Temperature high: {temp}°C (warn {warn}°C)")

    # Make/model baseline (historical fleet failure rate) adjusted for age (bathtub curve)
    rel = reliability.lookup(summary)
    base_p = rel["adjusted_afr"] / 100
    rel_text = (f"{rel['matched']}: {rel['afr']}% historical annual failure rate ({rel['source']})"
                + (f", x{rel['age_multiplier']} for age {rel['age_years']}y ({rel['age_phase']})"
                   if rel["age_multiplier"] != 1 else ""))
    if rel["adjusted_afr"] >= 5:
        add(base_p, "warning", "Elevated baseline risk — " + rel_text)
    elif rel["adjusted_afr"] >= 2.5:
        add(base_p, "info", "Baseline risk above average — " + rel_text)
    else:
        factors.append((base_p, "baseline", rel_text))   # counted in risk, not shown as an issue

    # Known firmware bugs / model caveats
    known = known_issue_db.lookup(summary)
    for k in known:
        if k["severity"] == "critical":
            add(0.25, "critical", f"Known issue: {k['title']}" + (f" (update firmware to {k['fix']})" if k.get("fix") else ""))
        elif k["severity"] == "warning":
            add(0.03, "warning", f"Known issue: {k['title']}")

    # Machine-learning failure prediction (trained on Backblaze fleet data)
    pred = ml.predict(summary, rel["afr"]) if cfg["ml_enabled"] else None
    if pred:
        ph = pred["p_horizon"]
        txt = f"AI model: {ph * 100:.1f}% chance of failure within {pred['horizon_days']} days"
        if ph >= 0.10:
            add(min(0.9, pred["p_year"]), "critical", txt)
        elif ph >= 0.01:
            add(min(0.6, pred["p_year"]), "warning", txt)

    # Benchmark trend
    perf = summary.get("perf")
    for lvl, text in (perf or {}).get("issues", []):
        add(0.25 if lvl == "critical" else 0.08, lvl, text)

    # RAID context
    raid_exposed = False
    for r in summary.get("raid") or []:
        if r["role"] != "data":
            continue
        if r["member_failed"]:
            add(0.5, "critical", f"Marked as FAILED by the RAID layer in {r['array']}")
        elif r["status"] in ("degraded", "rebuilding") and r["redundancy_left"] == 0:
            raid_exposed = True
            add(0.0, "warning", f"{r['array']} ({r['level']}) is {r['status']} with no redundancy left. "
                                "If this drive fails now, the volume is lost.")

    log = summary.get("selftest", {}).get("log") or []
    if log and log[0].get("passed") is False:
        add(0.5, "critical", f"Most recent self-test failed: {log[0].get('result')}")

    # ---- Trends ----------------------------------------------------------
    week_ago = now - 7 * 86400
    recent = [(ts, m) for ts, m in history if ts >= week_ago]
    if len(recent) >= 2:
        first, last = recent[0][1], recent[-1][1]
        deltas = []
        for aid in TREND_ATTRS:
            try:
                d = last["attrs"][str(aid)][3] - first["attrs"][str(aid)][3]
            except (KeyError, TypeError, IndexError):
                continue
            if d > 0:
                deltas.append(f"{ATA_COUNTERS[aid][0].lower()} +{d}")
        if first.get("nvme") and last.get("nvme"):
            d = (last["nvme"].get("media_errors") or 0) - (first["nvme"].get("media_errors") or 0)
            if d > 0:
                deltas.append(f"media errors +{d}")
        if first.get("scsi") and last.get("scsi"):
            d = (last["scsi"].get("grown_defects") or 0) - (first["scsi"].get("grown_defects") or 0)
            if d > 0:
                deltas.append(f"grown defects +{d}")
        if deltas:
            add(0.2, "critical", "Actively deteriorating in the last 7 days: " + ", ".join(deltas))

    month = [(ts, m) for ts, m in history if ts >= now - 30 * 86400]

    # Normalized ATA values trending toward their thresholds
    for aid, a in attrs.items():
        thresh = a.get("thresh") or 0
        if thresh <= 0 or a["value"] is None or a["value"] <= thresh:
            continue
        slope = _regress(_hist_series(month, lambda m, k=str(aid): m["attrs"][k][0]))
        if slope is not None and slope < -0.02:
            days = (a["value"] - thresh) / -slope
            if days < 3 * 365:
                predictions.append({
                    "days": days,
                    "level": "critical" if days < 90 else "warning",
                    "text": f"{a['name']} is declining {abs(slope):.2f}/day and is projected to reach "
                            f"its failure threshold in {_fmt_days(days)}"})

    # Raw counter growth rates
    for aid in TREND_ATTRS:
        if aid not in attrs:
            continue
        slope = _regress(_hist_series(month, lambda m, k=str(aid): m["attrs"][k][3]))
        if slope is not None and slope > 0.05:
            predictions.append({"days": None, "level": "warning",
                                "text": f"{ATA_COUNTERS[aid][0]} growing ~{slope:.1f}/day "
                                        f"(~{slope * 30:.0f}/month)"})

    # SSD wear projections
    if nv and nv.get("percentage_used") is not None:
        slope = _regress(_hist_series(month, lambda m: m["nvme"]["percentage_used"]))
        if slope and slope > 0 and nv["percentage_used"] < 100:
            days = (100 - nv["percentage_used"]) / slope
            predictions.append({"days": days, "level": "warning" if days < 365 else "info",
                                "text": f"Rated endurance projected to be reached in {_fmt_days(days)}"})
    elif life_left is not None:
        for aid in SSD_LIFE_ATTRS:
            if aid in attrs:
                slope = _regress(_hist_series(month, lambda m, k=str(aid): m["attrs"][k][0]))
                if slope and slope < 0:
                    days = life_left / -slope
                    predictions.append({"days": days, "level": "warning" if days < 365 else "info",
                                        "text": f"SSD endurance projected to run out in {_fmt_days(days)}"})
                break

    # ---- Combine ---------------------------------------------------------
    survive = 1.0
    for p, _, _ in factors:
        survive *= (1 - p)
    risk = 1 - survive
    for pr in predictions:
        if pr["days"] is not None and pr["days"] < 30:
            risk = max(risk, 0.6)
        elif pr["days"] is not None and pr["days"] < 90:
            risk = max(risk, 0.35)

    health = int(round(100 * (1 - risk)))
    if failing_now:
        status = "failing"
    elif risk >= 0.5:
        status = "critical"
    elif risk >= 0.15:
        status = "warning"
    else:
        status = "good"
    if status == "good" and any(lvl == "critical" for _, lvl, _ in factors):
        status = "warning"
    if raid_exposed and status == "warning":
        status = "critical"

    issues = [{"level": lvl, "text": text, "impact": round(p * 100, 1)}
              for p, lvl, text in sorted(factors, key=lambda f: -f[0]) if lvl != "baseline"]
    predictions.sort(key=lambda p: (p["days"] is None, p["days"] or 0))
    eta = min((p["days"] for p in predictions if p["days"] is not None), default=None)

    return {
        "health": health,
        "risk": round(risk * 100, 1),
        "status": status,
        "issues": issues,
        "predictions": predictions,
        "eta_days": eta,
        "life_left": life_left,
        "reliability": rel,
        "ml": pred,
        "known_issues": known,
        "perf": {k: perf[k] for k in ("score", "baseline_seq", "runs", "latest")} if perf else None,
        "raid": summary.get("raid") or [],
        "verdict": _verdict(status, eta, issues),
        "analyzed_at": int(now),
    }


def _verdict(status, eta, issues):
    if status == "failing":
        return "Drive is failing. Back up its data now and replace it as soon as possible."
    if status == "critical":
        return ("High risk of failure" + (f" (projected within {_fmt_days(eta)})" if eta else "")
                + ". Ensure backups are current and plan an immediate replacement.")
    if status == "warning":
        return "Early warning signs detected. Keep monitoring and make sure backups are current."
    if issues:
        return "Healthy. Minor observations noted."
    return "Healthy. No warning signs detected."
