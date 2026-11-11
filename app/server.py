"""HTTP server: JSON API, Prometheus metrics and the static web UI (stdlib only)."""
import base64
import hmac
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import alerts
import ml
import raid
import reliability

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "1.1.0"


def drive_card(d):
    s, a = d.get("summary") or {}, d.get("analysis") or {}
    return {
        "serial": d["serial"], "model": d["model"], "family": s.get("family"),
        "device": d["device"], "protocol": d["protocol"], "state": d["state"],
        "capacity": s.get("capacity"), "is_ssd": s.get("is_ssd"),
        "temperature": s.get("temperature"), "power_on_hours": s.get("power_on_hours"),
        "health": a.get("health"), "risk": a.get("risk"), "status": a.get("status", "unknown"),
        "verdict": a.get("verdict"), "top_issue": (a.get("issues") or [{}])[0].get("text"),
        "issue_count": len(a.get("issues") or []), "eta_days": a.get("eta_days"),
        "life_left": a.get("life_left"), "last_seen": d["last_seen"],
        "selftest_running": (s.get("selftest") or {}).get("running"),
        "selftest_remaining": (s.get("selftest") or {}).get("remaining"),
        "perf_score": (a.get("perf") or {}).get("score"),
        "ml_p": (a.get("ml") or {}).get("p_horizon"),
        "known_issues": len([k for k in a.get("known_issues") or [] if k["severity"] != "info"]),
        "arrays": [r["array"] for r in a.get("raid") or [] if r.get("role") == "data"],
    }


