# partstrack - a small inventory for electronic parts
# Copyright (C) 2026 Kayla Theil (accounts@kaylatheil.de)
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from partstrack.store import Inventory

SCRIPT = Path(__file__).resolve().parent.parent / "contrib" / "systemd" / "partstrack-backup"


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar"), "needs bash and tar")
class BackupScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "data" / "parts.db"
        with Inventory(self.db) as inv:
            inv.add_part("NE555")
        self.dest = Path(self.tmp.name) / "backups"

    def run_script(self, keep):
        env = {**os.environ, "PARTSTRACK_DB": str(self.db), "KEEP": keep}
        return subprocess.run(["bash", str(SCRIPT), str(self.dest)], env=env, capture_output=True, text=True)

    def backups(self):
        return sorted(p.name for p in self.dest.glob("partstrack-*.tar.gz"))

    def test_keeps_the_newest_backups_and_nothing_else(self):
        self.dest.mkdir()
        (self.dest / "partstrack-manual.tar.gz").write_bytes(b"mine")
        old = ["partstrack-2020-01-01_000000.tar.gz", "partstrack-2020-01-02_000000.tar.gz"]
        for name in old:
            (self.dest / name).write_bytes(b"old")
        # Two runs within the same second must not overwrite each other.
        for _ in range(2):
            result = self.run_script("2")
            self.assertEqual(result.returncode, 0, result.stderr)
        backups = self.backups()
        self.assertIn("partstrack-manual.tar.gz", backups)
        made = [b for b in backups if b not in old and b != "partstrack-manual.tar.gz"]
        self.assertEqual(len(made), 2)
        self.assertEqual(len(backups), 3)

    def test_invalid_keep_is_refused_before_anything_is_deleted(self):
        self.dest.mkdir()
        (self.dest / "partstrack-2020-01-01_000000.tar.gz").write_bytes(b"old")
        for keep in ("0", "-1", "five", "3x"):
            with self.subTest(keep=keep):
                result = self.run_script(keep)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("KEEP", result.stderr)
                self.assertEqual(self.backups(), ["partstrack-2020-01-01_000000.tar.gz"])


if __name__ == "__main__":
    unittest.main()
