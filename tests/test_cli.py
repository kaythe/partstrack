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
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from partstrack import __version__
from partstrack.cli import build_parser, main
from partstrack.labels import qr_matrix
from partstrack.store import Inventory

QR_COLOURS = "\033[30;107m"  # black on bright white
RESET = "\033[0m"


class TerminalOutput(io.StringIO):
    def __init__(self, encoding="utf-8"):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding

    def write(self, text):
        text.encode(self._encoding)  # fail like a real terminal that can't show it
        return super().write(text)

    def isatty(self):
        return True


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

    def test_tokens(self):
        self.assertIn("No tokens yet.", self.run_cli("token", "list"))
        output = self.run_cli("token", "add", "Pixel 8")
        token = next(word for word in output.split() if word.startswith("pt_"))
        with Inventory(self.db) as inv:
            self.assertEqual(inv.check_token(token).name, "Pixel 8")
        listing = self.run_cli("token", "ls")
        self.assertIn("Pixel 8", listing)
        self.assertNotIn(token, listing)
        self.assertIn("Revoked token 'Pixel 8'", self.run_cli("token", "rm", "pixel 8"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--db", self.db, "token", "rm", "Pixel 8"]), 1)
        self.assertIn("no token 'Pixel 8'", err.getvalue())

    def add_token_on_terminal(self, name, encoding="utf-8"):
        out = TerminalOutput(encoding)
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--db", self.db, "token", "add", name]), 0)
        output = out.getvalue()
        token = next(word for word in output.split() if word.startswith("pt_"))
        with Inventory(self.db) as inv:
            self.assertEqual(inv.check_token(token).name, name)
        return output, token

    @unittest.skipUnless(importlib.util.find_spec("segno"), "needs the optional 'segno' package")
    def test_new_token_is_shown_as_qr_code_on_a_terminal(self):
        output, token = self.add_token_on_terminal("Pixel 8")
        # Read the modules back: black ('▀' top, '▄' bottom, '█' both) on white, two rows per line.
        lines = [line.removeprefix(QR_COLOURS).removesuffix(RESET) for line in output.splitlines()
                 if line.startswith(QR_COLOURS)]
        self.assertTrue(lines)
        rows = []
        for line in lines:
            self.assertTrue(set(line) <= set(" ▀▄█"), line)
            rows.append([c in "▀█" for c in line])
            rows.append([c in "▄█" for c in line])
        matrix = qr_matrix(token)
        quiet = [False] * 4
        expected = [[False] * (len(matrix) + 8)] * 4 + [quiet + row + quiet for row in matrix]
        expected += [[False] * (len(matrix) + 8)] * (len(rows) - len(expected))
        self.assertGreaterEqual(len(rows) - len(matrix), 8)  # a quiet zone of 4 rows at the bottom, too
        self.assertEqual(rows, expected)
        self.assertNotIn("\033", output.replace(QR_COLOURS, "").replace(RESET, ""))

    def test_new_token_without_segno_or_terminal_is_only_text(self):
        with mock.patch.dict(sys.modules, {"segno": None}):  # as if it weren't installed
            output, token = self.add_token_on_terminal("Pixel 8")
        self.assertIn(f"    {token}\n", output)
        self.assertNotIn("\033", output)
        self.assertNotIn("\033", self.run_cli("token", "add", "Pixel 9"))  # output isn't a terminal
        for encoding in ("ascii", "cp1252"):  # e.g. LANG=C; the token is saved, so don't fail after it
            with self.subTest(encoding=encoding):
                output, token = self.add_token_on_terminal(f"Pixel {encoding}", encoding)
                self.assertIn(f"    {token}\n", output)
                self.assertNotIn("\033", output)

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

    def test_numbers_beyond_the_database_range(self):
        self.run_cli("add", "NE555", "-q", "5", "-l", "Box")
        huge = "9" * 30
        for args in (["stock", "NE555", "Box", huge], ["stock", "NE555", "Box", "+" + huge],
                     ["add", "R2", "-q", huge, "-l", "Box"], ["add", "R3", "--min", huge],
                     ["detach", huge], ["show", "#" + huge]):
            with self.subTest(args=args):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--db", self.db, *args]), 1)
                self.assertTrue(err.getvalue().startswith("error: "), err.getvalue())
        self.assertIn("Stock (total 5)", self.run_cli("show", "NE555"))

    def run_failing(self, *args, db=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", db or self.db, *args]), 1)
        self.assertTrue(err.getvalue().startswith("error: "), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        return err.getvalue()

    def test_malformed_link_is_reported_without_traceback(self):
        self.run_cli("add", "NE555")
        self.assertIn("no such file: https://[", self.run_failing("attach", "NE555", "https://["))

    def test_file_and_database_errors_are_reported_without_traceback(self):
        self.run_cli("add", "NE555")
        self.assertIn("nonexistent", self.run_failing("export", "-o", str(Path(self.tmp.name) / "nonexistent" / "x.csv")))
        self.assertIn(self.tmp.name, self.run_failing("list", db=self.tmp.name))  # a folder
        not_a_db = Path(self.tmp.name) / "notes.txt"
        not_a_db.write_text("just text, " * 100)
        self.assertIn("notes.txt", self.run_failing("list", db=str(not_a_db)))

    def test_invalid_delimiter_is_a_usage_error(self):
        for delimiter in (";;", "", '"', "\n"):
            for command in (["export"], ["import", "x.csv"]):
                with self.subTest(delimiter=delimiter, command=command):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                        main(["--db", self.db, *command, "--delimiter", delimiter])
                    self.assertEqual(exit.exception.code, 2)
                    self.assertIn("--delimiter", err.getvalue())
        self.run_cli("export", "--delimiter", "\t", "-o", str(Path(self.tmp.name) / "tabs.csv"))

    def test_closed_output_pipe_is_not_an_error(self):
        with Inventory(self.db) as inv, inv.transaction():
            for i in range(3000):
                inv.add_part(f"Part number {i:05}", description="enough text to fill the pipe's buffer")
        process = subprocess.Popen([sys.executable, "-m", "partstrack", "--db", self.db, "list"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=Path(__file__).resolve().parent.parent)
        process.stdout.readline()
        process.stdout.close()  # like `partstrack list | head -1`
        stderr = process.stderr.read().decode()
        process.stderr.close()
        process.wait(timeout=30)
        self.assertEqual(stderr, "")

    @unittest.skipUnless(os.name == "nt", "only Windows refuses '?' in file names (with EINVAL, like a closed pipe)")
    def test_invalid_file_name_is_an_error_while_output_is_a_pipe(self):
        output = Path(self.tmp.name) / "a?b.csv"
        result = subprocess.run([sys.executable, "-m", "partstrack", "--db", self.db, "export", "-o", str(output)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=Path(__file__).resolve().parent.parent, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("a?b.csv", result.stderr)


if __name__ == "__main__":
    unittest.main()
