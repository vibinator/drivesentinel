"""Backblaze Drive Stats importer.

Downloads Backblaze's public quarterly SMART datasets, then in a single pass:
  1. computes per-model annualized failure rates (AFR) -> backblaze_stats.json
  2. builds a labelled training set ("fails within N days") and fits a logistic
     regression failure-prediction model -> ml_model.json

Usage (inside the container or anywhere with Python 3.10+):
  python backblaze.py --quarters 2 --out /data            # download + build
  python backblaze.py --zips data_Q1_2026.zip --out /data  # use already-downloaded zips
"""
import argparse
import datetime as dt
import io
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
import zipfile
import zlib

import features

BASE_URL = "https://f001.backblazeb2.com/file/Backblaze-Hard-Drive-Data/data_{q}.zip"
HORIZON_DAYS = 30
MIN_DRIVE_DAYS = 20000          # ~55 drive-years before we trust a model's AFR


# ---------------------------------------------------------------------------- download
def _quarter_of(d):
    return (d.month - 1) // 3 + 1


def available_quarters(n, today=None, log=print):
    """Find the n most recent published quarters (newest first)."""
    today = today or dt.date.today()
    y, q = today.year, _quarter_of(today)
    found, tries = [], 0
    while len(found) < n and tries < n + 6:
        name = f"Q{q}_{y}"
        try:
            req = urllib.request.Request(BASE_URL.format(q=name), method="HEAD",
                                         headers={"User-Agent": "DriveSentinel"})
            with urllib.request.urlopen(req, timeout=20) as r:
                size = int(r.headers.get("Content-Length") or 0)
            found.append((name, size))
        except Exception:  # noqa: BLE001 - not published yet / unavailable
            pass
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        tries += 1
    return found


