# How health is assessed

Each scan, every drive goes through `analyzer.analyze()`. Many independent signals each
contribute a **failure probability** *pᵢ*, and these are combined:

```
risk   = 1 − Π (1 − pᵢ)          (0–100 %)
health = 100 − risk
```

Two kinds of signal are produced: **findings** (things that are true now) and **predictions**
(projections from trends). Both feed into the status.

## Status levels

| Status | Rule |
|---|---|
| **Failing** | SMART overall-health FAILED, any attribute at or below its vendor threshold, or NVMe "reliability degraded" or "read-only" |
| **Critical** | risk ≥ 50%, a projected threshold crossing within 30 days (risk raised to at least 60%), or a *warning* drive sitting in a pool with no redundancy left |
| **Warning** | risk ≥ 15%, a projection within 90 days (risk raised to at least 35%), or any critical-level finding |
| **Good** | everything else |

The **verdict** sentence on each drive's page is derived from the status and the nearest projection.

## 1. SMART indicators (ATA / SATA)

Counters use a log scale, `p = min(cap, base + step · log₂(1 + count))`, so the first few errors
matter most:

| ID | Attribute | base | step | cap | Level |
|---|---|---|---|---|---|
| 5 | Reallocated sectors | 0.12 | 0.06 | 0.60 | warning (critical at ≥50) |
| 197 | Current pending sectors | 0.20 | 0.07 | 0.70 | critical |
| 198 | Offline uncorrectable | 0.20 | 0.07 | 0.70 | critical |
| 187 | Reported uncorrectable | 0.15 | 0.06 | 0.60 | critical |
| 184 | End-to-end errors | 0.35 | 0.05 | 0.60 | critical |
| 10 | Spin retries | 0.20 | 0.04 | 0.45 | warning |
| 188 | Command timeouts* | 0.04 | 0.03 | 0.30 | warning |
| 196 | Reallocation events | 0.04 | 0.02 | 0.20 | warning |

\* Seagate packs three counters into attribute 188. Values above 0xFFFF are masked to their
low 16 bits.

Other ATA signals:
- **Attribute at or below its threshold:** p = 0.9, and the drive is *failing*.
- **Attribute crossed its threshold in the past** (worst ≤ thresh): p = 0.25.
- **Temperature attributes (190, 194) are excluded from both rules.** They "fail" when a drive
  overheats, which is a cooling problem. Past overheating is listed as an info finding
  (p = 0.01), and current temperature is handled in section 4.
- **Limited data:** a device that reports no attributes and no health status gets an info
  finding and a "Limited data" badge, because a clean score would be misleading.
- **UDMA CRC errors (199):** p = 0.02, info. These usually point to a cable or backplane, not
  the disk.
- **SATA SSD endurance** (attributes 231/233/202/177/169, normalized value as % remaining):
  ≤10% gives p = 0.4 (critical), ≤25% gives p = 0.1 (warning).

## 2. NVMe

| Signal | p |
|---|---|
| Critical warning bit 0x01 (spare below threshold) | 0.6 |
| 0x02 (temperature) | 0.08 |
| 0x04 (reliability degraded) | 0.7, failing |
| 0x08 (read-only) | 0.9, failing |
| 0x10 / 0x20 (backup / PMR) | 0.3 |
| Percentage used ≥100 / ≥90 | 0.4 / 0.15 |
| Available spare < 30% (above threshold) | 0.1 |
| Media errors | log scale: 0.2 + 0.06 · log₂(1+n), cap 0.6 |

## 3. SAS / SCSI

Grown defect list: log scale (0.12, 0.06, cap 0.6). Uncorrected read, write and verify errors:
log scale (0.15, 0.06, cap 0.6).

## 4. Temperature and self-tests

- At or above the critical limit: p = 0.08. At or above the warning limit: p = 0.02.
  Limits are set per HDD/SSD in Settings.
- The most recent self-test failed: p = 0.5, critical.

## 5. Make/model reliability baseline

`reliability.lookup()` finds the drive's historical **annualized failure rate (AFR)**:

1. Exact model match: Backblaze-computed stats (50 models, 382k drives, Oct 2025 – Sep 2026)
   or your overrides.
2. Product-family match: curated estimates (WD Red, IronWolf, N300, Exos, BarraCuda,
   white-label WD, and so on).
