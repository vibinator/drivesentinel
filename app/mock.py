"""Simulated drives producing smartctl-style JSON. Enabled with MOCK=1 (for demo/testing)."""
import random
import time

T0 = time.time() - 30 * 86400   # overwritten by the monitor so the simulation is stable
_tests = {}                     # dev -> (kind, started_ts)

DRIVES = [
    {"dev": "/dev/sda", "model": "WDC WD80EFZZ-68BTXN0", "family": "Western Digital Red",
     "serial": "WD-CA1XK0LM", "fw": "81.00A81", "bytes": 8001563222016, "rpm": 5400,
     "poh0": 14200, "kind": "healthy"},
    {"dev": "/dev/sdb", "model": "ST8000VN004-2M2101", "family": "Seagate IronWolf",
     "serial": "WSD4R9KX", "fw": "SC60", "bytes": 8001563222016, "rpm": 7200,
     "poh0": 31800, "kind": "degrading"},
    {"dev": "/dev/sdc", "model": "TOSHIBA HDWQ140", "family": "Toshiba N300",
     "serial": "X8E3K0AFFAYG", "fw": "FJ1M", "bytes": 4000787030016, "rpm": 7200,
     "poh0": 53900, "kind": "old"},
    {"dev": "/dev/sdd", "model": "Samsung SSD 870 EVO 1TB", "family": "Samsung based SSDs",
     "serial": "S6PTNM0T812345K", "fw": "SVT02B6Q", "bytes": 1000204886016, "rpm": 0,
     "poh0": 9100, "kind": "ssd"},
    {"dev": "/dev/nvme0", "model": "Samsung SSD 980 PRO 2TB", "serial": "S69ENF0R123456A",
     "fw": "3B2QGXA7", "bytes": 2000398934016, "kind": "nvme", "poh0": 7300},
    {"dev": "/dev/sde", "model": "WDC WD80EFZZ-68BTXN0", "family": "Western Digital Red",
     "serial": "WD-CA1XK7QP", "fw": "81.00A81", "bytes": 8001563222016, "rpm": 5400,
     "poh0": 14150, "kind": "hot"},
]

MDSTAT_HEALTHY = """Personalities : [linear] [raid0] [raid1] [raid10] [raid6] [raid5] [raid4] [multipath]
md1 : active raid5 sda3[0] sdb3[1] sdc3[2] sde3[3]
      23421567744 blocks super 1.0 level 5, 512k chunk, algorithm 2 [4/4] [UUUU]

md2 : active raid1 sdd3[0] nvme0n1p3[1]
      966261760 blocks super 1.0 [2/2] [UU]

md322 : active raid1 sde5[3](S) sdc5[2](S) sdb5[1] sda5[0]
      7235136 blocks super 1.0 [2/2] [UU]
      bitmap: 0/1 pages [0KB], 65536KB chunk

md256 : active raid1 sdb2[1] sda2[0]
      530112 blocks super 1.0 [2/2] [UU]

md13 : active raid1 sda4[0] sdb4[1] sdc4[2] sde4[3] sdd4[4] nvme0n1p4[5]
      458880 blocks super 1.0 [64/6] [UUUUUU__________________________________________________________]
      bitmap: 1/1 pages [4KB], 65536KB chunk

md9 : active raid1 sda1[0] sdb1[1] sdc1[2] sde1[3] sdd1[4] nvme0n1p1[5]
      530048 blocks super 1.0 [64/6] [UUUUUU__________________________________________________________]
      bitmap: 1/1 pages [4KB], 65536KB chunk

unused devices: <none>
"""

MDSTAT_DEGRADED = MDSTAT_HEALTHY.replace(
    "sdb3[1] sdc3[2] sde3[3]\n      23421567744 blocks super 1.0 level 5, 512k chunk, algorithm 2 [4/4] [UUUU]",
    "sdb3[1](F) sdc3[2] sde3[3]\n      23421567744 blocks super 1.0 level 5, 512k chunk, algorithm 2 [4/3] [U_UU]")


def mdstat_text(_now=None):
    import os
    return MDSTAT_DEGRADED if os.environ.get("MOCK_RAID") == "degraded" else MDSTAT_HEALTHY


