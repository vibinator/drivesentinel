"""Linux md-RAID status (QNAP storage pools are built on md) parsed from /proc/mdstat."""
import os
import re

MDSTAT = os.environ.get("MDSTAT_PATH", "/proc/mdstat")

# QNAP creates these internal arrays on every disk; md9/md13 are 64-slot RAID1 system arrays
QNAP_SYSTEM = {"md9", "md13"}
QNAP_SWAP = {"md256", "md321", "md322"}

REDUNDANCY = {"raid1": None, "raid4": 1, "raid5": 1, "raid6": 2, "raid10": 1, "raid0": 0, "linear": 0}


def disk_of(member):
    """'sda3' -> 'sda', 'nvme0n1p2' -> 'nvme0', 'sdaa1' -> 'sdaa'."""
    m = re.match(r"(nvme\d+)n\d+(p\d+)?$", member)
    if m:
        return m.group(1)
    return re.sub(r"\d+$", "", member)


def disk_key(device):
    """Normalize a drive device path for matching array members: /dev/nvme0n1 -> nvme0."""
    name = os.path.basename(device or "")
    m = re.match(r"(nvme\d+)(n\d+)?$", name)
    return m.group(1) if m else name


def parse(text):
    arrays = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(md\d+)\s*:\s*(\S+)\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        name, state, rest = m.groups()
        tokens = rest.split()
        level = None
        if tokens and (tokens[0].startswith("raid") or tokens[0] in ("linear", "multipath")):
            level = tokens.pop(0)
        if tokens and tokens[0].startswith("("):          # e.g. "(auto-read-only)"
            state += " " + tokens.pop(0)
        members = []
        for t in tokens:
            mm = re.match(r"([\w]+)\[(\d+)\]((?:\([A-Z]\))*)", t)
            if mm:
                flags = mm.group(3)
                members.append({"part": mm.group(1), "disk": disk_of(mm.group(1)), "slot": int(mm.group(2)),
                                "failed": "(F)" in flags, "spare": "(S)" in flags,
                                "write_mostly": "(W)" in flags})
        arr = {"name": name, "state": state, "level": level, "members": members,
               "size_bytes": None, "slots": None, "active": None, "map": "",
               "sync": None, "sync_pct": None, "sync_eta_min": None, "sync_speed": None}
        i += 1
        while i < len(lines) and lines[i].startswith((" ", "\t")) and lines[i].strip():
            ln = lines[i]
            b = re.search(r"(\d+) blocks", ln)
            if b and arr["size_bytes"] is None:
                arr["size_bytes"] = int(b.group(1)) * 1024
            s = re.search(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]", ln)
            if s:
                arr["slots"], arr["active"], arr["map"] = int(s.group(1)), int(s.group(2)), s.group(3)
            r = re.search(r"(recovery|resync|check|repair|reshape)\s*=\s*([\d.]+)%", ln)
            if r:
                arr["sync"], arr["sync_pct"] = r.group(1), float(r.group(2))
                f = re.search(r"finish=([\d.]+)min", ln)
                sp = re.search(r"speed=(\d+)K/sec", ln)
                arr["sync_eta_min"] = float(f.group(1)) if f else None
                arr["sync_speed"] = int(sp.group(1)) * 1024 if sp else None
            if re.search(r"resync=(DELAYED|PENDING)", ln):
                arr["sync"] = "pending"
            i += 1
        arrays.append(_classify(arr))
    return arrays


