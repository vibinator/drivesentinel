"""DriveSentinel test suite (standard library only).

Run from the repo root:   python -m unittest discover -s tests -v
Covers the parsers, scoring, scheduling and the HTTP API end to end in demo mode.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="ds-test-")
os.environ["DATA_DIR"] = TMP
os.environ["MOCK"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import analyzer  # noqa: E402
import bench  # noqa: E402
import features  # noqa: E402
import issues  # noqa: E402
import ml  # noqa: E402
import mock  # noqa: E402
import peers  # noqa: E402
import raid  # noqa: E402
import reliability  # noqa: E402
import smart  # noqa: E402
from config import Config  # noqa: E402
from db import DB  # noqa: E402
from monitor import Monitor  # noqa: E402

CFG = Config()
DAY = 86400


def attr(aid, name, value=100, worst=100, thresh=0, raw=0):
    return {"id": aid, "name": name, "value": value, "worst": worst, "thresh": thresh, "raw": raw,
            "raw_str": str(raw), "when_failed": "", "prefailure": False}


def hdd(attrs=(), **kw):
    base = {"serial": "T1", "device": "/dev/sdx", "model": "ST8000VN004-2M2101", "protocol": "ATA",
            "is_ssd": False, "smart_passed": True, "temperature": 35, "power_on_hours": 20000,
            "attributes": list(attrs) or [attr(5, "Reallocated_Sector_Ct", thresh=10)],
            "nvme": None, "scsi": None, "selftest": {"log": []}, "firmware": "SC60"}
    base.update(kw)
    return base


def snap(ts, summary):
    return ts, smart.metrics_from(summary)


class SmartNormalize(unittest.TestCase):
    def test_ata_fields(self):
        raw = mock.read_device("/dev/sdb")
        s = smart.normalize(raw, "/dev/sdb", "sat")
        self.assertEqual(s["protocol"], "ATA")
        self.assertFalse(s["is_ssd"])
        self.assertTrue(s["attributes"])
        self.assertIsInstance(s["power_on_hours"], int)

    def test_nvme_fields(self):
        s = smart.normalize(mock.read_device("/dev/nvme0"), "/dev/nvme0", "nvme")
        self.assertTrue(s["is_ssd"])
        self.assertEqual(s["nvme"]["available_spare"], 100)

    def test_standby_is_sleeping(self):
        raw = {"smartctl": {"messages": [{"string": "Device is in STANDBY mode, exit(2)"}]}}
        self.assertTrue(smart.normalize(raw, "/dev/sda", None).get("sleeping"))

    def test_no_serial_is_skipped_not_error(self):
        s = smart.normalize({"smartctl": {"messages": []}, "device": {}}, "/dev/sdz", None)
        self.assertIn("skip", s)

    def test_error_passthrough(self):
        self.assertIn("error", smart.normalize({"_error": "boom"}, "/dev/sda", None))

    def test_zero_temperature_means_unknown(self):
        raw = {"serial_number": "X", "device": {"protocol": "SCSI"}, "temperature": {"current": 0}}
        self.assertIsNone(smart.normalize(raw, "/dev/sda", "scsi")["temperature"])

    def test_raw_string_parsing(self):
        raw = {"serial_number": "X", "device": {"protocol": "ATA"}, "ata_smart_attributes": {"table": [
            {"id": 194, "name": "Temperature_Celsius", "value": 60, "worst": 50, "thresh": 0,
             "raw": {"value": 1234567, "string": "41 (Min/Max 20/55)"}},
            {"id": 9, "name": "Power_On_Hours", "value": 90, "worst": 90, "thresh": 0,
             "raw": {"value": 99, "string": "8760h+12m+01.2s"}}]}}
        s = smart.normalize(raw, "/dev/sda", "sat")
        self.assertEqual(s["temperature"], 41)
        self.assertEqual(s["power_on_hours"], 8760)

    def test_sata_behind_scsi_retries_with_sat(self):
        scsi = {"device": {"protocol": "SCSI"}, "serial_number": "Z", "temperature": {"current": 0}}
        sat = {"device": {"protocol": "ATA"}, "serial_number": "Z", "model_name": "ST8000DM004",
               "ata_smart_attributes": {"table": []}, "temperature": {"current": 33}}
        real = smart._run
        smart._run = lambda args, timeout=90: ((sat if "sat" in args else scsi), None)
        try:
            raw = smart.read_device("/dev/sda", "scsi")
        finally:
            smart._run = real
        s = smart.normalize(raw, "/dev/sda", "scsi")
        self.assertEqual((s["protocol"], s["dev_type"], s["temperature"]), ("ATA", "sat", 33))

    def test_real_sas_stays_scsi(self):
        scsi = {"device": {"protocol": "SCSI"}, "serial_number": "Z", "scsi_grown_defect_list": 3}
        real = smart._run
        smart._run = lambda args, timeout=90: ((None if "sat" in args else scsi), "fail")
        try:
            raw = smart.read_device("/dev/sda", "scsi")
        finally:
            smart._run = real
        s = smart.normalize(raw, "/dev/sda", "scsi")
        self.assertEqual(s["protocol"], "SCSI")
        self.assertEqual(s["scsi"]["grown_defects"], 3)


class Analyzer(unittest.TestCase):
    def test_healthy_drive_is_good(self):
        r = analyzer.analyze(hdd(), [], CFG)
        self.assertEqual(r["status"], "good")
        self.assertGreaterEqual(r["health"], 95)

    def test_smart_failed_is_failing(self):
        self.assertEqual(analyzer.analyze(hdd(smart_passed=False), [], CFG)["status"], "failing")

    def test_attribute_below_threshold_is_failing(self):
        r = analyzer.analyze(hdd([attr(5, "Reallocated_Sector_Ct", value=5, worst=5, thresh=10, raw=4000)]), [], CFG)
        self.assertEqual(r["status"], "failing")

    def test_pending_sectors_escalate(self):
        one = analyzer.analyze(hdd([attr(197, "Current_Pending_Sector", raw=1)]), [], CFG)
        many = analyzer.analyze(hdd([attr(197, "Current_Pending_Sector", raw=500),
                                     attr(198, "Offline_Uncorrectable", raw=200),
                                     attr(5, "Reallocated_Sector_Ct", thresh=10, raw=900)]), [], CFG)
        self.assertEqual(one["status"], "warning")
        self.assertEqual(many["status"], "critical")
        self.assertGreater(many["risk"], one["risk"])

    def test_crc_errors_alone_stay_good(self):
        r = analyzer.analyze(hdd([attr(199, "UDMA_CRC_Error_Count", raw=40)]), [], CFG)
        self.assertEqual(r["status"], "good")

    def test_seagate_packed_command_timeout_ignored(self):
        r = analyzer.analyze(hdd([attr(188, "Command_Timeout", raw=0x000100010000)]), [], CFG)
        self.assertFalse([i for i in r["issues"] if "timeout" in i["text"].lower()])

    def test_noisy_attribute_never_projects_failure(self):
        """Seagate read-error-rate / airflow-temperature bounce around; they must not predict failure."""
        now = time.time()
        hist = []
        for i in range(200):
            v = 84 - (i % 17) - i // 12
            hist.append(snap(now - (200 - i) * 3600, hdd([
                attr(1, "Raw_Read_Error_Rate", value=v, worst=60, thresh=44, raw=i * 99991),
                attr(190, "Airflow_Temperature_Cel", value=70 - i // 10, worst=40, thresh=40, raw=30 + i // 10)])))
        cur = hdd([attr(1, "Raw_Read_Error_Rate", value=66, worst=60, thresh=44),
                   attr(190, "Airflow_Temperature_Cel", value=50, worst=40, thresh=40, raw=50)])
        r = analyzer.analyze(cur, hist + [snap(now, cur)], CFG)
        self.assertEqual(r["predictions"], [])
        self.assertEqual(r["status"], "good")

    def test_steady_reallocation_decline_projects_failure(self):
        now = time.time()
        hist = []
        for d in range(10, 0, -1):
            hist.append(snap(now - d * DAY, hdd([attr(5, "Reallocated_Sector_Ct", value=100 - (10 - d) * 3,
                                                      thresh=10, raw=(10 - d) * 24)])))
        cur = hdd([attr(5, "Reallocated_Sector_Ct", value=70, worst=70, thresh=10, raw=240)])
        r = analyzer.analyze(cur, hist + [snap(now, cur)], CFG)
        self.assertIsNotNone(r["eta_days"])
        self.assertLess(r["eta_days"], 40)
        self.assertIn(r["status"], ("critical", "warning"))
        self.assertTrue(any("deteriorating" in i["text"] for i in r["issues"]))

    def test_one_day_of_history_makes_no_projection(self):
        now = time.time()
        hist = [snap(now - h * 3600, hdd([attr(5, "Reallocated_Sector_Ct", value=100 - (30 - h) // 10, thresh=10)]))
                for h in range(30, 0, -1)]
        cur = hdd([attr(5, "Reallocated_Sector_Ct", value=97, thresh=10)])
        self.assertIsNone(analyzer.analyze(cur, hist + [snap(now, cur)], CFG)["eta_days"])

    def nvme(self, used, poh=34000):
        return {"serial": "N1", "device": "/dev/nvme0", "model": "WD Red SN700 1000GB", "protocol": "NVMe",
                "is_ssd": True, "smart_passed": True, "temperature": 38, "power_on_hours": poh, "attributes": [],
                "nvme": {"critical_warning": 0, "available_spare": 100, "available_spare_threshold": 10,
                         "percentage_used": used, "media_errors": 0, "data_units_written": 9e7,
                         "data_units_read": 5e7}, "scsi": None, "selftest": {"log": []}}

    def test_wear_step_does_not_cause_false_alarm(self):
        """A 1% wear tick during a short history window must not predict imminent wear-out."""
        now = time.time()
        hist = [snap(now - 2 * DAY, self.nvme(19)), snap(now - DAY, self.nvme(19)), snap(now - 3600, self.nvme(20))]
        r = analyzer.analyze(self.nvme(20), hist + [snap(now, self.nvme(20))], CFG)
        self.assertEqual(r["status"], "good")
        self.assertGreater(r["eta_days"], 5 * 365)

    def test_nvme_critical_warning(self):
        s = self.nvme(20)
        s["nvme"]["critical_warning"] = 0x04
        self.assertEqual(analyzer.analyze(s, [], CFG)["status"], "failing")

    def test_nvme_worn_out(self):
        self.assertIn(analyzer.analyze(self.nvme(101), [], CFG)["status"], ("warning", "critical"))

    def test_failed_selftest(self):
        s = hdd(selftest={"log": [{"type": "Short", "result": "read failure", "passed": False}]})
        self.assertIn(analyzer.analyze(s, [], CFG)["status"], ("warning", "critical"))

    def test_limited_data_flagged(self):
        s = hdd(protocol="SCSI", smart_passed=None, power_on_hours=None, temperature=None)
        s["attributes"] = []
        r = analyzer.analyze(s, [], CFG)
        self.assertTrue(r["limited"])
        self.assertEqual(r["reliability"]["age_phase"], "age unknown")
        self.assertEqual(r["reliability"]["age_multiplier"], 1.0)

    def test_raid_exposure_escalates(self):
        s = hdd([attr(5, "Reallocated_Sector_Ct", thresh=10, raw=8)])
        self.assertEqual(analyzer.analyze(s, [], CFG)["status"], "warning")
        s["raid"] = [{"array": "md1", "level": "raid5", "status": "degraded", "role": "data",
                      "redundancy_left": 0, "member_failed": False, "spare": False}]
        self.assertEqual(analyzer.analyze(s, [], CFG)["status"], "critical")

    def test_load_cycle_and_workload_findings(self):
        s = hdd([attr(193, "Load_Cycle_Count", raw=700000),
                 attr(241, "Total_LBAs_Written", raw=int(900e12 / 512))], model="ST8000DM004-2CX188",
                power_on_hours=17532)
        r = analyzer.analyze(s, [], CFG)
        text = " ".join(i["text"] for i in r["issues"])
        self.assertIn("load/unload", text)
        self.assertIn("Workload", text)
        self.assertEqual(r["usage"]["drive_class"], "desktop")
        self.assertAlmostEqual(r["usage"]["tb_written"], 900, delta=1)


class Reliability(unittest.TestCase):
    CASES = {
        "ST8000DM004-2CX188": ("BarraCuda", "desktop"), "WDC WD80EZAZ-11TDBA0": ("white label", "NAS"),
        "TOSHIBA HDWG480": ("N300", "NAS"), "WDC WD80EFZZ-68BTXN0": ("Red", "NAS"),
        "ST16000NE000-2RW103": ("IronWolf Pro", "NAS Pro"), "ST4000VN008-2DR166": ("IronWolf", "NAS"),
        "WDC WD40EZRZ-00GXCB0": ("Blue", "desktop"), "WDC WD102KFBX-68M95N0": ("Red Pro", "NAS Pro"),
    }

    def test_family_and_class(self):
        for model, (name, cls) in self.CASES.items():
            s = {"model": model, "power_on_hours": 20000}
            self.assertIn(name, reliability.lookup(s)["matched"], model)
            self.assertEqual(reliability.drive_class(s)["name"], cls, model)

    def test_backblaze_exact_match_beats_family(self):
        r = reliability.lookup({"model": "ST16000NM001G-2KK103", "power_on_hours": 20000})
        self.assertEqual(r["match_type"], "model")
        self.assertGreater(r["drives"], 1000)

    def test_similar_model_number_does_not_match(self):
        r = reliability.lookup({"model": "ST16000NM001GX", "power_on_hours": 20000})
        self.assertIsNone(r["drives"])       # not matched to the Backblaze entry for ST16000NM001G

    def test_age_curve(self):
        afr = lambda h: reliability.lookup({"model": "Unknown Disk", "power_on_hours": h})["adjusted_afr"]  # noqa: E731
        self.assertGreater(afr(100), afr(20000))
        self.assertGreater(afr(70000), afr(50000))
        self.assertGreater(afr(50000), afr(20000))

    def test_nvme_default(self):
        r = reliability.lookup({"model": "Mystery NVMe", "protocol": "NVMe", "is_ssd": True, "power_on_hours": 5})
        self.assertEqual(r["match_type"], "default")

    def test_all_regexes_compile(self):
        import re
        base = os.path.join(os.path.dirname(__file__), "..", "app", "data")
        for fname, keys in (("model_stats.json", ("models", "families")), ("backblaze_stats.json", ("models",)),
                            ("known_issues.json", ("issues",))):
            data = json.load(open(os.path.join(base, fname), encoding="utf-8"))
            for key in keys:
                for e in data[key]:
                    re.compile(e["match"])
                    if key != "issues":
                        self.assertTrue(0 <= e["afr"] <= 60, e)


class KnownIssues(unittest.TestCase):
    def test_firmware_specific(self):
        hit = issues.lookup({"model": "Samsung SSD 980 PRO 2TB", "firmware": "3B2QGXA7"})
        self.assertEqual(hit[0]["severity"], "critical")
        self.assertEqual(issues.lookup({"model": "Samsung SSD 980 PRO 2TB", "firmware": "5B2QGXA7"}), [])

    def test_smr(self):
        self.assertTrue(any("SMR" in i["title"] for i in issues.lookup({"model": "ST8000DM004-2CX188"})))
        self.assertTrue(any("SMR" in i["title"] for i in issues.lookup({"model": "WDC WD40EFAX-68JH4N0"})))
        self.assertEqual(issues.lookup({"model": "WDC WD40EFRX-68N32N0"}), [])
        self.assertEqual(issues.lookup({"model": "WDC WD80EZAZ-11TDBA0"}), [])


class ML(unittest.TestCase):
    def p(self, **raw):
        attrs = [attr(int(k), str(k), raw=v) for k, v in raw.items()]
        out = ml.predict(hdd(attrs), 1.2)
        return out["p_horizon"]

    def test_model_loads_and_matches_features(self):
        m = ml.load()
        self.assertEqual(m["features"], features.FEATURES)
        self.assertGreater(m["auc"], 0.8)

    def test_monotonic_in_errors(self):
        clean, some, bad = self.p(**{"9": 20000}), self.p(**{"9": 20000, "197": 8}), \
            self.p(**{"9": 20000, "197": 800, "5": 3000, "187": 200})
        self.assertLess(clean, 0.005)
        self.assertGreater(some, clean * 5)
        self.assertGreater(bad, some)
        self.assertLess(bad, 1.0)

    def test_not_applied_to_ssd(self):
        self.assertIsNone(ml.predict(hdd(is_ssd=True), 1.0))

    def test_feature_vector_shape_and_packed_timeout(self):
        v = features.vector({188: 0x000200020005, 194: 200}, 1.5)
        self.assertEqual(len(v), len(features.FEATURES))
        self.assertAlmostEqual(v[2], features._l(5))
        self.assertEqual(v[11], 35.0)       # implausible temperature -> neutral value


class Raid(unittest.TestCase):
    def by_name(self, text):
        return {a["name"]: a for a in raid.parse(text)}

    def test_healthy_and_qnap_system_arrays(self):
        a = self.by_name(mock.MDSTAT_HEALTHY)
        self.assertEqual(a["md1"]["status"], "healthy")
        self.assertEqual(a["md1"]["redundancy_left"], 1)
        self.assertEqual((a["md9"]["role"], a["md9"]["status"]), ("system", "healthy"))   # 64-slot mirror
        self.assertEqual(a["md322"]["role"], "swap")

    def test_degraded(self):
        a = self.by_name(mock.MDSTAT_DEGRADED)["md1"]
        self.assertEqual((a["status"], a["redundancy_left"], a["failed_members"]), ("degraded", 0, ["sdb"]))

    def test_rebuild_progress(self):
        a = raid.parse("md1 : active raid6 sda3[0] sdb3[1] sdc3[2] sdd3[4]\n"
                       "      100 blocks super 1.0 level 6, 512k chunk [5/4] [UUUU_]\n"
                       "      [==>....]  recovery = 12.6% (1/9) finish=321.5min speed=150000K/sec\n")[0]
        self.assertEqual((a["status"], a["sync_pct"], a["sync_eta_min"], a["redundancy_left"]),
                         ("rebuilding", 12.6, 321.5, 1))

    def test_raid0_member_loss_is_failed(self):
        a = raid.parse("md5 : active raid0 sda3[0] sdb3[1](F)\n      100 blocks super 1.2 [2/1] [U_]\n")[0]
        self.assertEqual(a["status"], "failed")

    def test_scrub_is_syncing_not_degraded(self):
        a = raid.parse("md1 : active raid5 sda3[0] sdb3[1] sdc3[2]\n      100 blocks [3/3] [UUU]\n"
                       "      [>....]  check =  3.0% (1/9) finish=99.0min speed=1000K/sec\n")[0]
        self.assertEqual(a["status"], "syncing")
        self.assertTrue(raid.busy([a]))

    def test_inactive_array(self):
        self.assertEqual(raid.parse("md7 : inactive sdc3[2](S)\n      100 blocks super 1.0\n")[0]["status"], "failed")

    def test_disk_names(self):
        self.assertEqual(raid.disk_of("nvme0n1p3"), "nvme0")
        self.assertEqual(raid.disk_of("sdaa12"), "sdaa")
        self.assertEqual(raid.disk_key("/dev/nvme1n1"), "nvme1")
        self.assertEqual(raid.disk_key("/dev/sdb"), "sdb")

    def test_empty_and_garbage(self):
        self.assertEqual(raid.parse(""), [])
        self.assertEqual(raid.parse("Personalities : [raid1]\nunused devices: <none>\n"), [])

    def test_zfs_state(self):
        base = tempfile.mkdtemp(dir=TMP)
        for name, st in (("zpool1", "ONLINE"), ("zpool2", "DEGRADED"), ("zpool3", "SUSPENDED")):
            os.makedirs(os.path.join(base, name))
            open(os.path.join(base, name, "state"), "w").write(st + "\n")
        got = {p["name"]: p["status"] for p in raid.read_zfs(base)}
        self.assertEqual(got, {"zpool1": "healthy", "zpool2": "degraded", "zpool3": "failed"})
        self.assertEqual(raid.read_zfs(os.path.join(base, "missing")), [])


class Bench(unittest.TestCase):
    def run_(self, seq, lat=10.0):
        return {"seq_avg": seq, "rand_lat_ms": lat, "seq_mbps": [seq, seq, seq]}

    def test_no_data(self):
        self.assertIsNone(bench.evaluate([]))

    def test_single_run_never_alerts(self):
        self.assertEqual(bench.evaluate([self.run_(50)])["issues"], [])

    def test_drop_levels(self):
        base = [self.run_(200), self.run_(205), self.run_(195)]
        self.assertEqual(bench.evaluate(base + [self.run_(190)])["issues"], [])
        self.assertEqual(bench.evaluate(base + [self.run_(140)])["issues"][0][0], "warning")
        self.assertEqual(bench.evaluate(base + [self.run_(90)])["issues"][0][0], "critical")
        self.assertEqual(bench.evaluate(base + [self.run_(400)])["score"], 100)

    def test_latency_regression(self):
        base = [self.run_(200), self.run_(200), self.run_(200)]
        ev = bench.evaluate(base + [self.run_(200, lat=40.0)])
        self.assertTrue(any("latency" in t for _, t in ev["issues"]))

    def test_nvme_block_device_name(self):
        self.assertEqual(bench.block_device("/dev/nvme0"), "/dev/nvme0n1")
        self.assertEqual(bench.block_device("/dev/sda"), "/dev/sda")

    def test_real_read_on_a_file(self):
        """Exercise the actual read loop against a temp file (no O_DIRECT needed)."""
        path = os.path.join(TMP, "disk.img")
        with open(path, "wb") as f:
            f.write(os.urandom(1 << 20) * 40)
        if not hasattr(os, "preadv"):
            self.skipTest("os.preadv unavailable on this platform")
        res = bench.run(path, seq_mb=8, random_reads=50)
        self.assertEqual(len(res["seq_mbps"]), 3)
        self.assertGreater(res["seq_avg"], 0)
        self.assertGreater(res["rand_iops"], 0)


class EndToEnd(unittest.TestCase):
    """Boot the whole app in demo mode on a real port and drive it through the HTTP API."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.db = DB(os.path.join(TMP, "e2e.db"))
        cls.mon = Monitor(CFG, cls.db, mock)
        mock.T0 = time.time() - 30 * DAY
        last_bench = 0
        for ts in range(int(mock.T0), int(time.time()) - 3600, 6 * 3600):
            cls.mon.scan(now=ts, quiet=True)
            if ts - last_bench >= 7 * DAY:
                last_bench = ts
                for d in cls.db.list_drives():
                    cls.mon.run_benchmark(d["serial"], now=ts, quiet=True)
        cls.mon.scan()
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler({"cfg": CFG, "db": cls.db,
                                                                               "monitor": cls.mon}))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None, ctype="application/json", raw=False):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": ctype} if body is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def drives(self):
        return {d["device"]: d for d in self.call("/api/drives")[1]}

    def test_scan_has_no_errors(self):
        st = self.call("/api/status")[1]
        self.assertEqual(st["errors"], [])
        self.assertEqual(st["drive_count"], 6)
        self.assertTrue(st["ml"] and st["dataset"])

    def test_expected_verdicts(self):
        d = self.drives()
        self.assertEqual(d["/dev/sda"]["status"], "good")
        self.assertEqual(d["/dev/sdb"]["status"], "critical")
        self.assertEqual(d["/dev/sdc"]["status"], "good")
        self.assertEqual(d["/dev/sdd"]["status"], "good")          # SSD wearing slowly: no false alarm
        self.assertEqual(d["/dev/nvme0"]["status"], "warning")     # known firmware bug
        self.assertLess(d["/dev/sdb"]["perf_score"], 75)
        self.assertGreater(d["/dev/sdb"]["ml_p"], d["/dev/sda"]["ml_p"] * 10)

    def test_drive_detail_and_history(self):
        serial = self.drives()["/dev/sdb"]["serial"]
        code, det = self.call(f"/api/drives/{serial}")
        self.assertEqual(code, 200)
        for key in ("card", "summary", "analysis", "events", "peers", "benchmarks", "bench_eval"):
            self.assertIn(key, det)
        self.assertTrue(det["analysis"]["predictions"])
        h = self.call(f"/api/drives/{serial}/history?days=30")[1]
        self.assertGreater(len(h["ts"]), 50)
        self.assertEqual(len(h["ts"]), len(h["temp"]))
        self.assertEqual(self.call(f"/api/drives/{serial}/history?days=abc")[0], 200)
        self.assertEqual(self.call(f"/api/drives/{serial}/raw")[0], 200)

    def test_peer_outlier(self):
        serial = self.drives()["/dev/sde"]["serial"]
        ins = self.call(f"/api/drives/{serial}")[1]["peers"]["insights"]
        self.assertTrue(any(i["metric"] == "temp" and i["level"] == "warning" for i in ins))

    def test_unknown_routes(self):
        self.assertEqual(self.call("/api/drives/NOPE")[0], 404)
        self.assertEqual(self.call("/api/nope")[0], 404)
        self.assertEqual(self.call("/static/../config.py")[0], 404)
        self.assertEqual(self.call("/api/drives/NOPE/selftest", {"type": "short"})[0], 500)

    def test_post_requires_json_content_type(self):
        """Cross-site form posts (CSRF) must be rejected."""
        self.assertEqual(self.call("/api/settings", {"scan_interval_min": 1}, ctype="text/plain")[0], 415)
        self.assertEqual(self.call("/api/settings")[1]["scan_interval_min"], 60)

    def test_settings_roundtrip_and_secret_masking(self):
        code, cfg = self.call("/api/settings", {"hdd_temp_warn": "47", "smtp_pass": "hunter2", "bogus": 1})
        self.assertEqual((code, cfg["hdd_temp_warn"], cfg["smtp_pass"]), (200, 47, "********"))
        self.call("/api/settings", {"smtp_pass": "********"})
        self.assertEqual(CFG["smtp_pass"], "hunter2")
        self.call("/api/settings", {"hdd_temp_warn": 50, "smtp_pass": ""})
        self.assertEqual(self.call("/api/settings", [1, 2])[0], 200)      # non-object body is ignored

    def test_label(self):
        serial = self.drives()["/dev/sda"]["serial"]
        self.assertEqual(self.call(f"/api/drives/{serial}/label", {"label": "Bay 1"})[0], 200)
        self.assertEqual(self.drives()["/dev/sda"]["label"], "Bay 1")
        self.call(f"/api/drives/{serial}/label", {"label": ""})
        self.assertIsNone(self.drives()["/dev/sda"]["label"])

    def test_selftest_and_validation(self):
        serial = self.drives()["/dev/sda"]["serial"]
        self.assertTrue(self.call(f"/api/drives/{serial}/selftest", {"type": "short"})[1]["ok"])
        self.assertEqual(self.call(f"/api/drives/{serial}/selftest", {"type": "rm -rf"})[0], 400)
        self.call(f"/api/drives/{serial}/selftest", {"type": "abort"})

    def test_arrays_and_metrics_and_diagnostics(self):
        arr = self.call("/api/arrays")[1]
        self.assertTrue(any(a["name"] == "md1" for a in arr["arrays"]))
        self.assertIn("sda", arr["drives"])
        text = self.call("/metrics", raw=True)[1].decode()
        self.assertIn("drivesentinel_health_percent{", text)
        self.assertIn("drivesentinel_failure_probability_30d{", text)
        self.assertEqual(self.call("/api/diagnostics")[0], 200)
        self.assertEqual(self.call("/healthz")[1], {"ok": True})

    def test_static_ui_served(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            code, body = self.call(path, raw=True)
            self.assertEqual(code, 200)
            self.assertGreater(len(body), 500)

    def test_alert_test_without_channels(self):
        self.assertEqual(self.call("/api/alerts/test", {})[0], 400)

    def test_events_logged(self):
        ev = self.call("/api/events?limit=5000")[1]
        titles = " | ".join(e["title"] for e in ev)
        for needle in ("New drive detected", "increased on", "stands out from its peers", "Storage array"):
            self.assertIn(needle, titles)
        self.assertEqual(self.call("/api/events?limit=xyz")[0], 200)


class Scheduling(unittest.TestCase):
    def setUp(self):
        self.db = DB(os.path.join(TMP, f"sched-{time.time_ns()}.db"))
        self.started = []
        backend = type("B", (), {
            "scan_devices": staticmethod(mock.scan_devices), "read_device": staticmethod(mock.read_device),
            "mdstat_text": staticmethod(mock.mdstat_text), "run_benchmark": staticmethod(mock.run_benchmark),
            "start_selftest": staticmethod(lambda dev, t, kind: (self.started.append((dev, kind)), (True, "ok"))[1])})
        mock._tests.clear()
        self.mon = Monitor(CFG, self.db, backend)
        self.mon.scan()

    def test_extended_tests_are_staggered_one_at_a_time(self):
        """Regression: every drive used to start its extended test at the same moment."""
        now = int(time.time())
        self.mon._check_selftests(now)                       # seeds the schedule
        self.assertEqual(self.started, [])
        self.mon._check_selftests(now + 40 * DAY)            # everything is overdue
        self.assertEqual(len([s for s in self.started if s[1] == "long"]), 1)
        self.assertEqual(len([s for s in self.started if s[1] == "short"]), 5)

    def test_first_extended_tests_spread_over_days(self):
        now = int(time.time())
        self.mon._check_selftests(now)
        state = self.db.kv_get("selftest_schedule")
        due = sorted(v + 30 * DAY - now for k, v in state.items() if k.endswith(":long"))
        self.assertEqual([round(d / DAY) for d in due], [1, 2, 3, 4, 5, 6])

    def test_maintenance_window(self):
        t = lambda h, m=0: time.mktime(time.localtime()[:3] + (h, m, 0, 0, 0, -1))  # noqa: E731
        CFG.update({"maintenance_window": "01:00-06:00"})
        try:
            self.assertTrue(self.mon.in_window(t(3)))
            self.assertFalse(self.mon.in_window(t(12)))
            CFG.update({"maintenance_window": "22:00-04:30"})
            self.assertTrue(self.mon.in_window(t(23)))
            self.assertTrue(self.mon.in_window(t(4, 15)))
            self.assertFalse(self.mon.in_window(t(5)))
            CFG.update({"maintenance_window": "nonsense"})
            self.assertTrue(self.mon.in_window(t(12)))
        finally:
            CFG.update({"maintenance_window": ""})

    def test_alert_cooldown(self):
        sent = []
        import alerts
        real = alerts.send
        alerts.send = lambda *a, **k: sent.append(a)
        try:
            for _ in range(5):
                self.mon.notify("warning", "Reallocated sectors increased", "1 -> 2", "S1", cooldown_key="S1:5")
            self.mon.notify("warning", "other", "x", "S1")
            time.sleep(0.3)
        finally:
            alerts.send = real
        self.assertEqual(len(sent), 2)
        self.assertEqual(len([e for e in self.db.events(100) if e["title"].startswith("Reallocated")]), 5)

    def test_missing_drive_and_return(self):
        real = mock.scan_devices
        self.mon.backend.scan_devices = staticmethod(lambda s="": [d for d in real() if d[0] != "/dev/sdc"])
        self.mon.scan()
        states = {d["device"]: d["state"] for d in self.db.list_drives()}
        self.assertEqual(states["/dev/sdc"], "missing")
        self.mon.backend.scan_devices = staticmethod(real)
        self.mon.scan()
        self.assertEqual({d["device"]: d["state"] for d in self.db.list_drives()}["/dev/sdc"], "online")
        titles = [e["title"] for e in self.db.events(200)]
        self.assertTrue(any("not detected" in t for t in titles) and any("reconnected" in t for t in titles))

    def test_raid_source_outage_is_not_reported_as_pool_loss(self):
        self.mon.backend.mdstat_text = staticmethod(lambda ts=None: "")
        self.mon.scan()
        self.assertFalse([e for e in self.db.events(200) if "disappeared" in e["title"]])

    def test_prune_thins_old_snapshots(self):
        serial = self.db.list_drives()[0]["serial"]
        old = int(time.time()) - 60 * DAY
        for i in range(48):
            self.db.add_snapshot(serial, old + i * 3600, {"temp": 30})
        before = len(self.db.history(serial, 0))
        self.db.prune(365)
        after = len(self.db.history(serial, 0))
        self.assertLess(after, before - 30)
        self.assertGreaterEqual(after, 8)

    def test_loop_survives_exceptions(self):
        boom = {"n": 0}

        def bad():
            boom["n"] += 1
            raise RuntimeError("boom")
        self.mon._due_benchmarks = bad
        threading.Thread(target=self.mon.loop, daemon=True).start()
        self.mon.trigger()
        time.sleep(1.0)
        self.mon.trigger()
        time.sleep(1.0)
        self.assertGreaterEqual(boom["n"], 2)


class PeerAnalysis(unittest.TestCase):
    def test_single_drive_has_no_group(self):
        db = DB(os.path.join(TMP, "peer.db"))
        s = hdd()
        db.upsert_drive("T1", s, analyzer.analyze(s, [], CFG), {}, int(time.time()))
        rep = peers.analyze(db.list_drives(), db, {})
        self.assertIsNone(rep["T1"]["group"])


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
