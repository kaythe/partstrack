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

"""SQLite-backed storage for the parts inventory.

All application logic lives here so that the CLI and a future GUI can share it.
The public API takes and returns plain Python values and dataclasses; nothing
here prints or reads from the terminal.

Locations are nested and addressed by paths like "Cabinet A/Drawer 3". A
trailing part of a path ("Drawer 3") is enough as long as it is unambiguous;
a leading "/" requires the full path from the top level.
"""

from __future__ import annotations

import http.client
import ipaddress
import shutil
import socket
import sqlite3
import string
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

ATTACHMENT_KINDS = ("datasheet", "image", "other")
EDITABLE_FIELDS = ("name", "description", "manufacturer", "mpn", "category", "package", "notes")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg"}
# Links ending in these are web pages rather than files, so they are never downloaded.
PAGE_SUFFIXES = {".htm", ".html", ".php", ".asp", ".aspx", ".jsp", ".cgi"}
DOWNLOAD_TIMEOUT = 30  # seconds without data before a download is given up
MAX_DOWNLOAD_SIZE = 200 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; partstrack)"
LOCATION_SEPARATOR = "/"

# Prefix for queries that need the full path of each location.
LOCATION_PATHS = """
WITH RECURSIVE location_paths (id, path) AS (
    SELECT id, name FROM locations WHERE parent_id IS NULL
    UNION ALL
    SELECT l.id, lp.path || '/' || l.name FROM locations l JOIN location_paths lp ON l.parent_id = lp.id
)
"""


