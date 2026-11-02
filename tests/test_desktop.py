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

    def test_minimum_low_filter_and_history(self):
        dialog = self.new_part_dialog(name="NE555")
        dialog.quantity.setValue(4)
        dialog.location.setEditText("Box")
        dialog.min_quantity.setValue(5)
        dialog.accept()
        with self.inventory() as inv:
            inv.add_part("LM358")
        self.window.current_part_id = dialog.part_id
        self.window.refresh()
        self.window.low_filter.setChecked(True)
        self.assertEqual(self.table_names(), ["NE555"])

        part = self.window.current_part()
        dialog = desktop.StockDialog(self.window.inv, part, self.window, operation="add")
        dialog.amount.setValue(2)
        dialog.note.setText("restocked")
        dialog.accept()
        self.window.refresh()
        self.assertEqual(self.table_names(), [])
        dialog = desktop.MoveDialog(self.window.inv, self.window.inv.get_part(part.id), self.window)
        dialog.destination.setEditText("Bench")
        dialog.note.setText("testing")
        dialog.accept()
        with self.inventory() as inv:
            self.assertEqual([c.note for c in inv.stock_history(part.id)], ["testing", "testing", "restocked", "new part"])

        dialog = desktop.PartDialog(self.window.inv, self.window.inv.get_part(part.id), self.window)
        self.assertEqual(dialog.min_quantity.value(), 5)
        dialog.min_quantity.setValue(0)  # shown as "none"
        dialog.accept()
        with self.inventory() as inv:
            self.assertIsNone(inv.get_part(part.id).min_quantity)

    def test_print_labels_as_pdf(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555", package="DIP-8")
            inv.set_stock(part_id, "Lab/Cabinet A/Drawer 3", 5)
            inv.add_location("Shelf")
        self.window.refresh()
        settings = self.window.settings
        dialog = desktop.LabelDialog(self.window.inv, settings, self.window, location_paths=["Lab/Cabinet A"],
                                     part_ids=[part_id])
        self.addCleanup(dialog.deleteLater)
        pdf = self.dir / "labels.pdf"
        self.assertFalse(dialog.save_pdf(pdf))  # no address yet
        self.assertIn("address", self.errors[-1])

        dialog.url.setText("http://parts.lan:8080")
        dialog.layout_combo.setCurrentIndex(dialog.layout_combo.findData("printer-62x29"))
        self.assertEqual(dialog.start.maximum(), 1)
        self.assertEqual([l.path for l in dialog.checked_locations()], ["Lab/Cabinet A", "Lab/Cabinet A/Drawer 3"])
        self.assertTrue(dialog.save_pdf(pdf))
        self.assertEqual(pdf.read_bytes().count(b"/Type /Page\n") + pdf.read_bytes().count(b"/Type /Page\r"), 3)

        dialog.sublocations.setChecked(False)
        dialog.layout_combo.setCurrentIndex(dialog.layout_combo.findData("plain-4x10"))
        dialog.start.setValue(40)  # the last place: the second label goes on a new page
        self.assertTrue(dialog.save_pdf(pdf))
        self.assertEqual(pdf.read_bytes().count(b"/Type /Page\n") + pdf.read_bytes().count(b"/Type /Page\r"), 2)

        dialog.url.setText("http://localhost:8080")
        self.answers.append(False)
        self.assertFalse(dialog.save_pdf(pdf))  # declined the warning about a local address

        dialog = desktop.LabelDialog(self.window.inv, settings, self.window)  # remembers the choices
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.layout_combo.currentData(), "plain-4x10")
        self.assertEqual(dialog.url.text(), "http://parts.lan:8080/")
        self.assertFalse(dialog.sublocations.isChecked())
        dialog.part_filter.setText("555")
        self.assertFalse(dialog.part_list.topLevelItem(0).isHidden())
        dialog.part_filter.setText("xyz")
        self.assertTrue(dialog.part_list.topLevelItem(0).isHidden())

    def test_label_dialog_layout_fields_and_preview_size(self):
        dialog = desktop.LabelDialog(self.window.inv, self.window.settings, self.window)
        self.addCleanup(dialog.deleteLater)
        enabled = lambda: {name for name, field in dialog.size_fields.items() if field.isEnabled()}
        self.assertEqual(enabled(), set())  # a preset
        dialog.layout_combo.setCurrentIndex(dialog.layout_combo.findData("custom-printer"))
        self.assertEqual(enabled(), {"label_width", "label_height"})
        dialog.layout_combo.setCurrentIndex(dialog.layout_combo.findData("custom-sheet"))
        self.assertEqual(enabled(), set(dialog.size_fields))
        self.assertTrue(dialog.custom_box.isVisibleTo(dialog))

        preview = dialog.preview_dialog(desktop.QPrinter())
        self.addCleanup(preview.deleteLater)
        screen = dialog.screen().availableGeometry()
        self.assertGreater(preview.width(), screen.width() // 2)
        preview.resize(700, 500)
        self.window.settings.setValue("labels/preview_geometry", preview.saveGeometry())
        again = dialog.preview_dialog(desktop.QPrinter())
        self.addCleanup(again.deleteLater)
        self.assertEqual((again.width(), again.height()), (700, 500))

    def test_duplicate_part(self):
        with self.inventory() as inv:
            part_id = inv.add_part("NE555", package="DIP-8")
            inv.add_tags(part_id, ["timer"])
            inv.set_min_quantity(part_id, 5)
            inv.set_stock(part_id, "Box", 10)
        self.window.current_part_id = part_id
        self.window.refresh()
        dialog = desktop.PartDialog(self.window.inv, None, self.window, copy_of=self.window.current_part())
        self.assertEqual(dialog.windowTitle(), "New part (copy of NE555)")
        self.assertEqual(dialog.quantity.value(), 0)
        dialog.accept()  # the name is still taken
        self.assertIn("already exists", self.errors[-1])
        dialog.fields["name"].setText("NE556")
        dialog.accept()
        with self.inventory() as inv:
            copy = inv.get_part(dialog.part_id)
        self.assertEqual((copy.name, copy.package, copy.tags, copy.min_quantity, copy.stock),
                         ("NE556", "DIP-8", ["timer"], 5, []))

    def test_attribute_search(self):
        with self.inventory() as inv:
            inv.set_attributes(inv.add_part("R1"), {"resistance": "4k7"})
            inv.set_attributes(inv.add_part("R2"), {"resistance": "10k"})
        self.window.refresh()
        self.window.search.setText("resistance>=10k")
        self.window.refresh_parts()
        self.assertEqual(self.table_names(), ["R2"])
        self.window.search.setText("resistance>=lots")
        self.window.refresh_parts()
        self.assertEqual(self.table_names(), [])
        self.assertIn("not a number", self.window.statusBar().currentMessage())

    def wait_for_copy(self):
        deadline = time.monotonic() + 10
        while self.window._copy_downloads and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self.window._copy_downloads, "the copy download did not finish")

    def test_download_copy_from_server(self):
        from partstrack.snapshot import write_snapshot

        with self.inventory() as server:
            part_id = server.add_part("NE555")
            server.set_stock(part_id, "Box", 5)
            write_snapshot(server, self.dir / "snapshot.tar.gz")
            server.add_part("LM358")
            write_snapshot(server, self.dir / "snapshot2.tar.gz")
        folder = self.dir / "copy"
        routes = {"/api/snapshot": (200, "application/gzip", (self.dir / "snapshot.tar.gz").read_bytes())}
        with serve(routes) as url:
            self.window.download_copy(url, folder)
            self.wait_for_copy()
            self.assertEqual(self.errors, [])
            self.assertEqual(self.window.db_path, folder / "parts.db")  # opened, after confirm() said yes
            self.assertTrue(self.window.read_only)
            self.assertIn("(read-only)", self.window.windowTitle())
            self.assertIn(url, self.window.copy_label.text())
            self.assertEqual(self.table_names(), ["NE555"])

            self.window.current_part_id = part_id
            self.window.refresh()
            self.assertFalse(any(a.isEnabled() for a in self.window.part_actions))
            self.assertFalse(self.window.new_part_action.isEnabled())
            self.assertFalse(any(b.isEnabled() for b in self.window.location_write_buttons))
            self.window.edit_part()  # e.g. by double-click: refused without a dialog
            self.assertIn("read-only copy", self.window.statusBar().currentMessage())
            self.window.attempt(self.window.inv.delete_part, part_id)
            self.assertIn("read-only copy", self.errors[-1])

            # A later download replaces the copy that is open.
            routes["/api/snapshot"] = (200, "application/gzip", (self.dir / "snapshot2.tar.gz").read_bytes())
            self.window.download_copy(url, folder)
            self.wait_for_copy()
            self.assertEqual(self.table_names(), ["LM358", "NE555"])
            self.assertTrue(self.window.read_only)
            self.assertEqual([p.name for p in folder.iterdir() if p.name.startswith(".partstrack-")], [])

        self.assertTrue(self.window.open_database(self.db))  # back to the own database: writable
        self.assertFalse(self.window.read_only)
        self.assertTrue(self.window.new_part_action.isEnabled())
        self.assertEqual(self.window.copy_label.text(), "")

    def test_copy_errors_keep_the_open_database(self):
        (self.dir / "mine").mkdir()
        (self.dir / "mine" / "notes.txt").write_text("mine")
        with serve({}) as url:
            self.window.download_copy(url, self.dir / "copy")
            self.wait_for_copy()
            self.assertIn("partstrack server", self.errors[-1])
        with self.inventory() as server:
            from partstrack.snapshot import write_snapshot
            write_snapshot(server, self.dir / "snapshot.tar.gz")
        with serve({"/api/snapshot": (200, "application/gzip", (self.dir / "snapshot.tar.gz").read_bytes())}) as url:
            self.window.download_copy(url, self.dir / "mine")
            self.wait_for_copy()
        self.assertIn("contains other files", self.errors[-1])
        self.assertEqual(self.window.db_path, self.db)
        self.assertFalse(self.window.read_only)
        self.assertEqual(sorted(p.name for p in (self.dir / "mine").iterdir()), ["notes.txt"])

    def test_open_backup(self):
        from partstrack.snapshot import write_snapshot

        with self.inventory() as inv:
            inv.add_part("NE555")
            write_snapshot(inv, self.dir / "partstrack-2026-11-01_030000.tar.gz")
            inv.add_part("LM358")
            write_snapshot(inv, self.dir / "partstrack-2026-11-02_030000.tar.gz")
        folder = self.dir / "opened-backup"
        self.window.settings.setValue("backup/folder", str(folder))

        self.assertTrue(self.window.open_backup(self.dir / "partstrack-2026-11-01_030000.tar.gz"))
        self.assertEqual(self.window.db_path, folder / "parts.db")
        self.assertTrue(self.window.read_only)
        self.assertIn("backup partstrack-2026-11-01_030000.tar.gz from", self.window.windowTitle())
        self.assertEqual(self.table_names(), ["NE555"])

        self.assertTrue(self.window.open_backup(self.dir / "partstrack-2026-11-02_030000.tar.gz"))  # replaces it
        self.assertEqual(self.table_names(), ["LM358", "NE555"])
        self.assertIn("2026-11-02_030000", self.window.copy_label.text())

        (self.dir / "broken.tar.gz").write_bytes(b"nope")
        self.assertFalse(self.window.open_backup(self.dir / "broken.tar.gz"))
        self.assertIn("not a valid snapshot or backup", self.errors[-1])
        self.assertEqual(self.table_names(), ["LM358", "NE555"])  # the open backup is still there
        self.assertTrue(self.window.read_only)

    def test_server_address_is_shared_with_labels(self):
        self.window.settings.setValue("labels/url", "http://old.lan/")  # saved by an older version
        self.assertEqual(desktop.server_url(self.window.settings), "http://old.lan/")
        dialog = desktop.LabelDialog(self.window.inv, self.window.settings, self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.url.text(), "http://old.lan/")

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
        self.assertIn("GNU General Public License", texts)
        self.assertTrue(any(not label.pixmap().isNull() for label in dialog.findChildren(desktop.QLabel)))


if __name__ == "__main__":
    unittest.main()
