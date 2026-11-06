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

"""Read-only copies of a server's inventory or of a backup, for the desktop app.

The web interface sends a snapshot (GET /api/snapshot): a .tar.gz with
'parts.db' and the 'attachments' folder, the same layout as the backups of
contrib/systemd/partstrack-backup. The desktop app unpacks a snapshot or a
backup into a folder of its own and opens it read-only. A marker file
(COPY_INFO) next to the database records what it is a copy of and when that
was taken; a database with that marker is always opened read-only, and
unpacking into the same folder again replaces the copy.
"""

from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .labels import normalize_base_url
from .store import USER_AGENT, Inventory, InventoryError, portable_filename, whole_number

DATABASE = "parts.db"
ATTACHMENTS = "attachments"
COPY_INFO = "partstrack-copy.json"
TEMP_PREFIX = ".partstrack-"  # downloads and unpacking in progress, inside the copy's folder
SNAPSHOT_PATH = "api/snapshot"
DOWNLOAD_TIMEOUT = 60  # seconds without data
SQLITE_HEADER = b"SQLite format 3\x00"  # the first bytes of every SQLite database file


SERVER, BACKUP = "server", "backup"  # kinds of copies


@dataclass(frozen=True)
class CopyInfo:
    source: str  # the server's address, or the backup file
    taken_at: str  # when the snapshot or backup was made: UTC, 'YYYY-MM-DD HH:MM:SS'
    kind: str = SERVER

    @property
    def description(self) -> str:
        """E.g. 'copy of http://parts.lan:8080/ from 2026-11-02 14:30:00 UTC'."""
        when = self.taken_at + (" UTC" if self.taken_at[:1].isdigit() else "")
        if self.kind == BACKUP:
            return f"backup {Path(self.source).name} from {when}"
        return f"copy of {self.source} from {when}"


def write_snapshot(inv: Inventory, target: Path | BinaryIO) -> None:
    """Write the database and its attachments to `target` (a path or a binary file)
    as a .tar.gz. The database is copied with SQLite's backup API, so it is
    consistent even while others write.

    Attachment files are not locked: one deleted while the snapshot is made is
    left out (the copy then shows it as missing) rather than failing the whole
    snapshot. Locking would hold up every change for as long as writing the
    archive takes."""
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / DATABASE
        copy = sqlite3.connect(database)
        try:
            with copy:
                inv.conn.backup(copy)
            # Anyone who can reach the server may download a snapshot, so it must
            # not contain the API tokens' hashes; secure_delete overwrites them.
            # The backup copies pages as they are, including free space that
            # still holds rows deleted on the server (e.g. revoked tokens) where
            # SQLite doesn't overwrite deleted data, so VACUUM rebuilds the file
            # from the remaining rows only.
            copy.execute("PRAGMA secure_delete = ON")
            with copy:
                copy.execute("DELETE FROM api_tokens")
            copy.execute("VACUUM")
        finally:
            copy.close()
        if isinstance(target, Path):
            tar = tarfile.open(target, "w:gz")
        else:
            tar = tarfile.open(fileobj=target, mode="w:gz")
        with tar:
            tar.add(database, arcname=DATABASE)
            if inv.attachments_dir.is_dir():
                for path in sorted(inv.attachments_dir.rglob("*")):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue  # unpack_copy would refuse it
                    name = f"{ATTACHMENTS}/{path.relative_to(inv.attachments_dir).as_posix()}"
                    try:
                        tar.add(path, arcname=name, recursive=False)
                    except FileNotFoundError:
                        continue  # deleted meanwhile