3. Generic default for the drive type: HDD 1.5%, SATA SSD 0.9%, NVMe 0.9%.

The AFR is then multiplied by the **age (bathtub-curve) factor**:

| Age | Multiplier | Phase |
|---|---|---|
| < 1 year | 1.4 | infant mortality |
| 1–4 years | 1.0 | useful life |
| 4–5 years | 1.4 | wear-out begins |
| 5–7 years | 2.0 | wear-out |
| > 7 years | 3.0 | beyond design life |

SSDs are capped at 1.4, because their wear is tracked separately. The adjusted AFR is added
as p = AFR/100. It is listed as a finding only when it's ≥2.5% (info) or ≥5% (warning);
otherwise it just quietly contributes to the risk.

Low-evidence Backblaze models (<10 failures) are shrunk toward the fleet average with
empirical Bayes (a prior of 50,000 drive-days), so a model with no observed failures doesn't
show a misleading 0%.

## 6. AI failure prediction

`ml.predict()` applies a **logistic-regression model** trained on Backblaze's daily SMART records.

- **Label:** the drive fails within the next 30 days.
- **Features** (`features.py`): log1p of attributes 5, 187, 188, 197, 198, 10, 184 and 199; a
  sector-error flag; age and age²; temperature; and log(model AFR).
- **Training** (`backblaze.py`): the daily files are processed newest-first, so every
  failure date is already known when earlier rows are read. Rows from the 30 days before a
  failure are positives. Negatives are randomly subsampled at 0.4%, excluding the final 30
  days, because those drives might still fail just after the data ends. Features are
  standardized, then fitted with Newton-Raphson (IRLS) and L2 regularization. The intercept is
  then corrected for the subsampling (`+ ln(0.004)`), so outputs are **calibrated probabilities**.
- **Evaluation:** 20% of *drives* are held out (never rows, since one drive's daily records
  are near-duplicates). The model file records the AUC, and at the 1% warning threshold the
  share of soon-to-fail drive-days it catches and the share of healthy drive-days it flags.
  Settings → AI & datasets shows these figures for the model in use.
- **Scope:** SATA/ATA hard drives only. SSDs and NVMe drives use the indicators above.
- **Effect on risk:** a 30-day probability ≥10% adds a critical finding; ≥1% adds a warning.
  The 12-month equivalent, `1 − (1 − p)^(365/30)`, is used as the contribution.
- **Drivers:** each feature's contribution (weight × standardized value) is grouped and shown,
  largest first.

The model learns from data-center drives, where survivorship affects how age relates to
failure. Age is therefore mostly handled by the bathtub factor in section 5.

## 7. Trends and predictions

These use up to 30 days of stored snapshots, needing at least 3 points spanning at least 1 day.

- **Threshold projection:** a linear regression of the normalized value, for attributes that
  decline steadily as a drive degrades (5, 10, 184, 187, 196, 197, 198 and the SSD wear and
  spare attributes). It needs at least 3 days of history, a fall faster than 0.02 per day and a
  consistent trend (r² ≥ 0.5). It then projects the days until the threshold (shown if under
  3 years; critical if under 90 days). Noisy attributes such as read/seek error rates and
  temperatures are never extrapolated.
- **Counter growth:** a regression of the raw values of attributes 5, 197, 198 and 187. Growth
  above 0.05 per day is reported per day and per month.
- **Wear projection:** NVMe percentage used, or SATA SSD life attributes. Wear counters move
  in whole 1% steps, so the rate is the drive's lifetime average (wear ÷ age). The recent
  trend is used as well once it spans 14 days, and the faster of the two wins.
- **Actively deteriorating:** any growth over the last 7 days in reallocated, pending or
  uncorrectable sectors, NVMe media errors or SAS grown defects. This is a critical finding
  with p = 0.2.

## 7b. Workload and mechanical wear

`analyzer._usage()` reports, where the drive exposes them:

- **Data written / read:** attributes 241/242 (units taken from the attribute name: LBAs,
  GiB or 32 MiB) or the NVMe data-unit counters.
- **Workload:** (written + read) ÷ age, in TB/year, compared with the rating for the drive's
  class: desktop 55, NAS 180, NAS Pro 300, enterprise 550. More than 120% of the rating is a
  warning (p = 0.03).
