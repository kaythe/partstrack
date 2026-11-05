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
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from partstrack import __version__
from partstrack.store import Inventory

try:
    from partstrack.web.app import create_app
except ImportError:  # the optional web dependencies are not installed
    create_app = None


@unittest.skipIf(create_app is None, "needs the 'web' extra (Flask)")
class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "parts.db"
        (Path(self.tmp.name) / "ds.pdf").write_bytes(b"%PDF")
        with Inventory(db) as inv:
            self.ne555 = inv.add_part("NE555", package="DIP-8", category="IC")
            inv.add_tags(self.ne555, ["timer"])
            inv.set_attribute(self.ne555, "vcc", "4.5-16V")
            inv.set_min_quantity(self.ne555, 10)
            inv.set_stock(self.ne555, "Cabinet/Drawer 1", 5, note="counted")
            inv.add_attachment(self.ne555, str(Path(self.tmp.name) / "ds.pdf"))
            inv.add_attachment(self.ne555, "https://example.com/ne555", "other")
            self.r1 = inv.add_part("R1", category="Resistor")
            inv.set_attribute(self.r1, "resistance", "10k")
            inv.set_stock(self.r1, "Bench", 100)
        self.client = create_app(db).test_client()

    def get(self, url, status=200):
        response = self.client.get(url)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertEqual(response.content_type, "application/json")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        return response.get_json()

    def test_index(self):
        data = self.get("/api")
        self.assertEqual(data["version"], __version__)
        self.assertIn("/api/parts", data["endpoints"])

    def test_parts_and_filters(self):
        self.assertEqual([p["name"] for p in self.get("/api/parts")], ["NE555", "R1"])
        self.assertEqual([p["name"] for p in self.get("/api/parts?q=resistance>=4k7")], ["R1"])
        self.assertEqual([p["name"] for p in self.get("/api/parts?low=1")], ["NE555"])
        self.assertEqual([p["name"] for p in self.get("/api/parts?location=Cabinet&tag=timer")], ["NE555"])
        self.assertEqual([p["name"] for p in self.get("/api/parts?category=resistor")], ["R1"])
        self.assertIn("not a number", self.get("/api/parts?q=resistance<lots", 400)["error"])

    def test_part(self):
        part = self.get(f"/api/parts/{self.ne555}")
        self.assertEqual(
            {k: part[k] for k in ("name", "package", "min_quantity", "total_quantity", "low", "tags", "attributes")},
            {"name": "NE555", "package": "DIP-8", "min_quantity": 10, "total_quantity": 5, "low": True,
             "tags": ["timer"], "attributes": {"vcc": "4.5-16V"}},
        )
        self.assertEqual(part["stock"], [{"location": "Cabinet/Drawer 1", "quantity": 5}])
        self.assertEqual(part["web_url"], f"http://localhost/parts/{self.ne555}")
        stored, link = part["attachments"]
        self.assertEqual((stored["kind"], stored["name"], stored["stored"]), ("datasheet", "ds.pdf", True))
        self.assertEqual(stored["url"], f"http://localhost/attachments/{stored['id']}")
        self.assertNotIn(self.tmp.name, str(part))  # no file paths on the server
        self.assertEqual((link["stored"], link["url"]), (False, "https://example.com/ne555"))
        self.assertEqual(self.get("/api/parts/999", 404), {"error": "no part with id 999"})

    def test_locations_tags_categories_history(self):
        locations = self.get("/api/locations")
        self.assertEqual([(l["path"], l["parent"]) for l in locations],
                         [("Bench", None), ("Cabinet", None), ("Cabinet/Drawer 1", "Cabinet")])
        self.assertEqual(locations[2]["parts_url"], "http://localhost/api/parts?location=/Cabinet/Drawer+1")
        self.assertEqual(self.get("/api/tags"), [{"name": "timer", "parts": 1}])
        self.assertEqual(self.get("/api/categories"), ["IC", "Resistor"])
        history = self.get("/api/history")
        self.assertEqual([(c["part_name"], c["delta"]) for c in history], [("R1", 100), ("NE555", 5)])
        self.assertEqual(self.get(f"/api/history?part={self.ne555}")[0]["note"], "counted")
        self.assertEqual(len(self.get("/api/history?limit=1")), 1)
        self.assertEqual([c["location"] for c in self.get("/api/history?location=/Bench&limit=0")], ["Bench"])
        self.assertIn("whole number", self.get("/api/history?limit=x", 400)["error"])
        self.assertIn("whole number", self.get("/api/history?limit=%C2%B2", 400)["error"])

    def test_snapshot(self):
        temp = Path(self.tmp.name) / "temp"
        temp.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(temp)):  # where the server builds the archive
            response = self.client.get("/api/snapshot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/gzip")
        self.assertRegex(response.headers["Content-Disposition"], r"partstrack-\d{4}-\d\d-\d\d_\d{6}\.tar\.gz")
        with tarfile.open(fileobj=io.BytesIO(response.get_data()), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertIn("parts.db", names)
        self.assertTrue(any(n.startswith("attachments/") and n.endswith("ds.pdf") for n in names))
        response.close()
        self.assertEqual(list(temp.iterdir()), [])  # nothing left behind

    def test_errors_are_json_and_writes_are_refused(self):
        self.assertIn("no such API endpoint", self.get("/api/nope", 404)["error"])
        response = self.client.post("/api/parts", data={"name": "X"})
        self.assertEqual((response.status_code, response.get_json()["error"]),
                         (405, "the API is read-only; only GET is allowed"))
        response = self.client.delete(f"/api/parts/{self.ne555}")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.client.get("/nope").content_type, "text/html; charset=utf-8")  # not under /api

    def test_numbers_beyond_the_database_range(self):
        huge = "9" * 30
        self.get(f"/api/parts/{huge}", 404)
        self.get(f"/api/history?part={huge}", 404)
        self.assertEqual(len(self.get(f"/api/history?limit={huge}", 200)), 2)


if __name__ == "__main__":
    unittest.main()