def download_snapshot(base_url: str, path: Path, progress: Callable[[int], None] | None = None) -> None:
    """Download the snapshot of the server at `base_url` to `path`.
    `progress` is called with the number of bytes received so far."""
    url = normalize_base_url(base_url) + SNAPSHOT_PATH
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, open(path, "wb") as out:
            expected = response.headers.get("Content-Length")
            received = 0
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received)
            # http.client stops at a closed connection without complaint when the
            # length was announced; a chunked answer cut short raises IncompleteRead.
            if expected is not None and (size := whole_number(expected)) is not None and received != size:
                raise InventoryError(f"the download from {url} was cut off ({received} of {expected} bytes)")
    except http.client.IncompleteRead:
        raise InventoryError(f"the download from {url} was cut off") from None
    except http.client.HTTPException as e:  # not an OSError, so the handlers below miss it
        raise InventoryError(f"cannot download {url}: the server's answer is invalid ({type(e).__name__})") from None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise InventoryError(f"{url} was not found; is that a partstrack server, and is it up to date?") from None
        raise InventoryError(f"cannot download {url}: {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise InventoryError(f"cannot download {url}: {reason}") from None


def unpack_copy(archive: Path, folder: Path, *, source: str, kind: str = SERVER) -> Path:
    """Unpack a snapshot or backup into `folder` as a read-only copy of `source`
    (a server's address, or with kind=BACKUP the backup file), replacing an
    earlier copy there; returns the database's path.

    `folder` must be empty or hold an earlier copy, so no other database is ever
    overwritten. Only the database and files in 'attachments' are unpacked; the
    old copy is replaced only once the new one has been unpacked and checked.
    Attachment files whose names some systems don't allow (a server on Linux
    may have 'a:b.pdf') are renamed, and the copy's database follows them.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for leftover in folder.glob(TEMP_PREFIX + "old-*"):  # an old copy that couldn't be deleted last time
        shutil.rmtree(leftover, ignore_errors=True)
    others = [p for p in folder.iterdir() if not p.name.startswith(TEMP_PREFIX)]
    if others and not (folder / COPY_INFO).is_file():
        raise InventoryError(f"{folder} contains other files; choose an empty folder or one with an earlier copy")
    staging = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX + "new-", dir=folder))
    try:
        made_at, renamed = _extract(archive, staging)
        if made_at is None:
            raise InventoryError("the archive contains no database")
        # SQLite would treat an empty or foreign file as a new database, and the
        # migrations would turn it into an empty inventory that replaces the copy.
        with open(staging / DATABASE, "rb") as database:
            if database.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise InventoryError("the database in the archive is empty or not a database")
        try:
            copy = Inventory(staging / DATABASE)  # upgrades an older schema; refuses a newer one
        except sqlite3.DatabaseError as e:
            raise InventoryError(f"the database in the archive is damaged: {e}") from None
        except InventoryError:
            raise InventoryError(
                "the database was made by a newer version of partstrack; update this app to open it"
            ) from None
        try:
            copy.move_stored_attachments(renamed)
        finally:
            copy.close()
        taken_at = made_at.strftime("%Y-%m-%d %H:%M:%S")
        info = {"source": source, "taken_at": taken_at, "kind": kind}
        (staging / COPY_INFO).write_text(json.dumps(info, indent=2) + "\n")

        _swap_in(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return folder / DATABASE


# What a copy consists of. Attachments come first: on Windows, a file that a
# viewer has open makes moving its folder fail, and then nothing has changed yet.
COPY_FILES = (ATTACHMENTS, DATABASE, DATABASE + "-journal", COPY_INFO)


def _swap_in(staging: Path, folder: Path) -> None:
    """Replace the copy in `folder` with the files in `staging`, so that either the
    new copy is complete or the old one is back as it was. The old files are only
    moved aside (renames in the same folder) and deleted once the new ones are in."""
    retired = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX + "old-", dir=folder))
    moved_aside, installed = [], []
    try:
        for name in COPY_FILES:
            if (folder / name).exists():
                (folder / name).rename(retired / name)
                moved_aside.append(name)
        for item in staging.iterdir():
            item.rename(folder / item.name)
            installed.append(item.name)
    except OSError as e:
        try:
            for name in installed:
                (folder / name).rename(staging / name)
            for name in moved_aside:
                (retired / name).rename(folder / name)
        except OSError:
            raise InventoryError(
                f"the copy could not be replaced ({e}), and the old one could not be completely put back "
                f"either; the rest of its files are in {retired}"
            ) from None
        shutil.rmtree(retired, ignore_errors=True)
        raise InventoryError(f"the copy could not be replaced, so the old one was kept: {e}") from None
    shutil.rmtree(retired, ignore_errors=True)  # a file still in use stays behind and is removed next time


def _extract(archive: Path, destination: Path) -> tuple[datetime | None, dict[str, str]]:
    """Unpack only 'parts.db' and regular files under 'attachments/'. Anything else
    (absolute paths, '..', links, devices) means the archive isn't one of ours.
    Names in 'attachments' are made portable (see _portable_path).

    Returns the time the database was written (when the snapshot or backup was
    made), or None if the archive has no database, and the renamed attachment
    files as {old path: new path}, relative to 'attachments' like the database's."""
    made_at = None
    portable: dict[tuple[str, ...], tuple[str, ...]] = {}
    taken: dict[tuple[str, ...], set[str]] = {}
    renamed = {}
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                parts = PurePosixPath(member.name).parts
                allowed = (
                    parts
                    and not member.name.startswith("/")
                    and ".." not in parts
                    and (parts == (DATABASE,) or parts[0] == ATTACHMENTS)
                )
                if not allowed or not (member.isfile() or member.isdir()) or (member.isdir() and parts == (DATABASE,)):
                    raise InventoryError(f"the archive contains something unexpected: {member.name}")
                if parts[0] == ATTACHMENTS:
                    new_parts = _portable_path(parts, portable, taken)
                    if member.isfile() and new_parts != parts:
                        renamed["/".join(parts[1:])] = "/".join(new_parts[1:])
                    parts = new_parts
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as data, open(target, "wb") as out:
                    shutil.copyfileobj(data, out)
                if parts == (DATABASE,):
                    made_at = datetime.fromtimestamp(member.mtime, timezone.utc)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise InventoryError(f"the file is not a valid snapshot or backup: {e}") from None
    return made_at, renamed


def _portable_path(
    parts: tuple[str, ...], portable: dict[tuple[str, ...], tuple[str, ...]], taken: dict[tuple[str, ...], set[str]]
) -> tuple[str, ...]:
    """The path to unpack `parts` to: each name made portable and unique in its
    folder, ignoring case as Windows does ('a:b.pdf' and 'A_b.pdf' become
    'a_b.pdf' and 'A_b-1.pdf'). `portable` remembers what each path of the
    archive became, `taken` the (casefolded) names used in each folder."""
    for i in range(1, len(parts) + 1):
        if parts[:i] in portable:
            continue
        folder = portable[parts[: i - 1]] if i > 1 else ()
        used = taken.setdefault(folder, set())
        name = candidate = portable_filename(parts[i - 1]) if i > 1 else parts[0]
        n = 1
        while candidate.casefold() in used:
            candidate = f"{PurePosixPath(name).stem}-{n}{PurePosixPath(name).suffix}"
            n += 1
        used.add(candidate.casefold())
        portable[parts[:i]] = folder + (candidate,)
    return portable[parts]


def open_inventory(db_path: str | Path) -> Inventory:
    """Open a database for any front end: copies (see read_copy_info) open read-only,
    so nothing can be changed in them that the next download would throw away."""
    return Inventory(db_path, read_only=read_copy_info(Path(db_path).expanduser()) is not None)


def read_copy_info(db_path: Path) -> CopyInfo | None:
    """Where and when the database at `db_path` was copied from, or None if it
    isn't a copy. A damaged marker still counts as a copy, so it stays read-only."""
    marker = Path(db_path).parent / COPY_INFO
    if Path(db_path).name != DATABASE or not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text())
        return CopyInfo(str(data["source"]), str(data["taken_at"]), str(data.get("kind", SERVER)))
    except (OSError, ValueError, KeyError, TypeError):
        return CopyInfo("an unknown server", "an unknown time")