def _classify(a):
    name = a["name"]
    a["role"] = "system" if name in QNAP_SYSTEM else "swap" if name in QNAP_SWAP else "data"
    active_members = [m for m in a["members"] if not m["spare"]]
    failed = [m for m in a["members"] if m["failed"]]
    ups = a["map"].count("U") if a["map"] else len(active_members) - len(failed)
    if a["role"] == "system":
        missing = max(0, len(active_members) - len(failed) - ups) + len(failed)
    else:
        missing = max(0, (a["slots"] or len(active_members)) - ups)
    a["missing"] = missing
    a["failed_members"] = [m["disk"] for m in failed]
    redundancy = REDUNDANCY.get(a["level"] or "", 1)
    if redundancy is None:                       # raid1: n-way mirror
        redundancy = max(0, (a["slots"] or len(active_members)) - 1)
    a["redundancy"] = redundancy
    a["redundancy_left"] = max(0, redundancy - missing) if a["role"] != "system" else None
    if a["state"].startswith("inactive"):
        status = "failed"
    elif missing and redundancy == 0:
        status = "failed"
    elif a["sync"] in ("recovery", "reshape"):
        status = "rebuilding"
    elif missing:
        status = "degraded"
    elif a["sync"] in ("resync", "check", "repair", "pending"):
        status = "syncing"
    else:
        status = "healthy"
    a["status"] = status
    return a


ZFS_KSTAT = os.environ.get("ZFS_KSTAT_PATH", "/proc/spl/kstat/zfs")
ZFS_STATUS = {"ONLINE": "healthy", "DEGRADED": "degraded", "FAULTED": "failed", "UNAVAIL": "failed",
              "SUSPENDED": "failed", "REMOVED": "failed", "OFFLINE": "failed"}


def read_zfs(base=None):
    """ZFS pool health (QuTS hero) from the kernel's kstat files.

    Only the pool state is visible from inside a container (member disks need `zpool status`,
    which isn't available here), so pools are reported without a member list.
    """
    base = base or ZFS_KSTAT
    pools = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    for name in names:
        state_file = os.path.join(base, name, "state")
        if not os.path.isfile(state_file):
            continue
        try:
            with open(state_file, encoding="utf-8", errors="replace") as f:
                state = f.read().strip().upper()
        except OSError:
            continue
        pools.append({"name": name, "state": state.lower(), "level": "zfs", "members": [],
                      "size_bytes": None, "slots": None, "active": None, "map": "",
                      "sync": None, "sync_pct": None, "sync_eta_min": None, "sync_speed": None,
                      "role": "data", "missing": 0 if state == "ONLINE" else None,
                      "failed_members": [], "redundancy": None, "redundancy_left": None,
                      "status": ZFS_STATUS.get(state, "unknown"), "zfs": True})
    return pools


def read(path=None, text=None):
    zfs = []
    if text is None:
        import zfs as zfs_host
        zfs = zfs_host.read()           # full detail via the host's zpool tool, when available
        if zfs is None:
            zfs = read_zfs()            # fallback: pool state only, from kernel stats
    if text is None:
        path = path or MDSTAT
        if not os.path.exists(path):
            return zfs
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return zfs
    return parse(text) + zfs


def diagnostics():
    """Raw sources used for pool detection, for troubleshooting."""
    import zfs as zfs_host
    out = {"mdstat": None, "zfs_kstat": None, "zpool": zfs_host.diagnostics()}
    try:
        with open(MDSTAT, encoding="utf-8", errors="replace") as f:
            out["mdstat"] = f.read()
    except OSError as e:
        out["mdstat"] = f"unavailable: {e}"
    try:
        out["zfs_kstat"] = {n: sorted(os.listdir(os.path.join(ZFS_KSTAT, n)))
                            for n in os.listdir(ZFS_KSTAT) if os.path.isdir(os.path.join(ZFS_KSTAT, n))}
    except OSError as e:
        out["zfs_kstat"] = f"unavailable: {e}"
    return out


def membership(arrays):
    """disk key -> list of {array, level, status, role, redundancy_left}"""
    out = {}
    for a in arrays:
        for m in a["members"]:
            out.setdefault(m["disk"], []).append({
                "array": a["name"], "level": m.get("vdev_type") or a["level"], "status": a["status"],
                "role": a["role"], "redundancy_left": m.get("vdev_left", a["redundancy_left"]),
                "member_failed": m["failed"], "spare": m["spare"],
                "errors": {k: m.get(k, 0) for k in ("read", "write", "cksum")} if a.get("zfs") else None})
    return out


def busy(arrays):
    """True while any data array is rebuilding/syncing (avoid benchmarks then)."""
    return any(a["status"] in ("rebuilding", "syncing") for a in arrays if a["role"] == "data")