def run_benchmark(dev, now=None):
    now = now or time.time()
    d = next(x for x in DRIVES if x["dev"] == dev)
    rnd = random.Random(int(now) ^ sum(map(ord, d["serial"])))
    days = max(0.0, (now - T0) / 86400.0)
    if d["kind"] == "nvme":
        base, shape, lat = 3300, (1.0, 1.0, 0.98), 0.07
    elif d["kind"] == "ssd":
        base, shape, lat = 540, (1.0, 1.0, 0.99), 0.11
    else:
        base, shape, lat = (205 if d["rpm"] == 5400 else 255), (1.0, 0.82, 0.55), 11.5
    factor = 1.0
    if d["kind"] == "degrading":
        factor = max(0.4, 1 - max(0.0, days - 10) * 0.028)
        lat /= factor ** 1.5
    seq = [round(base * s * factor * rnd.uniform(0.96, 1.03), 1) for s in shape]
    lat_ms = round(lat * rnd.uniform(0.9, 1.15), 3)
    time.sleep(0 if now < time.time() - 60 else 2)   # feel like work when run live
    return {"seq_mbps": seq, "seq_avg": round(sum(seq) / 3, 1), "rand_lat_ms": lat_ms,
            "rand_p95_ms": round(lat_ms * 1.8, 3), "rand_iops": round(1000 / lat_ms),
            "direct_io": True, "duration_s": 42.0}


def _attr(aid, name, value, worst, thresh, raw, prefail=False):
    return {"id": aid, "name": name, "value": value, "worst": worst, "thresh": thresh,
            "when_failed": "", "flags": {"prefailure": prefail},
            "raw": {"value": raw, "string": str(raw)}}


def scan_devices(_setting=""):
    return [(d["dev"], "nvme" if d["kind"] == "nvme" else "sat") for d in DRIVES]


def _selftest_state(dev, now):
    t = _tests.get(dev)
    if not t:
        return None, []
    kind, start = t
    dur = 120 if kind == "short" else 900
    elapsed = now - start
    if elapsed < dur:
        return int(100 - elapsed / dur * 100), []
    return None, [{"type": {"string": "Short offline" if kind == "short" else "Extended offline"},
                   "status": {"string": "Completed without error", "passed": True},
                   "lifetime_hours": None, "_done": start + dur}]


