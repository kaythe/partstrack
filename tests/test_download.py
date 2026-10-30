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
import tempfile
import unittest
import urllib.request
from pathlib import Path

from partstrack.csvio import import_csv
from partstrack.store import Inventory, InventoryError, _CheckedRedirectHandler, downloadable_name

from .fileserver import serve

PDF = b"%PDF-1.4 test"


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inv = Inventory(Path(self.tmp.name) / "parts.db")
        self.part_id = self.inv.add_part("NE555")

    def tearDown(self):
        self.inv.close()
        self.tmp.cleanup()

    def test_downloadable_name(self):
        self.assertEqual(downloadable_name("https://x.com/ds/ne555.pdf"), "ne555.pdf")
        self.assertEqual(downloadable_name("https://x.com/a/pin%20out.png?v=17"), "pin out.png")
        self.assertIsNone(downloadable_name("https://x.com/product/6311"))
        self.assertIsNone(downloadable_name("https://x.com/shop/joystick/"))
        self.assertIsNone(downloadable_name("https://x.com/index.html"))
        self.assertIsNone(downloadable_name("https://x.com/.hidden"))
        self.assertIsNone(downloadable_name("ftp://x.com/ne555.pdf"))

    def test_download_replaces_link_with_file(self):
        with serve({"/ds/ne555.pdf": (200, "application/pdf", PDF)}) as base:
            url = f"{base}/ds/ne555.pdf"
            attachment_id = self.inv.add_attachment(self.part_id, url)
            stored = self.inv.download_attachment(attachment_id)
        self.assertEqual((stored.id, stored.kind, stored.url, stored.source_url), (attachment_id, "datasheet", None, url))
        self.assertEqual((stored.original_name, stored.path.read_bytes()), ("ne555.pdf", PDF))
        self.assertEqual([p.name for p in self.inv.attachments_dir.iterdir()], [str(self.part_id)])  # no temp files

    def test_failed_downloads_keep_the_link(self):
        routes = {
            "/page.pdf": (200, "text/html", b"<html>login</html>"),
            "/moved.pdf": (302, "text/plain", b"/ds.pdf"),
            "/ds.pdf": (200, "application/pdf", PDF),
        }
        with serve(routes) as base:
            ids = [self.inv.add_attachment(self.part_id, f"{base}{path}")
                   for path in ("/page.pdf", "/missing.pdf", "/moved.pdf")]
            page_id = self.inv.add_attachment(self.part_id, f"{base}/shop/item", "image")
            results = self.inv.download_links(ids + [page_id])
        errors = [str(error) for _, error in results]
        self.assertEqual(len(results), 3)  # the page link is not even tried
        self.assertIn("web page, not a file", errors[0])
        self.assertIn("HTTP 404", errors[1])
        self.assertIsNone(results[2][1])  # redirects are followed
        self.assertEqual(results[2][0].path.read_bytes(), PDF)
        links = [a for a in self.inv.get_part(self.part_id).attachments if a.url]
        self.assertEqual(len(links), 3)
        self.assertEqual(len(self.inv.downloadable_attachments()), 2)

    def test_local_addresses_are_refused(self):
        with serve({"/ds.pdf": (200, "application/pdf", PDF)}) as base:
            attachment_id = self.inv.add_attachment(self.part_id, f"{base}/ds.pdf")
        with self.assertRaisesRegex(InventoryError, "local or reserved address"):
            self.inv.download_attachment(attachment_id)
        attachment_id = self.inv.add_attachment(self.part_id, "http://169.254.169.254/latest/meta.json")
        with self.assertRaisesRegex(InventoryError, "local or reserved address"):
            self.inv.download_attachment(attachment_id)

    def test_redirect_to_local_address_is_refused(self):
        handler = _CheckedRedirectHandler(allow_local=False)
        request = urllib.request.Request("https://example.com/ds.pdf")
        with self.assertRaisesRegex(InventoryError, "local or reserved address"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1:8080/secret.pdf")

    def test_csv_import_does_not_duplicate_downloaded_files(self):
        with serve({"/ne555.pdf": (200, "application/pdf", PDF)}) as base:
            csv_text = f"name,datasheets\nNE555,{base}/ne555.pdf\n"
            result = import_csv(self.inv, io.StringIO(csv_text))
            self.inv.download_links(result.link_ids)
            result = import_csv(self.inv, io.StringIO(csv_text))
        self.assertEqual(result.link_ids, [])
        self.assertEqual(len(self.inv.get_part(self.part_id).attachments), 1)


if __name__ == "__main__":
    unittest.main()