def download(quarter, dest_dir, progress=None):
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"data_{quarter}.zip")
    url = BASE_URL.format(q=quarter)
    req = urllib.request.Request(url, headers={"User-Agent": "DriveSentinel"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        if os.path.exists(path) and total and os.path.getsize(path) == total:
            return path
        done, tmp = 0, path + ".part"
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------- dataset pass
def _day_members(zf):
    out = []
    for name in zf.namelist():
        base = os.path.basename(name)
        if name.startswith("__MACOSX") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.csv", base):
            continue
        out.append((dt.date.fromisoformat(base[:10]), name))
    return out


def _int(s):
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


class Builder:
    def __init__(self, neg_rate=0.004, seed=7, log=print):
        self.neg_rate = neg_rate
        self.rng = random.Random(seed)
        self.log = log
        self.models = {}            # model -> [drive_days, failures, set(serials), capacity]
        self.fail_date = {}         # serial -> failure date
        self.samples = []           # (label, model, {attr: raw}, serial)
        self._norm = {}             # raw model string -> normalized (regex is slow per row)
        self.first_day = self.last_day = None

    def process(self, zip_paths, progress=None):
        days = []
        handles = [zipfile.ZipFile(p) for p in zip_paths]
        for zf in handles:
            days += [(d, zf, name) for d, name in _day_members(zf)]
        days.sort(key=lambda x: x[0], reverse=True)          # newest first
        if not days:
            raise RuntimeError("No daily CSV files found in the archive(s)")
        self.last_day, self.first_day = days[0][0], days[-1][0]
        neg_cutoff = self.last_day - dt.timedelta(days=HORIZON_DAYS)
        t0 = time.time()
        for i, (day, zf, name) in enumerate(days):
            self._day(zf, name, day, neg_cutoff)
            if progress:
                progress((i + 1) / len(days))
            if i % 10 == 0:
                self.log(f"  {day}  ({i + 1}/{len(days)} days, {len(self.samples)} samples, "
                         f"{time.time() - t0:.0f}s)")
        for zf in handles:
            zf.close()

    def _day(self, zf, name, day, neg_cutoff):
        with zf.open(name) as fb:
            f = io.TextIOWrapper(fb, encoding="utf-8", errors="replace", newline="")
            header = f.readline().strip().split(",")
            col = {c: i for i, c in enumerate(header)}
            i_serial, i_model, i_cap, i_fail = (col["serial_number"], col["model"],
                                                 col["capacity_bytes"], col["failure"])
            i_attr = {a: col.get(f"smart_{a}_raw") for a in features.ATTRS}
            nfirst = max(i_serial, i_model, i_cap, i_fail) + 1
            models, fail_date, rng, neg_rate = self.models, self.fail_date, self.rng, self.neg_rate
            norm = self._norm
            for line in f:
                parts = line.split(",", nfirst)
                if len(parts) <= nfirst - 1:
                    continue
                raw_model = parts[i_model]
                model = norm.get(raw_model)
                if model is None:
                    model = norm[raw_model] = features.normalize_model(raw_model)
                serial = parts[i_serial]
                failed = parts[i_fail] == "1"
                m = models.get(model)
                if m is None:
                    m = models[model] = [0, 0, set(), _int(parts[i_cap]) or 0]
                m[0] += 1
                m[2].add(serial)
                if failed:
                    m[1] += 1
                    fail_date[serial] = day
                if features.is_ssd_model(model):
                    continue
                fd = fail_date.get(serial)
                if fd is not None and (fd - day).days <= HORIZON_DAYS:
                    label = 1
                elif day <= neg_cutoff and rng.random() < neg_rate:
                    label = 0
                else:
                    continue
                full = line.rstrip("\r\n").split(",")
                raw = {a: _int(full[ix]) if ix is not None and ix < len(full) else None
                       for a, ix in i_attr.items()}
                self.samples.append((label, model, raw, serial))

    # ---------------------------------------------------------------- outputs
    def model_stats(self):
        out = []
        shrunk, _ = self.model_afr_lookup()
        for model, (days, fails, serials, cap) in self.models.items():
            if days < MIN_DRIVE_DAYS:
                continue
            afr_raw = fails / (days / 365.0) * 100
            afr = shrunk[model] if fails < 10 else afr_raw   # too few failures: shrink toward fleet
            out.append({
                "match": "^" + re.escape(model).replace("\\ ", r"\s+") + r"(?:$|[-\s_])",
                "name": model, "afr": round(afr, 2), "afr_raw": round(afr_raw, 2),
                "low_confidence": fails < 10, "failures": fails,
                "drive_days": days, "drives": len(serials),
                "capacity_tb": round(cap / 1e12, 1) if cap else None,
                "ssd": features.is_ssd_model(model),
                "source": f"Backblaze {self.first_day:%b %Y}–{self.last_day:%b %Y}",
            })
        out.sort(key=lambda e: -e["drive_days"])
        return {
            "_about": "Computed from Backblaze Drive Stats (backblaze.com/cloud-storage/resources/"
                      "hard-drive-test-data) by DriveSentinel's importer.",
            "as_of": f"{self.first_day.isoformat()} to {self.last_day.isoformat()}",
            "fleet_average_afr": round(self._fleet_afr(), 2),
            "models": out,
        }

    def _fleet_afr(self):
        hdd = [v for k, v in self.models.items() if not features.is_ssd_model(k)]
        days, fails = sum(v[0] for v in hdd), sum(v[1] for v in hdd)
        return fails / (days / 365.0) * 100 if days else 1.5

    def model_afr_lookup(self):
        fleet = self._fleet_afr()
        out = {}
        for model, (days, fails, _, _) in self.models.items():
            # shrink low-volume models toward the fleet average (empirical Bayes)
            prior_days = 50000
            out[model] = (fails + fleet / 100 * prior_days / 365) / ((days + prior_days) / 365) * 100
        return out, fleet

    def train(self, l2=1.0):
        afr, fleet = self.model_afr_lookup()
        pos = [s for s in self.samples if s[0] == 1]
        neg = [s for s in self.samples if s[0] == 0]
        if len(pos) < 50:
            raise RuntimeError(f"Only {len(pos)} positive samples; use more quarters")
        # Hold out 20% of *drives* (not rows): a drive's daily records are near-duplicates,
        # so a row-level split would leak and overstate accuracy.
        train = [s for s in pos + neg if zlib.crc32(s[3].encode()) % 5]
        test = [s for s in pos + neg if not zlib.crc32(s[3].encode()) % 5]
        self.rng.shuffle(train)
        data = train + test
        X = [features.vector(raw, afr.get(model, fleet)) for _, model, raw, _ in data]
        y = [s[0] for s in data]
        split = len(train)
        mu, sd = _standardize_params(X[:split])
        Z = [[(v - m) / s for v, m, s in zip(row, mu, sd)] for row in X]
        w = _irls(Z[:split], y[:split], l2=l2, log=self.log)
        scores = [_dot(w, z) for z in Z[split:]]
        auc = _auc(scores, y[split:])
        # At the 1% "warning" threshold: share of soon-to-fail drive-days caught, and share of
        # healthy drive-days flagged (scores are on the subsampled scale, hence the offset).
        cut = math.log(0.01 / 0.99) - math.log(self.neg_rate)
        tp = sum(1 for sc, t in zip(scores, y[split:]) if t and sc >= cut)
        fp = sum(1 for sc, t in zip(scores, y[split:]) if not t and sc >= cut)
        n_pos = sum(y[split:]) or 1
        n_neg = (len(scores) - sum(y[split:])) or 1
        # prior correction for negative subsampling: logit_true = logit_sample + ln(rate)
        intercept = w[0] + math.log(self.neg_rate)
        return {
            "type": "logistic_regression",
            "horizon_days": HORIZON_DAYS,
            "features": features.FEATURES,
            "mean": [round(v, 6) for v in mu], "std": [round(v, 6) for v in sd],
            "weights": [round(v, 6) for v in w[1:]], "intercept": round(intercept, 6),
            "auc": round(auc, 4), "positives": len(pos), "negatives": len(neg),
            "holdout": "20% of drives", "recall_at_1pct": round(tp / n_pos, 3),
            "false_alarm_rate_at_1pct": round(fp / n_neg, 4),
            "negative_sampling_rate": self.neg_rate,
            "trained_on": f"Backblaze {self.first_day.isoformat()} to {self.last_day.isoformat()}",
            "trained_at": dt.date.today().isoformat(),
        }


# ---------------------------------------------------------------------------- math helpers
def _dot(w, z):
    return w[0] + sum(a * b for a, b in zip(w[1:], z))


def _sigmoid(t):
    if t < -35:
        return 1e-15
    if t > 35:
        return 1 - 1e-15
    return 1 / (1 + math.exp(-t))


def _standardize_params(X):
    n, k = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(k)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1.0 for j in range(k)]
    return mu, sd


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _irls(Z, y, l2=1.0, iters=25, log=print):
    """Newton-Raphson logistic regression with L2 penalty (intercept unpenalized)."""
    k = len(Z[0]) + 1
    w = [0.0] * k
    for it in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for z, t in zip(Z, y):
            x = [1.0] + z
            p = _sigmoid(_dot(w, z))
            r, s = p - t, p * (1 - p)
            for i in range(k):
                g[i] += r * x[i]
                xi = s * x[i]
                Hi = H[i]
                for j in range(i, k):
                    Hi[j] += xi * x[j]
        for i in range(k):
            for j in range(i):
                H[i][j] = H[j][i]
            if i:
                g[i] += l2 * w[i]
                H[i][i] += l2
        step = _solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    log(f"  logistic regression converged after {it + 1} iterations")
    return w


def _auc(scores, labels):
    pairs = sorted(zip(scores, labels))
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2
        rank_sum += avg_rank * sum(1 for _, lab in pairs[i:j] if lab)
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


# ---------------------------------------------------------------------------- entry points
def build(zip_paths, out_dir, log=print, progress=None, neg_rate=0.004):
    b = Builder(neg_rate=neg_rate, log=log)
    log(f"Processing {len(zip_paths)} archive(s)...")
    b.process(zip_paths, progress=progress)
    stats = b.model_stats()
    log(f"Computed AFR for {len(stats['models'])} models (fleet average {stats['fleet_average_afr']}%)")
    log("Training failure-prediction model...")
    model = b.train()
    log(f"Model AUC on held-out data: {model['auc']} ({model['positives']} failure samples)")
    os.makedirs(out_dir, exist_ok=True)
    for fname, obj in (("backblaze_stats.json", stats), ("ml_model.json", model)):
        tmp = os.path.join(out_dir, fname + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, os.path.join(out_dir, fname))
    return stats, model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quarters", type=int, default=1, help="number of recent quarters to download")
    ap.add_argument("--zips", nargs="*", help="use existing zip files instead of downloading")
    ap.add_argument("--cache", default=os.path.join(os.environ.get("DATA_DIR", "/data"), "backblaze"))
    ap.add_argument("--out", default=os.environ.get("DATA_DIR", "/data"))
    ap.add_argument("--neg-rate", type=float, default=0.004)
    ap.add_argument("--keep", action="store_true", help="keep downloaded zips")
    a = ap.parse_args()
    paths = a.zips or []
    downloaded = []
    if not paths:
        qs = available_quarters(a.quarters)
        print("Quarters:", ", ".join(f"{q} ({s / 1e9:.1f} GB)" for q, s in qs))
        for q, _ in qs:
            print(f"Downloading {q}...")
            paths.append(download(q, a.cache, lambda f: print(f"\r  {f * 100:5.1f}%", end="", flush=True)))
            downloaded.append(paths[-1])
            print()
    build(paths, a.out, neg_rate=a.neg_rate)
    if downloaded and not a.keep:
        for p in downloaded:
            os.remove(p)


if __name__ == "__main__":
    sys.exit(main())
