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

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from partstrack import __version__
from partstrack.cli import build_parser, main
from partstrack.store import Inventory


class CliTest(unittest.TestCase):
    def output(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args(args)
        return out.getvalue()

    def test_help_and_version_name_the_license(self):
        for args in (["--help"], ["--version"]):
            with self.subTest(args=args):
                self.assertIn("GNU GPL version 3 or later", self.output(*args))
        self.assertTrue(self.output("--version").startswith(f"partstrack {__version__}\n"))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "parts.db")

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--db", self.db, *args]), 0)
        return out.getvalue()

    def test_minimum_low_stock_and_history(self):
        self.run_cli("add", "NE555", "-q", "20", "-l", "Drawer 3", "--min", "10")
        self.run_cli("add", "LM358", "-q", "1", "-l", "Bench")
        self.assertIn("below the minimum of 10", self.run_cli("stock", "NE555", "Drawer 3", "-12", "-m", "amp board"))
        self.run_cli("move", "NE555", "Drawer 3", "Bench", "3", "-m", "testing")
        listing = self.run_cli("ls", "--low")
        self.assertIn("8 (min 10)", listing)
        self.assertNotIn("LM358", listing)
        self.assertIn("minimum 10, LOW", self.run_cli("show", "NE555"))

        history = self.run_cli("history", "NE555").splitlines()[2:]
        self.assertEqual(len(history), 4)
        self.assertIn("new part", history[0])
        self.assertIn("amp board", history[1])
        self.assertIn("-12", history[1])
        self.assertEqual(len(self.run_cli("history", "-n", "2").splitlines()), 4)
        self.assertIn("LM358", self.run_cli("history", "-l", "Bench"))

        self.run_cli("edit", "NE555", "--no-min")
        self.assertEqual(self.run_cli("ls", "--low").strip(), "No parts found.")


    def test_copy(self):
        self.run_cli("add", "NE555", "-p", "DIP-8", "-t", "timer", "-q", "5", "-l", "Box")
        self.assertIn("to part #2 'NE556'", self.run_cli("cp", "NE555", "NE556"))
        shown = self.run_cli("show", "NE556")
        self.assertIn("DIP-8", shown)
        self.assertIn("timer", shown)
        self.assertIn("Stock (total 0)", shown)

    def test_copies_are_read_only(self):
        from partstrack.snapshot import unpack_copy, write_snapshot

        self.run_cli("add", "NE555", "-q", "5", "-l", "Box")
        with Inventory(self.db) as inv:
            write_snapshot(inv, Path(self.tmp.name) / "s.tar.gz")
        copy = str(unpack_copy(Path(self.tmp.name) / "s.tar.gz", Path(self.tmp.name) / "copy", source="http://a/"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["--db", copy, "stock", "NE555", "Box", "-1"]), 1)
            self.assertEqual(main(["--db", copy, "show", "NE555"]), 0)
        self.assertIn("read-only copy", err.getvalue())
        self.assertIn("Stock (total 5)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
