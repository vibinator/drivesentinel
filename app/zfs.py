"""ZFS pool status (QuTS hero) read through the NAS's own `zpool` tool.

A container can't see ZFS pool state through /proc on QuTS hero, so the host's zpool binary
is run instead. That needs one of these in the compose file:
  pid: host            -> nsenter into the host's mount namespace   (preferred)
  - /:/host:ro         -> chroot into a read-only view of the host
Only read-only commands are ever run: `zpool status` and `zpool list`.
"""
import os
import re
import subprocess

HOST_PATH = "export PATH=/sbin:/usr/sbin:/usr/local/sbin:/bin:/usr/bin:/usr/local/bin:$PATH; "
STATUS = {"ONLINE": "healthy", "DEGRADED": "degraded", "FAULTED": "failed", "UNAVAIL": "failed",
          "SUSPENDED": "failed", "REMOVED": "failed", "OFFLINE": "failed"}
BAD_LEAF = {"FAULTED", "UNAVAIL", "REMOVED", "OFFLINE"}
SECTIONS = {"logs", "cache", "spares", "special", "dedup"}
UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}

_last = {"method": None, "error": None, "status_text": None}


def _methods():
    out = []
    if os.getpid() != 1 and os.path.exists("/proc/1/ns/mnt"):       # compose: pid: host
        out.append(("nsenter", ["nsenter", "-t", "1", "-m", "--", "/bin/sh", "-c"]))
    if os.path.isdir("/host/bin") or os.path.isdir("/host/sbin"):   # compose: - /:/host:ro
        out.append(("chroot", ["chroot", "/host", "/bin/sh", "-c"]))
    return out


