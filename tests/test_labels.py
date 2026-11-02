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

import importlib.util
import unittest

from partstrack import labels
from partstrack.store import InventoryError, Location, Part

HAVE_SEGNO = importlib.util.find_spec("segno") is not None


def location(path, description=""):
    return Location(1, path.rpartition("/")[2], path, None, description, 0, 0)


class LayoutTest(unittest.TestCase):
    def test_presets_fit_their_pages(self):
        for layout in labels.LAYOUTS.values():
            with self.subTest(layout=layout.key):
                layout.check()
                self.assertGreaterEqual(labels.geometry(layout).text_width, 5)

    def test_positions_fill_rows_and_break_pages(self):
        layout = labels.LAYOUTS["sheet-3x8-70x37"]
        pages = labels.positions(layout, 26, start=2)
        self.assertEqual([len(page) for page in pages], [22, 4])
        self.assertEqual(pages[0][:2], [(140.0, 0.5), (0.0, 37.5)])
        self.assertEqual(pages[1][0], (0.0, 0.5))
        self.assertEqual(labels.positions(layout, 0), [])
        with self.assertRaises(InventoryError):
            labels.positions(layout, 1, start=24)

    def test_label_printer_has_one_label_per_page(self):
        pages = labels.positions(labels.LAYOUTS["printer-62x29"], 3)
        self.assertEqual(pages, [[(0.0, 0.0)]] * 3)

    def test_custom_layouts(self):
        layout = labels.layout_from_options({"layout": "custom-sheet", "columns": "2", "rows": "5",
                                             "label_width": "99,1", "label_height": "57", "margin_top": "5"})
        self.assertEqual((layout.columns, layout.label_width, layout.margin_top), (2, 99.1, 5.0))
        printer = labels.layout_from_options({"layout": "custom-printer", "label_width": "54",
                                              "label_height": "25", "columns": "9"})
        self.assertEqual((printer.page_width, printer.page_height, printer.per_page), (54.0, 25.0, 1))
        for options in [{"columns": "4"}, {"columns": "x"}, {"label_height": "5"}, {"gap_x": "-1"},
                        {"label_width": "nan"}]:
            with self.subTest(options=options), self.assertRaises(InventoryError):
                labels.layout_from_options({"layout": "custom-sheet", **options})
        with self.assertRaises(InventoryError):
            labels.layout_from_options({"layout": "nope"})
        self.assertEqual(labels.layout_from_options({}).key, labels.DEFAULT_LAYOUT)


class ContentTest(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(labels.location_url("http://parts.lan:8080", "Lab/Drawer #3"),
                         "http://parts.lan:8080/?location=/Lab/Drawer%20%233")
        self.assertEqual(labels.part_url("https://x/partstrack/", 12), "https://x/partstrack/parts/12")
        for bad in ["parts.lan", "ftp://x/", "http://"]:
            with self.subTest(url=bad), self.assertRaises(InventoryError):
                labels.normalize_base_url(bad)
        self.assertTrue(labels.is_local_url("http://127.0.0.1:8080/"))
        self.assertTrue(labels.is_local_url("http://localhost/"))
        self.assertFalse(labels.is_local_url("http://192.168.1.5/"))

    def test_label_texts(self):
        label = labels.location_label("http://x/", location("Lab/Cabinet A/Drawer 3", "SMD"))
        self.assertEqual((label.title, label.subtitle, label.detail), ("Drawer 3", "Lab/Cabinet A", "SMD"))
        both = labels.part_label("http://x/", Part(5, "NE555", package="DIP-8", mpn="NE555P", description="Timer"))
        self.assertEqual((both.url, both.title, both.subtitle, both.detail),
                         ("http://x/parts/5", "NE555", "DIP-8", "Timer"))
        self.assertEqual(labels.part_label("http://x/", Part(6, "LM358", mpn="LM358DR")).subtitle, "LM358DR")

    def test_select_locations(self):
        all_locations = [location(p) for p in ["Lab", "Lab/A", "Lab/A/1", "Lab/AB", "Shelf"]]
        paths = lambda found: [l.path for l in found]
        self.assertEqual(paths(labels.select_locations(all_locations, ["Lab/A", "Shelf"], sublocations=False)),
                         ["Lab/A", "Shelf"])
        self.assertEqual(paths(labels.select_locations(all_locations, ["Lab/A"], sublocations=True)),
                         ["Lab/A", "Lab/A/1"])
        self.assertEqual(labels.select_locations(all_locations, [], sublocations=True), [])

    @unittest.skipUnless(HAVE_SEGNO, "needs segno (in the 'web' and 'desktop' extras)")
    def test_qr_code(self):
        matrix = labels.qr_matrix("http://parts.lan:8080/?location=/Lab")
        self.assertEqual(len(matrix), len(matrix[0]))
        self.assertTrue(all(matrix[0][:7]))  # finder pattern
        path = labels.svg_path([[True, True, False], [False, True, False]])
        self.assertEqual(path, "M0 0h2v1h-2zM1 1h1v1h-1z")


if __name__ == "__main__":
    unittest.main()
