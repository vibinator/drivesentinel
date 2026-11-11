"""Fleet comparison: flag drives that behave differently from identical/similar drives in this NAS."""
import statistics
import time


def _cls(s):
    return "nvme" if s.get("protocol") == "NVMe" else "ssd" if s.get("is_ssd") else "hdd"


def analyze(drives, db, bench_eval):
    """drives: db drive rows; bench_eval: serial -> bench.evaluate() result. Returns serial -> report."""
    now = time.time()
    rows = []
    for d in drives:
        s, a = d.get("summary") or {}, d.get("analysis") or {}
        if d["state"] == "missing" or not s:
            continue
        hist = db.history(d["serial"], int(now - 7 * 86400))
        temps = [m.get("temp") for _, m in hist if m.get("temp") is not None]
        attrs = {x["id"]: x["raw"] for x in s.get("attributes", [])}
        nv = s.get("nvme") or {}
        sector = (attrs.get(5, 0) + attrs.get(197, 0) + attrs.get(198, 0) + attrs.get(187, 0)
                  + (nv.get("media_errors") or 0) + ((s.get("scsi") or {}).get("grown_defects") or 0))
        be = bench_eval.get(d["serial"]) or {}
        rows.append({
            "serial": d["serial"], "model": d["model"], "device": d["device"], "cls": _cls(s),
            "temp": round(statistics.fmean(temps), 1) if temps else s.get("temperature"),
            "health": a.get("health"), "sector_errors": sector, "poh": s.get("power_on_hours") or 0,
            "seq": (be.get("latest") or {}).get("seq_avg"), "status": a.get("status"),
        })

    reports = {}
    for r in rows:
        same_model = [p for p in rows if p["model"] == r["model"] and p["serial"] != r["serial"]]
        same_cls = [p for p in rows if p["cls"] == r["cls"] and p["serial"] != r["serial"]]
        group, gname = (same_model, "identical drives") if same_model else (same_cls, f"other {r['cls'].upper()}s")
        insights = []
        if group:
            pt = [p["temp"] for p in group if p["temp"] is not None]
            if pt and r["temp"] is not None:
                diff = r["temp"] - statistics.median(pt)
                if diff >= 7:
                    insights.append({"level": "warning", "metric": "temp",
                                     "text": f"Runs {diff:.0f}°C hotter than its {gname} (7-day avg {r['temp']}°C vs "
                                             f"{statistics.median(pt):.0f}°C). Check the bay's airflow and fan, "
                                             "and that the drive is seated properly."})
                elif diff <= -7:
                    insights.append({"level": "info", "metric": "temp",
                                     "text": f"Runs {-diff:.0f}°C cooler than its {gname}."})
            if r["sector_errors"] > 0 and all(p["sector_errors"] == 0 for p in group):
                insights.append({"level": "warning", "metric": "errors",
                                 "text": f"The only drive among its {gname} with media/sector errors "
                                         f"({r['sector_errors']} vs 0)."})
            ps = [p["seq"] for p in group if p["seq"]]
            if ps and r["seq"]:
                ratio = r["seq"] / statistics.median(ps)
                if ratio < 0.75 and same_model:
                    insights.append({"level": "warning", "metric": "perf",
                                     "text": f"Reads {100 - ratio * 100:.0f}% slower than identical drives "
                                             f"({r['seq']:.0f} vs {statistics.median(ps):.0f} MB/s)."})
            ph = [p["health"] for p in group if p["health"] is not None]
            if ph and r["health"] is not None and statistics.median(ph) - r["health"] >= 20:
                insights.append({"level": "info", "metric": "health",
                                 "text": f"Health {r['health']}% vs a median of {statistics.median(ph):.0f}% for its {gname}."})
        if len(rows) >= 3 and r["cls"] == "hdd":
            oldest = max((p for p in rows if p["cls"] == "hdd"), key=lambda p: p["poh"])
            if oldest["serial"] == r["serial"] and r["poh"] > 3 * 8766:
                insights.append({"level": "info", "metric": "age",
                                 "text": f"Oldest hard drive in this NAS ({r['poh'] / 8766:.1f} years powered on). "
                                         "When you replace drives, start with this one."})
        reports[r["serial"]] = {
            "group": gname if group else None,
            "insights": insights,
            "peers": [{k: p[k] for k in ("serial", "model", "device", "temp", "health", "sector_errors", "seq", "status")}
                      for p in [r] + group],
        }
    return reports
