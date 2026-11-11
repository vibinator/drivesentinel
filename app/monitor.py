"""Scan scheduler, history recording, event/alert generation and self-test scheduling."""
import logging
import os
import re
import shutil
import threading
import time

import alerts
import analyzer
import bench
import peers
import raid
import smart
from config import DATA_DIR

log = logging.getLogger("monitor")

LEVEL_RANK = analyzer.LEVEL_RANK
STATUS_RANK = analyzer.STATUS_RANK
STATUS_LEVEL = {"good": "info", "warning": "warning", "critical": "critical", "failing": "critical"}
WATCH = {"5": "Reallocated sectors", "197": "Pending sectors", "198": "Offline uncorrectable",
         "187": "Reported uncorrectable", "184": "End-to-end errors", "10": "Spin retries"}


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} EB"


class Monitor:
    def __init__(self, cfg, db, backend):
        self.cfg, self.db, self.backend = cfg, db, backend
        self.mock = backend is not smart
        self._scan_lock = threading.Lock()
        self._wake = threading.Event()
        self._temp_alerted = {}
        self._pushed = {}
        self._bench_batch_active = False
        self.last_scan = db.kv_get("last_scan", 0)
        self.last_scan_duration = 0
        self.last_errors = []
        self.last_skipped = []
        self.scanning = False
        self.arrays = []
        self._membership = {}
        self.peer_reports = db.kv_get("peers", {})
        self._bench_lock = threading.Lock()
        self.benching = None
        self.bb_job = {"state": "idle"}

    # ------------------------------------------------------------------
    def notify(self, level, title, message, serial=None, ts=None, cooldown_key=None, cooldown_h=12):
        """Record an event and push it to the configured channels.

        cooldown_key: repeat alerts with the same key are still logged as events, but pushed
        at most once per cooldown_h hours (stops hourly spam while a counter keeps growing).
        """
        self.db.add_event(level, title, message, serial, ts)
        log.info("[%s] %s - %s", level, title, message)
        if cooldown_key and ts is None:
            last = self._pushed.get(cooldown_key, 0)
            if time.time() - last < cooldown_h * 3600:
                return
            self._pushed[cooldown_key] = time.time()
        if ts is None and LEVEL_RANK.get(level, 0) >= LEVEL_RANK.get(self.cfg["alert_min_level"], 1):
            threading.Thread(target=alerts.send, args=(self.cfg, level, title, message,
                                                       {"serial": serial}), daemon=True).start()

    # ------------------------------------------------------------------
    def scan(self, now=None, quiet=False):
        """Collect SMART data from every device. `now` lets the mock backfill history."""
        if not self._scan_lock.acquire(blocking=False):
            return False
        self.scanning = True
        started = time.time()
        ts = int(now or started)
        errors, skipped, seen = [], [], set()
        try:
            self.arrays = raid.read(text=self.backend.mdstat_text(ts)) if self.mock else raid.read()
            self._membership = raid.membership(self.arrays)
            devices = self.backend.scan_devices(self.cfg["devices"])
            if not devices:
                errors.append("No devices found. Is the container running privileged with /dev mounted?")
            for dev, dtype in devices:
                kwargs = {"now": ts} if self.mock else {}
                raw = self.backend.read_device(dev, dtype, self.cfg["skip_standby"], **kwargs)
                summary = smart.normalize(raw, dev, dtype)
                if summary.get("sleeping"):
                    if summary.get("serial"):
                        seen.add(summary["serial"])
                        self.db.set_drive_state(summary["serial"], "standby")
                    else:
                        for d in self.db.list_drives():
                            if d["device"] == dev:
                                seen.add(d["serial"])
                                self.db.set_drive_state(d["serial"], "standby")
                    continue
                if summary.get("skip"):
                    skipped.append(f"{dev}: {summary['skip']}")
                    continue
                if summary.get("error"):
                    errors.append(f"{dev}: {summary['error']}")
                    continue
                serial = summary["serial"]
                if serial in seen:
                    continue   # same disk reachable via multiple device paths
                seen.add(serial)
                self._process(summary, raw, ts, quiet)
            self._check_missing(seen, ts, quiet)
            self._check_raid(ts, quiet)
            self._update_peers(ts, quiet)
            self._check_selftests(ts)
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scan failed")
            errors.append(str(e))
        finally:
            self.last_errors = errors
            self.last_skipped = skipped
            self.last_scan = ts
            self.last_scan_duration = round(time.time() - started, 1)
            self.db.kv_set("last_scan", ts)
            self.scanning = False
            self._scan_lock.release()
        return True

    def _process(self, summary, raw, ts, quiet):
        serial = summary["serial"]
        prev = self.db.get_drive(serial)
        prev_snap = self.db.last_snapshot(serial)
        summary["raid"] = self._membership.get(raid.disk_key(summary["device"]), [])
        summary["perf"] = bench.evaluate(self.db.benchmarks(serial))
        metrics = smart.metrics_from(summary)
        history = self.db.history(serial, ts - 30 * 86400) + [(ts, metrics)]
        result = analyzer.analyze(summary, history, self.cfg, now=ts if quiet else None)
        metrics["health"], metrics["risk"] = result["health"], result["risk"]
        self.db.add_snapshot(serial, ts, metrics)
        self.db.upsert_drive(serial, summary, result, raw, ts)

        ev_ts = ts if quiet else None
        name = f"{summary['model']} ({summary['device']}, S/N {serial})"
        if not prev:
            self.notify("info", f"New drive detected: {summary['model']}",
                        f"{name}, {human_bytes(summary['capacity'])}. Health {result['health']}%.",
                        serial, ev_ts)
            return
        if prev.get("state") == "missing":
            self.notify("info", f"Drive reconnected: {summary['model']}", name, serial, ev_ts)

        old_status = (prev.get("analysis") or {}).get("status", "unknown")
        new_status = result["status"]
        if STATUS_RANK[new_status] > STATUS_RANK.get(old_status, -1):
            details = "\n".join(f"- {i['text']}" for i in result["issues"][:6])
            preds = "\n".join(f"- {p['text']}" for p in result["predictions"][:3])
            self.notify(STATUS_LEVEL[new_status],
                        f"{summary['model']} is now {new_status.upper()} (health {result['health']}%)",
                        f"{name}\n{result['verdict']}\n\n{details}" + (f"\n\nPredictions:\n{preds}" if preds else ""),
                        serial, ev_ts)
        elif STATUS_RANK[new_status] < STATUS_RANK.get(old_status, -1):
            self.notify("info", f"{summary['model']} status improved to {new_status.upper()}",
                        f"{name}. Health {result['health']}%.", serial, ev_ts)

        if prev_snap:
            pm = prev_snap[1]
            for aid, label in WATCH.items():
                try:
                    before, after = pm["attrs"][aid][3], metrics["attrs"][aid][3]
                except (KeyError, TypeError, IndexError):
                    continue
                if after > before:
                    big = after - before >= 10
                    self.notify("critical" if aid in ("197", "198", "187") or big else "warning",
                                f"{label} increased on {summary['model']}",
                                f"{name}: {label} {before} → {after}", serial, ev_ts,
                                cooldown_key=None if big else f"{serial}:{aid}")
            for key, label in (("media_errors", "NVMe media errors"), ("critical_warning", "NVMe critical warning")):
                b, a = (pm.get("nvme") or {}).get(key), (metrics.get("nvme") or {}).get(key)
                if b is not None and a is not None and a > b:
                    self.notify("critical", f"{label} on {summary['model']}", f"{name}: {b} → {a}", serial, ev_ts)

            old_log = (prev.get("summary") or {}).get("selftest", {}).get("log") or []
            new_log = summary["selftest"]["log"]
            if new_log and (not old_log or new_log[0] != old_log[0]) and len(new_log) >= len(old_log):
                t = new_log[0]
                if t.get("passed") is False:
                    self.notify("critical", f"Self-test FAILED on {summary['model']}",
                                f"{name}: {t['type']} — {t['result']}", serial, ev_ts)
                else:
                    self.notify("info", f"Self-test completed on {summary['model']}",
                                f"{name}: {t['type']} — {t['result']}", serial, ev_ts)

        temp = summary.get("temperature")
        crit = self.cfg["ssd_temp_crit"] if summary.get("is_ssd") else self.cfg["hdd_temp_crit"]
        if temp is not None and temp >= crit and not quiet:
            last = self._temp_alerted.get(serial, 0)
            if time.time() - last > self.cfg["temp_alert_cooldown_h"] * 3600:
                self._temp_alerted[serial] = time.time()
                self.notify("critical", f"{summary['model']} is overheating ({temp}°C)",
                            f"{name}: {temp}°C exceeds the {crit}°C limit. Check cooling/fans.", serial)

    def _check_missing(self, seen, ts, quiet):
        for d in self.db.list_drives():
            if d["serial"] in seen or d["state"] == "missing":
                continue
            self.db.set_drive_state(d["serial"], "missing")
            self.notify("warning", f"Drive not detected: {d['model']}",
                        f"{d['model']} (S/N {d['serial']}, last at {d['device']}) did not respond to this scan. "
                        "It may have been removed or failed. Use 'Forget' in the UI if it was removed on purpose.",
                        d["serial"], ts if quiet else None)

    # ------------------------------------------------------------------ RAID
    def _check_raid(self, ts, quiet):
        prev = self.db.kv_get("raid_state", {})
        if prev and not self.arrays:
            return   # pool sources unreadable this scan; don't report every pool as gone
        cur = {}
        ev_ts = ts if quiet else None
        for a in self.arrays:
            name, st = a["name"], a["status"]
            cur[name] = st
            old = prev.get(name)
            if old == st:
                continue
            lvl = (a["level"] or "").upper()
            label = f"{name} ({lvl})" if a["role"] == "data" else f"QNAP {a['role']} array {name}"
            members = ", ".join(m["disk"] for m in a["members"])
            if old is None:
                if a["role"] == "data":
                    self.notify("info", f"Storage array {label} detected: {st}", f"Members: {members}", None, ev_ts)
                if st in ("healthy", "syncing"):
                    continue
            if st in ("degraded", "failed"):
                failed = ", ".join(a["failed_members"]) or f"{a['missing']} member(s) missing"
                level = "critical" if a["role"] == "data" else "warning"
                self.notify(level, f"RAID {label} is {st.upper()}",
                            f"Failed/missing: {failed}. Redundancy left: {a['redundancy_left']}. "
                            "Replace the failed disk as soon as possible, and avoid heavy I/O until the rebuild is done.",
                            None, ev_ts)
            elif st == "rebuilding":
                self.notify("warning", f"RAID {label} is rebuilding",
                            f"{a['sync_pct']}% complete, ~{(a['sync_eta_min'] or 0) / 60:.1f} h remaining.", None, ev_ts)
            elif st == "healthy" and old in ("degraded", "rebuilding", "failed"):
                self.notify("info", f"RAID {label} is healthy again", f"Members: {members}", None, ev_ts)
        for name in prev:
            if name not in cur and name not in raid.QNAP_SWAP:
                self.notify("warning", f"Storage array {name} disappeared",
                            "It is no longer listed in /proc/mdstat.", None, ev_ts)
        self.db.kv_set("raid_state", cur)

    # ------------------------------------------------------------------ peers
    def bench_evals(self):
        return {d["serial"]: bench.evaluate(self.db.benchmarks(d["serial"])) for d in self.db.list_drives()}

    def _update_peers(self, ts, quiet):
        reports = peers.analyze(self.db.list_drives(), self.db, self.bench_evals())
        self.peer_reports = reports
        self.db.kv_set("peers", reports)
        alerted = set(self.db.kv_get("peer_alerted", []))
        now_keys = set()
        for serial, rep in reports.items():
            for ins in rep["insights"]:
                if ins["level"] != "warning":
                    continue
                key = f"{serial}:{ins['metric']}"
                now_keys.add(key)
                if key not in alerted:
                    d = self.db.get_drive(serial)
                    self.notify("warning", f"{d['model']} stands out from its peers", ins["text"], serial,
                                ts if quiet else None)
        self.db.kv_set("peer_alerted", sorted(now_keys))

    # ------------------------------------------------------------------ benchmarks
    def run_benchmark(self, serial, now=None, quiet=False):
        d = self.db.get_drive(serial)
        if not d:
            return False, "Unknown drive"
        if not self._bench_lock.acquire(blocking=False):
            return False, "Another benchmark is already running"
        self.benching = serial
        try:
            if self.mock:
                res = self.backend.run_benchmark(d["device"], now=now)
            else:
                res = bench.run(d["device"], seq_mb=self.cfg["bench_seq_mb"])
                # A slow result is often just the NAS being busy: confirm with a second run
                prior = bench.evaluate(self.db.benchmarks(serial))
                if prior and res.get("seq_avg") and res["seq_avg"] < prior["baseline_seq"] * 0.75:
                    time.sleep(45)
                    again = bench.run(d["device"], seq_mb=self.cfg["bench_seq_mb"])
                    if (again.get("seq_avg") or 0) > res["seq_avg"]:
                        res = again
                    res["retried"] = True
            ts = int(now or time.time())
            self.db.add_benchmark(serial, ts, res)
            state = self.db.kv_get("bench_last", {})
            state[serial] = ts
            self.db.kv_set("bench_last", state)
            ev = bench.evaluate(self.db.benchmarks(serial))
            if not quiet:
                self.db.add_event("info", f"Benchmark completed on {d['model']}",
                                  f"Sequential {res.get('seq_avg')} MB/s, random 4K {res.get('rand_lat_ms')} ms "
                                  f"({res.get('rand_iops')} IOPS). Performance score "
                                  f"{ev['score'] if ev else '-'}%.", serial)
                for lvl, text in (ev or {}).get("issues", []):
                    self.notify(lvl, f"Performance drop on {d['model']}", text, serial)
            return True, res
        except Exception as e:  # noqa: BLE001
            log.warning("benchmark failed on %s: %s", d["device"], e)
            if not quiet:
                self.db.add_event("warning", f"Benchmark failed on {d['model']}", str(e), serial)
            return False, str(e)
        finally:
            self.benching = None
            self._bench_lock.release()

    def _due_benchmarks(self):
        interval = self.cfg["bench_interval_days"]
        if interval <= 0 or raid.busy(self.arrays) or self.benching or self._bench_batch_active \
                or not self.in_window():
            return []
        state = self.db.kv_get("bench_last", {})
        now, due, changed = time.time(), [], False
        for d in self.db.list_drives():
            if d["state"] == "missing" or (d["state"] == "standby" and self.cfg["skip_standby"]):
                continue
            last = state.get(d["serial"])
            if last is None:   # first baseline one hour after discovery
                state[d["serial"]] = now - interval * 86400 + 3600
                changed = True
            elif now - last >= interval * 86400:
                due.append(d["serial"])
        if changed:
            self.db.kv_set("bench_last", state)
        return due

    def _bench_batch(self, serials):
        try:
            for s in serials:
                if raid.busy(self.arrays):
                    break
                self.run_benchmark(s)
        finally:
            self._bench_batch_active = False
        self.trigger()

    # ------------------------------------------------------------------ Backblaze
    def start_backblaze(self, quarters):
        if self.bb_job.get("state") == "running":
            return False, "Already running"
        quarters = max(1, min(8, int(quarters)))
        self.bb_job = {"state": "running", "step": "Finding datasets", "progress": 0, "log": [],
                       "started": int(time.time())}
        threading.Thread(target=self._bb_run, args=(quarters,), daemon=True).start()
        return True, "Started"

    def _bb_run(self, quarters):
        import backblaze
        job = self.bb_job

        def logf(msg):
            log.info("backblaze: %s", msg)
            job["log"] = (job["log"] + [msg])[-30:]

        cache = os.path.join(DATA_DIR, "backblaze")
        paths = []
        try:
            qs = backblaze.available_quarters(quarters, log=logf)
            if not qs:
                raise RuntimeError("Could not reach Backblaze or find any published quarters")
            need = sum(sz for _, sz in qs) * 1.15
            free = shutil.disk_usage(DATA_DIR).free
            if free < need:
                raise RuntimeError(f"Not enough free space in /data: need {need / 1e9:.1f} GB, "
                                   f"have {free / 1e9:.1f} GB")
            logf("Using " + ", ".join(q for q, _ in qs))
            for i, (q, size) in enumerate(qs):
                job["step"] = f"Downloading {q} ({size / 1e9:.1f} GB), {i + 1} of {len(qs)}"
                paths.append(backblaze.download(q, cache, lambda f, i=i: job.update(
                    progress=round((i + f) / len(qs) * 50, 1))))
            job["step"] = "Analyzing data and training the model"
            stats, model = backblaze.build(paths, DATA_DIR, log=logf,
                                           progress=lambda f: job.update(progress=round(50 + f * 48, 1)))
            job.update(state="done", progress=100, step="Complete", finished=int(time.time()),
                       result={"models": len(stats["models"]), "fleet_afr": stats["fleet_average_afr"],
                               "auc": model["auc"], "positives": model["positives"], "as_of": stats["as_of"]})
            self.db.add_event("info", "Reliability data and AI model updated from Backblaze",
                              f"{len(stats['models'])} drive models, data {stats['as_of']}. Model AUC {model['auc']}.")
            self.trigger()
        except Exception as e:  # noqa: BLE001
            log.exception("backblaze update failed")
            job.update(state="error", step="Failed", error=str(e), finished=int(time.time()))
        finally:
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def run_selftest(self, serial, kind):
        if kind not in ("short", "long", "conveyance", "abort"):
            raise ValueError("invalid test type")
        d = self.db.get_drive(serial)
        if not d:
            return False, "Unknown drive"
        ok, msg = self.backend.start_selftest(d["device"], d["dev_type"], kind)
        if ok and kind != "abort":
            self.db.add_event("info", f"{kind.capitalize()} self-test started on {d['model']}",
                              f"{d['device']} (S/N {serial})", serial)
        return ok, msg

    def in_window(self, ts=None):
        """True when inside the maintenance window (or when none is configured)."""
        m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*",
                         self.cfg["maintenance_window"] or "")
        if not m:
            return True
        start = int(m.group(1)) % 24 * 60 + int(m.group(2) or 0)
        end = int(m.group(3)) % 24 * 60 + int(m.group(4) or 0)
        lt = time.localtime(ts or time.time())
        cur = lt.tm_hour * 60 + lt.tm_min
        if start == end:
            return True
        return start <= cur < end if start < end else (cur >= start or cur < end)

    def _check_selftests(self, ts):
        """Start due self-tests. Extended tests run one drive at a time, inside the
        maintenance window, so the whole array is never surface-scanned at once."""
        if self.mock and ts < time.time() - 60:
            return   # don't schedule tests while backfilling simulated history
        state = self.db.kv_get("selftest_schedule", {})
        changed = False
        drives = [d for d in self.db.list_drives() if d["state"] == "online"]

        def testing(d):
            return bool((d.get("summary") or {}).get("selftest", {}).get("running"))

        long_slot_free = not any(testing(d) for d in drives)
        for i, d in enumerate(drives):
            for kind, key in (("long", "long_test_interval_days"), ("short", "short_test_interval_days")):
                interval = self.cfg[key]
                if interval <= 0:
                    continue
                k = f"{d['serial']}:{kind}"
                last = state.get(k)
                if last is None:
                    # first run: short tests ~6 h after discovery, extended tests one drive per day
                    first_in = 6 * 3600 if kind == "short" else (i + 1) * 86400
                    state[k] = ts - interval * 86400 + first_in
                    changed = True
                    continue
                if ts - last < interval * 86400 or testing(d):
                    continue
                if kind == "long" and not (long_slot_free and self.in_window(ts)):
                    continue
                ok, _ = self.run_selftest(d["serial"], kind)
                changed = True
                if not ok:
                    state[k] = ts - interval * 86400 + 86400     # try again tomorrow
                    continue
                state[k] = ts
                if kind == "long":
                    long_slot_free = False
                    state[f"{d['serial']}:short"] = ts           # an extended test covers the short one
                break
        if changed:
            self.db.kv_set("selftest_schedule", state)

    # ------------------------------------------------------------------
    def trigger(self):
        self._wake.set()

    def loop(self):
        last_prune = 0
        while True:
            try:
                interval = max(5, self.cfg["scan_interval_min"]) * 60
                if time.time() - self.last_scan >= interval or self._wake.is_set():
                    self._wake.clear()
                    self.scan()
                due = self._due_benchmarks()
                if due:
                    self._bench_batch_active = True
                    threading.Thread(target=self._bench_batch, args=(due,), daemon=True).start()
                if time.time() - last_prune > 86400:
                    self.db.prune(self.cfg["retention_days"])
                    last_prune = time.time()
            except Exception:  # noqa: BLE001 - the scheduler must never die
                log.exception("monitor loop error")
            self._wake.wait(30)