def make_handler(app):
    cfg, db, mon = app["cfg"], app["db"], app["monitor"]
    auth_user, auth_pass = os.environ.get("AUTH_USER", ""), os.environ.get("AUTH_PASS", "")

    class Handler(BaseHTTPRequestHandler):
        server_version = "DriveSentinel/" + VERSION

        def log_message(self, *_):
            pass

        # ---- helpers ---------------------------------------------------
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                json.dumps(body).encode() if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(min(n, 1_000_000)))
            except ValueError:
                return {}

        def _authorized(self):
            if not auth_user:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                    if hmac.compare_digest(u, auth_user) and hmac.compare_digest(p, auth_pass):
                        return True
                except ValueError:
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DriveSentinel"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _static(self, path):
            rel = "index.html" if path in ("", "/") else path.lstrip("/").removeprefix("static/")
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                self._send(200, f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream")

        # ---- routing ---------------------------------------------------
        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            p = url.path
            if p == "/api/status":
                drives = db.list_drives()
                counts = {}
                for d in drives:
                    st = "missing" if d["state"] == "missing" else (d.get("analysis") or {}).get("status", "unknown")
                    counts[st] = counts.get(st, 0) + 1
                return self._send(200, {
                    "version": VERSION, "mock": mon.mock, "scanning": mon.scanning,
                    "last_scan": mon.last_scan, "last_scan_duration": mon.last_scan_duration,
                    "next_scan": mon.last_scan + cfg["scan_interval_min"] * 60,
                    "errors": mon.last_errors, "skipped": mon.last_skipped, "counts": counts, "drive_count": len(drives),
                    "channels": alerts.channels(cfg), "now": int(time.time()),
                    "ml": ml.info(), "dataset": reliability.dataset_info(), "benching": mon.benching,
                    "backblaze": {k: v for k, v in mon.bb_job.items() if k != "log"},
                    "arrays": [{k: a[k] for k in ("name", "status", "role")} for a in mon.arrays]})
            if p == "/api/arrays":
                drives = {raid.disk_key(d["device"]): {"serial": d["serial"], "model": d["model"],
                                                       "status": (d.get("analysis") or {}).get("status"),
                                                       "state": d["state"]}
                          for d in db.list_drives()}
                return self._send(200, {"arrays": mon.arrays, "drives": drives})
            if p == "/api/backblaze":
                return self._send(200, mon.bb_job)
            if p == "/api/drives":
                return self._send(200, [drive_card(d) for d in db.list_drives()])
            m = re.fullmatch(r"/api/drives/([^/]+)(/history|/raw)?", p)
            if m:
                serial = unquote(m.group(1))
                d = db.get_drive(serial)
                if not d:
                    return self._send(404, {"error": "unknown drive"})
                if m.group(2) == "/raw":
                    return self._send(200, d.get("raw") or {})
                if m.group(2) == "/history":
                    days = max(1, min(3650, int(q.get("days", 30))))
                    return self._send(200, _history_payload(db.history(serial, int(time.time()) - days * 86400)))
                import bench
                benches = db.benchmarks(serial, 100)
                return self._send(200, {"card": drive_card(d), "summary": d["summary"],
                                        "analysis": d["analysis"], "first_seen": d["first_seen"],
                                        "events": db.events(50, serial),
                                        "peers": mon.peer_reports.get(serial),
                                        "benchmarks": benches, "bench_eval": bench.evaluate(benches),
                                        "benching": mon.benching == serial})
            if p == "/api/events":
                return self._send(200, db.events(int(q.get("limit", 300)), q.get("serial")))
            if p == "/api/settings":
                return self._send(200, cfg.public())
            if p == "/metrics":
                return self._send(200, _prometheus(db), "text/plain; version=0.0.4")
            if p == "/healthz":
                return self._send(200, {"ok": True})
            if p.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            return self._static(p)

        def do_POST(self):
            if not self._authorized():
                return
            p = urlparse(self.path).path
            body = self._json_body()
            if p == "/api/scan":
                if mon.scanning:
                    return self._send(409, {"error": "Scan already in progress"})
                threading.Thread(target=mon.scan, daemon=True).start()
                return self._send(202, {"ok": True})
            m = re.fullmatch(r"/api/drives/([^/]+)/(selftest|forget|benchmark)", p)
            if m:
                serial = unquote(m.group(1))
                if m.group(2) == "benchmark":
                    if mon.benching:
                        return self._send(409, {"error": "A benchmark is already running"})
                    if not db.get_drive(serial):
                        return self._send(404, {"error": "unknown drive"})
                    threading.Thread(target=mon.run_benchmark, args=(serial,), daemon=True).start()
                    warn = " (note: a RAID rebuild/sync is running, so results will be lower)" if raid.busy(mon.arrays) else ""
                    return self._send(202, {"ok": True, "message": "Benchmark started; it takes about a minute" + warn})
                if m.group(2) == "forget":
                    db.delete_drive(serial)
                    return self._send(200, {"ok": True})
                try:
                    ok, msg = mon.run_selftest(serial, body.get("type", "short"))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                if ok:
                    threading.Timer(5, mon.trigger).start()
                return self._send(200 if ok else 500, {"ok": ok, "message": msg})
            if p == "/api/settings":
                cfg.update(body)
                mon.trigger()
                return self._send(200, cfg.public())
            if p == "/api/backblaze":
                ok, msg = mon.start_backblaze(body.get("quarters") or cfg["backblaze_quarters"])
                return self._send(202 if ok else 409, {"ok": ok, "message": msg})
            if p == "/api/alerts/test":
                if not alerts.channels(cfg):
                    return self._send(400, {"error": "No notification channels configured"})
                res = alerts.send(cfg, "info", "Test notification",
                                  "If you can read this, DriveSentinel notifications are working.")
                return self._send(200, {"results": res})
            return self._send(404, {"error": "not found"})

    return Handler


def _history_payload(rows):
    out = {"ts": [], "temp": [], "health": [], "realloc": [], "pending": [], "uncorrectable": [],
           "wear": [], "media_errors": []}
    for ts, m in rows:
        a = m.get("attrs") or {}
        nv = m.get("nvme") or {}
        sc = m.get("scsi") or {}

        def raw(k):
            return a[k][3] if k in a else None

        out["ts"].append(ts)
        out["temp"].append(m.get("temp"))
        out["health"].append(m.get("health"))
        out["realloc"].append(raw("5") if "5" in a else sc.get("grown_defects"))
        out["pending"].append(raw("197"))
        out["uncorrectable"].append(raw("198") if "198" in a else nv.get("media_errors"))
        wear = nv.get("percentage_used")
        if wear is None:
            for k in ("231", "233", "202", "177", "169"):
                if k in a:
                    wear = 100 - a[k][0]
                    break
        out["wear"].append(wear)
    return out


def _prometheus(db):
    lines = [
        "# HELP drivesentinel_health_percent Estimated drive health (0-100).",
        "# TYPE drivesentinel_health_percent gauge",
        "# HELP drivesentinel_failure_risk_percent Estimated failure risk (0-100).",
        "# TYPE drivesentinel_failure_risk_percent gauge",
        "# HELP drivesentinel_temperature_celsius Current drive temperature.",
        "# TYPE drivesentinel_temperature_celsius gauge",
        "# HELP drivesentinel_power_on_hours Power-on hours.",
        "# TYPE drivesentinel_power_on_hours counter",
        "# HELP drivesentinel_status Status code (0 good, 1 warning, 2 critical, 3 failing).",
        "# TYPE drivesentinel_status gauge",
        "# HELP drivesentinel_performance_score_percent Latest benchmark vs. the drive's own baseline.",
        "# TYPE drivesentinel_performance_score_percent gauge",
        "# HELP drivesentinel_failure_probability_30d AI-estimated probability of failure within 30 days.",
        "# TYPE drivesentinel_failure_probability_30d gauge",
        "# HELP drivesentinel_smart_attribute_raw Raw SMART attribute value.",
        "# TYPE drivesentinel_smart_attribute_raw gauge",
    ]
    codes = {"good": 0, "warning": 1, "critical": 2, "failing": 3}
    for d in db.list_drives():
        s, a = d.get("summary") or {}, d.get("analysis") or {}
        lbl = 'serial="%s",model="%s",device="%s"' % (d["serial"], (d["model"] or "").replace('"', ""), d["device"])
        for name, val in (("health_percent", a.get("health")), ("failure_risk_percent", a.get("risk")),
                          ("temperature_celsius", s.get("temperature")),
                          ("power_on_hours", s.get("power_on_hours")),
                          ("status", codes.get(a.get("status"))),
                          ("performance_score_percent", (a.get("perf") or {}).get("score")),
                          ("failure_probability_30d", (a.get("ml") or {}).get("p_horizon"))):
            if val is not None:
                lines.append(f"drivesentinel_{name}{{{lbl}}} {val}")
        for attr in s.get("attributes") or []:
            lines.append(f'drivesentinel_smart_attribute_raw{{{lbl},id="{attr["id"]}",name="{attr["name"]}"}} {attr["raw"]}')
    return "\n".join(lines) + "\n"


def serve(app, host="0.0.0.0", port=8090):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    httpd.serve_forever()
