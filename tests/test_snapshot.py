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

import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from partstrack.snapshot import (
    BACKUP,
    COPY_INFO,
    CopyInfo,
    download_snapshot,
    read_copy_info,
    unpack_copy,
    write_snapshot,
)
from partstrack.store import MIGRATIONS, Inventory, InventoryError

from .fileserver import serve


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.server = Inventory(self.dir / "server" / "parts.db")
        self.addCleanup(self.server.close)
        (self.dir / "ds.pdf").write_bytes(b"%PDF datasheet")
        self.part_id = self.server.add_part("NE555")
        self.server.set_stock(self.part_id, "Cabinet/Drawer 1", 5)
        self.server.add_attachment(self.part_id, str(self.dir / "ds.pdf"))

    def snapshot(self) -> Path:
        path = self.dir / "snapshot.tar.gz"
        write_snapshot(self.server, path)
        return path

    def test_copy_round_trip_is_read_only(self):
        folder = self.dir / "copy"
        db_path = unpack_copy(self.snapshot(), folder, source="http://parts.lan:8080/")
        info = read_copy_info(db_path)
        self.assertEqual(info.source, "http://parts.lan:8080/")
        self.assertRegex(info.taken_at, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        with Inventory(db_path, read_only=True) as copy:
            part = copy.get_part(self.part_id)
            self.assertEqual(part.total_quantity, 5)
            self.assertEqual(part.attachments[0].path.read_bytes(), b"%PDF datasheet")
            self.assertEqual(len(copy.stock_history()), 1)
            for change in [lambda: copy.add_part("LM358"), lambda: copy.set_stock(self.part_id, "Box", 1),
                           lambda: copy.delete_part(self.part_id), lambda: copy.add_location("Shelf")]:
                with self.assertRaisesRegex(InventoryError, "read-only copy"):
                    change()
            with self.assertRaises(sqlite3.OperationalError):  # SQLite refuses too
                copy.conn.execute("DELETE FROM parts")
        self.assertIsNone(read_copy_info(self.server.db_path))

    def test_later_download_replaces_the_copy(self):
        folder = self.dir / "copy"
        unpack_copy(self.snapshot(), folder, source="http://a/")
        self.server.add_part("LM358")
        self.server.remove_attachment(self.server.get_part(self.part_id).attachments[0].id)
        db_path = unpack_copy(self.snapshot(), folder, source="http://b/")
        with Inventory(db_path, read_only=True) as copy:
            self.assertEqual([p.name for p in copy.list_parts()], ["LM358", "NE555"])
        self.assertEqual(list((folder / "attachments").rglob("*.pdf")), [])
        self.assertEqual(read_copy_info(db_path).source, "http://b/")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(["attachments", COPY_INFO, "parts.db"]))

    def test_never_overwrites_other_files(self):
        folder = self.dir / "mine"
        folder.mkdir()
        (folder / "parts.db").write_bytes(b"my own database")
        with self.assertRaisesRegex(InventoryError, "contains other files"):
            unpack_copy(self.snapshot(), folder, source="http://a/")
        self.assertEqual((folder / "parts.db").read_bytes(), b"my own database")

    def test_unexpected_archive_contents_are_refused(self):
        def archive(*members):
            path = self.dir / "bad.tar.gz"
            with tarfile.open(path, "w:gz") as tar:
                for name, kind in members:
                    info = tarfile.TarInfo(name)
                    if kind == "link":
                        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                        tar.addfile(info)
                    else:
                        info.size = 4
                        tar.addfile(info, io.BytesIO(b"data"))
            return path

        for members in [[("../evil", "file")], [("/etc/evil", "file")], [("attachments/../../evil", "file")],
                        [("parts.db", "file"), ("attachments/1/x", "link")], [("other.txt", "file")]]:
            with self.subTest(members=members), self.assertRaisesRegex(InventoryError, "something unexpected"):
                unpack_copy(archive(*members), self.dir / "copy", source="http://a/")
        with self.assertRaisesRegex(InventoryError, "no database"):
            unpack_copy(archive(("attachments/1/x.pdf", "file")), self.dir / "copy", source="http://a/")
        (self.dir / "junk.tar.gz").write_bytes(b"not a tar file")
        with self.assertRaisesRegex(InventoryError, "not a valid snapshot"):
            unpack_copy(self.dir / "junk.tar.gz", self.dir / "copy", source="http://a/")
        self.assertFalse((self.dir / "evil").exists())
        self.assertEqual([p.name for p in (self.dir / "copy").iterdir()], [])  # nothing left behind

    def test_copy_from_a_newer_server_is_refused(self):
        self.server.conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        with self.assertRaisesRegex(InventoryError, "newer version"):
            unpack_copy(self.snapshot(), self.dir / "copy", source="http://a/")

    def test_older_copy_is_upgraded_when_opened(self):
        db_path = unpack_copy(self.snapshot(), self.dir / "copy", source="http://a/")
        with sqlite3.connect(db_path) as conn:  # as if an older partstrack had made it
            conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) - 1}")
            conn.execute("DROP TABLE stock_log")
            conn.execute("ALTER TABLE parts DROP COLUMN min_quantity")
        with Inventory(db_path, read_only=True) as copy:
            self.assertEqual(copy.get_part(self.part_id).min_quantity, None)

    def test_damaged_marker_still_means_read_only(self):
        db_path = unpack_copy(self.snapshot(), self.dir / "copy", source="http://a/")
        (db_path.parent / COPY_INFO).write_text("{broken")
        self.assertEqual(read_copy_info(db_path).source, "an unknown server")

    def test_backup_from_the_backup_script(self):
        # Like contrib/systemd/partstrack-backup: tar -czf ... -C work parts.db -C data attachments
        database = self.dir / "work" / "parts.db"
        database.parent.mkdir()
        copy = sqlite3.connect(database)
        with copy:
            self.server.conn.backup(copy)
        copy.close()
        backup = self.dir / "partstrack-2026-11-01_030000.tar.gz"
        with tarfile.open(backup, "w:gz") as tar:
            info = tar.gettarinfo(database, arcname="parts.db")
            info.mtime = 1_793_502_000  # 2026-11-01 03:00:00 UTC
            with open(database, "rb") as data:
                tar.addfile(info, data)
            tar.add(self.server.attachments_dir, arcname="attachments")
        db_path = unpack_copy(backup, self.dir / "opened", source=str(backup), kind=BACKUP)
        info = read_copy_info(db_path)
        self.assertEqual((info.kind, info.taken_at), (BACKUP, "2026-11-01 03:00:00"))
        self.assertEqual(info.description, "backup partstrack-2026-11-01_030000.tar.gz from 2026-11-01 03:00:00 UTC")
        with Inventory(db_path, read_only=True) as copy:
            self.assertEqual(copy.get_part(self.part_id).attachments[0].path.read_bytes(), b"%PDF datasheet")

    def test_marker_without_kind_is_a_server_copy(self):
        db_path = unpack_copy(self.snapshot(), self.dir / "copy", source="http://a/")
        (db_path.parent / COPY_INFO).write_text(json.dumps({"source": "http://a/", "taken_at": "2026-11-02 08:00:00"}))
        self.assertEqual(read_copy_info(db_path).description, "copy of http://a/ from 2026-11-02 08:00:00 UTC")
        self.assertEqual(CopyInfo("x", "an unknown time").description, "copy of x from an unknown time")

    def test_download(self):
        data = self.snapshot().read_bytes()
        with serve({"/api/snapshot": (200, "application/gzip", data)}) as url:
            seen = []
            download_snapshot(url, self.dir / "down.tar.gz", seen.append)
            self.assertEqual((self.dir / "down.tar.gz").read_bytes(), data)
            self.assertEqual(seen[-1], len(data))
        with serve({}) as url, self.assertRaisesRegex(InventoryError, "partstrack server"):
            download_snapshot(url, self.dir / "down.tar.gz")
        with self.assertRaisesRegex(InventoryError, "cannot download"):
            download_snapshot("http://127.0.0.1:9/", self.dir / "down.tar.gz")


if __name__ == "__main__":
    unittest.main()
