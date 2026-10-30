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

"""CSV import and export.

The CSV has one row per part and location, so a part stored in two places
takes two rows (a part without stock gets one row with an empty location).
Tags and attachments are separated by ';' within a cell, and every attribute
gets its own column named 'attr:<name>'.

Importing matches parts by name: new parts are created, existing ones are
updated. Empty cells never clear existing values, so a CSV with only some
columns (e.g. name, location, quantity) works too.
"""

from __future__ import annotations

import csv
import filecmp
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .store import EDITABLE_FIELDS, LOCATION_SEPARATOR, Inventory, InventoryError, Part, is_url, nocase

COLUMNS = [
    "name", "description", "manufacturer", "mpn", "category", "package",
    "location", "quantity", "tags", "datasheets", "images", "other_attachments", "notes",
]
ATTRIBUTE_PREFIX = "attr:"
LIST_SEPARATOR = ";"
ATTACHMENT_COLUMNS = {"datasheets": "datasheet", "images": "image", "other_attachments": "other"}

# Alternative header names accepted on import (compared case-insensitively).
HEADER_ALIASES = {
    "qty": "quantity",
    "amount": "quantity",
    "part number": "mpn",
    "manufacturer part number": "mpn",
    "mfr": "manufacturer",
    "storage": "location",
    "tag": "tags",
    "datasheet": "datasheets",
    "image": "images",
    "attachments": "other_attachments",
}


@dataclass
class ImportResult:
    rows: int = 0
    created: set[int] = field(default_factory=set)  # part ids
    updated: set[int] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    skipped_files: int = 0  # file attachments not imported because allow_files was off
    link_ids: list[int] = field(default_factory=list)  # link attachments added; see Inventory.download_links


class _DryRun(Exception):
    pass


def export_csv(inv: Inventory, stream: TextIO, *, delimiter: str = ",") -> int:
    """Write all parts to `stream`; returns the number of parts written."""
    parts = inv.list_parts()
    attribute_keys: dict[str, str] = {}  # case-folded like the database -> first spelling seen
    for part in parts:
        for key in part.attributes:
            attribute_keys.setdefault(nocase(key), key)
    keys = sorted(attribute_keys.values(), key=str.casefold)

    writer = csv.writer(stream, delimiter=delimiter)
    writer.writerow(COLUMNS + [ATTRIBUTE_PREFIX + key for key in keys])
    for part in parts:
        attributes = {nocase(k): v for k, v in part.attributes.items()}
        attachments = {
            column: LIST_SEPARATOR.join(a.target for a in part.attachments if a.kind == kind)
            for column, kind in ATTACHMENT_COLUMNS.items()
        }
        for entry in part.stock or [None]:
            row = {
                **{name: getattr(part, name) for name in EDITABLE_FIELDS},
                # Full paths, so that importing never matches a different
                # location that merely ends with the same name.
                "location": LOCATION_SEPARATOR + entry.location if entry else "",
                "quantity": entry.quantity if entry else "",
                "tags": LIST_SEPARATOR.join(part.tags),
                **attachments,
            }
            writer.writerow([row[c] for c in COLUMNS] + [attributes.get(nocase(k), "") for k in keys])
    return len(parts)