def _nest_locations(conn: sqlite3.Connection) -> None:
    """Schema v2: locations get a parent; v1 names like "Cabinet/Drawer 2" become nested."""
    # The UNIQUE constraint on the name has to go, which requires rebuilding the table.
    conn.execute(
        """
        CREATE TABLE new_locations (
            id          INTEGER PRIMARY KEY,
            parent_id   INTEGER REFERENCES locations(id) ON DELETE RESTRICT,
            name        TEXT NOT NULL COLLATE NOCASE,
            description TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("INSERT INTO new_locations (id, name, description) SELECT id, name, description FROM locations")
    conn.execute("DROP TABLE locations")
    conn.execute("ALTER TABLE new_locations RENAME TO locations")

    def child(parent_id: int | None, name: str) -> int:
        row = conn.execute(
            "SELECT id FROM locations WHERE parent_id IS ? AND name = ?", (parent_id, name)
        ).fetchone()
        if row:
            return row[0]
        return conn.execute(
            "INSERT INTO locations (parent_id, name) VALUES (?, ?)", (parent_id, name)
        ).lastrowid

    for location_id, name in conn.execute("SELECT id, name FROM locations WHERE instr(name, '/') > 0").fetchall():
        segments = [s.strip() for s in name.split(LOCATION_SEPARATOR) if s.strip()] or ["unnamed"]
        parent_id = None
        for segment in segments[:-1]:
            parent_id = child(parent_id, segment)
        existing = conn.execute(
            "SELECT id FROM locations WHERE parent_id IS ? AND name = ? AND id <> ?",
            (parent_id, segments[-1], location_id),
        ).fetchone()
        if existing:
            # Two v1 locations end up at the same place: merge their stock.
            conn.execute(
                "INSERT INTO stock (part_id, location_id, quantity)"
                " SELECT part_id, ?, quantity FROM stock WHERE location_id = ?"
                " ON CONFLICT (part_id, location_id) DO UPDATE SET quantity = quantity + excluded.quantity",
                (existing[0], location_id),
            )
            conn.execute("DELETE FROM stock WHERE location_id = ?", (location_id,))
            conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        else:
            conn.execute(
                "UPDATE locations SET parent_id = ?, name = ? WHERE id = ?",
                (parent_id, segments[-1], location_id),
            )

    conn.execute("CREATE UNIQUE INDEX locations_name_idx ON locations (COALESCE(parent_id, 0), name)")
    conn.execute("CREATE INDEX locations_parent_idx ON locations (parent_id)")


# Each entry upgrades the schema by one version (tracked in PRAGMA user_version)
# and is either an SQL script or a function taking the connection. Append new
# migrations; never edit existing ones.
MIGRATIONS: list[str | Callable[[sqlite3.Connection], None]] = [
    """
    CREATE TABLE parts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description  TEXT NOT NULL DEFAULT '',
        manufacturer TEXT NOT NULL DEFAULT '',
        mpn          TEXT NOT NULL DEFAULT '',
        category     TEXT NOT NULL DEFAULT '',
        package      TEXT NOT NULL DEFAULT '',
        notes        TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE locations (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE stock (
        part_id     INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
        location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE RESTRICT,
        quantity    INTEGER NOT NULL CHECK (quantity >= 0),
        PRIMARY KEY (part_id, location_id)
    );

    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE
    );

    CREATE TABLE part_tags (
        part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
        tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (part_id, tag_id)
    );

    CREATE TABLE attributes (
        part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
        key     TEXT NOT NULL COLLATE NOCASE,
        value   TEXT NOT NULL,
        PRIMARY KEY (part_id, key)
    );

    -- Either a file copied into the attachments directory (path, relative to
    -- that directory) or an external link (url), never both.
    CREATE TABLE attachments (
        id            INTEGER PRIMARY KEY,
        part_id       INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('datasheet', 'image', 'other')),
        path          TEXT,
        url           TEXT,
        original_name TEXT NOT NULL DEFAULT '',
        added_at      TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK ((path IS NULL) <> (url IS NULL))
    );

    CREATE INDEX stock_location_idx ON stock(location_id);
    CREATE INDEX part_tags_tag_idx ON part_tags(tag_id);
    CREATE INDEX attachments_part_idx ON attachments(part_id);
    """,
    _nest_locations,
    # Where a downloaded attachment came from.
    "ALTER TABLE attachments ADD COLUMN source_url TEXT;",
]


class InventoryError(Exception):
    """A user-facing error, e.g. invalid input or a constraint violation."""


class NotFoundError(InventoryError):
    pass


@dataclass
class StockEntry:
    location: str  # full path
    quantity: int


@dataclass
class Attachment:
    id: int
    part_id: int
    kind: str
    original_name: str
    added_at: str
    path: Path | None = None  # absolute path of a stored file
    url: str | None = None
    source_url: str | None = None  # link a stored file was downloaded from

    @property
    def target(self) -> str:
        return self.url if self.url is not None else str(self.path)


@dataclass
class Part:
    id: int
    name: str
    description: str = ""
    manufacturer: str = ""
    mpn: str = ""
    category: str = ""
    package: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    stock: list[StockEntry] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(s.quantity for s in self.stock)


@dataclass
class Location:
    id: int
    name: str
    path: str
    parent_id: int | None
    description: str
    part_count: int  # parts stored directly here, not in sublocations
    total_quantity: int

    @property
    def depth(self) -> int:
        return self.path.count(LOCATION_SEPARATOR)


def guess_attachment_kind(source: str) -> str:
    suffix = Path(urlparse(source).path if is_url(source) else source).suffix.lower()
    if suffix == ".pdf":
        return "datasheet"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "other"


def is_url(source: str) -> bool:
    return urlparse(source).scheme in ("http", "https", "ftp")


def downloadable_name(url: str) -> str | None:
    """Return the name of the file a link points to, or None if the link does
    not point to a single file ('https://x.com/ds/ne555.pdf' -> 'ne555.pdf')."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    name = Path(unquote(parsed.path).replace("\\", "/")).name.strip()
    suffix = Path(name).suffix.lower()
    if name.startswith(".") or not suffix or suffix in PAGE_SUFFIXES:
        return None
    return name


