"""Read-only drive benchmarks: sequential throughput at 3 surface positions + random 4K reads.

Never writes to the disk. Uses O_DIRECT so results reflect the drive, not the page cache.
"""
import mmap
import os
import random
import re
import statistics
import time

SEQ_POSITIONS = (0.0, 0.5, 0.95)   # outer, middle, inner tracks on HDDs
BLOCK = 4 << 20                     # 4 MiB sequential read size


def block_device(dev):
    """/dev/nvme0 (controller char device) -> /dev/nvme0n1 (namespace block device)."""
    if re.fullmatch(r"/dev/nvme\d+", dev or ""):
        return dev + "n1"
    return dev


def _open(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        return os.open(path, flags), True
    except OSError:
        return os.open(path, os.O_RDONLY), False


def _drop_cache(fd):
    if hasattr(os, "posix_fadvise"):
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass


def run(dev, seq_mb=256, random_reads=300, max_seconds=180):
    path = block_device(dev)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found")
    fd, direct = _open(path)
    started = time.time()
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        if size < 4 * BLOCK:
            raise ValueError("device too small")
        buf = mmap.mmap(-1, BLOCK)      # page-aligned, as O_DIRECT requires
        seq = []
        for pos in SEQ_POSITIONS:
            if not direct:
                _drop_cache(fd)
            off = int((size - seq_mb * (1 << 20) - BLOCK) * pos) // BLOCK * BLOCK
            want = seq_mb * (1 << 20)
            done, t0 = 0, time.perf_counter()
            while done < want:
                n = os.preadv(fd, [buf], off + done)
                if n <= 0:
                    break
                done += n
                if time.time() - started > max_seconds:
                    break
            dt = time.perf_counter() - t0
            seq.append(round(done / dt / 1e6, 1) if dt > 0 else None)
        small = mmap.mmap(-1, 4096)
        lat = []
        rng = random.Random()
        blocks = size // 4096
        if not direct:
            _drop_cache(fd)
        for _ in range(random_reads):
            off = rng.randrange(0, blocks) * 4096
            t0 = time.perf_counter()
            os.preadv(fd, [small], off)
            lat.append((time.perf_counter() - t0) * 1000)
            if time.time() - started > max_seconds:
                break
        lat.sort()
        mean = statistics.fmean(lat) if lat else None
        return {
            "seq_mbps": seq,
            "seq_avg": round(statistics.fmean([s for s in seq if s]), 1) if any(seq) else None,
            "rand_lat_ms": round(mean, 3) if mean else None,
            "rand_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 3) if lat else None,
            "rand_iops": round(1000 / mean) if mean else None,
            "direct_io": direct,
            "duration_s": round(time.time() - started, 1),
        }
    finally:
        os.close(fd)


def evaluate(results):
    """Compare the latest benchmark with the drive's own baseline.

    results: list of benchmark dicts (oldest first) each with seq_avg / rand_lat_ms.
    """
    valid = [r for r in results if r.get("seq_avg")]
    if not valid:
        return None
    latest = valid[-1]
    base_set = valid[:3]
    base_seq = statistics.median(r["seq_avg"] for r in base_set)
    lats = [r["rand_lat_ms"] for r in base_set if r.get("rand_lat_ms")]
    base_lat = statistics.median(lats) if lats else None
    score = min(100, round(latest["seq_avg"] / base_seq * 100)) if base_seq else None
    out = {"latest": latest, "baseline_seq": round(base_seq, 1), "baseline_lat": base_lat,
           "score": score, "runs": len(valid), "issues": []}
    if len(valid) >= 2 and score is not None:
        drop = 100 - score
        if drop >= 45:
            out["issues"].append(("critical", f"Sequential read speed dropped {drop}% vs. this drive's baseline "
                                              f"({latest['seq_avg']} vs {base_seq:.0f} MB/s)"))
        elif drop >= 25:
            out["issues"].append(("warning", f"Sequential read speed dropped {drop}% vs. baseline "
                                             f"({latest['seq_avg']} vs {base_seq:.0f} MB/s)"))
        if base_lat and latest.get("rand_lat_ms") and latest["rand_lat_ms"] > base_lat * 2.5:
            out["issues"].append(("warning", f"Random read latency up {latest['rand_lat_ms'] / base_lat:.1f}× "
                                             f"vs. baseline ({latest['rand_lat_ms']:.1f} ms)"))
    return out
