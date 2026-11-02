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

import sqlite3
import tempfile
import unittest
from pathlib import Path

from partstrack.store import MIGRATIONS, Inventory, InventoryError, NotFoundError


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.inv = Inventory(self.dir / "parts.db")

    def tearDown(self):
        self.inv.close()
        self.tmp.cleanup()

    def test_add_and_get_part(self):
        part_id = self.inv.add_part("NE555", manufacturer="TI", package="DIP-8")
        part = self.inv.get_part(part_id)
        self.assertEqual((part.name, part.manufacturer, part.package), ("NE555", "TI", "DIP-8"))
        self.assertEqual(part.total_quantity, 0)

    def test_duplicate_name_rejected_case_insensitively(self):
        self.inv.add_part("NE555")
        with self.assertRaises(InventoryError):
            self.inv.add_part("ne555")

    def test_find_part_by_name_then_id(self):
        first = self.inv.add_part("LM358")
        named_like_id = self.inv.add_part(str(first))
        self.assertEqual(self.inv.find_part("lm358"), first)
        self.assertEqual(self.inv.find_part(str(first)), named_like_id)
        self.assertEqual(self.inv.find_part(f"#{first}"), first)
        with self.assertRaises(NotFoundError):
            self.inv.find_part("nope")

    def test_stock_add_take_set_and_move(self):
        part_id = self.inv.add_part("10k resistor")
        self.assertEqual(self.inv.adjust_stock(part_id, "Drawer A1", 100), 100)
        self.assertEqual(self.inv.adjust_stock(part_id, "Drawer A1", -30), 70)
        with self.assertRaises(InventoryError):
            self.inv.adjust_stock(part_id, "Drawer A1", -71)
        self.assertEqual(self.inv.move_stock(part_id, "Drawer A1", "Bench", 20), 20)
        part = self.inv.get_part(part_id)
        self.assertEqual({s.location: s.quantity for s in part.stock}, {"Bench": 20, "Drawer A1": 50})
        self.inv.set_stock(part_id, "Bench", 0)
        self.assertEqual([s.location for s in self.inv.get_part(part_id).stock], ["Drawer A1"])

    def test_failed_move_changes_nothing(self):
        part_id = self.inv.add_part("BC547")
        self.inv.set_stock(part_id, "Box", 5)
        with self.assertRaises(InventoryError):
            self.inv.move_stock(part_id, "Box", "Box", 2)
        self.assertEqual(self.inv.get_part(part_id).total_quantity, 5)

    def test_tags_and_attributes(self):
        part_id = self.inv.add_part("1N4148")
        self.inv.add_tags(part_id, ["diode", "SMD", "diode"])
        self.inv.set_attribute(part_id, "Vr", "100V")
        self.inv.set_attribute(part_id, "vr", "75V")
        part = self.inv.get_part(part_id)
        self.assertEqual(part.tags, ["diode", "SMD"])
        self.assertEqual(part.attributes, {"vr": "75V"})
        self.inv.remove_tags(part_id, ["smd"])
        self.assertEqual(self.inv.list_tags(), [("diode", 1)])

    def test_search_and_filters(self):
        a = self.inv.add_part("10k resistor", package="0805", category="Resistors")
        b = self.inv.add_part("ATmega328P", category="MCU")
        self.inv.add_tags(b, ["avr"])
        self.inv.set_attribute(a, "tolerance", "1%")
        self.inv.set_stock(a, "Drawer A1", 10)
        names = lambda parts: [p.name for p in parts]
        self.assertEqual(names(self.inv.list_parts()), ["10k resistor", "ATmega328P"])
        self.assertEqual(names(self.inv.list_parts("resistor 0805")), ["10k resistor"])
        self.assertEqual(names(self.inv.list_parts("1%")), ["10k resistor"])
        self.assertEqual(names(self.inv.list_parts("AVR")), ["ATmega328P"])
        self.assertEqual(names(self.inv.list_parts(tag="avr")), ["ATmega328P"])
        self.assertEqual(names(self.inv.list_parts(location="drawer a1")), ["10k resistor"])
        self.assertEqual(names(self.inv.list_parts(category="mcu")), ["ATmega328P"])

    def test_file_and_url_attachments(self):
        part_id = self.inv.add_part("ESP32")
        src = self.dir / "esp32.pdf"
        src.write_bytes(b"%PDF-1.4")
        file_id = self.inv.add_attachment(part_id, str(src))
        self.inv.add_attachment(part_id, str(src))  # same name again is stored separately
        self.inv.add_attachment(part_id, "https://example.com/esp32.jpg")
        part = self.inv.get_part(part_id)
        kinds = sorted(a.kind for a in part.attachments)
        self.assertEqual(kinds, ["datasheet", "datasheet", "image"])
        stored = self.inv.get_attachment(file_id).path
        self.assertTrue(stored.is_file())
        self.inv.remove_attachment(file_id)
        self.assertFalse(stored.exists())
        self.inv.delete_part(part_id)
        self.assertFalse((self.inv.attachments_dir / str(part_id)).exists())

    def test_location_with_stock_cannot_be_deleted(self):
        part_id = self.inv.add_part("LED red")
        self.inv.set_stock(part_id, "Shelf", 3)
        with self.assertRaises(InventoryError):
            self.inv.delete_location("Shelf")
        self.inv.update_location("Shelf", new_name="Shelf 2")
        self.inv.set_stock(part_id, "Shelf 2", 0)
        self.inv.delete_location("Shelf 2")
        self.assertEqual(self.inv.list_locations(), [])

    def test_reopening_keeps_data(self):
        self.inv.add_part("TL431")
        self.inv.close()
        self.inv = Inventory(self.dir / "parts.db")
        self.assertEqual([p.name for p in self.inv.list_parts()], ["TL431"])

    def test_nested_locations(self):
        part_id = self.inv.add_part("NE555")
        self.inv.set_stock(part_id, "Cabinet A/Drawer 1", 5)
        self.inv.set_stock(part_id, "Drawer 1", 2)  # unique trailing name: same drawer
        self.inv.set_stock(part_id, "Cabinet B/Drawer 1", 3)
        with self.assertRaises(InventoryError):  # now ambiguous
            self.inv.adjust_stock(part_id, "drawer 1", 1)
        self.assertEqual(
            [(s.location, s.quantity) for s in self.inv.get_part(part_id).stock],
            [("Cabinet A/Drawer 1", 2), ("Cabinet B/Drawer 1", 3)],
        )
        self.assertEqual(
            [l.path for l in self.inv.list_locations()],
            ["Cabinet A", "Cabinet A/Drawer 1", "Cabinet B", "Cabinet B/Drawer 1"],
        )
        self.assertEqual(len(self.inv.list_parts(location="Cabinet B")), 1)

    def test_leading_slash_means_full_path(self):
        part_id = self.inv.add_part("LM317")
        self.inv.set_stock(part_id, "Shelf/Box", 1)
        self.inv.set_stock(part_id, "/Box", 4)
        self.assertEqual(
            [(s.location, s.quantity) for s in self.inv.get_part(part_id).stock],
            [("Box", 4), ("Shelf/Box", 1)],
        )
        self.assertEqual(self.inv.location_path("Box"), "Box")  # exact match wins

    def test_move_and_rename_location(self):
        part_id = self.inv.add_part("BC547")
        self.inv.set_stock(part_id, "Box", 5)
        self.inv.update_location("Box", new_name="Box 1", parent="Cabinet A")
        self.assertEqual(self.inv.get_part(part_id).stock[0].location, "Cabinet A/Box 1")
        with self.assertRaises(InventoryError):
            self.inv.update_location("Cabinet A", parent="Box 1")
        with self.assertRaises(InventoryError):
            self.inv.delete_location("Cabinet A")  # not empty
        self.inv.update_location("Box 1", top_level=True)
        self.assertEqual(self.inv.get_part(part_id).stock[0].location, "Box 1")
        with self.assertRaises(InventoryError):
            self.inv.add_location("Box 1")
        with self.assertRaises(InventoryError):
            self.inv.update_location("Box 1", new_name="a/b")

    def test_transaction_rolls_back_everything(self):
        src = self.dir / "x.pdf"
        src.write_bytes(b"%PDF")
        with self.assertRaises(InventoryError):
            with self.inv.transaction():
                part_id = self.inv.add_part("TL072")
                self.inv.set_stock(part_id, "Bin", 3)
                self.inv.add_attachment(part_id, str(src))
                self.inv.add_part("tl072")  # duplicate
        self.assertEqual(self.inv.list_parts(), [])
        self.assertEqual(list(self.inv.attachments_dir.rglob("*.pdf")), [])

    def test_failed_nested_transaction_is_undone_on_its_own(self):
        src = self.dir / "x.pdf"
        src.write_bytes(b"%PDF")
        with self.inv.transaction():
            part_id = self.inv.add_part("TL072")
            self.inv.add_location("A")
            with self.assertRaises(InventoryError):
                # Creates "A/New" before noticing the cycle; that must be undone.
                self.inv.update_location("A", parent="A/New")
            with self.assertRaises(InventoryError):
                with self.inv.transaction():
                    self.inv.add_attachment(part_id, str(src))
                    raise InventoryError("give up")
        self.assertEqual([l.path for l in self.inv.list_locations()], ["A"])
        self.assertEqual(self.inv.get_part(part_id).attachments, [])
        self.assertEqual(list(self.inv.attachments_dir.rglob("*.pdf")), [])

    def test_setting_zero_at_unknown_location_does_nothing(self):
        part_id = self.inv.add_part("LM7805")
        self.inv.set_stock(part_id, "Nowhere", 0)
        self.assertEqual(self.inv.list_locations(), [])

    def test_unknown_location_id(self):
        with self.assertRaises(NotFoundError):
            self.inv.location_path(999)

    def test_part_names_cannot_start_with_hash(self):
        with self.assertRaises(InventoryError):
            self.inv.add_part("#2 screw")
        part_id = self.inv.add_part("M2 screw")
        with self.assertRaises(InventoryError):
            self.inv.update_part(part_id, name=" #2 screw")

    def test_location_names_compare_like_sqlite(self):
        # SQLite's NOCASE only folds ASCII, so these are two different locations.
        self.inv.add_location("Straße")
        self.inv.add_location("STRASSE")
        self.assertEqual(self.inv.location_path("strasse"), "STRASSE")
        self.assertEqual(self.inv.location_path("straße"), "Straße")

    def test_low_stock(self):
        low = self.inv.add_part("NE555")
        empty = self.inv.add_part("LM358")
        enough = self.inv.add_part("BC547")
        self.inv.add_part("1N4148")  # no minimum: never low
        self.inv.set_stock(low, "Box", 3)
        self.inv.set_stock(low, "Bench", 1)
        self.inv.set_stock(enough, "Box", 10)
        self.inv.set_min_quantity(low, 5)
        self.inv.set_min_quantity(empty, 1)
        self.inv.set_min_quantity(enough, 10)  # exactly the minimum is not low
        self.assertEqual([p.name for p in self.inv.list_parts(low=True)], ["LM358", "NE555"])
        self.assertEqual([p.name for p in self.inv.list_parts(location="Box", low=True)], ["NE555"])
        self.assertTrue(self.inv.get_part(low).is_low)
        self.assertFalse(self.inv.get_part(enough).is_low)
        self.inv.set_min_quantity(low, None)
        self.assertIsNone(self.inv.get_part(low).min_quantity)
        with self.assertRaises(InventoryError):
            self.inv.set_min_quantity(empty, -1)

    def test_stock_history(self):
        part_id = self.inv.add_part("10k resistor")
        other = self.inv.add_part("NE555")
        self.inv.set_stock(part_id, "Cabinet/Drawer 1", 100, note="initial count")
        self.inv.adjust_stock(part_id, "Drawer 1", -30, note="  amp board  ")
        self.inv.set_stock(part_id, "Drawer 1", 70)  # unchanged: not recorded
        self.inv.move_stock(part_id, "Drawer 1", "Bench", 20)
        self.inv.set_stock(other, "Bench", 5)
        changes = self.inv.stock_history(part_id)
        self.assertEqual(
            [(c.location, c.delta, c.quantity, c.action, c.note) for c in reversed(changes)],
            [
                ("Cabinet/Drawer 1", 100, 100, "set", "initial count"),
                ("Cabinet/Drawer 1", -30, 70, "adjust", "amp board"),
                ("Cabinet/Drawer 1", -20, 50, "move", ""),
                ("Bench", 20, 20, "move", ""),
            ],
        )
        self.assertEqual(changes[0].part_name, "10k resistor")
        self.assertEqual(len(self.inv.stock_history()), 5)
        self.assertEqual(len(self.inv.stock_history(limit=2)), 2)
        self.assertEqual([c.delta for c in self.inv.stock_history(location="Cabinet")], [-20, -30, 100])

    def test_failed_stock_change_leaves_no_history(self):
        part_id = self.inv.add_part("BC547")
        self.inv.set_stock(part_id, "Box", 5)
        with self.assertRaises(InventoryError):
            with self.inv.transaction():
                self.inv.adjust_stock(part_id, "Box", -2)
                self.inv.adjust_stock(part_id, "Box", -10)
        self.assertEqual([c.delta for c in self.inv.stock_history(part_id)], [5])

    def test_history_outlives_its_location_but_not_its_part(self):
        part_id = self.inv.add_part("BC547")
        self.inv.set_stock(part_id, "Shelf/Box", 5)
        self.inv.move_stock(part_id, "Box", "Bench")
        self.inv.delete_location("/Shelf/Box")
        self.inv.update_location("Bench", new_name="Desk")
        self.assertEqual(
            [c.location for c in self.inv.stock_history(part_id)], ["Bench", "Shelf/Box", "Shelf/Box"]
        )
        self.inv.delete_part(part_id)
        self.assertEqual(self.inv.stock_history(), [])