def _check_download_url(url: str, allow_local: bool) -> None:
    """Refuse links to this machine or to link-local addresses: someone using the
    web interface must not be able to fetch services only the server can reach."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InventoryError(f"can only download http(s) links, not {url}")
    if allow_local:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise InventoryError(f"cannot find the server {parsed.hostname}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (address.is_loopback or address.is_link_local or address.is_unspecified
                or address.is_multicast or address.is_reserved):
            raise InventoryError(f"refusing to download from {parsed.hostname}, a local or reserved address")


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_local: bool):
        super().__init__()
        self.allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_download_url(newurl, self.allow_local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, dest: Path, *, allow_local: bool) -> None:
    _check_download_url(url, allow_local)
    opener = urllib.request.build_opener(_CheckedRedirectHandler(allow_local))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response, open(dest, "wb") as out:
            if response.headers.get_content_type() == "text/html":
                raise InventoryError(f"{url} leads to a web page, not a file")
            size = 0
            while chunk := response.read(1 << 16):
                size += len(chunk)
                if size > MAX_DOWNLOAD_SIZE:
                    raise InventoryError(f"{url} is larger than {MAX_DOWNLOAD_SIZE >> 20} MB")
                out.write(chunk)
    except urllib.error.HTTPError as e:
        raise InventoryError(f"downloading {url} failed: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise InventoryError(f"downloading {url} failed: {e.reason}") from None
    except (OSError, http.client.HTTPException) as e:
        raise InventoryError(f"downloading {url} failed: {e}") from None


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _unique_destination(directory: Path, name: str) -> Path:
    dest = directory / name
    n = 1
    while dest.exists():
        dest = directory / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    return dest


def _parse_location(ref: str) -> tuple[bool, list[str]]:
    """Split a location reference into (is_absolute, path segments)."""
    ref = ref.strip()
    segments = [s.strip() for s in ref.split(LOCATION_SEPARATOR) if s.strip()]
    if not segments:
        raise InventoryError("location must not be empty")
    return ref.startswith(LOCATION_SEPARATOR), segments


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def nocase(text: str) -> str:
    """Fold case the way SQLite's NOCASE collation does (ASCII letters only),
    so Python comparisons agree with the database's uniqueness rules."""
    return text.translate(_ASCII_LOWER)


def _path_key(path: str) -> list[str]:
    """Sort key that keeps each location directly before its children."""
    return [s.casefold() for s in path.split(LOCATION_SEPARATOR)]


def _sql_statements(script: str) -> Iterator[str]:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


