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

"""Read-only copies of a server's inventory, for the desktop app.

The web interface sends a snapshot (GET /api/snapshot): a .tar.gz with
'parts.db' and the 'attachments' folder, the same layout as the backups of
contrib/systemd/partstrack-backup. The desktop app downloads it into a folder
of its own and opens it read-only. A marker file (COPY_INFO) next to the
database records where and when the copy was taken; a database with that
marker is always opened read-only, and a later download into the same folder
replaces the copy.
"""

from __future__ import annotations

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
from .store import USER_AGENT, Inventory, InventoryError

DATABASE = "parts.db"
ATTACHMENTS = "attachments"
COPY_INFO = "partstrack-copy.json"
TEMP_PREFIX = ".partstrack-"  # downloads and unpacking in progress, inside the copy's folder
SNAPSHOT_PATH = "api/snapshot"
DOWNLOAD_TIMEOUT = 60  # seconds without data


@dataclass(frozen=True)
class CopyInfo:
    source: str  # the server's address
    taken_at: str  # UTC, 'YYYY-MM-DD HH:MM:SS'


def write_snapshot(inv: Inventory, target: Path | BinaryIO) -> None:
    """Write the database and its attachments to `target` (a path or a binary file)
    as a .tar.gz. The database is copied with SQLite's backup API, so it is
    consistent even while others write."""
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / DATABASE
        copy = sqlite3.connect(database)
        try:
            with copy:
                inv.conn.backup(copy)
        finally:
            copy.close()
        if isinstance(target, Path):
            tar = tarfile.open(target, "w:gz")
        else:
            tar = tarfile.open(fileobj=target, mode="w:gz")
        with tar:
            tar.add(database, arcname=DATABASE)
            if inv.attachments_dir.is_dir():
                tar.add(inv.attachments_dir, arcname=ATTACHMENTS)


def download_snapshot(base_url: str, path: Path, progress: Callable[[int], None] | None = None) -> None:
    """Download the snapshot of the server at `base_url` to `path`.
    `progress` is called with the number of bytes received so far."""
    url = normalize_base_url(base_url) + SNAPSHOT_PATH
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, open(path, "wb") as out:
            received = 0
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise InventoryError(f"{url} was not found; is that a partstrack server, and is it up to date?") from None
        raise InventoryError(f"cannot download {url}: {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise InventoryError(f"cannot download {url}: {reason}") from None


def unpack_copy(archive: Path, folder: Path, *, source: str) -> Path:
    """Unpack a snapshot into `folder` as a read-only copy of `source`, replacing
    an earlier copy there; returns the database's path.

    `folder` must be empty or hold an earlier copy, so no other database is ever
    overwritten. Only the database and files in 'attachments' are unpacked; the
    old copy is replaced only once the new one has been unpacked and checked.
    """
    folder.mkdir(parents=True, exist_ok=True)
    others = [p for p in folder.iterdir() if not p.name.startswith(TEMP_PREFIX)]
    if others and not (folder / COPY_INFO).is_file():
        raise InventoryError(f"{folder} contains other files; choose an empty folder or one with an earlier copy")
    staging = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX + "new-", dir=folder))
    try:
        _extract(archive, staging)
        if not (staging / DATABASE).is_file():
            raise InventoryError("the download contains no database")
        try:
            Inventory(staging / DATABASE).close()  # upgrades an older schema; refuses a newer one
        except sqlite3.DatabaseError as e:
            raise InventoryError(f"the downloaded database is damaged: {e}") from None
        except InventoryError:
            raise InventoryError(
                "the server runs a newer version of partstrack; update this app to open its inventory"
            ) from None
        taken_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        (staging / COPY_INFO).write_text(json.dumps({"source": source, "taken_at": taken_at}, indent=2) + "\n")

        for name in (DATABASE, DATABASE + "-journal", ATTACHMENTS, COPY_INFO):
            old = folder / name
            if old.is_dir():
                shutil.rmtree(old)
            elif old.exists():
                old.unlink()
        for item in staging.iterdir():
            item.rename(folder / item.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return folder / DATABASE


def _extract(archive: Path, destination: Path) -> None:
    """Unpack only 'parts.db' and regular files under 'attachments/'. Anything else
    (absolute paths, '..', links, devices) means the archive isn't one of ours."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                parts = PurePosixPath(member.name).parts
                allowed = (
                    parts
                    and not member.name.startswith("/")
                    and ".." not in parts
                    and not any("\\" in p or ":" in p for p in parts)
                    and (parts == (DATABASE,) or parts[0] == ATTACHMENTS)
                )
                if not allowed or not (member.isfile() or member.isdir()) or (member.isdir() and parts == (DATABASE,)):
                    raise InventoryError(f"the download contains something unexpected: {member.name}")
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as data, open(target, "wb") as out:
                    shutil.copyfileobj(data, out)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise InventoryError(f"the download is not a valid snapshot: {e}") from None


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
        return CopyInfo(str(data["source"]), str(data["taken_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return CopyInfo("an unknown server", "an unknown time")