def host_run(cmd, timeout=25):
    """Run a read-only command with the host's tools. Returns (stdout or None, method, error)."""
    err = "host access not configured (add 'pid: host' to the compose file)"
    for name, prefix in _methods():
        try:
            p = subprocess.run(prefix + [HOST_PATH + cmd], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            err = f"{name}: {e}"
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout, name, None
        err = f"{name}: exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}"
    return None, None, err


def available():
    return bool(_methods())


def _size(text):
    m = re.fullmatch(r"([\d.]+)([BKMGTP])?i?B?", (text or "").strip(), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * UNITS[(m.group(2) or "B").upper()])


def _vdev_type(name):
    m = re.match(r"(raidz[123]?|mirror|draid\d?|spare|replacing)", name)
    return m.group(1) if m else None


def parse_status(text):
    """Parse `zpool status` output into pool dicts shaped like raid.parse() arrays."""
    pools = []
    for block in re.split(r"(?m)^\s*pool:\s*", text)[1:]:
        lines = block.splitlines()
        name = lines[0].strip()
        state = (re.search(r"(?m)^\s*state:\s*(\S+)", block) or [None, "UNKNOWN"])[1].upper()
        scan = re.search(r"(?ms)^\s*scan:\s*(.*?)(?=^\s*\w+:|\Z)", block)
        scan_txt = " ".join(scan.group(1).split()) if scan else ""
        errors = re.search(r"(?m)^\s*errors:\s*(.*)$", block)

        vdevs, members, section, cur = [], [], "data", None
        in_cfg, base = False, None
        for ln in lines:
            if re.match(r"\s*NAME\s+STATE", ln):
                in_cfg = True
                continue
            if not in_cfg:
                continue
            if not ln.strip() or re.match(r"\s*errors:", ln):
                if re.match(r"\s*errors:", ln):
                    break
                continue
            body = ln.replace("\t", "")
            indent = len(body) - len(body.lstrip())
            parts = body.split()
            label = parts[0]
            if base is None:
                base = indent                        # the pool's own line
                continue
            depth = (indent - base) // 2
            if depth <= 0 and label in SECTIONS:
                section, cur = label, None
                continue
            lstate = parts[1].upper() if len(parts) > 1 else ""
            nums = [int(x) if x.isdigit() else 0 for x in parts[2:5]]
            nums += [0] * (3 - len(nums))
            vtype = _vdev_type(label)
            if vtype and vtype not in ("spare", "replacing"):
                cur = {"name": label, "type": vtype, "state": lstate, "section": section, "leaves": []}
                vdevs.append(cur)
                continue
            if vtype:                                # "spare-1"/"replacing-0" wrappers: keep children
                continue
            if cur is None or (depth == 1 and section == "data"):
                cur = {"name": label, "type": "stripe", "state": lstate, "section": section, "leaves": []}
                vdevs.append(cur)
            leaf = {"label": label, "state": lstate, "read": nums[0], "write": nums[1], "cksum": nums[2],
                    "section": section, "vdev": cur["name"],
                    "failed": lstate in BAD_LEAF, "spare": section == "spares"}
            cur["leaves"].append(leaf)
            members.append(leaf)

        data = [v for v in vdevs if v["section"] == "data"]
        left = []
        for v in data:
            n = len(v["leaves"])
            red = {"raidz": 1, "raidz1": 1, "raidz2": 2, "raidz3": 3, "mirror": max(0, n - 1)}.get(v["type"], 0)
            v_left = red - sum(1 for l in v["leaves"] if l["failed"])
            left.append((red, v_left))
            for leaf in v["leaves"]:        # each disk's exposure depends on its own vdev
                leaf["vdev_type"], leaf["vdev_left"] = v["type"], max(0, v_left)
        types = sorted({v["type"] for v in data})
        level = types[0] if len(types) == 1 else "mixed" if types else "zfs"

        sync, pct, eta = None, None, None
        if "in progress" in scan_txt:
            sync = "recovery" if "resilver" in scan_txt else "check"
            m = re.search(r"([\d.]+)%\s*done", scan_txt)
            pct = float(m.group(1)) if m else None
            m = re.search(r"(?:(\d+)\s*days?\s*)?(\d+):(\d+):(\d+)\s*to go", scan_txt)
            if m:
                eta = int(m.group(1) or 0) * 1440 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 60
        status = STATUS.get(state, "unknown")
        if sync == "recovery" and status in ("healthy", "degraded"):
            status = "rebuilding"
        elif sync == "check" and status == "healthy":
            status = "syncing"
        elif status == "degraded" and left and min(l for _, l in left) < 0:
            status = "failed"

        pools.append({
            "name": name, "state": state.lower(), "level": level, "zfs": True,
            # QNAP keeps its system files on a small mirror across every disk (zpoolExt*)
            "role": "system" if re.match(r"zpoolExt", name, re.I) else "data",
            "status": status, "members": members, "vdevs": [{k: v[k] for k in ("name", "type", "state", "section")}
                                                             for v in vdevs],
            "size_bytes": None, "alloc_bytes": None, "slots": None, "active": None, "map": "",
            "sync": sync, "sync_pct": pct, "sync_eta_min": eta, "sync_speed": None,
            "missing": sum(1 for m in members if m["failed"] and not m["spare"]),
            "failed_members": [m["label"] for m in members if m["failed"]],
            "redundancy": min((r for r, _ in left), default=None),
            "redundancy_left": max(0, min((l for _, l in left), default=0)) if left else None,
            "scan": scan_txt[:200], "errors": errors.group(1).strip() if errors else None,
        })
    return pools


def read():
    """Pools from the host's zpool, or None when host access isn't available/working."""
    if not available():
        _last.update(method=None, error="host access not configured", status_text=None)
        return None
    text, method, err = host_run("zpool status")
    _last.update(method=method, error=err, status_text=text)
    if text is None:
        return None
    pools = parse_status(text)
    sizes, _, _ = host_run("zpool list -H -o name,size,alloc")
    for ln in (sizes or "").splitlines():
        cols = ln.split()
        for p in pools:
            if len(cols) >= 3 and cols[0] == p["name"]:
                p["size_bytes"], p["alloc_bytes"] = _size(cols[1]), _size(cols[2])
    return pools


def resolve_members(pools, drives):
    """Attach each pool member to a drive: by device name, serial number or WWN in its label."""
    from raid import disk_key, disk_of
    keys = []
    for d in drives:
        s = d.get("summary") or {}
        keys.append((disk_key(d["device"]), [x.lower() for x in (d["serial"], s.get("wwn")) if x and len(x) >= 6]))
    for p in pools:
        for m in p["members"]:
            base = m["label"].split("/")[-1]
            disk = None
            if re.fullmatch(r"(sd[a-z]+\d*|nvme\d+n\d+(p\d+)?)", base):
                disk = disk_of(base)
            else:
                low = m["label"].lower()
                for key, ids in keys:
                    if any(i in low for i in ids):
                        disk = key
                        break
            m["disk"] = disk or base
            m["resolved"] = disk is not None
    return pools


def diagnostics():
    return {"host_access": [n for n, _ in _methods()], "method": _last["method"], "error": _last["error"],
            "zpool_status": (_last["status_text"] or "")[:8000] or None}