class Inventory:
    """The parts database. Attachment files are stored next to the database file."""

    # Downloads from this machine are refused (see _check_download_url); tests allow them.
    allow_local_downloads = False

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.attachments_dir = self.db_path.parent / "attachments"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._transaction_depth = 0
        self._after_commit: list[Callable[[], None]] = []
        self._after_rollback: list[Callable[[], None]] = []
        self._migrate()
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Inventory:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise InventoryError(f"{self.db_path} was created by a newer version of partstrack")
        # Table rebuilds need foreign keys off; that can only be changed outside a transaction.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.execute("BEGIN")
            try:
                if callable(migration):
                    migration(self.conn)
                else:
                    for statement in _sql_statements(migration):
                        self.conn.execute(statement)
                if self.conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise InventoryError(f"schema migration {number} left dangling references")
                self.conn.execute(f"PRAGMA user_version = {number}")
            except BaseException:
                self.conn.rollback()
                raise
            self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group operations so that they are saved together or not at all.

        Transactions can be nested: a failing inner block is undone on its own
        (using a savepoint), and only the outermost block commits.
        """
        depth = self._transaction_depth
        self._transaction_depth += 1
        try:
            if depth:
                yield from self._savepoint(f"level{depth}")
                return
            try:
                # Begin explicitly so that savepoints are always nested inside
                # this transaction rather than starting (and committing) their own.
                if not self.conn.in_transaction:
                    self.conn.execute("BEGIN")
                with self.conn:
                    yield
            except BaseException:
                self._run_callbacks(self._after_rollback)
                raise
            else:
                self._run_callbacks(self._after_commit)
            finally:
                self._after_commit.clear()
                self._after_rollback.clear()
        finally:
            self._transaction_depth = depth

    def _savepoint(self, name: str) -> Iterator[None]:
        commit_mark, rollback_mark = len(self._after_commit), len(self._after_rollback)
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
            self._run_callbacks(self._after_rollback[rollback_mark:])
            del self._after_rollback[rollback_mark:]
            del self._after_commit[commit_mark:]
            raise
        self.conn.execute(f"RELEASE {name}")

    @staticmethod
    def _run_callbacks(callbacks: list[Callable[[], None]]) -> None:
        for callback in callbacks:
            try:
                callback()
            except OSError:
                pass

    # -- parts ---------------------------------------------------------------

    def add_part(self, name: str, **fields: str) -> int:
        name = self._check_part_name(name)
        self._check_fields(fields)
        columns = ["name", *fields]
        try:
            with self.transaction():
                cur = self.conn.execute(
                    f"INSERT INTO parts ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                    [name, *fields.values()],
                )
        except sqlite3.IntegrityError:
            raise InventoryError(f"a part named '{name}' already exists") from None
        return cur.lastrowid

    def update_part(self, part_id: int, **fields: str) -> None:
        self._check_fields(fields)
        if "name" in fields:
            fields["name"] = self._check_part_name(fields["name"])
        self._require_part(part_id)
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        try:
            with self.transaction():
                self.conn.execute(
                    f"UPDATE parts SET {assignments}, updated_at = datetime('now') WHERE id = ?",
                    [*fields.values(), part_id],
                )
        except sqlite3.IntegrityError:
            raise InventoryError(f"a part named '{fields['name']}' already exists") from None

    def delete_part(self, part_id: int) -> None:
        with self.transaction():
            self._require_part(part_id)
            self.conn.execute("DELETE FROM parts WHERE id = ?", (part_id,))
            self._after_commit.append(
                lambda: shutil.rmtree(self.attachments_dir / str(part_id), ignore_errors=True)
            )

    def part_id_by_name(self, name: str) -> int | None:
        row = self.conn.execute("SELECT id FROM parts WHERE name = ?", (name.strip(),)).fetchone()
        return row["id"] if row else None

    def find_part(self, ref: str | int) -> int:
        """Resolve a part reference to its id.

        An exact (case-insensitive) name match wins; otherwise a number is taken
        as an id. Prefix with '#' (e.g. '#555') to force an id lookup.
        """
        ref = str(ref).strip()
        if not ref.startswith("#"):
            part_id = self.part_id_by_name(ref)
            if part_id is not None:
                return part_id
        number = ref.removeprefix("#")
        if number.isdigit():
            row = self.conn.execute("SELECT id FROM parts WHERE id = ?", (int(number),)).fetchone()
            if row:
                return row["id"]
        raise NotFoundError(f"no part '{ref}'")

    def get_part(self, part_id: int) -> Part:
        row = self.conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no part with id {part_id}")
        part = Part(**dict(row))
        part.stock = sorted(
            (
                StockEntry(r["path"], r["quantity"])
                for r in self.conn.execute(
                    LOCATION_PATHS + "SELECT lp.path, s.quantity FROM stock s"
                    " JOIN location_paths lp ON lp.id = s.location_id WHERE s.part_id = ?",
                    (part_id,),
                )
            ),
            key=lambda s: _path_key(s.location),
        )
        part.tags = [
            r["name"]
            for r in self.conn.execute(
                "SELECT t.name FROM part_tags pt JOIN tags t ON t.id = pt.tag_id"
                " WHERE pt.part_id = ? ORDER BY t.name",
                (part_id,),
            )
        ]
        part.attributes = {
            r["key"]: r["value"]
            for r in self.conn.execute(
                "SELECT key, value FROM attributes WHERE part_id = ? ORDER BY key", (part_id,)
            )
        }
        part.attachments = [
            self._attachment_from_row(r)
            for r in self.conn.execute(
                "SELECT * FROM attachments WHERE part_id = ? ORDER BY kind, id", (part_id,)
            )
        ]
        return part

    def list_parts(
        self,
        query: str = "",
        *,
        tag: str | None = None,
        location: str | None = None,
        category: str | None = None,
    ) -> list[Part]:
        """Return matching parts, sorted by name.

        Every word of `query` must appear (case-insensitively) in the part's
        text fields, tags or attribute values. `location` also matches parts
        stored in its sublocations.
        """
        where, params = [], []
        for word in query.split():
            pattern = _like_pattern(word)
            where.append(
                "("
                + " OR ".join(f"p.{c} LIKE ? ESCAPE '\\'" for c in EDITABLE_FIELDS)
                + " OR EXISTS (SELECT 1 FROM attributes a WHERE a.part_id = p.id AND a.value LIKE ? ESCAPE '\\')"
                + " OR EXISTS (SELECT 1 FROM part_tags pt JOIN tags t ON t.id = pt.tag_id"
                "              WHERE pt.part_id = p.id AND t.name LIKE ? ESCAPE '\\')"
                + ")"
            )
            params += [pattern] * (len(EDITABLE_FIELDS) + 2)
        if tag:
            where.append(
                "EXISTS (SELECT 1 FROM part_tags pt JOIN tags t ON t.id = pt.tag_id"
                " WHERE pt.part_id = p.id AND t.name = ?)"
            )
            params.append(tag)
        if location:
            location_ids = self._subtree_ids(self._location_id(location, create=False))
            where.append(
                "EXISTS (SELECT 1 FROM stock s WHERE s.part_id = p.id"
                f" AND s.location_id IN ({', '.join('?' * len(location_ids))}))"
            )
            params += location_ids
        if category:
            where.append("p.category = ? COLLATE NOCASE")
            params.append(category)
        sql = "SELECT p.id FROM parts p"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY p.name"
        return [self.get_part(r["id"]) for r in self.conn.execute(sql, params).fetchall()]

    # -- tags and attributes -------------------------------------------------

    def add_tags(self, part_id: int, tags: list[str]) -> None:
        self._require_part(part_id)
        with self.transaction():
            for tag in (t.strip() for t in tags):
                if not tag:
                    continue
                self.conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                self.conn.execute(
                    "INSERT OR IGNORE INTO part_tags (part_id, tag_id)"
                    " SELECT ?, id FROM tags WHERE name = ?",
                    (part_id, tag),
                )
            self._touch(part_id)

    def remove_tags(self, part_id: int, tags: list[str]) -> None:
        self._require_part(part_id)
        with self.transaction():
            for tag in tags:
                self.conn.execute(
                    "DELETE FROM part_tags WHERE part_id = ?"
                    " AND tag_id = (SELECT id FROM tags WHERE name = ?)",
                    (part_id, tag.strip()),
                )
            # Drop tags no longer used by any part.
            self.conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM part_tags)")
            self._touch(part_id)

    def set_tags(self, part_id: int, tags: list[str]) -> None:
        """Replace the part's tags with `tags`."""
        wanted = {nocase(t.strip()): t.strip() for t in tags if t.strip()}
        with self.transaction():
            current = self.get_part(part_id).tags
            self.remove_tags(part_id, [t for t in current if nocase(t) not in wanted])
            self.add_tags(part_id, list(wanted.values()))

    def list_tags(self) -> list[tuple[str, int]]:
        """Return (tag, number of parts) pairs, sorted by tag."""
        return [
            (r["name"], r["n"])
            for r in self.conn.execute(
                "SELECT t.name, COUNT(pt.part_id) AS n FROM tags t"
                " LEFT JOIN part_tags pt ON pt.tag_id = t.id GROUP BY t.id ORDER BY t.name"
            )
        ]

    def set_attribute(self, part_id: int, key: str, value: str) -> None:
        key = key.strip()
        if not key:
            raise InventoryError("attribute name must not be empty")
        self._require_part(part_id)
        with self.transaction():
            self.conn.execute(
                "INSERT INTO attributes (part_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT (part_id, key) DO UPDATE SET key = excluded.key, value = excluded.value",
                (part_id, key, value.strip()),
            )
            self._touch(part_id)

    def set_attributes(self, part_id: int, attributes: dict[str, str]) -> None:
        """Replace all of the part's attributes with `attributes`."""
        wanted = {nocase(key.strip()) for key in attributes}
        with self.transaction():
            for key in self.get_part(part_id).attributes:
                if nocase(key) not in wanted:
                    self.remove_attribute(part_id, key)
            for key, value in attributes.items():
                self.set_attribute(part_id, key, value)

    def list_categories(self) -> list[str]:
        """Return the categories in use, sorted."""
        return [
            r["category"]
            for r in self.conn.execute(
                "SELECT DISTINCT category FROM parts WHERE category <> '' ORDER BY category COLLATE NOCASE"
            )
        ]

    def remove_attribute(self, part_id: int, key: str) -> None:
        self._require_part(part_id)
        with self.transaction():
            cur = self.conn.execute(
                "DELETE FROM attributes WHERE part_id = ? AND key = ?", (part_id, key.strip())
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"part has no attribute '{key}'")
            self._touch(part_id)

    # -- locations -----------------------------------------------------------

    def add_location(self, path: str, description: str = "") -> int:
        """Create a location; missing parent locations are created as well."""
        with self.transaction():
            return self._create_location(path, description)

    def list_locations(self) -> list[Location]:
        """Return all locations in tree order (each parent before its children)."""
        rows = self.conn.execute(
            LOCATION_PATHS
            + "SELECT l.id, l.name, lp.path, l.parent_id, l.description,"
            " COUNT(s.part_id) AS part_count, COALESCE(SUM(s.quantity), 0) AS total"
            " FROM locations l JOIN location_paths lp ON lp.id = l.id"
            " LEFT JOIN stock s ON s.location_id = l.id GROUP BY l.id"
        ).fetchall()
        locations = [
            Location(r["id"], r["name"], r["path"], r["parent_id"], r["description"], r["part_count"], r["total"])
            for r in rows
        ]
        return sorted(locations, key=lambda l: _path_key(l.path))

    def location_path(self, location: str | int) -> str:
        """Return the full path of an existing location, given by reference or id."""
        location_id = location if isinstance(location, int) else self._location_id(location, create=False)
        row = self.conn.execute(
            LOCATION_PATHS + "SELECT path FROM location_paths WHERE id = ?", (location_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no location with id {location_id}")
        return row["path"]

    def update_location(
        self,
        ref: str,
        *,
        new_name: str | None = None,
        description: str | None = None,
        parent: str | None = None,
        top_level: bool = False,
    ) -> None:
        """Rename a location, change its description, or move it (with
        everything in it) into `parent` (created if needed) or to the top level."""
        if parent is not None and top_level:
            raise InventoryError("a location cannot both have a parent and be at the top level")
        if new_name is not None:
            new_name = self._check_location_name(new_name)
        try:
            with self.transaction():
                location_id = self._location_id(ref, create=False)
                if parent is not None or top_level:
                    parent_id = None if top_level else self._location_id(parent, create=True)
                    if parent_id in self._subtree_ids(location_id):
                        raise InventoryError("a location cannot be moved inside itself")
                    self.conn.execute("UPDATE locations SET parent_id = ? WHERE id = ?", (parent_id, location_id))
                if new_name is not None:
                    self.conn.execute("UPDATE locations SET name = ? WHERE id = ?", (new_name, location_id))
                if description is not None:
                    self.conn.execute(
                        "UPDATE locations SET description = ? WHERE id = ?", (description, location_id)
                    )
        except sqlite3.IntegrityError:
            raise InventoryError("there already is a location with that name in that place") from None

    def delete_location(self, ref: str) -> None:
        location_id = self._location_id(ref, create=False)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM stock WHERE location_id = ?", (location_id,)
        ).fetchone()[0]
        if count:
            raise InventoryError(f"location '{ref}' still holds {count} part(s); move them first")
        children = self.conn.execute(
            "SELECT COUNT(*) FROM locations WHERE parent_id = ?", (location_id,)
        ).fetchone()[0]
        if children:
            raise InventoryError(f"location '{ref}' still contains {children} sublocation(s)")
        with self.transaction():
            self.conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))

    # -- stock ---------------------------------------------------------------

    def set_stock(self, part_id: int, location: str, quantity: int) -> None:
        if quantity < 0:
            raise InventoryError("quantity must not be negative")
        self._require_part(part_id)
        with self.transaction():
            if quantity == 0:
                location_id = self._find_location(location)
                if location_id is None:
                    return  # nothing stored at a location that doesn't exist
            else:
                location_id = self._location_id(location, create=True)
            self._write_stock(part_id, location_id, quantity)

    def adjust_stock(self, part_id: int, location: str, delta: int) -> int:
        """Add (positive delta) or take (negative delta) parts; returns the new quantity."""
        self._require_part(part_id)
        with self.transaction():
            location_id = self._location_id(location, create=delta > 0)
            current = self._read_stock(part_id, location_id)
            new = current + delta
            if new < 0:
                raise InventoryError(f"only {current} in '{location}', cannot take {-delta}")
            self._write_stock(part_id, location_id, new)
        return new

    def move_stock(self, part_id: int, source: str, destination: str, quantity: int | None = None) -> int:
        """Move `quantity` (default: all) parts between locations; returns the amount moved."""
        self._require_part(part_id)
        with self.transaction():
            source_id = self._location_id(source, create=False)
            available = self._read_stock(part_id, source_id)
            if quantity is None:
                quantity = available
            if quantity <= 0:
                raise InventoryError(f"nothing to move from '{source}'")
            if quantity > available:
                raise InventoryError(f"only {available} in '{source}', cannot move {quantity}")
            destination_id = self._location_id(destination, create=True)
            if destination_id == source_id:
                raise InventoryError("source and destination are the same location")
            self._write_stock(part_id, source_id, available - quantity)
            self._write_stock(part_id, destination_id, self._read_stock(part_id, destination_id) + quantity)
        return quantity

    # -- attachments ---------------------------------------------------------

    def add_attachment(self, part_id: int, source: str, kind: str | None = None) -> int:
        """Attach a URL or a local file (which is copied into the attachments directory)."""
        kind = kind or guess_attachment_kind(source)
        if kind not in ATTACHMENT_KINDS:
            raise InventoryError(f"attachment kind must be one of {', '.join(ATTACHMENT_KINDS)}")
        self._require_part(part_id)

        if is_url(source):
            with self.transaction():
                cur = self.conn.execute(
                    "INSERT INTO attachments (part_id, kind, url, original_name) VALUES (?, ?, ?, ?)",
                    (part_id, kind, source, Path(urlparse(source).path).name),
                )
                self._touch(part_id)
            return cur.lastrowid

        src = Path(source).expanduser()
        if not src.is_file():
            raise InventoryError(f"no such file: {source}")
        with self.transaction():
            dest_dir = self.attachments_dir / str(part_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_destination(dest_dir, src.name)
            # Registered before copying so that a partially written copy is removed too.
            self._after_rollback.append(lambda: dest.unlink(missing_ok=True))
            shutil.copy2(src, dest)
            cur = self.conn.execute(
                "INSERT INTO attachments (part_id, kind, path, original_name) VALUES (?, ?, ?, ?)",
                (part_id, kind, dest.relative_to(self.attachments_dir).as_posix(), src.name),
            )
            self._touch(part_id)
        return cur.lastrowid

    def remove_attachment(self, attachment_id: int) -> None:
        row = self.conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no attachment with id {attachment_id}")
        with self.transaction():
            self.conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            self._touch(row["part_id"])
            if row["path"]:
                stored = self.attachments_dir / row["path"]
                self._after_commit.append(lambda: stored.unlink(missing_ok=True))

    def downloadable_attachments(self, part_id: int | None = None) -> list[Attachment]:
        """Return the link attachments (of one part, or of all parts) that point to a single file."""
        sql, params = "SELECT * FROM attachments WHERE url IS NOT NULL", []
        if part_id is not None:
            sql, params = sql + " AND part_id = ?", [part_id]
        attachments = [self._attachment_from_row(r) for r in self.conn.execute(sql + " ORDER BY part_id, id", params)]
        return [a for a in attachments if downloadable_name(a.url)]

    def download_attachment(self, attachment_id: int) -> Attachment:
        """Replace a link that points to a single file with a stored copy of
        that file. The link is kept as the attachment's `source_url`.

        Call this outside of transaction(): downloading can take a while, and
        an open transaction would block other writers meanwhile.
        """
        attachment = self.get_attachment(attachment_id)
        if attachment.url is None:
            raise InventoryError("this attachment is already a stored file")
        name = downloadable_name(attachment.url)
        if name is None:
            raise InventoryError(f"{attachment.url} does not point to a single file")
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        # Download next to the final location, so moving it there is cheap and atomic.
        with tempfile.TemporaryDirectory(dir=self.attachments_dir) as tmp:
            downloaded = Path(tmp) / name
            _download(attachment.url, downloaded, allow_local=self.allow_local_downloads)
            with self.transaction():
                dest_dir = self.attachments_dir / str(attachment.part_id)
                dest_dir.mkdir(exist_ok=True)
                dest = _unique_destination(dest_dir, name)
                self._after_rollback.append(lambda: dest.unlink(missing_ok=True))
                downloaded.replace(dest)
                updated = self.conn.execute(
                    "UPDATE attachments SET path = ?, url = NULL, source_url = ?, original_name = ?"
                    " WHERE id = ? AND url = ?",
                    (dest.relative_to(self.attachments_dir).as_posix(), attachment.url, name,
                     attachment_id, attachment.url),
                ).rowcount
                if not updated:
                    raise InventoryError("the attachment was changed or removed during the download")
                self._touch(attachment.part_id)
        return self.get_attachment(attachment_id)

    def download_links(self, attachment_ids: Iterable[int]) -> list[tuple[Attachment, InventoryError | None]]:
        """Try download_attachment() for each attachment that is a link to a
        single file; others are skipped. Returns (attachment, error) pairs: the
        stored file and None, or the unchanged link and why downloading failed."""
        results = []
        for attachment_id in attachment_ids:
            attachment = self.get_attachment(attachment_id)
            if attachment.url is None or downloadable_name(attachment.url) is None:
                continue
            try:
                results.append((self.download_attachment(attachment_id), None))
            except InventoryError as e:
                results.append((attachment, e))
        return results

    def get_attachment(self, attachment_id: int) -> Attachment:
        row = self.conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no attachment with id {attachment_id}")
        return self._attachment_from_row(row)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _check_fields(fields: dict[str, str]) -> None:
        unknown = set(fields) - set(EDITABLE_FIELDS)
        if unknown:
            raise InventoryError(f"unknown part field(s): {', '.join(sorted(unknown))}")

    @staticmethod
    def _check_part_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise InventoryError("part name must not be empty")
        if name.startswith("#"):
            raise InventoryError("part names cannot start with '#', which marks part ids")
        return name

    @staticmethod
    def _check_location_name(name: str) -> str:
        name = name.strip()
        if not name:
            raise InventoryError("location name must not be empty")
        if LOCATION_SEPARATOR in name:
            raise InventoryError(f"location names cannot contain '{LOCATION_SEPARATOR}'")
        return name

    def _require_part(self, part_id: int) -> None:
        if not self.conn.execute("SELECT 1 FROM parts WHERE id = ?", (part_id,)).fetchone():
            raise NotFoundError(f"no part with id {part_id}")

    def _touch(self, part_id: int) -> None:
        self.conn.execute("UPDATE parts SET updated_at = datetime('now') WHERE id = ?", (part_id,))

    def _find_location(self, ref: str) -> int | None:
        """Return the id of the location `ref` refers to, or None if there is none."""
        absolute, segments = _parse_location(ref)
        wanted = [nocase(s) for s in segments]
        suffix_matches = []
        for row in self.conn.execute(LOCATION_PATHS + "SELECT id, path FROM location_paths"):
            path = [nocase(s) for s in row["path"].split(LOCATION_SEPARATOR)]
            if path == wanted:
                return row["id"]
            if not absolute and path[-len(wanted):] == wanted:
                suffix_matches.append(row)
        if len(suffix_matches) > 1:
            paths = ", ".join(sorted(r["path"] for r in suffix_matches))
            raise InventoryError(f"'{ref}' could be any of: {paths}; give more of the path")
        return suffix_matches[0]["id"] if suffix_matches else None

    def _location_id(self, ref: str, *, create: bool) -> int:
        location_id = self._find_location(ref)
        if location_id is not None:
            return location_id
        if not create:
            raise NotFoundError(f"no location '{ref}'")
        return self._create_location(ref)

    def _create_location(self, ref: str, description: str = "") -> int:
        absolute, segments = _parse_location(ref)
        parent_id = None
        if len(segments) > 1:
            parent_ref = LOCATION_SEPARATOR.join(segments[:-1])
            parent_id = self._location_id(LOCATION_SEPARATOR + parent_ref if absolute else parent_ref, create=True)
        try:
            return self.conn.execute(
                "INSERT INTO locations (parent_id, name, description) VALUES (?, ?, ?)",
                (parent_id, self._check_location_name(segments[-1]), description),
            ).lastrowid
        except sqlite3.IntegrityError:
            raise InventoryError(f"location '{ref.strip()}' already exists") from None

    def _subtree_ids(self, location_id: int) -> list[int]:
        """Return the id of a location and of everything nested inside it."""
        return [
            r["id"]
            for r in self.conn.execute(
                "WITH RECURSIVE subtree (id) AS ("
                " SELECT ? UNION ALL SELECT l.id FROM locations l JOIN subtree s ON l.parent_id = s.id)"
                " SELECT id FROM subtree",
                (location_id,),
            )
        ]

    def _read_stock(self, part_id: int, location_id: int) -> int:
        row = self.conn.execute(
            "SELECT quantity FROM stock WHERE part_id = ? AND location_id = ?", (part_id, location_id)
        ).fetchone()
        return row["quantity"] if row else 0

    def _write_stock(self, part_id: int, location_id: int, quantity: int) -> None:
        if quantity == 0:
            self.conn.execute(
                "DELETE FROM stock WHERE part_id = ? AND location_id = ?", (part_id, location_id)
            )
        else:
            self.conn.execute(
                "INSERT INTO stock (part_id, location_id, quantity) VALUES (?, ?, ?)"
                " ON CONFLICT (part_id, location_id) DO UPDATE SET quantity = excluded.quantity",
                (part_id, location_id, quantity),
            )
        self._touch(part_id)

    def _attachment_from_row(self, row: sqlite3.Row) -> Attachment:
        return Attachment(
            id=row["id"],
            part_id=row["part_id"],
            kind=row["kind"],
            original_name=row["original_name"],
            added_at=row["added_at"],
            path=self.attachments_dir / row["path"] if row["path"] else None,
            url=row["url"],
            source_url=row["source_url"],
        )
