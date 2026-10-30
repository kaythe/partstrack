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
import unittest

from partstrack import __version__
from partstrack.cli import build_parser


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


if __name__ == "__main__":
    unittest.main()
