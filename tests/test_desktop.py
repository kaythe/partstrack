import os
import tempfile
import time
import unittest
from pathlib import Path

from partstrack.store import Inventory

from .fileserver import serve

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed
try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from partstrack.desktop import app as desktop
except ImportError:  # the optional desktop dependency is not installed
    desktop = None


@unittest.skipIf(desktop is None, "needs the 'desktop' extra (PySide6)")
class DesktopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "parts.db"
        self.errors = []
        self.answers = []  # replies to confirm(); yes when empty
        self._patch("show_error", lambda parent, message: self.errors.append(str(message)))
        self._patch("confirm", lambda parent, question, details="": self.answers.pop(0) if self.answers else True)
        self.window = desktop.MainWindow(self.db, QSettings(str(self.dir / "settings.ini"), QSettings.Format.IniFormat))
        self.assertTrue(self.window.open_database(self.db))

    def tearDown(self):
        self.window.close()
        self.tmp.cleanup()

    def _patch(self, name, replacement):
        original = getattr(desktop, name)
        setattr(desktop, name, replacement)
        self.addCleanup(setattr, desktop, name, original)

    def inventory(self):
        return Inventory(self.db)

    def table_names(self):
        return [self.window.table.item(row, 0).text() for row in range(self.window.table.rowCount())]

    def wait_for_downloads(self):
        deadline = time.monotonic() + 10
        while self.window._downloads and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window._downloads, "downloads did not finish")

    def new_part_dialog(self, **values):
        dialog = desktop.PartDialog(self.window.inv, None, self.window)
        for name, value in values.items():
            widget = dialog.fields[name]
            widget.setEditText(value) if name == "category" else widget.setText(value)
        return dialog

    def test_add_part_with_stock_attachments_and_download(self):
        datasheet = self.dir / "ne555.pdf"
        datasheet.write_bytes(b"%PDF-1.4")
        with serve({"/pinout.png": (200, "image/png", b"PNG")}) as base:
            dialog = self.new_part_dialog(name="NE555", description="Timer", category="IC")
            dialog.tags.setText("timer, tht,")
            dialog.attributes.set_cell(0, 0, "vcc")
            dialog.attributes.set_cell(0, 1, "4.5-16V")
            dialog.quantity.setValue(20)
            dialog.location.setEditText("Cabinet A/Drawer 1")
            dialog.sources.addItems([str(datasheet), f"{base}/pinout.png", f"{base}/shop/ne555"])
            dialog.accept()
            self.assertEqual(self.errors, [])
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
            self.window.current_part_id = dialog.part_id
            self.window.refresh()
            self.window.start_downloads(dialog.link_ids)
            self.wait_for_downloads()

        with self.inventory() as inv:
            part = inv.get_part(dialog.part_id)
        self.assertEqual((part.name, part.category, part.tags), ("NE555", "IC", ["tht", "timer"]))
        self.assertEqual(part.attributes, {"vcc": "4.5-16V"})
        self.assertEqual([(s.location, s.quantity) for s in part.stock], [("Cabinet A/Drawer 1", 20)])
        stored = {a.original_name: (a.url is None, a.kind) for a in part.attachments}
        self.assertEqual(stored, {"ne555.pdf": (True, "datasheet"), "pinout.png": (True, "image"),
                                  "ne555": (False, "other")})
        self.assertEqual(self.table_names(), ["NE555"])
        self.assertEqual(self.window.part_view.part.id, part.id)
        self.assertIn("pinout.png", self.window.statusBar().currentMessage())

    def test_failed_add_keeps_dialog_open_and_saves_nothing(self):
        dialog = self.new_part_dialog(name="NE555")
        dialog.quantity.setValue(5)
        dialog.accept()
        self.assertEqual(self.errors, ["please choose where the parts are stored"])
        self.assertNotEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertIsNone(dialog.part_id)
        dialog.attributes.set_cell(0, 1, "value without a name")
        dialog.quantity.setValue(0)
        dialog.accept()
        self.assertIn("needs a name", self.errors[-1])
        with self.inventory() as inv:
            self.assertEqual(inv.list_parts(), [])

    def test_edit_part(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555")
            inv.set_tags(part_id, ["timer"])
            inv.set_attributes(part_id, {"vcc": "5V"})
        dialog = desktop.PartDialog(self.window.inv, self.window.inv.get_part(part_id), self.window)
        dialog.fields["name"].setText("NE555P")
        dialog.tags.setText("")
        dialog.attributes.set_cell(0, 0, "")
        dialog.attributes.set_cell(0, 1, "")
        dialog.accept()
        self.assertEqual(self.errors, [])
        with self.inventory() as inv:
            part = inv.get_part(part_id)
        self.assertEqual((part.name, part.tags, part.attributes), ("NE555P", [], {}))

    def test_tag_suggestions(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555")
            inv.set_tags(part_id, ["SMD", "smd-led", "timer", "tht"])
        edit = desktop.PartDialog(self.window.inv, None, self.window).tags

        def suggest(text, cursor=None):
            edit.setText(text)
            edit.setCursorPosition(len(text) if cursor is None else cursor)
            edit._suggest()
            model = edit.suggestions.completionModel()
            return [model.index(i, 0).data() for i in range(model.rowCount())]

        self.assertEqual(suggest("sm"), ["SMD", "smd-led"])
        self.assertEqual(suggest("smd, t"), ["tht", "timer"])
        self.assertEqual(suggest("timer, t"), ["tht"])  # already used tags aren't offered
        self.assertEqual(suggest("tim, smd", cursor=2), ["timer"])  # the tag at the cursor
        self.assertEqual(suggest("smd, "), [])
        self.assertEqual(suggest("xyz"), [])

        edit.setText("smd, ti")
        edit.insert_tag("timer")
        self.assertEqual((edit.text(), edit.cursorPosition()), ("smd, timer, ", 12))
        edit.setText("ti, smd")
        edit.setCursorPosition(2)
        edit.insert_tag("timer")
        self.assertEqual((edit.text(), edit.cursorPosition()), ("timer, smd", 7))

    def test_filters_and_selection(self):
        with self.inventory() as inv:
            for name, tag, location in [("NE555", "timer", "Lab/Drawer 1"), ("LM358", "opamp", "Lab/Drawer 2"),
                                        ("10k", "", "Bench")]:
                part_id = inv.add_part(name, category="IC" if tag else "Resistor")
                inv.set_tags(part_id, [tag] if tag else [])
                inv.set_stock(part_id, location, 1)
        self.window.refresh()
        self.assertEqual(self.table_names(), ["10k", "LM358", "NE555"])
        self.window.location_filter.setCurrentIndex(self.window.location_filter.findData("/Lab"))
        self.assertEqual(self.table_names(), ["LM358", "NE555"])
        self.window.tag_filter.setCurrentIndex(self.window.tag_filter.findData("timer"))
        self.assertEqual(self.table_names(), ["NE555"])
        self.window.table.selectRow(0)
        self.assertEqual(self.window.part_view.part.name, "NE555")
        self.window.tag_filter.setCurrentIndex(0)
        self.window.search.setText("lm3")
        self.window.refresh_parts()  # instead of waiting for the search timer
        self.assertEqual(self.table_names(), ["LM358"])
        self.assertEqual(self.window.part_view.part.name, "NE555")  # still shown, though filtered out

    def test_stock_and_move(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555")
            inv.set_stock(part_id, "Shelf/Box", 10)
            inv.add_location("Box")  # a second "Box", so bare names are ambiguous
        self.window.current_part_id = part_id
        self.window.refresh()
        part = self.window.current_part()

        dialog = desktop.StockDialog(self.window.inv, part, self.window)
        self.assertEqual(dialog.location.reference(), "/Shelf/Box")
        dialog.amount.setValue(3)
        dialog.accept()
        dialog = desktop.StockDialog(self.window.inv, part, self.window)
        dialog.amount.setValue(99)
        dialog.accept()
        self.assertIn("only 7", self.errors[-1])

        dialog = desktop.MoveDialog(self.window.inv, self.window.inv.get_part(part_id), self.window)
        dialog.quantity.setValue(2)
        dialog.destination.setEditText("Bench")
        dialog.accept()
        self.assertEqual(dialog.moved, 2)
        with self.inventory() as inv:
            stock = {s.location: s.quantity for s in inv.get_part(part_id).stock}
        self.assertEqual(stock, {"Bench": 2, "Shelf/Box": 5})

    def test_attachments_from_the_part_view(self):
        with serve({"/ds.pdf": (200, "application/pdf", b"%PDF")}) as base:
            with self.inventory() as inv:
                part_id = inv.add_part("LM358")
                attachment_id = inv.add_attachment(part_id, f"{base}/ds.pdf")
            self.window.current_part_id = part_id
            self.window.refresh()
            view = self.window.part_view
            view.attachments.setCurrentItem(view.attachments.topLevelItem(0))
            self.assertTrue(view.store_button.isEnabled())
            view.store_button.click()
            self.wait_for_downloads()
        attachment = self.window.inv.get_attachment(attachment_id)
        self.assertEqual((attachment.url, attachment.path.read_bytes()), (None, b"%PDF"))
        self.assertEqual(self.window.part_view.part.attachments[0].source_url, f"{base}/ds.pdf")

        view = self.window.part_view
        view.attachments.setCurrentItem(view.attachments.topLevelItem(0))
        self.assertFalse(view.store_button.isEnabled())
        self.answers = [False]
        view.remove_button.click()
        self.assertEqual(len(self.window.part_view.part.attachments), 1)
        view.remove_button.click()
        self.assertEqual(self.window.part_view.part.attachments, [])
        self.assertFalse(attachment.path.exists())

    def test_locations(self):
        dialog = desktop.LocationDialog(self.window.inv, None, self.window)
        dialog.path.setText("Lab/Cabinet A")
        dialog.description.setText("tall")
        dialog.accept()
        self.window.refresh()
        tree = self.window.location_tree
        self.assertEqual(tree.topLevelItem(0).text(0), "Lab")
        cabinet = tree.topLevelItem(0).child(0)
        self.assertEqual((cabinet.text(0), cabinet.text(3)), ("Cabinet A", "tall"))

        location = next(l for l in self.window.inv.list_locations() if l.path == "Lab/Cabinet A")
        dialog = desktop.LocationDialog(self.window.inv, location, self.window)
        self.assertEqual(dialog.parent_box.reference(), "/Lab")
        dialog.name.setText("Cabinet B")
        dialog.parent_box.setEditText("")
        dialog.accept()
        self.assertEqual(self.errors, [])
        self.window.refresh()
        self.assertEqual([tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())],
                         ["Cabinet B", "Lab"])
        tree.setCurrentItem(tree.topLevelItem(1))
        self.window.delete_location()
        self.assertEqual(tree.topLevelItemCount(), 1)

    def test_delete_part_asks_first(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555")
        self.window.refresh()
        self.window.table.selectRow(0)
        self.answers = [False]
        self.window.delete_part()
        self.assertEqual(self.table_names(), ["NE555"])
        self.window.delete_part()
        self.assertEqual(self.table_names(), [])
        self.assertIsNone(self.window.current_part())
        self.assertFalse(any(action.isEnabled() for action in self.window.part_actions))

    def test_csv_round_trip(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555", category="IC")
            inv.set_stock(part_id, "Drawer 1", 20)
        exported = self.dir / "parts.csv"
        self.window.export_file(exported, delimiter=";", bom=True)
        self.assertTrue(exported.read_bytes().startswith("﻿name;".encode()))

        exported.write_text(exported.read_text(encoding="utf-8-sig") + "LM358;;;;IC;;/Drawer 1;5;;;;;\n",
                            encoding="utf-8")
        self.answers = [False]  # declined after the check: nothing is saved
        self.assertIsNone(self.window.import_file(exported))
        self.assertEqual(self.table_names(), ["NE555"])
        result = self.window.import_file(exported)
        self.assertEqual((result.rows, len(result.created), len(result.updated)), (2, 1, 1))
        self.assertEqual(self.table_names(), ["LM358", "NE555"])

    def test_about_dialog(self):
        dialog = desktop.AboutDialog(self.db, self.window)
        texts = " ".join(label.text() for label in dialog.findChildren(desktop.QLabel))
        self.assertIn(desktop.__version__, texts)
        self.assertIn("https://github.com/kaythe/partstrack", texts)
        self.assertIn(str(self.db), texts)
        self.assertTrue(any(not label.pixmap().isNull() for label in dialog.findChildren(desktop.QLabel)))


if __name__ == "__main__":
    unittest.main()
