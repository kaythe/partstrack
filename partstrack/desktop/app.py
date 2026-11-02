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

"""Desktop application for the parts inventory (Qt for Windows and Linux).

Needs the optional 'desktop' dependency (PySide6):
    pip install -e '.[desktop]'
    partstrack-desktop

The main window owns one Inventory, used only by the GUI thread. Downloads
run in a worker thread that opens its own Inventory (an SQLite connection may
only be used by the thread that created it) and report back through a Qt
signal. There are no change notifications, so every view is re-read from the
database after a write.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
from functools import wraps
from pathlib import Path

from PySide6.QtCore import QMarginsF, QObject, QRectF, QSettings, QSizeF, QStringListModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
    QTextOption,
)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrintPreviewDialog
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import LOGO_PATH, REPOSITORY_URL, __version__
from ..cli import default_db_path
from ..csvio import ImportResult, decode_csv, export_csv, import_csv
from ..snapshot import TEMP_PREFIX, CopyInfo, download_snapshot, open_inventory, read_copy_info, unpack_copy
from ..labels import (
    CUSTOM_LAYOUT_NAMES,
    CUSTOM_PRINTER,
    CUSTOM_SHEET,
    CUSTOM_SHEET_OPTIONS,
    DEFAULT_LAYOUT,
    LAYOUTS,
    Label,
    Layout,
    geometry,
    is_local_url,
    layout_from_options,
    location_label,
    normalize_base_url,
    part_label,
    positions,
    qr_matrix,
    select_locations,
)
from ..store import (
    EDITABLE_FIELDS,
    NEW_PART_NOTE,
    Attachment,
    Inventory,
    InventoryError,
    Location,
    NotFoundError,
    Part,
    downloadable_name,
    is_url,
    nocase,
)

MAX_QUANTITY = 1_000_000_000
FIELD_LABELS = {
    "name": "Name",
    "description": "Description",
    "manufacturer": "Manufacturer",
    "mpn": "MPN",
    "category": "Category",
    "package": "Package",
    "notes": "Notes",
}
THEMES = {
    "auto": ("Follow the system", Qt.ColorScheme.Unknown),
    "light": ("Light", Qt.ColorScheme.Light),
    "dark": ("Dark", Qt.ColorScheme.Dark),
}
# File dialog filter -> (delimiter, write a BOM). Excel in European locales
# expects ';' and only detects UTF-8 with a BOM.
EXPORT_FORMATS = {
    "CSV (*.csv)": (",", False),
    "CSV for Excel, separated by ; (*.csv)": (";", True),
    "Tab-separated (*.tsv)": ("\t", False),
}
ROLE_ID = Qt.ItemDataRole.UserRole
READ_ONLY_MESSAGE = "This is a read-only copy of a server's inventory; it can't be changed."


def server_url(settings: QSettings) -> str:
    """The web interface's address, for label QR codes and for copies of its inventory."""
    return settings.value("server/url", "") or settings.value("labels/url", "")  # older versions: labels/url
HISTORY_IN_PART_VIEW = 10  # newest stock changes shown with a part


# -- small helpers ------------------------------------------------------------
# show_error and confirm are module functions so tests can replace them.


def show_error(parent: QWidget | None, message: object) -> None:
    QMessageBox.warning(parent, "partstrack", str(message))


def confirm(parent: QWidget | None, question: str, details: str = "") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, "partstrack", question,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    if details:
        box.setDetailedText(details)
    return run_dialog(box) == QMessageBox.StandardButton.Yes


def run_dialog(dialog: QDialog) -> int:
    """Show a modal dialog, then delete it. A dialog with a parent otherwise
    lives on, hidden, until the parent goes; such leftovers crash the
    interpreter's shutdown. The Python attributes stay readable afterwards."""
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()


def open_attachment(parent: QWidget, attachment: Attachment) -> None:
    if attachment.url is not None:
        QDesktopServices.openUrl(QUrl(attachment.url))
    elif attachment.path is not None and attachment.path.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(attachment.path)))
    else:
        show_error(parent, f"the file {attachment.path} is missing")