def read_device(dev, _dtype=None, _skip=True, now=None):
    now = now or time.time()
    d = next((x for x in DRIVES if x["dev"] == dev), None)
    if not d:
        return {"_error": "no such device"}
    days = max(0.0, (now - T0) / 86400.0)
    rnd = random.Random(int(now // 3600) * 31 + sum(map(ord, d["serial"])))
    hour = time.localtime(now).tm_hour
    poh = int(d["poh0"] + days * 24)
    temp = (30 if d["kind"] != "nvme" else 41) + rnd.randint(0, 3) + (3 if 9 <= hour <= 22 else 0)
    if d["kind"] == "degrading":
        temp += 6
    if d["kind"] == "hot":
        temp += 11   # e.g. a bay with poor airflow
    remaining, new_log = _selftest_state(dev, now)
    for entry in new_log:
        entry["lifetime_hours"] = int(d["poh0"] + (entry.pop("_done") - T0) / 3600)
    base = {
        "json_format_version": [1, 0],
        "smartctl": {"version": [7, 4], "exit_status": 0, "messages": []},
        "device": {"name": dev, "type": "nvme" if d["kind"] == "nvme" else "sat",
                   "protocol": "NVMe" if d["kind"] == "nvme" else "ATA"},
        "model_name": d["model"], "model_family": d.get("family"), "serial_number": d["serial"],
        "firmware_version": d["fw"], "user_capacity": {"bytes": d["bytes"]},
        "smart_support": {"available": True, "enabled": True},
        "smart_status": {"passed": True},
        "temperature": {"current": temp},
        "power_on_time": {"hours": poh},
        "power_cycle_count": 120 + int(days / 10),
    }
    if d["kind"] == "nvme":
        base["nvme_total_capacity"] = d["bytes"]
        base["nvme_smart_health_information_log"] = {
            "critical_warning": 0, "temperature": temp, "available_spare": 100,
            "available_spare_threshold": 10, "percentage_used": int(31 + days * 0.12),
            "data_units_read": int(9.1e7 + days * 4e5), "data_units_written": int(1.2e8 + days * 6e5),
            "power_cycles": 410, "power_on_hours": poh, "unsafe_shutdowns": 23,
            "media_errors": 0, "num_err_log_entries": 4}
        base["nvme_self_test_log"] = {
            "current_self_test_operation": {"value": 1 if remaining else 0,
                                            "string": "Short self-test in progress" if remaining else
                                            "No self-test in progress"},
            "current_self_test_completion_percent": 100 - (remaining or 0),
            "table": [{"self_test_code": {"string": e["type"]["string"]},
                       "self_test_result": {"value": 0, "string": e["status"]["string"]},
                       "power_on_hours": poh} for e in new_log]}
        return base

    base["rotation_rate"] = d["rpm"]
    base["form_factor"] = {"name": "2.5 inches" if d["kind"] == "ssd" else "3.5 inches"}
    base["interface_speed"] = {"current": {"string": "6.0 Gb/s"}}
    realloc = pending = 0
    if d["kind"] == "degrading":
        # sectors start failing ~20 days into the simulation window and accelerate
        bad_days = max(0.0, days - 10)
        realloc = int(bad_days ** 1.6 * 1.4)
        pending = int(bad_days * 0.6) if bad_days > 3 else 0
    t_attr = 194
    attrs = [
        _attr(1, "Raw_Read_Error_Rate", 100 if d["kind"] != "degrading" else 82, 64, 44, 0, True),
        _attr(3, "Spin_Up_Time", 97, 95, 0, 0, True) if d["rpm"] else None,
        _attr(4, "Start_Stop_Count", 100, 100, 20, 1532),
        _attr(5, "Reallocated_Sector_Ct", max(1, 100 - realloc // 8), max(1, 100 - realloc // 8), 10,
              realloc, True),
        _attr(9, "Power_On_Hours", 100 - min(99, poh // 1000), 100 - min(99, poh // 1000), 0, poh),
        _attr(12, "Power_Cycle_Count", 100, 100, 20, 120),
        _attr(t_attr, "Temperature_Celsius", 100 - temp, 50, 0, temp),
        _attr(197, "Current_Pending_Sector", 100, 100, 0, pending),
        _attr(198, "Offline_Uncorrectable", 100, 100, 0, pending // 3),
        _attr(199, "UDMA_CRC_Error_Count", 200, 200, 0, 12 if d["kind"] == "old" else 0),
    ]
    if d["kind"] == "ssd":
        wear = max(1, int(91 - days * 0.15))
        attrs.append(_attr(177, "Wear_Leveling_Count", wear, wear, 0, 210))
        attrs.append(_attr(241, "Total_LBAs_Written", 99, 99, 0, int(5.1e10 + days * 2e8)))
    if d["rpm"]:
        attrs.append(_attr(10, "Spin_Retry_Count", 100, 100, 97, 0, True))
        attrs.append(_attr(187, "Reported_Uncorrect", 100 - pending // 2, 100, 0, pending // 2))
        attrs.append(_attr(188, "Command_Timeout", 100, 100, 0, 0))
    base["ata_smart_attributes"] = {"table": [a for a in attrs if a]}
    log = [{"type": e["type"], "status": e["status"], "lifetime_hours": e["lifetime_hours"]}
           for e in new_log]
    log.append({"type": {"string": "Short offline"},
                "status": {"string": "Completed without error", "passed": True},
                "lifetime_hours": d["poh0"] - 100})
    base["ata_smart_self_test_log"] = {"standard": {"table": log}}
    base["ata_smart_data"] = {"self_test": {"status": {
        "value": 0xF9 if remaining else 0,
        "string": f"in progress, {remaining}% remaining" if remaining else "completed without error",
        "remaining_percent": remaining}}}
    base["ata_smart_error_log"] = {"summary": {"count": pending // 4}}
    return base


def start_selftest(dev, _dtype, kind):
    if kind == "abort":
        _tests.pop(dev, None)
        return True, "Self-test aborted (simulated)"
    _tests[dev] = (kind, time.time())
    return True, f"{kind.capitalize()} self-test started (simulated)"
