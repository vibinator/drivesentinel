"""ZFS pool parsing (QuTS hero). Run with: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ds-zfs-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import analyzer  # noqa: E402
import raid  # noqa: E402
import zfs  # noqa: E402
from config import Config  # noqa: E402

HEALTHY = """  pool: zpool1
 state: ONLINE
  scan: scrub repaired 0B in 05:12:11 with 0 errors on Sun Nov  1 05:12:13 2026
config:

\tNAME                                      STATE     READ WRITE CKSUM
\tzpool1                                    ONLINE       0     0     0
\t  raidz1-0                                ONLINE       0     0     0
\t    qzfs/enc_0/disk_0x1_ZR13AFAS_3        ONLINE       0     0     0
\t    qzfs/enc_0/disk_0x2_ZR1391P3_3        ONLINE       0     0     0
\t    qzfs/enc_0/disk_0x3_5000C500E1F2A3B4_3  ONLINE     0     0     7
\t    sdd3                                  ONLINE       0     0     0

errors: No known data errors

  pool: zpool2
 state: ONLINE
  scan: none requested
config:

\tNAME           STATE     READ WRITE CKSUM
\tzpool2         ONLINE       0     0     0
\t  mirror-0     ONLINE       0     0     0
\t    nvme0n1p3  ONLINE       0     0     0
\t    nvme1n1p3  ONLINE       0     0     0
\tcache
\t  sdz3         ONLINE       0     0     0
\tspares
\t  sdy3         AVAIL

errors: No known data errors
"""

DEGRADED = """  pool: zpool1
 state: DEGRADED
status: One or more devices is currently being resilvered.
action: Wait for the resilver to complete.
  scan: resilver in progress since Wed Nov 11 01:00:00 2026
\t1.20T scanned at 300M/s, 800G issued at 200M/s, 4.00T total
\t100G resilvered, 34.50% done, 0 days 02:11:30 to go
config:

\tNAME             STATE     READ WRITE CKSUM
\tzpool1           DEGRADED     0     0     0
\t  raidz1-0       DEGRADED     0     0     0
\t    sda3         ONLINE       0     0     0
\t    replacing-1  DEGRADED     0     0     0
\t      sdb3       FAULTED     12     3     0  too many errors
\t      sdi3       ONLINE       0     0     0  (resilvering)
\t    sdc3         ONLINE       0     0     0

errors: No known data errors
"""

DRIVES = [
    {"serial": "ZR13AFAS", "device": "/dev/sda", "summary": {}},
    {"serial": "ZR1391P3", "device": "/dev/sdb", "summary": {}},
    {"serial": "ZR13A00Z", "device": "/dev/sdc", "summary": {"wwn": "5000c500e1f2a3b4"}},
    {"serial": "22035J800070", "device": "/dev/nvme0", "summary": {}},
]


class ZfsParse(unittest.TestCase):
    def test_healthy_pools(self):
        p1, p2 = zfs.parse_status(HEALTHY)
        self.assertEqual((p1["name"], p1["status"], p1["level"], p1["redundancy_left"]),
                         ("zpool1", "healthy", "raidz1", 1))
        self.assertEqual(len(p1["members"]), 4)
        self.assertEqual((p2["level"], p2["redundancy_left"]), ("mirror", 1))
        sections = {m["label"]: m["section"] for m in p2["members"]}
        self.assertEqual(sections, {"nvme0n1p3": "data", "nvme1n1p3": "data", "sdz3": "cache", "sdy3": "spares"})
        self.assertTrue([m for m in p2["members"] if m["spare"]])
        self.assertEqual(p2["missing"], 0)

    def test_members_resolve_by_serial_wwn_and_device(self):
        pools = zfs.resolve_members(zfs.parse_status(HEALTHY), DRIVES)
        self.assertEqual([m["disk"] for m in pools[0]["members"]], ["sda", "sdb", "sdc", "sdd"])
        self.assertEqual(pools[1]["members"][0]["disk"], "nvme0")
        mem = raid.membership(pools)
        self.assertEqual(mem["sdc"][0]["errors"], {"read": 0, "write": 0, "cksum": 7})
        self.assertEqual(mem["sda"][0]["level"], "raidz1")

    def test_tiered_pool_and_qnap_system_pool(self):
        """Real QuTS hero layout: one pool with an SSD mirror and an HDD RAIDZ1, plus zpoolExt."""
        text = HEALTHY.replace("\t  raidz1-0 ", "\t  mirror-0                                  ONLINE       0     0     0\n"
                               "\t    nvme0n1p3                               ONLINE       0     0     0\n"
                               "\t    nvme1n1p3                               ONLINE       0     0     0\n\t  raidz1-1 ")
        text = text.replace("zpool2", "zpoolExt2")
        p1, p2 = zfs.resolve_members(zfs.parse_status(text), DRIVES)
        self.assertEqual((p1["level"], p1["role"], p2["role"]), ("mixed", "data", "system"))
        mem = raid.membership([p1, p2])
        self.assertEqual((mem["nvme0"][0]["level"], mem["nvme0"][0]["redundancy_left"]), ("mirror", 1))
        self.assertEqual((mem["sda"][0]["level"], mem["sda"][0]["redundancy_left"]), ("raidz1", 1))

    def test_degraded_resilver(self):
        p = zfs.parse_status(DEGRADED)[0]
        self.assertEqual((p["status"], p["sync"], p["sync_pct"]), ("rebuilding", "recovery", 34.5))
        self.assertAlmostEqual(p["sync_eta_min"], 131.5)
        self.assertEqual((p["redundancy_left"], p["failed_members"]), (0, ["sdb3"]))
        self.assertTrue(raid.busy([p]))

    def test_scrub_and_fault_states(self):
        scrub = HEALTHY.replace("scrub repaired 0B in 05:12:11 with 0 errors on Sun Nov  1 05:12:13 2026",
                                "scrub in progress since Sun\n\t10% done, 03:00:00 to go")
        self.assertEqual(zfs.parse_status(scrub)[0]["status"], "syncing")
        dead = DEGRADED.replace("state: DEGRADED", "state: FAULTED")
        self.assertEqual(zfs.parse_status(dead)[0]["status"], "failed")

    def test_garbage(self):
        self.assertEqual(zfs.parse_status(""), [])
        self.assertEqual(zfs.parse_status("no pools available\n"), [])

    def test_sizes(self):
        self.assertEqual(zfs._size("1.5T"), int(1.5 * 1024 ** 4))
        self.assertEqual(zfs._size("960G"), 960 * 1024 ** 3)
        self.assertIsNone(zfs._size("-"))

    def test_no_host_access_falls_back(self):
        self.assertIsNone(zfs.read())           # not running with pid: host here
        self.assertIn("host_access", zfs.diagnostics())

    def test_zfs_errors_become_a_finding(self):
        pools = zfs.resolve_members(zfs.parse_status(HEALTHY), DRIVES)
        s = {"serial": "ZR13A00Z", "device": "/dev/sdc", "model": "ST8000DM004-2CX188", "protocol": "ATA",
             "is_ssd": False, "smart_passed": True, "temperature": 35, "power_on_hours": 30000,
             "attributes": [{"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 10,
                             "raw": 0, "raw_str": "0"}], "nvme": None, "scsi": None, "selftest": {"log": []},
             "raid": raid.membership(pools)["sdc"]}
        r = analyzer.analyze(s, [], Config())
        self.assertTrue(any("ZFS recorded errors" in i["text"] and "7 checksum" in i["text"] for i in r["issues"]))
        self.assertNotEqual(r["status"], "good")


if __name__ == "__main__":
    unittest.main()