def parse_tags(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def selectable_label(text: str, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def heading(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    for button in buttons:
        row.addWidget(button)
    row.addStretch()
    return row


def fill_combo(combo: QComboBox, items: list[tuple[str, object]], all_label: str) -> None:
    """Replace a filter combo's items, keeping the selection if it still exists."""
    current = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    combo.addItem(all_label, None)
    for text, data in items:
        combo.addItem(text, data)
    index = combo.findData(current) if current is not None else 0
    combo.setCurrentIndex(max(index, 0))
    combo.blockSignals(False)


class LocationBox(QComboBox):
    """An editable combo box for a location reference.

    Known locations are returned as exact '/'-paths, so suffix matching can't
    pick a different location with the same name. Typed text is returned as
    it is: stock operations create new locations, and a unique last part like
    'Drawer 3' still works.
    """

    def __init__(self, locations: list[Location], *, placeholder: str = "", current: str = ""):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(24)
        for location in locations:
            self.addItem(location.path, "/" + location.path)
        self.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.lineEdit().setPlaceholderText(placeholder)
        self.setEditText(current)

    def reference(self) -> str:
        text = self.currentText().strip()
        index = self.findText(text)
        return self.itemData(index) if index >= 0 else text


class TagEdit(QLineEdit):
    """Comma-separated tags. Suggests existing tags for the one being typed,
    leaving out tags that are already in the field."""

    def __init__(self, known_tags: list[str], text: str = ""):
        super().__init__(text)
        self.known_tags = known_tags
        self.setPlaceholderText("comma-separated, e.g. smd, timer")
        self.tag_model = QStringListModel(self)
        self.suggestions = QCompleter(self.tag_model, self)
        self.suggestions.setWidget(self)  # not setCompleter(), which would replace the whole text
        self.suggestions.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.suggestions.activated.connect(self.insert_tag)
        self.textEdited.connect(self._suggest)

    def _current_token(self) -> tuple[int, int]:
        """Start and end of the tag the cursor is in."""
        text, cursor = self.text(), self.cursorPosition()
        start = text.rfind(",", 0, cursor) + 1
        end = text.find(",", cursor)
        return start, len(text) if end < 0 else end

    def _suggest(self) -> None:
        start, end = self._current_token()
        prefix = self.text()[start:self.cursorPosition()].strip()
        if not prefix:
            self.tag_model.setStringList([])
            self.suggestions.popup().hide()
            return
        used = {nocase(t) for t in parse_tags(self.text()[:start] + self.text()[end:])}
        self.tag_model.setStringList([t for t in self.known_tags if nocase(t) not in used])
        self.suggestions.setCompletionPrefix(prefix)
        if self.suggestions.completionCount():
            self.suggestions.complete()
        else:
            self.suggestions.popup().hide()

    def insert_tag(self, tag: str) -> None:
        """Replace the tag being typed with `tag` and start the next one."""
        start, end = self._current_token()
        text = self.text()
        before = text[:start].rstrip()
        before = before + " " if before else ""
        after = text[end + 1:].strip() if end < len(text) else ""
        new = before + tag + ", "
        self.setText(new + after)
        self.setCursorPosition(len(new))


class AttributeTable(QTableWidget):
    """Key/value editor that always ends with an empty row for a new attribute."""

    def __init__(self, attributes: dict[str, str]):
        super().__init__(0, 2)
        self.setHorizontalHeaderLabels(["Attribute", "Value"])
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().hide()
        for key, value in attributes.items():
            self._append(key, value)
        self._append("", "")
        self.itemChanged.connect(self._keep_empty_row)

    def _append(self, key: str, value: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        self.setItem(row, 0, QTableWidgetItem(key))
        self.setItem(row, 1, QTableWidgetItem(value))

    def _cell(self, row: int, column: int) -> str:
        item = self.item(row, column)
        return item.text().strip() if item else ""

    def _keep_empty_row(self) -> None:
        last = self.rowCount() - 1
        if self._cell(last, 0) or self._cell(last, 1):
            self.blockSignals(True)
            self._append("", "")
            self.blockSignals(False)

    def set_cell(self, row: int, column: int, text: str) -> None:
        self.item(row, column).setText(text)

    def attributes(self) -> dict[str, str]:
        result = {}
        for row in range(self.rowCount()):
            key, value = self._cell(row, 0), self._cell(row, 1)
            if not key and not value:
                continue
            if not key:
                raise InventoryError(f"the attribute with the value '{value}' needs a name")
            result[key] = value
        return result


class ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class Downloader(QObject):
    """Stores the files behind links locally, in a worker thread."""

    # (this downloader, [(file name, error message or None)])
    finished = Signal(object, list)

    def __init__(self, db_path: Path, attachment_ids: list[int]):
        super().__init__()
        self.db_path = db_path
        self.attachment_ids = attachment_ids

    def start(self) -> None:
        threading.Thread(target=self._run, name="partstrack-download").start()

    def _run(self) -> None:
        results = []
        try:
            with Inventory(self.db_path) as inv:
                for attachment, error in inv.download_links(self.attachment_ids):
                    results.append((attachment.original_name, None if error is None else str(error)))
        except (InventoryError, sqlite3.Error, OSError) as e:
            results.append(("", str(e)))
        self.finished.emit(self, results)


class CopyDownloader(QObject):
    """Downloads a snapshot of a server's inventory to a file, in a worker thread.
    Unpacking happens in the GUI thread, which may have to close the old copy first."""

    progress = Signal(int)  # bytes received
    finished = Signal(object, object)  # (this downloader, error message or None)

    def __init__(self, url: str, folder: Path):
        super().__init__()
        self.url = url
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        for leftover in folder.glob(TEMP_PREFIX + "download-*"):  # from a download cut short by quitting
            leftover.unlink(missing_ok=True)
        handle, name = tempfile.mkstemp(prefix=TEMP_PREFIX + "download-", suffix=".tar.gz", dir=folder)
        os.close(handle)
        self.archive = Path(name)

    def start(self) -> None:
        # A daemon thread, so quitting doesn't wait for a large download to finish.
        threading.Thread(target=self._run, name="partstrack-copy", daemon=True).start()

    def _run(self) -> None:
        try:
            download_snapshot(self.url, self.archive, self.progress.emit)
        except InventoryError as e:
            self.finished.emit(self, str(e))
        else:
            self.finished.emit(self, None)


# -- dialogs ------------------------------------------------------------------
# Each dialog does its work in save(), inside a transaction, and stays open
# with the user's input if that fails.


class CopyDialog(QDialog):
    """Asks for the server's address and the folder for a read-only copy."""

    def __init__(self, url: str, folder: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Download copy from server")
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("e.g. http://parts.lan:8080/")
        self.folder = QLineEdit(str(folder))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        note = QLabel("The copy opens read-only. A later download into the same folder replaces it.\n"
                      "The folder must be empty or hold an earlier copy.")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Download")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Web interface:", self.url)
        form.addRow("Folder:", folder_row)
        form.addRow(note)
        form.addRow(buttons)
        self.resize(560, self.sizeHint().height())

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder for the copy", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def accept(self) -> None:
        try:
            normalize_base_url(self.url.text())
        except InventoryError as e:
            show_error(self, e)
            return
        if not self.folder.text().strip():
            show_error(self, "please choose a folder for the copy")
            return
        super().accept()


class SavingDialog(QDialog):
    def __init__(self, parent: QWidget | None, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def save(self) -> None:
        raise NotImplementedError

    def accept(self) -> None:
        try:
            self.save()
        except InventoryError as e:
            show_error(self, e)
            return
        super().accept()


class PartDialog(SavingDialog):
    """Add a part (with stock and attachments) or edit one. With `copy_of`, a new
    part starts with that part's fields, tags, attributes and minimum quantity."""

    def __init__(self, inv: Inventory, part: Part | None, parent: QWidget | None = None, *,
                 copy_of: Part | None = None):
        if part is not None:
            title = f"Edit {part.name}"
        else:
            title = f"New part (copy of {copy_of.name})" if copy_of else "New part"
        super().__init__(parent, title)
        self.inv = inv
        self.part_id = part.id if part else None
        source = part or copy_of
        self.link_ids: list[int] = []  # links added by save(), to download afterwards
        self.resize(560, 640)

        form = QFormLayout()
        self.fields: dict[str, QLineEdit | QComboBox | QPlainTextEdit] = {}
        for name in EDITABLE_FIELDS:
            value = getattr(source, name) if source else ""
            if name == "category":
                widget = QComboBox()
                widget.setEditable(True)
                widget.addItems(inv.list_categories())
                widget.setEditText(value)
            elif name == "notes":
                widget = QPlainTextEdit(value)
                widget.setTabChangesFocus(True)
                widget.setFixedHeight(80)
            else:
                widget = QLineEdit(value)
            self.fields[name] = widget
            form.addRow(FIELD_LABELS[name] + ":", widget)
        self.tags = TagEdit([name for name, _ in inv.list_tags()], ", ".join(source.tags) if source else "")
        form.addRow("Tags:", self.tags)
        self.attributes = AttributeTable(source.attributes if source else {})
        self.attributes.setMinimumHeight(120)
        form.addRow("Attributes:", self.attributes)
        self.min_quantity = QSpinBox()
        self.min_quantity.setRange(0, MAX_QUANTITY)
        self.min_quantity.setSpecialValueText("none")
        self.min_quantity.setValue((source.min_quantity or 0) if source else 0)
        self.min_quantity.setToolTip("Less than this in total counts as low stock")
        form.addRow("Minimum quantity:", self.min_quantity)

        if part is None:
            self.quantity = QSpinBox()
            self.quantity.setRange(0, MAX_QUANTITY)
            self.location = LocationBox(inv.list_locations(), placeholder="e.g. Cabinet A/Drawer 3")
            stock = QHBoxLayout()
            stock.addWidget(self.quantity)
            stock.addWidget(QLabel("in"))
            stock.addWidget(self.location, 1)
            form.addRow("Quantity:", stock)

            self.sources = QListWidget()
            self.sources.setFixedHeight(80)
            add_files = QPushButton("Add files…")
            add_files.clicked.connect(self._add_files)
            add_links = QPushButton("Add links…")
            add_links.clicked.connect(self._add_links)
            remove = QPushButton("Remove")
            remove.clicked.connect(lambda: self.sources.takeItem(self.sources.currentRow()))
            attachments = QVBoxLayout()
            attachments.addWidget(self.sources)
            attachments.addLayout(button_row(add_files, add_links, remove))
            form.addRow("Attachments:", attachments)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.buttons)
        self.fields["name"].setFocus()
        self.fields["name"].selectAll()  # a copy needs a new name

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        self.sources.addItems(paths)

    def _add_links(self) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Attach links", "Links, one per line:")
        if ok:
            self.sources.addItems(text.split())

    def field_value(self, name: str) -> str:
        widget = self.fields[name]
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText().strip()
        return widget.text().strip()

    def save(self) -> None:
        fields = {name: self.field_value(name) for name in EDITABLE_FIELDS}
        tags = parse_tags(self.tags.text())
        attributes = self.attributes.attributes()
        link_ids = []
        with self.inv.transaction():
            if self.part_id is not None:
                part_id = self.part_id
                self.inv.update_part(part_id, **fields)
            else:
                part_id = self.inv.add_part(fields.pop("name"), **fields)
                quantity = self.quantity.value()
                if quantity:
                    location = self.location.reference()
                    if not location:
                        raise InventoryError("please choose where the parts are stored")
                    self.inv.set_stock(part_id, location, quantity, note=NEW_PART_NOTE)
                for i in range(self.sources.count()):
                    source = self.sources.item(i).text()
                    attachment_id = self.inv.add_attachment(part_id, source)
                    if is_url(source):
                        link_ids.append(attachment_id)
            self.inv.set_tags(part_id, tags)
            self.inv.set_attributes(part_id, attributes)
            self.inv.set_min_quantity(part_id, self.min_quantity.value() or None)
        self.part_id = part_id
        self.link_ids = link_ids


class StockDialog(SavingDialog):
    OPERATIONS = {"add": "Add", "take": "Take", "set": "Set to"}

    def __init__(self, inv: Inventory, part: Part, parent: QWidget | None = None, *,
                 location: str = "", operation: str = "take"):
        super().__init__(parent, f"Stock of {part.name}")
        self.inv = inv
        self.part_id = part.id
        if not location and part.stock:
            location = part.stock[0].location
        self.operation = QComboBox()
        for key, label in self.OPERATIONS.items():
            self.operation.addItem(label, key)
        self.operation.setCurrentIndex(self.operation.findData(operation))
        self.amount = QSpinBox()
        self.amount.setRange(0, MAX_QUANTITY)
        self.amount.setValue(1)
        self.location = LocationBox(inv.list_locations(), current=location)
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional, kept in the history")

        form = QFormLayout(self)
        row = QHBoxLayout()
        row.addWidget(self.operation)
        row.addWidget(self.amount, 1)
        form.addRow("Change:", row)
        form.addRow("Location:", self.location)
        form.addRow("Note:", self.note)
        form.addRow(self.buttons)
        self.amount.setFocus()
        self.amount.selectAll()

    def save(self) -> None:
        location = self.location.reference()
        if not location:
            raise InventoryError("please choose a location")
        amount = self.amount.value()
        note = self.note.text()
        match self.operation.currentData():
            case "add":
                self.inv.adjust_stock(self.part_id, location, amount, note=note)
            case "take":
                self.inv.adjust_stock(self.part_id, location, -amount, note=note)
            case "set":
                self.inv.set_stock(self.part_id, location, amount, note=note)


class MoveDialog(SavingDialog):
    def __init__(self, inv: Inventory, part: Part, parent: QWidget | None = None):
        super().__init__(parent, f"Move {part.name}")
        self.inv = inv
        self.part_id = part.id
        self.moved = 0
        self.source = QComboBox()
        for entry in part.stock:
            self.source.addItem(f"{entry.location} ({entry.quantity})", "/" + entry.location)
        self.quantity = QSpinBox()
        self.quantity.setRange(0, MAX_QUANTITY)
        self.quantity.setSpecialValueText("all")
        self.destination = LocationBox(inv.list_locations(), placeholder="new or existing location")
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional, kept in the history")

        form = QFormLayout(self)
        form.addRow("From:", self.source)
        form.addRow("Quantity:", self.quantity)
        form.addRow("To:", self.destination)
        form.addRow("Note:", self.note)
        form.addRow(self.buttons)
        self.destination.setFocus()

    def save(self) -> None:
        if self.source.currentData() is None:
            raise InventoryError("this part isn't stored anywhere")
        destination = self.destination.reference()
        if not destination:
            raise InventoryError("please choose where to move the parts")
        quantity = self.quantity.value() or None
        self.moved = self.inv.move_stock(
            self.part_id, self.source.currentData(), destination, quantity, note=self.note.text()
        )


class LocationDialog(SavingDialog):
    """Add a location (by path), or rename, describe or move one."""

    def __init__(self, inv: Inventory, location: Location | None, parent: QWidget | None = None, *,
                 parent_path: str = ""):
        super().__init__(parent, "New location" if location is None else f"Edit {location.path}")
        self.inv = inv
        self.location = location
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        if location is None:
            self.path = QLineEdit(parent_path + "/" if parent_path else "")
            self.path.setPlaceholderText("e.g. Lab/Cabinet A/Drawer 3")
            form.addRow("Path:", self.path)
        else:
            self.name = QLineEdit(location.name)
            form.addRow("Name:", self.name)
            inside = location.path + "/"
            candidates = [l for l in inv.list_locations() if l.id != location.id and not l.path.startswith(inside)]
            self.parent_box = LocationBox(candidates, placeholder="(top level)",
                                          current=location.path.rpartition("/")[0])
            form.addRow("Inside:", self.parent_box)
        self.description = QLineEdit(location.description if location else "")
        form.addRow("Description:", self.description)
        form.addRow(self.buttons)

    def save(self) -> None:
        if self.location is None:
            self.inv.add_location(self.path.text().strip(), self.description.text().strip())
            return
        parent = self.parent_box.reference().rstrip("/")
        self.inv.update_location(
            "/" + self.location.path,
            new_name=self.name.text().strip(),
            description=self.description.text().strip(),
            parent=parent or None,
            top_level=not parent,
        )


class ImportDialog(QDialog):
    def __init__(self, locations: list[Location], start_dir: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Import CSV")
        self.setMinimumWidth(480)
        self.start_dir = start_dir
        self.path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_row = QHBoxLayout()
        file_row.addWidget(self.path, 1)
        file_row.addWidget(browse)
        self.add_stock = QCheckBox("Add quantities to the stock instead of replacing it")
        self.location = LocationBox(locations, placeholder="(rows must name a location)")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Check…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout(self)
        form.addRow("File:", file_row)
        form.addRow("", self.add_stock)
        form.addRow("Rows without a location go to:", self.location)
        form.addRow(QLabel("Nothing is saved until you have seen what the file contains."))
        form.addRow(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import CSV", self.start_dir,
                                              "CSV files (*.csv *.tsv *.txt);;All files (*)")
        if path:
            self.path.setText(path)


class AboutDialog(QDialog):
    def __init__(self, db_path: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("About partstrack")
        text = QLabel(
            f"<h2>partstrack {__version__}</h2>"
            "<p>A small inventory for electronic parts, backed by SQLite.</p>"
            f'<p><a href="{REPOSITORY_URL}">{REPOSITORY_URL}</a><br>'
            f'<a href="{REPOSITORY_URL}/blob/main/CHANGELOG.md">Changelog</a> · '
            f'<a href="{REPOSITORY_URL}/issues">Report a problem</a></p>'
            "<p>Copyright © 2026 Kayla Theil. partstrack is free software: you can<br>"
            "redistribute it and/or modify it under the terms of the<br>"
            f'<a href="{REPOSITORY_URL}/blob/main/LICENSE">GNU General Public License, version 3 or later</a>.<br>'
            "It comes with ABSOLUTELY NO WARRANTY.</p>"
        )
        text.setOpenExternalLinks(True)
        logo = QLabel()
        logo.setPixmap(QIcon(str(LOGO_PATH)).pixmap(96))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(logo)
        layout.addWidget(text)
        layout.addWidget(QLabel("Database:"))
        layout.addWidget(selectable_label(str(db_path)))
        layout.addWidget(buttons)


# -- labels -------------------------------------------------------------------


def paint_labels(printer: QPrinter, layout: Layout, labels: list[Label], start: int = 0) -> None:
    """Draw labels onto `printer` (a real printer or a PDF file), one page per sheet.
    The positions are the same as on the web interface's labels page."""
    width, height = layout.page_width, layout.page_height
    # Qt treats custom page sizes as portrait; wide labels need landscape.
    landscape = width > height
    size = QPageSize(QSizeF(height, width) if landscape else QSizeF(width, height), QPageSize.Unit.Millimeter,
                     layout.name, QPageSize.SizeMatchPolicy.ExactMatch)
    orientation = QPageLayout.Orientation.Landscape if landscape else QPageLayout.Orientation.Portrait
    printer.setFullPage(True)
    printer.setPageLayout(QPageLayout(size, orientation, QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter))

    dots = printer.resolution() / 25.4  # device pixels per mm
    g = geometry(layout)
    title_font, text_font = QFont(), QFont()
    title_font.setBold(True)
    title_font.setPixelSize(max(1, round(g.title_size * dots)))
    text_font.setPixelSize(max(1, round(g.text_size * dots)))
    wrap = QTextOption()
    wrap.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)  # like overflow-wrap: anywhere
    items = iter(labels)
    painter = QPainter(printer)
    try:
        for number, page in enumerate(positions(layout, len(labels), start)):
            if number:
                printer.newPage()
            for x, y in page:
                label = next(items)
                painter.save()
                painter.translate(x * dots, y * dots)
                if layout.cut_lines:
                    pen = QPen(QColor("#9a9a9a"), 0.15 * dots, Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                    painter.drawRect(QRectF(0, 0, layout.label_width * dots, layout.label_height * dots))
                matrix = qr_matrix(label.url)
                module = g.qr_size / (len(matrix) + 4) * dots  # with a quiet zone of 2 modules
                left, top = g.qr_x * dots + 2 * module, g.qr_y * dots + 2 * module
                # One path for the whole code, so neighbouring modules leave no hairline gaps.
                code = QPainterPath()
                code.setFillRule(Qt.FillRule.WindingFill)
                for row, modules in enumerate(matrix):
                    for column, dark in enumerate(modules):
                        if dark:
                            code.addRect(QRectF(left + column * module, top + row * module, module, module))
                painter.fillPath(code.simplified(), Qt.GlobalColor.black)
                text = QRectF(g.text_x * dots, g.text_y * dots, g.text_width * dots, g.text_height * dots)
                painter.setClipRect(text)
                painter.setPen(Qt.GlobalColor.black)
                line_top = text.top()
                for font, line in [(title_font, label.title), (text_font, label.subtitle),
                                   (text_font, label.detail)]:
                    if not line:
                        continue
                    painter.setFont(font)
                    area = QRectF(text.left(), line_top, text.width(), text.bottom() - line_top)
                    painter.drawText(area, line, wrap)
                    line_top += painter.boundingRect(area, line, wrap).height()
                painter.restore()
    finally:
        painter.end()


class LabelDialog(QDialog):
    """Choose locations and parts, a layout and the web interface's address,
    then preview, print or save labels as PDF. The choices are remembered."""

    SIZE_FIELDS = {
        "label_width": "Label width", "label_height": "Label height", "columns": "Columns", "rows": "Rows",
        "margin_left": "Left margin", "margin_top": "Top margin",
        "gap_x": "Gap between columns", "gap_y": "Gap between rows",
    }

    def __init__(self, inv: Inventory, settings: QSettings, parent: QWidget | None = None, *,
                 location_paths: list[str] = (), part_ids: list[int] = ()):
        super().__init__(parent)
        self.setWindowTitle("Print labels")
        self.resize(640, 720)
        self.inv = inv
        self.settings = settings
        self.locations = inv.list_locations()
        try:
            stored = json.loads(settings.value("labels/options", "") or "{}")
        except ValueError:
            stored = {}
        options = {**CUSTOM_SHEET_OPTIONS, **(stored if isinstance(stored, dict) else {})}

        # Locations, as a tree with checkboxes.
        self.location_tree = QTreeWidget()
        self.location_tree.setHeaderLabels(["Location", "Description"])
        items: dict[int, QTreeWidgetItem] = {}
        for location in self.locations:
            item = QTreeWidgetItem([location.name, location.description])
            item.setData(0, ROLE_ID, location.path)
            item.setCheckState(0, Qt.CheckState.Checked if location.path in location_paths
                               else Qt.CheckState.Unchecked)
            parent_item = items.get(location.parent_id)
            if parent_item is None:
                self.location_tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            items[location.id] = item
        self.location_tree.expandAll()
        self.location_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.sublocations = QCheckBox("Include sublocations of checked locations")
        self.sublocations.setChecked(settings.value("labels/sublocations", "true") == "true")
        location_page = QWidget()
        location_layout = QVBoxLayout(location_page)
        location_layout.addWidget(self.location_tree)
        location_layout.addWidget(self.sublocations)
        location_layout.addLayout(self._check_buttons(self.location_tree))

        # Parts, as a filterable list with checkboxes.
        self.part_filter = QLineEdit()
        self.part_filter.setPlaceholderText("Filter…")
        self.part_filter.setClearButtonEnabled(True)
        self.part_filter.textChanged.connect(self._filter_parts)
        self.part_list = QTreeWidget()
        self.part_list.setHeaderLabels(["Name", "Package / MPN", "Description"])
        self.part_list.setRootIsDecorated(False)
        for part in inv.list_parts():
            item = QTreeWidgetItem([part.name, part.package or part.mpn, part.description])
            item.setData(0, ROLE_ID, part.id)
            item.setCheckState(0, Qt.CheckState.Checked if part.id in part_ids else Qt.CheckState.Unchecked)
            self.part_list.addTopLevelItem(item)
        self.part_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        part_page = QWidget()
        part_layout = QVBoxLayout(part_page)
        part_layout.addWidget(self.part_filter)
        part_layout.addWidget(self.part_list)
        part_layout.addLayout(self._check_buttons(self.part_list))

        self.tabs = QTabWidget()
        self.tabs.addTab(location_page, "Locations")
        self.tabs.addTab(part_page, "Parts")
        if part_ids and not location_paths:
            self.tabs.setCurrentWidget(part_page)

        # Layout and address.
        self.layout_combo = QComboBox()
        for layout in LAYOUTS.values():
            self.layout_combo.addItem(layout.name, layout.key)
        for key, name in CUSTOM_LAYOUT_NAMES.items():
            self.layout_combo.addItem(name, key)
        index = self.layout_combo.findData(options.get("layout", DEFAULT_LAYOUT))
        self.layout_combo.setCurrentIndex(max(index, 0))
        self.size_fields: dict[str, QSpinBox | QDoubleSpinBox] = {}
        # Always shown, so their values stay visible; enabled only for the custom layouts.
        sizes = QGridLayout()
        for index, (name, text) in enumerate(self.SIZE_FIELDS.items()):
            if isinstance(CUSTOM_SHEET_OPTIONS[name], int):
                field = QSpinBox()
                field.setRange(1, 50)
                field.setValue(int(options.get(name, CUSTOM_SHEET_OPTIONS[name])))
            else:
                field = QDoubleSpinBox()
                field.setRange(0, 300)
                field.setDecimals(1)
                field.setSuffix(" mm")
                field.setValue(float(options.get(name, CUSTOM_SHEET_OPTIONS[name])))
            field.valueChanged.connect(self._layout_changed)
            self.size_fields[name] = field
            row, column = divmod(index, 2)
            label = QLabel(text + ":")
            label.setBuddy(field)
            sizes.addWidget(label, row, 2 * column)
            sizes.addWidget(field, row, 2 * column + 1)
        sizes.setContentsMargins(0, 0, 0, 0)
        self.custom_box = QWidget()
        self.custom_box.setLayout(sizes)
        self.start = QSpinBox()
        self.start.setToolTip("Skip labels already used on the first sheet")
        self.url = QLineEdit(server_url(settings))
        self.url.setPlaceholderText("e.g. http://parts.lan:8080/")
        self.url.setToolTip("The address of partstrack-web as your phone reaches it; the QR codes link there")
        self.layout_combo.currentIndexChanged.connect(self._layout_changed)

        form = QFormLayout()
        form.addRow("Layout:", self.layout_combo)
        form.addRow("Custom size:", self.custom_box)
        form.addRow("Start at label:", self.start)
        form.addRow("Web interface:", self.url)

        preview = QPushButton("Preview…")
        preview.clicked.connect(lambda: self.preview())
        print_button = QPushButton("Print…")
        print_button.clicked.connect(lambda: self.print_labels())
        pdf = QPushButton("Save as PDF…")
        pdf.clicked.connect(lambda: self.choose_pdf())
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(form)
        layout.addLayout(button_row(preview, print_button, pdf, close))
        self._layout_changed()

    def _check_buttons(self, tree: QTreeWidget) -> QHBoxLayout:
        def check_all(state: Qt.CheckState) -> None:
            for item in self._items(tree):
                if not item.isHidden():
                    item.setCheckState(0, state)

        check = QPushButton("Check all")
        check.clicked.connect(lambda: check_all(Qt.CheckState.Checked))
        uncheck = QPushButton("Uncheck all")
        uncheck.clicked.connect(lambda: check_all(Qt.CheckState.Unchecked))
        return button_row(check, uncheck)

    @staticmethod
    def _items(tree: QTreeWidget) -> list[QTreeWidgetItem]:
        found = []

        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                found.append(item.child(i))
                walk(item.child(i))

        walk(tree.invisibleRootItem())
        return found

    def _filter_parts(self, text: str) -> None:
        words = nocase(text).split()
        for item in self._items(self.part_list):
            haystack = nocase(" ".join(item.text(column) for column in range(3)))
            item.setHidden(not all(word in haystack for word in words))

    def options(self) -> dict[str, str]:
        options = {"layout": self.layout_combo.currentData()}
        options.update({name: str(field.value()) for name, field in self.size_fields.items()})
        return options

    def _layout_changed(self) -> None:
        key = self.layout_combo.currentData()
        for name, field in self.size_fields.items():
            field.setEnabled(key == CUSTOM_SHEET or (key == CUSTOM_PRINTER and name in ("label_width", "label_height")))
        try:
            per_page = layout_from_options(self.options()).per_page
        except InventoryError:
            per_page = 1
        self.start.setRange(1, per_page)

    def checked_locations(self) -> list[Location]:
        paths = [item.data(0, ROLE_ID) for item in self._items(self.location_tree)
                 if item.checkState(0) == Qt.CheckState.Checked]
        return select_locations(self.locations, paths, sublocations=self.sublocations.isChecked())

    def checked_part_ids(self) -> list[int]:
        return [item.data(0, ROLE_ID) for item in self._items(self.part_list)
                if item.checkState(0) == Qt.CheckState.Checked]

    def labels(self) -> tuple[Layout, list[Label]]:
        """The chosen layout and labels; raises InventoryError if something is missing."""
        layout = layout_from_options(self.options())
        if not self.url.text().strip():
            raise InventoryError("please enter the web interface's address, which the QR codes link to")
        base_url = normalize_base_url(self.url.text())
        labels = [location_label(base_url, l) for l in self.checked_locations()]
        labels += [part_label(base_url, self.inv.get_part(part_id)) for part_id in self.checked_part_ids()]
        if not labels:
            raise InventoryError("please check the locations or parts to print labels for")
        return layout, labels

    def _prepared(self) -> tuple[Layout, list[Label]] | None:
        """The layout and labels, after showing any problem; remembers the choices."""
        try:
            layout, labels = self.labels()
        except InventoryError as e:
            show_error(self, e)
            return None
        if is_local_url(self.url.text()) and not confirm(
            self, "The web interface's address only works on this computer, so phones can't open the "
                  "QR codes. Print anyway?"):
            return None
        self.settings.setValue("labels/options", json.dumps(self.options()))
        self.settings.setValue("server/url", normalize_base_url(self.url.text()))
        self.settings.setValue("labels/sublocations", "true" if self.sublocations.isChecked() else "false")
        return layout, labels

    def paint(self, printer: QPrinter) -> None:
        layout, labels = self.labels()
        paint_labels(printer, layout, labels, self.start.value() - 1)

    def preview(self) -> None:
        if self._prepared() is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = self.preview_dialog(printer)
        run_dialog(dialog)
        self.settings.setValue("labels/preview_geometry", dialog.saveGeometry())

    def preview_dialog(self, printer: QPrinter) -> QPrintPreviewDialog:
        """A preview window with its last size, or most of the screen the first time."""
        dialog = QPrintPreviewDialog(printer, self)
        dialog.paintRequested.connect(self.paint)
        geometry = self.settings.value("labels/preview_geometry")
        if not geometry or not dialog.restoreGeometry(geometry):
            screen = self.screen().availableGeometry()
            dialog.resize(int(screen.width() * 0.8), int(screen.height() * 0.85))
        return dialog

    def print_labels(self) -> None:
        prepared = self._prepared()
        if prepared is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if run_dialog(QPrintDialog(printer, self)):
            paint_labels(printer, *prepared, self.start.value() - 1)  # sets the page size again

    def choose_pdf(self) -> None:
        if self._prepared() is None:
            return
        start = Path(self.settings.value("last_dir", "") or Path.home()) / "labels.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Save labels", str(start), "PDF (*.pdf)")
        if path:
            self.settings.setValue("last_dir", str(Path(path).parent))
            self.save_pdf(Path(path))

    def save_pdf(self, path: Path) -> bool:
        prepared = self._prepared()
        if prepared is None:
            return False
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(path))
        paint_labels(printer, *prepared, self.start.value() - 1)
        return True


# -- part details -------------------------------------------------------------


class PartView(QScrollArea):
    """Shows one part: details, stock, attributes, notes and attachments."""

    def __init__(self, main: MainWindow):
        super().__init__()
        self.main = main
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.part: Part | None = None
        self.show_part(None)

    def show_part(self, part: Part | None) -> None:
        self.part = part
        content = QWidget()
        layout = QVBoxLayout(content)
        if part is None:
            hint = QLabel("Select a part, or add one with Part → New part.")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setEnabled(False)
            layout.addWidget(hint)
            self.setWidget(content)
            return

        title = selectable_label(part.name)
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.5)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        if part.description:
            layout.addWidget(selectable_label(part.description))
        actions = []
        for text, slot in [("Edit…", self.main.edit_part), ("Stock…", self.main.change_stock),
                           ("Move…", self.main.move_stock), ("Duplicate…", self.main.duplicate_part),
                           ("Delete", self.main.delete_part)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, slot=slot: slot())
            button.setEnabled(not self.main.read_only)
            actions.append(button)
        layout.addLayout(button_row(*actions))

        details = QFormLayout()
        for name in ("manufacturer", "mpn", "category", "package"):
            if getattr(part, name):
                details.addRow(FIELD_LABELS[name] + ":", selectable_label(getattr(part, name)))
        if part.tags:
            details.addRow("Tags:", selectable_label(", ".join(part.tags)))
        for key, value in part.attributes.items():
            details.addRow(key + ":", selectable_label(value))
        details.addRow("Added:", selectable_label(part.created_at))
        if part.updated_at != part.created_at:
            details.addRow("Changed:", selectable_label(part.updated_at))
        layout.addLayout(details)

        stock_title = f"Stock: {part.total_quantity}"
        if part.is_low:
            stock_title += f" (below the minimum of {part.min_quantity})"
        elif part.min_quantity is not None:
            stock_title += f" (minimum {part.min_quantity})"
        layout.addWidget(heading(stock_title))
        if part.stock:
            stock = QTreeWidget()
            stock.setHeaderLabels(["Location", "Quantity"])
            stock.setRootIsDecorated(False)
            for entry in part.stock:
                item = QTreeWidgetItem([entry.location, str(entry.quantity)])
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight)
                stock.addTopLevelItem(item)
            stock.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            stock.header().setStretchLastSection(False)
            stock.setFixedHeight(stock.sizeHintForRow(0) * (len(part.stock) + 1) + 2 * stock.frameWidth() + 8)
            stock.itemDoubleClicked.connect(lambda item: self.main.change_stock(location="/" + item.text(0)))
            stock.setToolTip("Double-click to change the stock at a location")
            layout.addWidget(stock)
        else:
            layout.addWidget(QLabel("Not stored anywhere."))

        changes = self.main.inv.stock_history(part.id, limit=HISTORY_IN_PART_VIEW)
        if changes:
            layout.addWidget(heading("History"))
            history = QTreeWidget()
            history.setHeaderLabels(["Time (UTC)", "Location", "Change", "Now", "Note"])
            history.setRootIsDecorated(False)
            for change in changes:
                item = QTreeWidgetItem([change.at, change.location, f"{change.delta:+d}", str(change.quantity),
                                        change.note])
                item.setToolTip(1, change.location)
                item.setToolTip(2, change.action)
                item.setToolTip(4, change.note)
                for column in (2, 3):
                    item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
                history.addTopLevelItem(item)
            history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            history.header().setStretchLastSection(False)
            for column, mode in enumerate([QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.Stretch,
                                           QHeaderView.ResizeMode.ResizeToContents,
                                           QHeaderView.ResizeMode.ResizeToContents,
                                           QHeaderView.ResizeMode.Stretch]):
                history.header().setSectionResizeMode(column, mode)
            history.setFixedHeight(history.sizeHintForRow(0) * (len(changes) + 1) + 2 * history.frameWidth() + 8)
            layout.addWidget(history)

        if part.notes:
            layout.addWidget(heading("Notes"))
            layout.addWidget(selectable_label(part.notes))

        layout.addWidget(heading("Attachments"))
        images = QHBoxLayout()
        for attachment in part.attachments:
            if attachment.kind != "image" or attachment.path is None:
                continue
            pixmap = QPixmap(str(attachment.path))
            if pixmap.isNull():
                continue
            label = ClickableLabel()
            label.setPixmap(pixmap.scaledToHeight(min(160, pixmap.height()),
                                                  Qt.TransformationMode.SmoothTransformation))
            label.setToolTip(attachment.original_name)
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.clicked.connect(lambda a=attachment: open_attachment(self, a))
            images.addWidget(label)
        if images.count():
            images.addStretch()
            layout.addLayout(images)

        self.attachments = QTreeWidget()
        self.attachments.setHeaderLabels(["Kind", "Name", "Stored as"])
        self.attachments.setRootIsDecorated(False)
        for attachment in part.attachments:
            if attachment.url is not None:
                stored = "link"
            elif attachment.source_url:
                stored = "file (from link)"
            else:
                stored = "file"
            item = QTreeWidgetItem([attachment.kind, attachment.original_name, stored])
            item.setData(0, ROLE_ID, attachment.id)
            item.setToolTip(1, attachment.source_url or attachment.target)
            self.attachments.addTopLevelItem(item)
        self.attachments.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rows = max(len(part.attachments), 1) + 1
        self.attachments.setFixedHeight(min(rows, 8) * (self.fontMetrics().height() + 8) + 8)
        self.attachments.itemDoubleClicked.connect(lambda: self._with_attachment(self._open))
        self.attachments.itemSelectionChanged.connect(self._update_attachment_buttons)
        layout.addWidget(self.attachments)

        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(lambda: self._with_attachment(self._open))
        self.store_button = QPushButton("Store locally")
        self.store_button.setToolTip("Download the file behind the link and keep it with the database")
        self.store_button.clicked.connect(lambda: self._with_attachment(self.main.store_locally))
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(lambda: self._with_attachment(self.main.remove_attachment))
        add_files = QPushButton("Add files…")
        add_files.clicked.connect(lambda: self.main.attach_files())
        add_links = QPushButton("Add links…")
        add_links.clicked.connect(lambda: self.main.attach_links())
        for button in (add_files, add_links):
            button.setEnabled(not self.main.read_only)
        layout.addLayout(button_row(self.open_button, self.store_button, self.remove_button))
        layout.addLayout(button_row(add_files, add_links))
        self._update_attachment_buttons()

        layout.addStretch()
        self.setWidget(content)

    def selected_attachment(self) -> Attachment | None:
        item = self.attachments.currentItem() if self.part else None
        if item is None or not item.isSelected():
            return None
        attachment_id = item.data(0, ROLE_ID)
        return next((a for a in self.part.attachments if a.id == attachment_id), None)

    def _with_attachment(self, function) -> None:
        attachment = self.selected_attachment()
        if attachment is not None:
            function(attachment)

    def _open(self, attachment: Attachment) -> None:
        open_attachment(self, attachment)

    def _update_attachment_buttons(self) -> None:
        attachment = self.selected_attachment()
        writable = not self.main.read_only
        self.open_button.setEnabled(attachment is not None)
        self.remove_button.setEnabled(writable and attachment is not None)
        self.store_button.setEnabled(
            writable and attachment is not None and attachment.url is not None
            and downloadable_name(attachment.url) is not None
        )


# -- main window --------------------------------------------------------------


def writes(method):
    """For MainWindow methods that change the inventory: on a read-only copy, they
    only say so. (Their buttons are disabled too, but double-clicks and shortcuts
    still reach them, and Inventory refuses the change anyway.)"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.read_only:
            self.statusBar().showMessage(READ_ONLY_MESSAGE)
            return None
        return method(self, *args, **kwargs)

    return wrapper


class MainWindow(QMainWindow):
    PART_COLUMNS = ["Name", "Description", "Qty", "Category", "Package", "Locations", "Tags"]

    def __init__(self, db_path: Path, settings: QSettings | None = None):
        super().__init__()
        self.settings = settings or QSettings()
        self.inv: Inventory | None = None
        self.db_path = db_path
        self.current_part_id: int | None = None
        self.copy_info: CopyInfo | None = None  # set while a read-only copy is open
        self._downloads: set[Downloader] = set()
        self._copy_downloads: set[CopyDownloader] = set()
        self.resize(1200, 760)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_parts_page(), "Parts")
        self.tabs.addTab(self._build_locations_page(), "Locations")
        self.setCentralWidget(self.tabs)
        self._build_menus()
        self.copy_label = QLabel()
        self.copy_label.setStyleSheet("font-weight: bold")
        self.statusBar().addPermanentWidget(self.copy_label)

        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter = self.settings.value("window/splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)

    # -- layout ---------------------------------------------------------------

    def _build_parts_page(self) -> QWidget:
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, description, MPN, tags, attributes… or e.g. resistance>=10k")
        self.search.setToolTip("Words must all match. key=value, key:text, key<value, key>=value etc. filter on "
                               "an attribute; numbers may have SI prefixes (10k, 100n).")
        self.search.setClearButtonEnabled(True)
        self.search_timer = QTimer(self, singleShot=True, interval=250)
        self.search_timer.timeout.connect(self.refresh_parts)
        self.search.textChanged.connect(self.search_timer.start)
        self.tag_filter = QComboBox()
        self.location_filter = QComboBox()
        self.category_filter = QComboBox()
        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        for combo in (self.tag_filter, self.location_filter, self.category_filter):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            combo.currentIndexChanged.connect(self.refresh_parts)
            filters.addWidget(combo)
        self.low_filter = QCheckBox("Low stock")
        self.low_filter.setToolTip("Only parts below their minimum quantity")
        self.low_filter.toggled.connect(self.refresh_parts)
        filters.addWidget(self.low_filter)

        self.table = QTableWidget(0, len(self.PART_COLUMNS))
        self.table.setHorizontalHeaderLabels(self.PART_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self._columns_sized = False
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._part_selected)
        self.table.itemDoubleClicked.connect(lambda: self.edit_part())
        QShortcut(QKeySequence.StandardKey.Delete, self.table, self.delete_part,
                  context=Qt.ShortcutContext.WidgetShortcut)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(filters)
        left_layout.addWidget(self.table)
        self.part_view = PartView(self)
        self.splitter = QSplitter()
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.part_view)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        return self.splitter

    def _build_locations_page(self) -> QWidget:
        self.location_tree = QTreeWidget()
        self.location_tree.setHeaderLabels(["Location", "Parts", "Items", "Description"])
        self.location_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.location_tree.itemDoubleClicked.connect(lambda: self.edit_location())
        buttons = []
        self.location_write_buttons = []
        for text, slot in [("Add…", self.add_location), ("Edit…", self.edit_location),
                           ("Delete", self.delete_location), ("Show parts", self.show_location_parts),
                           ("Labels…", self.print_labels)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, slot=slot: slot())
            buttons.append(button)
            if text in ("Add…", "Edit…", "Delete"):
                self.location_write_buttons.append(button)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.location_tree)
        layout.addLayout(button_row(*buttons))
        layout.addWidget(QLabel("“Parts” and “Items” count what is stored directly in a location."))
        return page

    def _action(self, menu, text: str, slot, shortcut=None) -> QAction:
        action = menu.addAction(text)
        action.triggered.connect(lambda: slot())
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        return action

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._action(file_menu, "&Open database…", self.choose_database, QKeySequence.StandardKey.Open)
        self._action(file_menu, "&Download copy from server…", self.choose_copy_download)
        file_menu.addSeparator()
        self.import_action = self._action(file_menu, "&Import CSV…", self.choose_import)
        self._action(file_menu, "&Export CSV…", self.choose_export)
        file_menu.addSeparator()
        self._action(file_menu, "Print &labels…", self.print_labels, QKeySequence.StandardKey.Print)
        file_menu.addSeparator()
        self._action(file_menu, "&Quit", self.close, QKeySequence.StandardKey.Quit)

        part_menu = self.menuBar().addMenu("&Part")
        new_part = self.new_part_action = self._action(part_menu, "&New part…", self.new_part,
                                                       QKeySequence.StandardKey.New)
        self.part_actions = [
            self._action(part_menu, "&Edit…", self.edit_part, "Ctrl+E"),
            self._action(part_menu, "&Stock…", self.change_stock, "Ctrl+T"),
            self._action(part_menu, "&Move…", self.move_stock, "Ctrl+M"),
            self._action(part_menu, "D&uplicate…", self.duplicate_part, "Ctrl+D"),
            self._action(part_menu, "Attach &files…", self.attach_files),
            self._action(part_menu, "Attach &links…", self.attach_links),
        ]
        part_menu.addSeparator()
        delete = self._action(part_menu, "&Delete", self.delete_part)
        self.part_actions.append(delete)

        view_menu = self.menuBar().addMenu("&View")
        self._action(view_menu, "&Refresh", self.refresh, QKeySequence.StandardKey.Refresh)
        self._action(view_menu, "&Find", self._focus_search, QKeySequence.StandardKey.Find)
        theme_menu = view_menu.addMenu("&Theme")
        group = QActionGroup(self)
        current = self.settings.value("theme", "auto")
        for key, (label, _) in THEMES.items():
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == current)
            action.triggered.connect(lambda checked, key=key: self.set_theme(key))
            group.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        self._action(help_menu, "&About partstrack", lambda: run_dialog(AboutDialog(self.db_path, self)))
        self._action(help_menu, "Project &website",
                     lambda: QDesktopServices.openUrl(QUrl(REPOSITORY_URL)))

        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("main-toolbar")
        toolbar.setMovable(False)
        toolbar.addAction(new_part)
        for action in self.part_actions[:3]:
            toolbar.addAction(action)

    def _focus_search(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.search.setFocus()
        self.search.selectAll()

    # -- database -------------------------------------------------------------

    def open_database(self, db_path: Path) -> bool:
        """Open a database; copies of a server's inventory (see partstrack.snapshot) open read-only."""
        copy_info = read_copy_info(Path(db_path))
        try:
            inv = open_inventory(db_path)
        except (InventoryError, sqlite3.Error, OSError) as e:
            show_error(self, f"cannot open {db_path}: {e}")
            return False
        if self.inv is not None:
            self.inv.close()
        self.inv = inv
        self.db_path = Path(db_path)
        self.copy_info = copy_info
        self.current_part_id = None
        if copy_info is None:
            self.setWindowTitle(f"partstrack – {self.db_path}")
            self.copy_label.clear()
        else:
            description = f"copy of {copy_info.source} from {copy_info.taken_at} UTC"
            self.setWindowTitle(f"partstrack – {description} (read-only)")
            self.copy_label.setText(f"Read-only {description}")
        self.refresh()
        return True

    @property
    def read_only(self) -> bool:
        return self.inv is not None and self.inv.read_only

    # -- copies of a server's inventory -----------------------------------------

    def default_copy_folder(self) -> Path:
        return Path(self.settings.value("copy/folder", "") or default_db_path().parent / "server-copy")

    def choose_copy_download(self) -> None:
        dialog = CopyDialog(server_url(self.settings), self.default_copy_folder(), self)
        if run_dialog(dialog):
            self.download_copy(dialog.url.text().strip(), Path(dialog.folder.text().strip()).expanduser())

    def download_copy(self, url: str, folder: Path) -> None:
        """Download a snapshot in the background; _copy_downloaded unpacks and opens it."""
        if self._copy_downloads:
            show_error(self, "A copy is already being downloaded.")
            return
        try:
            downloader = CopyDownloader(url, folder)
        except OSError as e:
            show_error(self, f"cannot write to {folder}: {e.strerror}")
            return
        self.settings.setValue("server/url", url)
        self.settings.setValue("copy/folder", str(folder))
        downloader.progress.connect(self._copy_progress)  # queued: these run in the GUI thread
        downloader.finished.connect(self._copy_downloaded)
        self._copy_downloads.add(downloader)
        self.statusBar().showMessage(f"Downloading a copy from {url}…")
        downloader.start()

    def _copy_progress(self, received: int) -> None:
        self.statusBar().showMessage(f"Downloading a copy… {received / 1e6:.1f} MB")

    def _copy_downloaded(self, downloader: CopyDownloader, error: str | None) -> None:
        self._copy_downloads.discard(downloader)
        try:
            if error is not None:
                show_error(self, error)
                return
            target = downloader.folder / "parts.db"
            replacing_open_copy = self.inv is not None and self.db_path.resolve() == target.resolve()
            if replacing_open_copy:  # the old copy's files are about to be replaced
                self.inv.close()
                self.inv = None
            try:
                db_path = unpack_copy(downloader.archive, downloader.folder, source=downloader.url)
            except (InventoryError, OSError) as e:
                show_error(self, f"The copy was not saved: {e}")
                if replacing_open_copy:
                    self.open_database(target)
                return
            self.statusBar().showMessage(f"Saved a copy of {downloader.url} in {downloader.folder}.")
            if replacing_open_copy or confirm(self, "The copy was saved. Open it now?",
                                              "It opens read-only. File → Open database… goes back to your own."):
                self.open_database(db_path)
        finally:
            downloader.archive.unlink(missing_ok=True)

    def choose_database(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Open or create a database", str(self.db_path.parent), "partstrack database (*.db);;All files (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path and self.open_database(Path(path)):
            self.settings.setValue("database", str(self.db_path))

    def attempt(self, function, *args, **kwargs):
        """Run a write; show an InventoryError instead of raising it. Refreshes either way."""
        try:
            return function(*args, **kwargs)
        except InventoryError as e:
            show_error(self, e)
            return None
        finally:
            self.refresh()

    def set_theme(self, key: str) -> None:
        self.settings.setValue("theme", key)
        apply_theme(key)

    def closeEvent(self, event) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter", self.splitter.saveState())
        if self.inv is not None:
            self.inv.close()
            self.inv = None
        super().closeEvent(event)

    # -- refreshing -----------------------------------------------------------

    def refresh(self) -> None:
        if self.inv is None:
            return
        locations = self.inv.list_locations()
        fill_combo(self.tag_filter, [(f"{name} ({count})", name) for name, count in self.inv.list_tags()],
                   "All tags")
        fill_combo(self.location_filter, [("    " * l.depth + l.name, "/" + l.path) for l in locations],
                   "All locations")
        fill_combo(self.category_filter, [(c, c) for c in self.inv.list_categories()], "All categories")
        self.refresh_parts()
        self.refresh_locations(locations)

    def refresh_parts(self) -> None:
        if self.inv is None:
            return
        try:
            parts = self.inv.list_parts(
                self.search.text(),
                tag=self.tag_filter.currentData(),
                location=self.location_filter.currentData(),
                category=self.category_filter.currentData(),
                low=self.low_filter.isChecked(),
            )
        except InventoryError as e:
            self.statusBar().showMessage(str(e))
            parts = []
        else:
            self.statusBar().showMessage(f"{len(parts)} part(s)")

        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(parts))
        for row, part in enumerate(parts):
            locations = ", ".join(f"{s.location} ({s.quantity})" for s in part.stock)
            cells = [part.name, part.description, part.total_quantity, part.category, part.package, locations,
                     ", ".join(part.tags)]
            for column, value in enumerate(cells):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, value)
                item.setData(ROLE_ID, part.id)
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if value == 0 or part.is_low:
                        item.setForeground(QColor("#d9534f"))
                    if part.is_low:
                        item.setToolTip(f"Below the minimum of {part.min_quantity}")
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)  # sorts by the column the user chose
        if parts and not self._columns_sized:
            self.table.resizeColumnsToContents()
            for column in range(self.table.columnCount()):
                self.table.setColumnWidth(column, min(self.table.columnWidth(column) + 12, 260))
            self._columns_sized = True
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(ROLE_ID) == self.current_part_id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.show_current_part()

    def refresh_locations(self, locations: list[Location]) -> None:
        selected = self.selected_location_path()
        self.location_tree.clear()
        items: dict[int, QTreeWidgetItem] = {}
        for location in locations:  # parents come before their children
            item = QTreeWidgetItem([location.name, str(location.part_count), str(location.total_quantity),
                                    location.description])
            item.setData(0, ROLE_ID, location.path)
            for column in (1, 2):
                item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
            parent = items.get(location.parent_id)
            if parent is None:
                self.location_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            items[location.id] = item
            if location.path == selected:
                self.location_tree.setCurrentItem(item)
        self.location_tree.expandAll()

    def show_current_part(self) -> None:
        part = None
        if self.current_part_id is not None:
            try:
                part = self.inv.get_part(self.current_part_id)
            except NotFoundError:
                self.current_part_id = None
        self.part_view.show_part(part)
        for action in self.part_actions:
            action.setEnabled(part is not None and not self.read_only)
        for widget in [self.new_part_action, self.import_action, *self.location_write_buttons]:
            widget.setEnabled(not self.read_only)

    def _part_selected(self) -> None:
        items = self.table.selectedItems()
        self.current_part_id = items[0].data(ROLE_ID) if items else None
        self.show_current_part()

    def current_part(self) -> Part | None:
        return self.part_view.part

    # -- part actions ---------------------------------------------------------

    @writes
    def new_part(self) -> None:
        dialog = PartDialog(self.inv, None, self)
        if run_dialog(dialog):
            self.current_part_id = dialog.part_id
            self.refresh()
            self.start_downloads(dialog.link_ids)

    @writes
    def duplicate_part(self) -> None:
        part = self.current_part()
        if part is None:
            return
        dialog = PartDialog(self.inv, None, self, copy_of=part)
        if run_dialog(dialog):
            self.current_part_id = dialog.part_id
            self.refresh()
            self.start_downloads(dialog.link_ids)

    @writes
    def edit_part(self) -> None:
        part = self.current_part()
        if part is not None and run_dialog(PartDialog(self.inv, part, self)):
            self.refresh()

    @writes
    def delete_part(self) -> None:
        part = self.current_part()
        if part is None or not confirm(self, f"Delete {part.name} with its stock and attachments?"):
            return
        self.attempt(self.inv.delete_part, part.id)
        self.statusBar().showMessage(f"Deleted {part.name}.")

    @writes
    def change_stock(self, *, location: str = "") -> None:
        part = self.current_part()
        if part is not None and run_dialog(StockDialog(self.inv, part, self, location=location)):
            self.refresh()

    @writes
    def move_stock(self) -> None:
        part = self.current_part()
        if part is None:
            return
        dialog = MoveDialog(self.inv, part, self)
        if run_dialog(dialog):
            self.refresh()
            self.statusBar().showMessage(f"Moved {dialog.moved}.")

    @writes
    def attach_files(self) -> None:
        part = self.current_part()
        if part is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, f"Attach files to {part.name}",
                                                self.settings.value("last_dir", ""))
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_attachments(part.id, paths)

    @writes
    def attach_links(self) -> None:
        part = self.current_part()
        if part is None:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, f"Attach links to {part.name}",
            "Links, one per line. Links to single files (…/datasheet.pdf) are downloaded and stored.")
        if ok and text.split():
            self.add_attachments(part.id, text.split())

    def add_attachments(self, part_id: int, sources: list[str]) -> None:
        def add() -> list[int]:
            with self.inv.transaction():
                ids = [self.inv.add_attachment(part_id, source) for source in sources]
            return [i for i, source in zip(ids, sources) if is_url(source)]

        link_ids = self.attempt(add)
        if link_ids:
            self.start_downloads(link_ids)

    @writes
    def store_locally(self, attachment: Attachment) -> None:
        self.start_downloads([attachment.id])

    @writes
    def remove_attachment(self, attachment: Attachment) -> None:
        if confirm(self, f"Remove {attachment.original_name}?"):
            self.attempt(self.inv.remove_attachment, attachment.id)

    # -- downloads ------------------------------------------------------------

    def start_downloads(self, attachment_ids: list[int]) -> None:
        if not attachment_ids:
            return
        downloader = Downloader(self.db_path, list(attachment_ids))
        downloader.finished.connect(self._downloads_finished)  # queued: runs in the GUI thread
        self._downloads.add(downloader)
        self.statusBar().showMessage(f"Downloading {len(attachment_ids)} file(s)…")
        downloader.start()

    def _downloads_finished(self, downloader: Downloader, results: list) -> None:
        self._downloads.discard(downloader)
        stored = [name for name, error in results if error is None]
        failed = [error for _, error in results if error is not None]
        self.refresh()
        if stored:
            self.statusBar().showMessage(f"Stored {', '.join(stored)} locally.")
        if failed:
            show_error(self, "Kept a link instead of the file:\n" + "\n".join(failed))

    # -- locations ------------------------------------------------------------

    def selected_location_path(self) -> str | None:
        item = self.location_tree.currentItem()
        return item.data(0, ROLE_ID) if item is not None else None

    @writes
    def add_location(self) -> None:
        dialog = LocationDialog(self.inv, None, self, parent_path=self.selected_location_path() or "")
        if run_dialog(dialog):
            self.refresh()

    @writes
    def edit_location(self) -> None:
        path = self.selected_location_path()
        location = next((l for l in self.inv.list_locations() if l.path == path), None)
        if location is not None and run_dialog(LocationDialog(self.inv, location, self)):
            self.refresh()

    @writes
    def delete_location(self) -> None:
        path = self.selected_location_path()
        if path is not None and confirm(self, f"Delete the location {path}?"):
            self.attempt(self.inv.delete_location, "/" + path)

    def show_location_parts(self) -> None:
        path = self.selected_location_path()
        if path is None:
            return
        self.search.clear()
        self.tag_filter.setCurrentIndex(0)
        self.category_filter.setCurrentIndex(0)
        self.location_filter.setCurrentIndex(max(self.location_filter.findData("/" + path), 0))
        self.tabs.setCurrentIndex(0)

    # -- CSV ------------------------------------------------------------------

    def print_labels(self) -> None:
        """Open the label dialog with the selected location or part checked."""
        on_locations = self.tabs.currentIndex() == 1
        location = self.selected_location_path() if on_locations else None
        part = None if on_locations else self.current_part()
        dialog = LabelDialog(self.inv, self.settings, self, location_paths=[location] if location else [],
                             part_ids=[part.id] if part else [])
        run_dialog(dialog)

    @writes
    def choose_import(self) -> None:
        dialog = ImportDialog(self.inv.list_locations(), self.settings.value("last_dir", ""), self)
        if not run_dialog(dialog) or not dialog.path.text().strip():
            return
        path = Path(dialog.path.text().strip())
        self.settings.setValue("last_dir", str(path.parent))
        self.import_file(path, add_stock=dialog.add_stock.isChecked(), location=dialog.location.reference())

    @writes
    def import_file(self, path: Path, *, add_stock: bool = False, location: str = "") -> ImportResult | None:
        """Check the file with a dry run, ask, then import it and download its links."""
        try:
            text = decode_csv(path.read_bytes())
        except OSError as e:
            show_error(self, f"cannot read {path}: {e.strerror}")
            return None

        def run(dry_run: bool) -> ImportResult:
            return import_csv(self.inv, io.StringIO(text, newline=""), base_dir=path.parent, add_stock=add_stock,
                              default_location=location or None, dry_run=dry_run,
                              note=f"CSV import: {path.name}")

        try:
            preview = run(True)
            summary = (f"{path.name} has {preview.rows} row(s): {len(preview.created)} new part(s),"
                       f" {len(preview.updated)} to update.")
            if not confirm(self, summary + "\n\nImport it?", "\n".join(preview.warnings)):
                return None
            result = run(False)
        except InventoryError as e:
            show_error(self, f"Nothing was imported: {e}")
            return None
        finally:
            self.refresh()
        self.statusBar().showMessage(f"Imported {result.rows} row(s).")
        self.start_downloads(result.link_ids)
        return result

    def choose_export(self) -> None:
        start = Path(self.settings.value("last_dir", "") or Path.home()) / "parts.csv"
        path, chosen = QFileDialog.getSaveFileName(self, "Export CSV", str(start), ";;".join(EXPORT_FORMATS))
        if path:
            self.settings.setValue("last_dir", str(Path(path).parent))
            delimiter, bom = EXPORT_FORMATS.get(chosen, (",", False))
            self.export_file(Path(path), delimiter=delimiter, bom=bom)

    def export_file(self, path: Path, *, delimiter: str = ",", bom: bool = False) -> None:
        try:
            with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
                count = export_csv(self.inv, f, delimiter=delimiter)
        except OSError as e:
            show_error(self, f"cannot write {path}: {e.strerror}")
            return
        self.statusBar().showMessage(f"Exported {count} part(s) to {path}.")


