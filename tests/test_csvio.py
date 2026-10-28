import io
import tempfile
import unittest
from pathlib import Path

from partstrack.csvio import decode_csv, export_csv, import_csv
from partstrack.store import Inventory, InventoryError


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.inv = Inventory(self.dir / "a" / "parts.db")

    def tearDown(self):
        self.inv.close()
        self.tmp.cleanup()

    def import_text(self, text, inv=None, **kwargs):
        return import_csv(inv or self.inv, io.StringIO(text), base_dir=self.dir, **kwargs)

    def stock(self, name, inv=None):
        inv = inv or self.inv
        return {s.location: s.quantity for s in inv.get_part(inv.find_part(name)).stock}

    def test_round_trip(self):
        (self.dir / "ds.pdf").write_bytes(b"%PDF")
        part_id = self.inv.add_part("NE555", manufacturer="TI", notes="line 1\nline 2")
        self.inv.add_tags(part_id, ["timer", "tht"])
        self.inv.set_attribute(part_id, "Vcc", "4.5-16V")
        self.inv.set_stock(part_id, "Cabinet/Drawer 1", 5)
        self.inv.set_stock(part_id, "Bench", 2)
        self.inv.add_attachment(part_id, str(self.dir / "ds.pdf"))
        self.inv.add_attachment(part_id, "https://example.com/ne555.jpg")
        self.inv.add_part("Empty part")

        out = io.StringIO()
        self.assertEqual(export_csv(self.inv, out, delimiter=";"), 2)
        with Inventory(self.dir / "b" / "parts.db") as other:
            result = self.import_text(out.getvalue(), inv=other)
            self.assertEqual((result.rows, len(result.created), result.warnings), (3, 2, []))
            part = other.get_part(other.find_part("NE555"))
            self.assertEqual((part.manufacturer, part.notes), ("TI", "line 1\nline 2"))
            self.assertEqual(part.tags, ["tht", "timer"])
            self.assertEqual(part.attributes, {"Vcc": "4.5-16V"})
            self.assertEqual(self.stock("NE555", other), {"Bench": 2, "Cabinet/Drawer 1": 5})
            self.assertEqual(sorted(a.kind for a in part.attachments), ["datasheet", "image"])
            self.assertEqual(part.attachments[0].path.read_bytes(), b"%PDF")

            # Importing the same file again changes nothing.
            result = self.import_text(out.getvalue(), inv=other)
            self.assertEqual((len(result.created), len(result.updated)), (0, 2))
            self.assertEqual(len(other.get_part(part.id).attachments), 2)
            self.assertEqual(self.stock("NE555", other), {"Bench": 2, "Cabinet/Drawer 1": 5})

    def test_partial_columns_aliases_and_add_stock(self):
        self.inv.add_part("10k", package="0805")
        self.inv.set_stock(self.inv.find_part("10k"), "Reel", 100)
        text = "Name,Qty,Price\n10k,50,0.01\n100n,20,0.02\n"
        result = self.import_text(text, add_stock=True, default_location="Incoming")
        self.assertEqual(result.warnings, ["ignoring unknown column 'Price'"])
        self.assertEqual(self.stock("10k"), {"Reel": 100, "Incoming": 50})
        self.assertEqual(self.inv.get_part(self.inv.find_part("10k")).package, "0805")  # kept
        self.assertEqual(self.stock("100n"), {"Incoming": 20})

    def test_error_rolls_back_whole_import(self):
        text = "name,location,quantity\nA,Box,1\nB,,5\n"
        with self.assertRaisesRegex(InventoryError, "line 3"):
            self.import_text(text)
        self.assertEqual(self.inv.list_parts(), [])

    def test_dry_run_saves_nothing(self):
        result = self.import_text("name,location,quantity\nA,Box,1\n", dry_run=True)
        self.assertEqual(len(result.created), 1)
        self.assertEqual(self.inv.list_parts(), [])
        self.assertEqual(self.inv.list_locations(), [])

    def test_round_trip_keeps_top_level_and_nested_locations_apart(self):
        self.inv.set_stock(self.inv.add_part("A"), "Shelf/Box", 2)
        self.inv.set_stock(self.inv.add_part("B"), "/Box", 1)
        out = io.StringIO()
        export_csv(self.inv, out)
        with Inventory(self.dir / "b" / "parts.db") as other:
            self.import_text(out.getvalue(), inv=other)
            self.assertEqual(self.stock("A", other), {"Shelf/Box": 2})
            self.assertEqual(self.stock("B", other), {"Box": 1})

    def test_zero_quantity_at_new_location(self):
        result = self.import_text("name,location,quantity\nC,Nowhere,0\n")
        self.assertEqual(len(result.created), 1)
        self.assertEqual(self.inv.list_locations(), [])

    def test_decode_excel_cp1252(self):
        self.assertEqual(decode_csv("name\nL 4µ7\n".encode("cp1252")), "name\nL 4µ7\n")
        self.assertEqual(decode_csv("\ufeffname\nL 4µ7\n".encode("utf-8")), "name\nL 4µ7\n")

    def test_malformed_csv_is_a_user_error(self):
        with self.assertRaisesRegex(InventoryError, "invalid CSV"):
            self.import_text("name,notes\nA," + "x" * 200_000 + "\n")
        self.assertEqual(self.inv.list_parts(), [])


if __name__ == "__main__":
    unittest.main()
