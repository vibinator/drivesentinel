"""smartctl wrapper and normalization of its JSON output (ATA / NVMe / SCSI)."""
import glob
import json
import re
import subprocess

SMARTCTL = "smartctl"


def _run(args, timeout=90):
    try:
        proc = subprocess.run([SMARTCTL, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "smartctl not found"
    except subprocess.TimeoutExpired:
        return None, "smartctl timed out"
    try:
        return json.loads(proc.stdout), None
    except ValueError:
        return None, (proc.stderr or proc.stdout or "").strip()[:500]


def scan_devices(devices_setting=""):
    """Return a list of (device, type) tuples."""
    if devices_setting.strip():
        out = []
        for item in devices_setting.split(","):
            item = item.strip()
            if not item:
                continue
            dev, _, dtype = item.partition(":")
            out.append((dev.strip(), dtype.strip() or None))
        return out
    out, seen = [], set()
    # 1) smartctl's own discovery (also finds RAID-controller passthrough devices)
    for args in (["--scan-open", "-j"], ["--scan", "-j"]):
        data, _ = _run(args, timeout=60)
        for d in (data or {}).get("devices", []):
            name = d.get("name")
            key = (name, d.get("type"))
            if name and key not in seen and name not in {n for n, _ in out}:
                seen.add(key)
                out.append((name, d.get("type")))
    # 2) brute-force fallback: every whole-disk block device the container can see
    #    (QNAP's controllers sometimes hide disks from --scan). Duplicates of the same
    #    physical disk are later collapsed by serial number.
    known = {n for n, _ in out}
    for pattern in ("/dev/sd[a-z]", "/dev/sd[a-z][a-z]", "/dev/nvme[0-9]", "/dev/nvme[0-9][0-9]"):
        for path in sorted(glob.glob(pattern)):
            if path not in known and path + "n1" not in known:
                known.add(path)
                out.append((path, None))
    return out


def _dev_args(dev, dtype):
    args = []
    if dtype and dtype not in ("auto",):
        args += ["-d", dtype]
    return args + [dev]


def read_device(dev, dtype=None, skip_standby=True):
    args = ["-j", "-a"]
    if skip_standby:
        args += ["-n", "standby"]
    data, err = _run(args + _dev_args(dev, dtype))
    # SATA disks behind a SCSI layer (QNAP reports them as "-d scsi") only return SCSI
    # inquiry data: no SMART attributes, temperature 0. Retry with SAT passthrough and
    # keep that result when it yields real ATA SMART data; genuine SAS disks fail it.
    if dtype in (None, "scsi") and data and (data.get("device") or {}).get("protocol") == "SCSI" \
            and "ata_smart_attributes" not in data:
        retry, _ = _run(args + _dev_args(dev, "sat"))
        if retry and ("ata_smart_attributes" in retry or _is_standby(retry)):
            retry["_dev_type"] = "sat"
            return retry
    # If auto-detection could not identify the disk, retry common passthrough types
    # (USB/SATA bridges and some NAS backplanes need -d sat; SAS needs -d scsi).
    if not dtype and (data is None or (not data.get("serial_number") and not _is_standby(data))):
        for alt in ("sat", "scsi", "sat,12"):
            retry, _ = _run(args + _dev_args(dev, alt))
            if retry and (retry.get("serial_number") or _is_standby(retry)):
                retry["_dev_type"] = alt
                return retry
    if data is None:
        return {"_error": err or "no output", "_device": dev}
    return data


def _is_standby(data):
    msgs = " ".join(m.get("string", "") for m in (data or {}).get("smartctl", {}).get("messages", []))
    return "STANDBY" in msgs.upper() or "SLEEP" in msgs.upper()


def start_selftest(dev, dtype, kind):
    if kind not in ("short", "long", "conveyance", "abort"):
        raise ValueError("invalid test type")
    args = ["-j", "-X"] if kind == "abort" else ["-j", "-t", kind]
    data, err = _run(args + _dev_args(dev, dtype), timeout=60)
    if data is None:
        return False, err
    msgs = [m.get("string", "") for m in data.get("smartctl", {}).get("messages", [])]
    rc = data.get("smartctl", {}).get("exit_status", 0)
    return (rc & 0b11) == 0, "; ".join(msgs) or "Self-test command sent"


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

def _first_int(s):
    m = re.match(r"\s*(-?\d+)", str(s))
    return int(m.group(1)) if m else 0


def normalize(raw, dev, dtype):
    """Convert smartctl JSON into a compact, protocol-independent summary."""
    if not raw or "_error" in raw:
        return {"error": (raw or {}).get("_error", "no data"), "device": dev}

    messages = [m.get("string", "") for m in raw.get("smartctl", {}).get("messages", [])]
    joined = " ".join(messages).upper()
    if ("STANDBY" in joined or "SLEEP" in joined) and "smart_status" not in raw:
        return {"sleeping": True, "device": dev,
                "serial": raw.get("serial_number")}

    dtype = raw.get("_dev_type") or dtype   # the type that actually worked
    serial = raw.get("serial_number")
    if not serial:
        # not a SMART-capable disk (e.g. QNAP's internal boot flash / DOM, card readers)
        return {"skip": "; ".join(messages) or "No SMART data / serial number", "device": dev}

    dev_info = raw.get("device", {})
    protocol = dev_info.get("protocol", "ATA")
    rotation = raw.get("rotation_rate")
    is_ssd = protocol == "NVMe" or rotation == 0
    out = {
        "serial": serial,
        "wwn": _wwn(raw.get("wwn")),
        "device": dev,
        "dev_type": dtype or dev_info.get("type"),
        "protocol": protocol,
        "model": raw.get("model_name") or raw.get("scsi_model_name")
                 or " ".join(filter(None, [raw.get("scsi_vendor"), raw.get("scsi_product")])) or "Unknown",
        "family": raw.get("model_family"),
        "firmware": raw.get("firmware_version") or raw.get("scsi_revision"),
        "capacity": (raw.get("user_capacity") or {}).get("bytes")
                    or raw.get("nvme_total_capacity") or 0,
        "rotation_rate": rotation,
        "logical_block_size": raw.get("logical_block_size") or 512,
        "form_factor": (raw.get("form_factor") or {}).get("name"),
        "interface_speed": ((raw.get("interface_speed") or {}).get("current") or {}).get("string"),
        "is_ssd": is_ssd,
        "smart_supported": (raw.get("smart_support") or {}).get("enabled", True),
        "smart_passed": (raw.get("smart_status") or {}).get("passed"),
        "temperature": (raw.get("temperature") or {}).get("current"),
        "power_on_hours": (raw.get("power_on_time") or {}).get("hours"),
        "power_cycles": raw.get("power_cycle_count"),
        "messages": messages,
        "attributes": [],
        "nvme": None,
        "scsi": None,
        "selftest": {"status": None, "running": False, "remaining": None, "log": []},
        "error_log_count": 0,
    }

    # ---- ATA -----------------------------------------------------------
    table = (raw.get("ata_smart_attributes") or {}).get("table", [])
    for a in table:
        raw_obj = a.get("raw") or {}
        raw_str = raw_obj.get("string", str(raw_obj.get("value", 0)))
        out["attributes"].append({
            "id": a.get("id"),
            "name": a.get("name", "Unknown"),
            "value": a.get("value"),
            "worst": a.get("worst"),
            "thresh": a.get("thresh"),
            "raw": _first_int(raw_str),
            "raw_str": raw_str,
            "when_failed": a.get("when_failed", ""),
            "prefailure": (a.get("flags") or {}).get("prefailure", False),
        })
    if not out["temperature"]:          # 0 means "not reported" (no drive runs at 0 C)
        out["temperature"] = None
        for a in out["attributes"]:
            if a["id"] in (194, 190):
                out["temperature"] = a["raw"]
                break
    if out["power_on_hours"] is None:
        for a in out["attributes"]:
            if a["id"] == 9:
                out["power_on_hours"] = a["raw"]

    ata_st = (raw.get("ata_smart_data") or {}).get("self_test", {})
    if ata_st:
        status = ata_st.get("status", {})
        out["selftest"]["status"] = status.get("string")
        out["selftest"]["running"] = bool(status.get("remaining_percent")) and \
            (status.get("value", 0) >> 4) == 0xF
        out["selftest"]["remaining"] = status.get("remaining_percent")
    for row in ((raw.get("ata_smart_self_test_log") or {}).get("standard") or {}).get("table", [])[:20]:
        st = row.get("status", {})
        out["selftest"]["log"].append({
            "type": (row.get("type") or {}).get("string", ""),
            "result": st.get("string", ""),
            "passed": st.get("passed", True),
            "hours": row.get("lifetime_hours"),
        })
    out["error_log_count"] = ((raw.get("ata_smart_error_log") or {}).get("summary") or {}).get("count", 0)

    # ---- NVMe ----------------------------------------------------------
    nv = raw.get("nvme_smart_health_information_log")
    if nv:
        out["nvme"] = {
            "critical_warning": nv.get("critical_warning", 0),
            "available_spare": nv.get("available_spare"),
            "available_spare_threshold": nv.get("available_spare_threshold"),
            "percentage_used": nv.get("percentage_used"),
            "data_units_read": nv.get("data_units_read"),
            "data_units_written": nv.get("data_units_written"),
            "media_errors": nv.get("media_errors", 0),
            "num_err_log_entries": nv.get("num_err_log_entries", 0),
            "unsafe_shutdowns": nv.get("unsafe_shutdowns", 0),
            "power_cycles": nv.get("power_cycles"),
        }
        if out["temperature"] is None:
            out["temperature"] = nv.get("temperature")
        if out["power_on_hours"] is None:
            out["power_on_hours"] = nv.get("power_on_hours")
        out["error_log_count"] = nv.get("num_err_log_entries", 0)
    nv_st = raw.get("nvme_self_test_log")
    if nv_st:
        cur = nv_st.get("current_self_test_operation", {})
        out["selftest"]["running"] = cur.get("value", 0) != 0
        out["selftest"]["remaining"] = (100 - nv_st.get("current_self_test_completion_percent", 0)) \
            if out["selftest"]["running"] else None
        out["selftest"]["status"] = cur.get("string")
        for row in (nv_st.get("table") or [])[:20]:
            res = row.get("self_test_result", {})
            out["selftest"]["log"].append({
                "type": (row.get("self_test_code") or {}).get("string", ""),
                "result": res.get("string", ""),
                "passed": res.get("value", 0) == 0,
                "hours": row.get("power_on_hours"),
            })

    # ---- SCSI / SAS ----------------------------------------------------
    if protocol == "SCSI":
        ecl = raw.get("scsi_error_counter_log") or {}
        out["scsi"] = {
            "grown_defects": raw.get("scsi_grown_defect_list", 0),
            "read_uncorrected": (ecl.get("read") or {}).get("total_uncorrected_errors", 0),
            "write_uncorrected": (ecl.get("write") or {}).get("total_uncorrected_errors", 0),
            "verify_uncorrected": (ecl.get("verify") or {}).get("total_uncorrected_errors", 0),
        }
        if out["power_on_hours"] is None:
            out["power_on_hours"] = (raw.get("power_on_time") or {}).get("hours")

    return out


def _wwn(w):
    """smartctl's {naa, oui, id} -> the usual 16-hex-digit form (e.g. 5000c500e1f2a3b4)."""
    try:
        return f"{w['naa']:x}{w['oui']:06x}{w['id']:09x}"
    except (KeyError, TypeError, ValueError):
        return None


def metrics_from(summary):
    """Compact numeric snapshot stored for history / trend analysis."""
    m = {
        "temp": summary.get("temperature"),
        "poh": summary.get("power_on_hours"),
        "ok": summary.get("smart_passed"),
        "attrs": {str(a["id"]): [a["value"], a["worst"], a["thresh"], a["raw"]]
                  for a in summary.get("attributes", [])},
    }
    if summary.get("nvme"):
        nv = summary["nvme"]
        m["nvme"] = {k: nv.get(k) for k in ("critical_warning", "available_spare", "percentage_used",
                                             "media_errors", "num_err_log_entries", "unsafe_shutdowns",
                                             "data_units_written")}
    if summary.get("scsi"):
        m["scsi"] = dict(summary["scsi"])
    return m