def decode_csv(data: bytes) -> str:
    """Decode a CSV file's bytes: UTF-8 (with or without BOM), falling back to
    Windows-1252, which is what Excel writes in Western European locales."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise InventoryError("the CSV file is neither UTF-8 nor Windows-1252 encoded")


def import_csv(
    inv: Inventory,
    stream: TextIO,
    *,
    base_dir: Path = Path("."),
    add_stock: bool = False,
    default_location: str | None = None,
    delimiter: str | None = None,
    dry_run: bool = False,
    allow_files: bool = True,
) -> ImportResult:
    """Import parts from CSV. Either every row is imported or, on error, nothing.

    Quantities replace the stock at the given location, or are added to it
    with `add_stock`. Rows without a location use `default_location`.
    Relative attachment paths are resolved against `base_dir`. With `dry_run`
    everything is checked but nothing is saved.

    Set `allow_files=False` when the CSV comes from someone who must not be
    able to read files on this machine (e.g. a web upload): only links are
    then imported as attachments, and file paths are skipped.
    """
    text = stream.read()
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter or _guess_delimiter(text))
    if not reader.fieldnames:
        raise InventoryError("the CSV file is empty")

    result = ImportResult()
    columns = {}  # CSV header -> our column name
    for header in reader.fieldnames:
        name = header.strip().lower()
        name = HEADER_ALIASES.get(name, name)
        if name.startswith(ATTRIBUTE_PREFIX) and header.strip()[len(ATTRIBUTE_PREFIX):].strip():
            columns[header] = ATTRIBUTE_PREFIX + header.strip()[len(ATTRIBUTE_PREFIX):].strip()
        elif name in COLUMNS:
            columns[header] = name
        else:
            result.warnings.append(f"ignoring unknown column '{header}'")
    if "name" not in columns.values():
        raise InventoryError("the CSV file needs a 'name' column")

    try:
        with inv.transaction():
            for row in reader:
                values = {columns[h]: (v or "").strip() for h, v in row.items() if h in columns}
                if not any(values.values()):
                    continue
                try:
                    _import_row(inv, values, result, base_dir, add_stock, default_location, allow_files)
                except InventoryError as e:
                    raise InventoryError(f"line {reader.line_num}: {e}") from None
                result.rows += 1
            if dry_run:
                raise _DryRun
    except _DryRun:
        result.link_ids.clear()  # rolled back, so there is nothing to download
    except csv.Error as e:
        raise InventoryError(f"line {reader.line_num}: invalid CSV: {e}") from None
    if result.skipped_files:
        result.warnings.append(
            f"skipped {result.skipped_files} file attachment(s); only links can be imported here"
        )
    return result


def _import_row(
    inv: Inventory,
    values: dict[str, str],
    result: ImportResult,
    base_dir: Path,
    add_stock: bool,
    default_location: str | None,
    allow_files: bool,
) -> None:
    name = values.get("name", "")
    if not name:
        raise InventoryError("missing part name")
    fields = {k: values[k] for k in EDITABLE_FIELDS if k != "name" and values.get(k)}
    part_id = inv.part_id_by_name(name)
    if part_id is None:
        part_id = inv.add_part(name, **fields)
        result.created.add(part_id)
    else:
        inv.update_part(part_id, **fields)
        if part_id not in result.created:
            result.updated.add(part_id)

    inv.add_tags(part_id, _split_list(values.get("tags", "")))
    for column, value in values.items():
        if column.startswith(ATTRIBUTE_PREFIX) and value:
            inv.set_attribute(part_id, column[len(ATTRIBUTE_PREFIX):], value)

    if values.get("quantity"):
        quantity = _parse_quantity(values["quantity"])
        location = values.get("location") or default_location
        if not location:
            raise InventoryError("quantity given but no location (set a default location for such rows)")
        if add_stock:
            if quantity:
                inv.adjust_stock(part_id, location, quantity)
        else:
            inv.set_stock(part_id, location, quantity)

    for column, kind in ATTACHMENT_COLUMNS.items():
        for source in _split_list(values.get(column, "")):
            if not is_url(source):
                if not allow_files:
                    result.skipped_files += 1
                    continue
                source = str(base_dir / Path(source).expanduser())
            if not _has_attachment(inv.get_part(part_id), source):
                attachment_id = inv.add_attachment(part_id, source, kind)
                if is_url(source):
                    result.link_ids.append(attachment_id)


def _has_attachment(part: Part, source: str) -> bool:
    """Whether `source` (a URL or file) is already attached, so re-imports don't duplicate it."""
    if is_url(source):
        return any(source in (a.url, a.source_url) for a in part.attachments)
    path = Path(source)
    if not path.is_file():
        return False  # add_attachment reports the missing file
    for a in part.attachments:
        if a.path is None or not a.path.is_file():
            continue
        if a.path.resolve() == path.resolve():
            return True
        if a.original_name == path.name and filecmp.cmp(a.path, path, shallow=False):
            return True
    return False


def _split_list(text: str) -> list[str]:
    return [item.strip() for item in text.split(LIST_SEPARATOR) if item.strip()]


def _parse_quantity(text: str) -> int:
    try:
        number = float(text)
    except ValueError:
        raise InventoryError(f"invalid quantity '{text}'") from None
    if not number.is_integer():
        raise InventoryError(f"quantity must be a whole number, got '{text}'")
    return int(number)


def _guess_delimiter(text: str) -> str:
    header = text.split("\n", 1)[0]
    return max([",", ";", "\t"], key=header.count)