- **Head load cycles** (attribute 193) against the class rating (desktop 300,000; others
  600,000). Above the rating is a warning (p = 0.05). If the current rate would reach the
  rating within 2 years, an info finding suggests disabling aggressive head parking.

The drive class comes from the `class` field of the matching family in `model_stats.json`.

## 8. Performance benchmarks

`bench.run()` opens the block device read-only with `O_DIRECT`, so the page cache doesn't
distort results.

- **Sequential:** reads `bench_seq_mb` MB in 4 MiB blocks at 0%, 50% and 95% of the disk.
  On an HDD those are the outer (fastest), middle and inner (slowest) tracks.
- **Random:** 300 aligned 4 KiB reads at random offsets. Reports mean latency, p95 latency and IOPS.
- **Baseline:** the median of the drive's first 3 runs. The **performance score** is the latest
  sequential average divided by the baseline.
- A drop of 25% or more is a warning (p = 0.08); 45% or more is critical (p = 0.25). Random
  latency above 2.5× the baseline is a warning.
- A scheduled or manual run that comes in more than 25% below the baseline is repeated after
  45 seconds and the better result is kept, since a busy NAS is the usual cause.

## 9. Storage pools (RAID)

`raid.py` parses `/proc/mdstat` (QTS md-RAID) and ZFS pool state (QuTS hero).

- **md arrays:** level, members, failed (F) and spare (S) flags, `[n/m] [UU_U]` maps, and
  recovery/resync/check progress, ETA and speed. QNAP's internal arrays are classified as
  `system` (md9, md13: 64-slot RAID1 across all disks) or `swap` (md256, md321, md322), and
  kept separate from `data` pools.
- **Status:** healthy, syncing, rebuilding, degraded or failed. Redundancy left is computed
  per level (RAID1: n−1, RAID5: 1, RAID6: 2, RAID0: 0).
- **ZFS pools (QuTS hero):** `zfs.py` runs the NAS's own `zpool status` and `zpool list`
  (read-only) through `nsenter`, which needs `pid: host` in the compose file. It parses each
  pool's state, vdev layout (RAIDZ1/2/3, mirror, stripe; cache, log and spare devices),
  scrub or resilver progress, and per-disk read, write and checksum error counts. Members are
  matched to drives by device name, serial number or WWN in the member's label. Without host
  access it falls back to the pool state from `/proc/spl/kstat/zfs`, where the kernel
  provides it.
- **ZFS errors on a disk** add a critical finding on a log scale (0.15 + 0.06 · log₂(1+n),
  cap 0.6). Checksum errors in particular often show up before SMART reports anything.
- **Per drive:** if a drive is marked FAILED by the RAID layer, that adds p = 0.5. A drive in a
  degraded pool with no redundancy left gets a warning, and a drive already at warning is
  escalated to critical.
- Benchmarks are postponed while any data pool is rebuilding or syncing.

## 10. Known issues

`issues.py` matches the model (regex) and, optionally, the exact firmware against
`known_issues.json`. Critical issues add p = 0.25 (with the fixed firmware version noted);
warnings add p = 0.03; info issues are display-only. Built-in entries include the Samsung
980 PRO (3B2QGXA7) and 990 PRO (early firmware) health bugs, IronWolf 10TB SC60, the Crucial
M4 5184-hour bug, Crucial MX500 spurious pending sectors, SMR drives (WD Red EFAX, Seagate
BarraCuda DM004/DM008 family, Toshiba P300/DT02), the ST3000DM001, and desktop-class drives.

## 11. Peer comparison

`peers.py` runs after each scan, across all drives:

- **Peer group:** drives of the identical model. If there are none, drives of the same type
  (HDD, SSD or NVMe).
- **Hotter than peers:** a 7-day average temperature 7°C or more above the group median is a
  warning (check bay airflow).
- **Only drive with errors:** the drive has sector or media errors while all its peers have none.
- **Slower than twins:** latest sequential speed below 75% of identical drives.
- **Health gap:** health 20 or more points below the group median.
- **Oldest HDD** in the NAS (more than 3 years): info, so you know which to replace first.

New warning-level insights raise an event or alert once, until they clear.
