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

"""Printable labels with QR codes for locations and parts.

This module only computes what goes where; the web interface and the desktop
app draw the result. All sizes are in millimetres, measured from the top left
corner of the page.

Three kinds of layout cover the usual ways of printing labels:
- plain paper: a grid of labels with cut lines, to cut out;
- sticker sheets: a grid that matches pre-cut adhesive labels;
- label printers: the page is a single label.

Custom sheets and label sizes can be given as options (see layout_from_options).
QR codes need the optional `segno` package (in the 'web' and 'desktop'
extras); it is imported only when a code is made, so the CLI stays free of
dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from .store import LOCATION_SEPARATOR, InventoryError, Location, Part

A4 = (210.0, 297.0)
MIN_LABEL_SIZE = 10.0  # mm; smaller labels can't hold a QR code that a phone reads
PADDING = 1.5  # mm between a label's edge and its contents


@dataclass(frozen=True)
class Layout:
    key: str
    name: str
    page_width: float
    page_height: float
    columns: int
    rows: int
    label_width: float
    label_height: float
    margin_left: float = 0.0
    margin_top: float = 0.0
    gap_x: float = 0.0
    gap_y: float = 0.0
    cut_lines: bool = False

    @property
    def per_page(self) -> int:
        return self.columns * self.rows

    def check(self) -> None:
        """Raise InventoryError unless the labels fit on the page."""
        if self.columns < 1 or self.rows < 1:
            raise InventoryError("a layout needs at least one column and one row")
        if min(self.label_width, self.label_height) < MIN_LABEL_SIZE:
            raise InventoryError(f"labels must be at least {MIN_LABEL_SIZE:g} mm wide and high")
        if min(self.margin_left, self.margin_top, self.gap_x, self.gap_y) < 0:
            raise InventoryError("margins and gaps must not be negative")
        width = self.margin_left + self.columns * self.label_width + (self.columns - 1) * self.gap_x
        height = self.margin_top + self.rows * self.label_height + (self.rows - 1) * self.gap_y
        if width > self.page_width + 0.01 or height > self.page_height + 0.01:
            raise InventoryError(
                f"the labels need {width:g} × {height:g} mm, more than the page's"
                f" {self.page_width:g} × {self.page_height:g} mm"
            )


def _plain(columns: int, rows: int) -> Layout:
    """Plain A4 paper filled with a centred grid of labels, separated by cut lines."""
    width, height = 200.0 / columns, 280.0 / rows
    return Layout(
        f"plain-{columns}x{rows}", f"Plain paper: {columns} × {rows} labels of {width:.4g} × {height:.4g} mm",
        *A4, columns, rows, round(width, 2), round(height, 2), 5.0, 8.5, cut_lines=True,
    )


def _sheet(columns: int, rows: int, width: float, height: float) -> Layout:
    """A4 sticker sheet with labels that touch each other, centred on the page."""
    width, height = float(width), float(height)
    return Layout(
        f"sheet-{columns}x{rows}-{width:g}x{height:g}",
        f"Sticker sheet: {columns} × {rows} labels of {width:g} × {height:g} mm",
        *A4, columns, rows, width, height,
        round((A4[0] - columns * width) / 2, 2), round((A4[1] - rows * height) / 2, 2),
    )


def _printer(width: float, height: float) -> Layout:
    width, height = float(width), float(height)
    return Layout(f"printer-{width:g}x{height:g}", f"Label printer: {width:g} × {height:g} mm",
                  width, height, 1, 1, width, height)


LAYOUTS: dict[str, Layout] = {
    layout.key: layout
    for layout in [
        _plain(4, 10),
        _plain(3, 8),
        _plain(2, 6),
        _sheet(3, 8, 70, 37),
        _sheet(3, 7, 70, 42.3),
        _sheet(4, 10, 48.5, 25.4),
        _sheet(2, 8, 105, 37),
        _printer(62, 29),
        _printer(89, 36),
    ]
}
DEFAULT_LAYOUT = "plain-4x10"
CUSTOM_SHEET = "custom-sheet"
CUSTOM_PRINTER = "custom-printer"
# Options of the custom layouts, with their defaults. Sheets are A4.
CUSTOM_SHEET_OPTIONS = {
    "columns": 3, "rows": 8, "label_width": 70.0, "label_height": 37.0,
    "margin_left": 0.0, "margin_top": 0.5, "gap_x": 0.0, "gap_y": 0.0,
}
CUSTOM_PRINTER_OPTIONS = {"label_width": 62.0, "label_height": 29.0}
CUSTOM_LAYOUT_NAMES = {CUSTOM_SHEET: "Custom sticker sheet (A4)", CUSTOM_PRINTER: "Custom label printer size"}


def layout_from_options(options: Mapping[str, str]) -> Layout:
    """Build the layout chosen in a form: options['layout'] is a key of
    LAYOUTS, CUSTOM_SHEET or CUSTOM_PRINTER; custom layouts take the values
    named in CUSTOM_SHEET_OPTIONS or CUSTOM_PRINTER_OPTIONS (defaults if missing)."""
    key = options.get("layout") or DEFAULT_LAYOUT
    if key in LAYOUTS:
        return LAYOUTS[key]
    if key == CUSTOM_SHEET:
        values = _numbers(options, CUSTOM_SHEET_OPTIONS)
        layout = Layout(key, CUSTOM_LAYOUT_NAMES[key], *A4, **values)
    elif key == CUSTOM_PRINTER:
        values = _numbers(options, CUSTOM_PRINTER_OPTIONS)
        layout = Layout(key, CUSTOM_LAYOUT_NAMES[key], values["label_width"], values["label_height"], 1, 1,
                        values["label_width"], values["label_height"])
    else:
        raise InventoryError(f"unknown label layout '{key}'")
    layout.check()
    return layout


def _numbers(options: Mapping[str, str], defaults: dict[str, float]) -> dict:
    values = {}
    for name, default in defaults.items():
        text = str(options.get(name, "")).strip().replace(",", ".")
        what = name.replace("_", " ")
        try:
            value = type(default)(text) if text else default
        except ValueError:
            kind = "a whole number" if isinstance(default, int) else "a number"
            raise InventoryError(f"{what} must be {kind}, got '{text}'") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise InventoryError(f"{what} must be a number, got '{text}'")
        values[name] = value
    return values


def positions(layout: Layout, count: int, start: int = 0) -> list[list[tuple[float, float]]]:
    """Top left corners of `count` labels, page by page, filled row by row.
    `start` skips that many places on the first page, to use up a partly used sheet."""
    if not 0 <= start < layout.per_page:
        raise InventoryError(f"the first label's position must be between 1 and {layout.per_page}")
    pages: list[list[tuple[float, float]]] = []
    for index in range(start, start + count):
        if index % layout.per_page == 0 or not pages:
            pages.append([])
        row, column = divmod(index % layout.per_page, layout.columns)
        pages[-1].append((
            round(layout.margin_left + column * (layout.label_width + layout.gap_x), 3),
            round(layout.margin_top + row * (layout.label_height + layout.gap_y), 3),
        ))
    return pages


# -- label contents -----------------------------------------------------------


@dataclass(frozen=True)
class Label:
    url: str  # what the QR code holds
    title: str
    subtitle: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Geometry:
    """Where things go inside a label, relative to its top left corner."""

    qr_x: float
    qr_y: float
    qr_size: float
    text_x: float
    text_y: float
    text_width: float
    text_height: float
    title_size: float  # font sizes (height of the em box)
    text_size: float


def geometry(layout: Layout) -> Geometry:
    """The QR code is a square on the left, the text fills the rest."""
    width, height = layout.label_width, layout.label_height
    qr_size = min(height, width / 2) - 2 * PADDING
    text_x = PADDING + qr_size + PADDING
    title_size = max(2.2, min(4.5, height / 7))
    return Geometry(
        qr_x=PADDING,
        qr_y=(height - qr_size) / 2,
        qr_size=qr_size,
        text_x=text_x,
        text_y=PADDING,
        text_width=width - text_x - PADDING,
        text_height=height - 2 * PADDING,
        title_size=round(title_size, 2),
        text_size=round(title_size * 0.75, 2),
    )


def normalize_base_url(url: str) -> str:
    """Check the web interface's address and give it a trailing slash."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InventoryError(f"the web interface's address must start with http:// or https://, got '{url}'")
    return url if url.endswith("/") else url + "/"