# -- startup ------------------------------------------------------------------


def set_windows_app_id() -> None:
    """Give the process its own taskbar identity on Windows.

    Otherwise Windows groups the window under the Python interpreter that runs
    it and shows Python's icon in the taskbar instead of the window icon.
    """
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("partstrack.desktop")


def apply_theme(key: str) -> None:
    """Ask the platform for a light or dark look. Where it can't do that (some
    Linux desktops), switch to the Fusion style with a palette of our own."""
    app = QApplication.instance()
    scheme = THEMES.get(key, THEMES["auto"])[1]
    hints = QGuiApplication.styleHints()
    hints.setColorScheme(scheme)
    if scheme == Qt.ColorScheme.Unknown or hints.colorScheme() == scheme:
        app.setPalette(QPalette())  # back to the platform's colours
        return
    app.setStyle("Fusion")
    app.setPalette(dark_palette() if scheme == Qt.ColorScheme.Dark else app.style().standardPalette())


def dark_palette() -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#2b2c2b",
        QPalette.ColorRole.WindowText: "#e8e8e3",
        QPalette.ColorRole.Base: "#1f201f",
        QPalette.ColorRole.AlternateBase: "#2b2c2b",
        QPalette.ColorRole.ToolTipBase: "#1f201f",
        QPalette.ColorRole.ToolTipText: "#e8e8e3",
        QPalette.ColorRole.PlaceholderText: "#8d8d86",
        QPalette.ColorRole.Text: "#e8e8e3",
        QPalette.ColorRole.Button: "#353635",
        QPalette.ColorRole.ButtonText: "#e8e8e3",
        QPalette.ColorRole.BrightText: "#f2877e",
        QPalette.ColorRole.Light: "#4a4b49",
        QPalette.ColorRole.Midlight: "#3f403e",
        QPalette.ColorRole.Mid: "#2f302f",
        QPalette.ColorRole.Dark: "#1a1b1a",
        QPalette.ColorRole.Shadow: "#000000",
        QPalette.ColorRole.Highlight: "#2f7d6a",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: "#5fb89f",
        QPalette.ColorRole.LinkVisited: "#5fb89f",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#7a7a75"))
    return palette


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="partstrack-desktop", description="Desktop app for the parts inventory.")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"database file (default: $PARTSTRACK_DB, the last one opened, or {default_db_path()})")
    args = parser.parse_args(argv)

    set_windows_app_id()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("partstrack")
    app.setApplicationName("partstrack")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(LOGO_PATH)))
    if app.style().name() == "windowsvista":
        app.setStyle("Fusion")  # the Windows 10 style has no dark mode
    settings = QSettings()
    apply_theme(settings.value("theme", "auto"))

    if args.db is not None:
        db_path = args.db
    elif os.environ.get("PARTSTRACK_DB") or not settings.value("database"):
        db_path = default_db_path()
    else:
        db_path = Path(settings.value("database"))

    window = MainWindow(db_path, settings)
    if not window.open_database(db_path):
        return 1
    window.show()
    status = app.exec()
    del window  # destroy the widgets while the QApplication still exists
    return status