class MigrationTest(unittest.TestCase):
    def test_v1_flat_locations_become_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "parts.db"
            conn = sqlite3.connect(db)
            conn.executescript(MIGRATIONS[0] + "; PRAGMA user_version = 1;")
            conn.executescript("""
                INSERT INTO parts (id, name) VALUES (1, 'NE555');
                INSERT INTO locations (id, name) VALUES (1, 'Cabinet 1'), (2, 'Cabinet 1/Drawer 4'),
                                                       (3, ' cabinet 1 / drawer 4 '), (4, 'Bench');
                INSERT INTO stock VALUES (1, 2, 5), (1, 3, 2), (1, 4, 1);
            """)
            conn.close()
            with Inventory(db) as inv:
                self.assertEqual(
                    [l.path for l in inv.list_locations()], ["Bench", "Cabinet 1", "Cabinet 1/Drawer 4"]
                )
                part = inv.get_part(1)
                self.assertEqual(
                    [(s.location, s.quantity) for s in part.stock], [("Bench", 1), ("Cabinet 1/Drawer 4", 7)]
                )
                with self.assertRaises(InventoryError):  # names are unique per parent now
                    inv.add_location("Cabinet 1/drawer 4")

    def test_v3_gets_minimum_and_initial_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "parts.db"
            conn = sqlite3.connect(db)
            for migration in MIGRATIONS[:3]:
                migration(conn) if callable(migration) else conn.executescript(migration)
            conn.executescript("""
                PRAGMA user_version = 3;
                INSERT INTO parts (id, name) VALUES (1, 'NE555'), (2, 'LM358');
                INSERT INTO locations (id, parent_id, name) VALUES (1, NULL, 'Cabinet'), (2, 1, 'Drawer 4'),
                                                                   (3, NULL, 'Bench');
                INSERT INTO stock VALUES (1, 2, 5), (1, 3, 1), (2, 3, 7);
            """)
            conn.close()
            with Inventory(db) as inv:
                self.assertIsNone(inv.get_part(1).min_quantity)
                self.assertEqual(
                    [(c.part_name, c.location, c.delta, c.quantity, c.action)
                     for c in reversed(inv.stock_history())],
                    [("NE555", "Bench", 1, 1, "initial"), ("NE555", "Cabinet/Drawer 4", 5, 5, "initial"),
                     ("LM358", "Bench", 7, 7, "initial")],
                )


if __name__ == "__main__":
    unittest.main()