def is_local_url(url: str) -> bool:
    """Whether `url` points at this machine only, so other devices can't open it."""
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def location_url(base_url: str, path: str) -> str:
    """The web interface's list of parts at a location (and inside it)."""
    return f"{normalize_base_url(base_url)}?location={quote(LOCATION_SEPARATOR + path, safe='/')}"


def part_url(base_url: str, part_id: int) -> str:
    return f"{normalize_base_url(base_url)}parts/{part_id}"


def location_label(base_url: str, location: Location) -> Label:
    parent = location.path.rpartition(LOCATION_SEPARATOR)[0]
    return Label(location_url(base_url, location.path), location.name, parent, location.description)


def part_label(base_url: str, part: Part) -> Label:
    return Label(part_url(base_url, part.id), part.name, part.package or part.mpn, part.description)


def select_locations(locations: list[Location], paths: list[str], *, sublocations: bool) -> list[Location]:
    """The locations with the given full paths (and, with `sublocations`, everything
    inside them), in the order of `locations`, without duplicates."""
    wanted = set(paths)
    prefixes = tuple(path + LOCATION_SEPARATOR for path in paths)
    return [
        l for l in locations
        if l.path in wanted or (sublocations and prefixes and l.path.startswith(prefixes))
    ]


# -- QR codes -----------------------------------------------------------------


def qr_matrix(text: str) -> list[list[bool]]:
    """The modules of a QR code for `text` (True is dark), without the quiet zone."""
    try:
        import segno
    except ImportError:
        raise InventoryError("printing QR codes needs the 'segno' package (pip install segno)") from None
    code = segno.make(text, error="m", micro=False)
    return [[bool(module) for module in row] for row in code.matrix]


def svg_path(matrix: list[list[bool]]) -> str:
    """SVG path data that draws the dark modules, one unit per module."""
    parts = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start} {y}h{x - start}v1h{start - x}z")
            else:
                x += 1
    return "".join(parts)
