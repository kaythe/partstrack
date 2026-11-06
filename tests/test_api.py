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
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from partstrack import __version__
from partstrack.store import Inventory

from .fileserver import serve

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

    def test_errors_are_json(self):
        self.assertIn("no such API endpoint", self.get("/api/nope", 404)["error"])
        response = self.client.put(f"/api/parts/{self.ne555}", json={})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.get_json()["error"], "PUT is not allowed here; allowed: DELETE, GET, HEAD, OPTIONS, PATCH")
        self.assertEqual(response.headers["Allow"], "DELETE, GET, HEAD, OPTIONS, PATCH")
        self.assertEqual(self.client.get("/nope").content_type, "text/html; charset=utf-8")  # not under /api

    def test_unexpected_errors_are_json(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(Inventory, "list_tags", side_effect=locked), self.assertLogs("partstrack.web.app"):
            body = self.get("/api/tags", 500)
        self.assertNotIn("locked", body["error"])  # no internals; the log has the details
        with mock.patch.object(Inventory, "list_tags", side_effect=locked), self.assertLogs("partstrack.web.app"):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")  # pages stay HTML

    def test_numbers_beyond_the_database_range(self):
        huge = "9" * 30
        self.get(f"/api/parts/{huge}", 404)
        self.get(f"/api/locations/{huge}", 404)
        self.get(f"/api/history?part={huge}", 404)
        self.assertEqual(len(self.get(f"/api/history?limit={huge}", 200)), 2)



@unittest.skipIf(create_app is None, "needs the 'web' extra (Flask)")
class ApiChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "parts.db"
        with Inventory(self.db) as inv:
            self.token = inv.create_token("Phone")
            self.ne555 = inv.add_part("NE555", package="DIP-8")
            inv.set_stock(self.ne555, "Cabinet/Drawer 1", 5)
            inv.set_stock(self.ne555, "Bench/Drawer 1", 2)  # 'Drawer 1' alone is ambiguous
        self.client = create_app(self.db).test_client()

    def send(self, method, url, body=None, status=200, token=True, **headers):
        if token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.client.open(url, method=method, json=body, headers=headers)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        if status == 204:
            self.assertEqual(response.get_data(), b"")
            return None
        self.assertEqual(response.content_type, "application/json")
        return response.get_json()

    def inventory(self):
        inv = Inventory(self.db)
        self.addCleanup(inv.close)
        return inv

    def stock(self, part):
        return {s["location"]: s["quantity"] for s in part["stock"]}

    def test_changes_need_a_valid_token(self):
        with Inventory(self.db) as inv:
            revoked = inv.create_token("Old phone")
            inv.revoke_token("Old phone")
        for headers in [{}, {"Authorization": "Bearer "}, {"Authorization": f"Basic {self.token}"},
                        {"Authorization": "Bearer pt_wrong"}, {"Authorization": f"Bearer {revoked}"}]:
            with self.subTest(headers=headers):
                response = self.client.post("/api/parts", json={"name": "LM358"}, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertIn("error", response.get_json())
                self.assertTrue(response.headers["WWW-Authenticate"].startswith('Bearer realm="partstrack"'))
                for method, url in [("PATCH", f"/api/parts/{self.ne555}"), ("DELETE", f"/api/parts/{self.ne555}"),
                                    ("POST", "/api/locations")]:
                    self.assertEqual(self.client.open(url, method=method, json={}, headers=headers).status_code, 401)
        self.assertEqual([p.name for p in self.inventory().list_parts()], ["NE555"])  # nothing changed
        self.assertEqual(len(self.inventory().list_locations()), 4)

    def test_token_requests_need_no_same_site_headers(self):
        # A phone app sends none of them; a browser page here would send same-origin.
        # Either way the token decides, and a cross-site page couldn't add it.
        self.send("POST", f"/api/parts/{self.ne555}/stock", {"location": "Cabinet/Drawer 1", "delta": 1},
                  **{"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
        self.assertIsNotNone(self.inventory().list_tokens()[0].last_used_at)
        # The pages' forms are still protected.
        response = self.client.post("/parts/new", data={"name": "X"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_unknown_urls_and_methods_are_json_from_any_site(self):
        # No route matched, so nothing can change: the API's JSON 404 and 405
        # come before the pages' cross-site check.
        cross_site = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}
        self.assertIn("no such API endpoint", self.send("POST", "/api/nope", {}, 404, **cross_site)["error"])
        self.assertIn("not allowed", self.send("PUT", f"/api/parts/{self.ne555}", {}, 405, **cross_site)["error"])
        self.assertIn("not allowed", self.send("POST", "/api/tags", {}, 405, token=False, **cross_site)["error"])

    def test_add_part(self):
        response = self.client.post("/api/parts", headers={"Authorization": f"Bearer {self.token}"}, json={
            "name": " LM358 ", "description": "dual op-amp", "package": "SOIC-8", "tags": ["opamp", "dual"],
            "attributes": {"channels": "2"}, "min_quantity": 5,
            "stock": [{"location": "Cabinet/Drawer 2", "quantity": 10}, {"location": "/Shelf", "quantity": 3}],
        })
        self.assertEqual(response.status_code, 201)
        part = response.get_json()
        self.assertEqual(response.headers["Location"], part["api_url"])
        self.assertEqual((part["name"], part["description"], part["package"], part["min_quantity"]),
                         ("LM358", "dual op-amp", "SOIC-8", 5))
        self.assertEqual(sorted(part["tags"]), ["dual", "opamp"])
        self.assertEqual(part["attributes"], {"channels": "2"})
        self.assertEqual(self.stock(part), {"Cabinet/Drawer 2": 10, "Shelf": 3})
        history = self.inventory().stock_history(part["id"])
        self.assertEqual({c.note for c in history}, {"new part"})
        minimal = self.send("POST", "/api/parts", {"name": "R1"}, 201)
        self.assertEqual((minimal["stock"], minimal["tags"], minimal["min_quantity"]), ([], [], None))

    def test_add_part_saves_all_or_nothing(self):
        error = self.send("POST", "/api/parts", {
            "name": "LM358", "tags": ["opamp"],
            "stock": [{"location": "Shelf", "quantity": 3}, {"location": "Bin", "quantity": -1}],
        }, 400)["error"]
        self.assertIn("negative", error)
        inv = self.inventory()
        self.assertIsNone(inv.part_id_by_name("LM358"))
        self.assertNotIn("Shelf", [l.path for l in inv.list_locations()])
        self.assertEqual(inv.list_tags(), [])
        self.assertIn("already exists", self.send("POST", "/api/parts", {"name": "ne555"}, 400)["error"])

    def test_bad_bodies(self):
        url = "/api/parts"
        for body, message in [
            (None, "must be a JSON object"),
            ([], "must be a JSON object"),
            ({}, "missing field(s): name"),
            ({"name": "X", "colour": "red", "Name": "Y"}, "unknown field(s): Name, colour"),
            ({"name": 5}, "'name' must be a string"),
            ({"name": "X", "tags": "a,b"}, "'tags' must be a list of strings"),
            ({"name": "X", "tags": [1]}, "'tags' must be a list of strings"),
            ({"name": "X", "attributes": {"a": 1}}, "'attributes' must be an object whose values are strings"),
            ({"name": "X", "min_quantity": "5"}, "'min_quantity' must be a whole number or null"),
            ({"name": "X", "min_quantity": 2.5}, "'min_quantity' must be a whole number or null"),
            ({"name": "X", "stock": {"Shelf": 1}}, "'stock' must be a list of objects"),
            ({"name": "X", "stock": [{"location": "Shelf"}]}, "missing field(s) in 'stock': quantity"),
            ({"name": "X", "stock": [{"location": "Shelf", "quantity": 1, "x": 0}]}, "unknown field(s) in 'stock': x"),
            ({"name": "X", "stock": [{"location": "Shelf", "quantity": True}]}, "'stock.quantity' must be a whole number"),
            ({"name": "X", "stock": [{"location": "", "quantity": 1}]}, "location must not be empty"),
        ]:
            with self.subTest(body=body):
                self.assertIn(message, self.send("POST", url, body, 400)["error"])
        response = self.client.post(url, data='{"name": "X"}', headers={"Authorization": f"Bearer {self.token}"})
        self.assertIn("Content-Type: application/json", response.get_json()["error"])  # not sent as JSON
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.inventory().part_id_by_name("X"))

    def test_update_part(self):
        url = f"/api/parts/{self.ne555}"
        self.send("PATCH", url, {"tags": ["timer", "ic"], "attributes": {"vcc": "5V", "pins": "8"}, "min_quantity": 3})
        part = self.send("PATCH", url, {"description": "timer", "tags": ["timer"], "attributes": {"vcc": "15V"}})
        self.assertEqual((part["name"], part["package"], part["description"]), ("NE555", "DIP-8", "timer"))
        self.assertEqual((part["tags"], part["attributes"], part["min_quantity"]), (["timer"], {"vcc": "15V"}, 3))
        part = self.send("PATCH", url, {"name": "NE555P", "min_quantity": None})
        self.assertEqual((part["name"], part["min_quantity"], part["tags"]), ("NE555P", None, ["timer"]))
        self.assertEqual(self.send("PATCH", url, {})["name"], "NE555P")
        self.assertIn("unknown field(s): stock", self.send("PATCH", url, {"stock": []}, 400)["error"])
        self.assertIn("must not be empty", self.send("PATCH", url, {"name": " "}, 400)["error"])
        self.assertEqual(self.send("PATCH", "/api/parts/999", {"notes": "x"}, 404), {"error": "no part with id 999"})

    def test_delete_part(self):
        self.send("DELETE", f"/api/parts/{self.ne555}", status=204)
        self.assertEqual(self.inventory().list_parts(), [])
        self.send("DELETE", f"/api/parts/{self.ne555}", status=404)

    def test_stock(self):
        url = f"/api/parts/{self.ne555}/stock"
        part = self.send("POST", url, {"location": "Cabinet/Drawer 1", "delta": -2, "note": "amp board"})
        self.assertEqual(self.stock(part), {"Cabinet/Drawer 1": 3, "Bench/Drawer 1": 2})
        part = self.send("POST", url, {"location": "/Bench/Drawer 1", "quantity": 7})
        self.assertEqual(self.stock(part)["Bench/Drawer 1"], 7)
        part = self.send("POST", url, {"location": "Shelf/Box", "delta": 4})  # created
        self.assertEqual(self.stock(part)["Shelf/Box"], 4)
        # A path is always the full path: 'Drawer 1' is a new top-level location, not a guess.
        part = self.send("POST", url, {"location": "Drawer 1", "quantity": 1})
        self.assertEqual(self.stock(part)["Drawer 1"], 1)
        self.assertIn("only 3", self.send("POST", url, {"location": "Cabinet/Drawer 1", "delta": -9}, 400)["error"])
        for body in [{"location": "Shelf"}, {"location": "Shelf", "quantity": 1, "delta": 1}]:
            self.assertIn("either 'quantity'", self.send("POST", url, body, 400)["error"])
        history = self.send("GET", f"/api/history?part={self.ne555}")
        self.assertEqual(history[-3]["note"], "amp board")
        self.assertEqual(history[-3]["delta"], -2)

    def test_move(self):
        url = f"/api/parts/{self.ne555}/move"
        part = self.send("POST", url, {"from": "Cabinet/Drawer 1", "to": "Shelf", "quantity": 2})
        self.assertEqual(self.stock(part), {"Cabinet/Drawer 1": 3, "Shelf": 2, "Bench/Drawer 1": 2})
        part = self.send("POST", url, {"from": "Cabinet/Drawer 1", "to": "Shelf", "note": "tidying"})
        self.assertEqual(self.stock(part), {"Shelf": 5, "Bench/Drawer 1": 2})
        part = self.send("POST", url, {"to": "Bin"})
        self.assertEqual(self.stock(part), {"Bin": 7})
        self.assertIn("needs 'from'", self.send("POST", url, {"to": "Shelf", "quantity": 1}, 400)["error"])
        self.assertIn("missing field(s): to", self.send("POST", url, {"from": "Bin"}, 400)["error"])
        self.assertIn("no location", self.send("POST", url, {"from": "Nowhere", "to": "Bin"}, 400)["error"])

    def test_locations(self):
        response = self.client.post("/api/locations", json={"path": "Shelf/Box 1", "description": "resistors"},
                                    headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 201)
        box = response.get_json()
        self.assertEqual((box["path"], box["parent"], box["description"]), ("Shelf/Box 1", "Shelf", "resistors"))
        self.assertEqual(response.headers["Location"], box["api_url"])
        self.assertEqual(self.send("GET", box["api_url"]), box)
        self.assertIn("already exists", self.send("POST", "/api/locations", {"path": "shelf/box 1"}, 400)["error"])

        url = f"/api/locations/{box['id']}"
        box = self.send("PATCH", url, {"name": "Box A", "parent": "Cabinet", "description": ""})
        self.assertEqual((box["path"], box["description"]), ("Cabinet/Box A", ""))
        self.assertEqual(self.send("PATCH", url, {"parent": None})["path"], "Box A")
        self.assertIn("inside itself", self.send("PATCH", f"/api/locations/{self.location_id('Cabinet')}",
                                                 {"parent": "Cabinet/Drawer 1"}, 400)["error"])
        self.send("DELETE", url, status=204)
        self.send("GET", url, status=404)
        self.send("PATCH", url, {"name": "x"}, 404)
        drawer = self.location_id("Cabinet/Drawer 1")
        self.assertIn("still holds", self.send("DELETE", f"/api/locations/{drawer}", status=400)["error"])

    def test_location_changes_apply_to_the_location_in_the_url(self):
        # Between reading the location and changing it, another request renames
        # it and creates a new location with its old name.
        real_location_path = Inventory.location_path

        for method, body, status in [("PATCH", {"description": "mine"}, 200), ("DELETE", None, 204)]:
            with self.subTest(method):
                name, lookalikes = f"Shelf {method}", []

                def racing_location_path(inv, location):
                    path = real_location_path(inv, location)
                    if path == name and not lookalikes:
                        other = self.inventory()
                        other.update_location(f"/{name}", new_name=f"Rack {method}")
                        lookalikes.append(other.add_location(f"/{name}"))
                    return path

                shelf = self.inventory().add_location(f"/{name}")
                with mock.patch.object(Inventory, "location_path", racing_location_path):
                    self.send(method, f"/api/locations/{shelf}", body, status)
                locations = {l.id: l for l in self.inventory().list_locations()}
                if lookalikes:
                    self.assertIn(lookalikes[0], locations, f"deleted the new '{name}' instead")
                    self.assertEqual(locations[lookalikes[0]].description, "", f"changed the new '{name}' instead")
                if method == "PATCH":
                    self.assertEqual(locations[shelf].description, "mine")
                else:
                    self.assertNotIn(shelf, locations)

    def test_unknown_location_in_body_is_a_bad_request(self):
        # 404 means the URL (the part) wasn't found; a wrong location in the body is the client's input error.
        stock, move = f"/api/parts/{self.ne555}/stock", f"/api/parts/{self.ne555}/move"
        self.assertIn("no location", self.send("POST", stock, {"location": "Nowhere", "delta": -1}, 400)["error"])
        self.assertIn("no location", self.send("POST", move, {"from": "Nowhere", "to": "Bin"}, 400)["error"])
        self.send("POST", "/api/parts/999/stock", {"location": "Nowhere", "delta": -1}, 404)
        self.send("POST", "/api/parts/999/move", {"from": "Nowhere", "to": "Bin"}, 404)

    def location_id(self, path):
        return next(l.id for l in self.inventory().list_locations() if l.path == path)

    def upload(self, part_id, data, status=201):
        response = self.client.post(f"/api/parts/{part_id}/attachments", data=data,
                                    headers={"Authorization": f"Bearer {self.token}"},
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        return response.get_json()

    def test_upload_file(self):
        attachment = self.upload(self.ne555, {"file": (io.BytesIO(b"\x89PNG"), "../top view.png")})
        self.assertEqual((attachment["kind"], attachment["name"], attachment["stored"]), ("image", "top view.png", True))
        with self.client.get(attachment["url"]) as response:
            self.assertEqual(response.get_data(), b"\x89PNG")
        stored = self.inventory().get_attachment(attachment["id"])
        self.assertEqual(stored.path.parent, Path(self.tmp.name) / "attachments" / str(self.ne555))
        datasheet = self.upload(self.ne555, {"file": (io.BytesIO(b"x"), "pinout.txt"), "kind": "datasheet"})
        self.assertEqual(datasheet["kind"], "datasheet")
        self.assertEqual(len(self.send("GET", f"/api/parts/{self.ne555}")["attachments"]), 2)

        for data, message in [
            ({}, "missing field(s): file"),
            ({"file": [(io.BytesIO(b"a"), "a.txt"), (io.BytesIO(b"b"), "b.txt")]}, "one file per request"),
            ({"file": (io.BytesIO(b"a"), "a.txt"), "kind": "photo"}, "'kind' must be one of"),
            ({"file": (io.BytesIO(b"a"), "a.txt"), "note": "x"}, "unknown field(s): note"),
            ({"file": (io.BytesIO(b"a"), "a.txt"), "other": (io.BytesIO(b"b"), "b.txt")}, "unknown field(s): other"),
        ]:
            with self.subTest(data=data):
                self.assertIn(message, self.upload(self.ne555, data, 400)["error"])
        self.upload(999, {"file": (io.BytesIO(b"a"), "a.txt")}, 404)
        self.assertEqual(len(self.inventory().get_part(self.ne555).attachments), 2)
        response = self.client.post(f"/api/parts/{self.ne555}/attachments", content_type="multipart/form-data",
                                    data={"file": (io.BytesIO(b"a"), "a.txt")})
        self.assertEqual(response.status_code, 401)  # uploads need a token too

    def test_upload_too_large(self):
        self.client.application.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        # Refused by its Content-Length, before anything is read.
        response = self.client.post(f"/api/parts/{self.ne555}/attachments", data=b"x" * 2 * 1024 * 1024,
                                    content_type="multipart/form-data; boundary=x",
                                    headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json(), {"error": "the request is larger than 1 MB"})

    def test_add_link(self):
        url = f"/api/parts/{self.ne555}/attachments"
        page = self.send("POST", url, {"url": "https://example.com/ne555"}, 201)
        self.assertEqual((page["stored"], page["url"], page["kind"]), (False, "https://example.com/ne555", "other"))
        self.assertNotIn("download_error", page)
        with serve({"/ds/ne555.pdf": (200, "application/pdf", b"%PDF"),
                    "/missing.pdf": (404, "text/plain", b"")}) as base:
            stored = self.send("POST", url, {"url": f"{base}/ds/ne555.pdf"}, 201)
            self.assertEqual((stored["stored"], stored["kind"], stored["source_url"]),
                             (True, "datasheet", f"{base}/ds/ne555.pdf"))
            with self.client.get(stored["url"]) as response:
                self.assertEqual(response.get_data(), b"%PDF")
            kept = self.send("POST", url, {"url": f"{base}/ds/ne555.pdf", "kind": "other", "download": False}, 201)
            self.assertEqual((kept["stored"], kept["kind"]), (False, "other"))
            failed = self.send("POST", url, {"url": f"{base}/missing.pdf"}, 201)
            self.assertFalse(failed["stored"])
            self.assertIn("HTTP 404", failed["download_error"])
            retried = self.send("POST", f"{url}/{kept['id']}/download")
            self.assertEqual((retried["id"], retried["stored"]), (kept["id"], True))
        self.assertIn("already a stored file", self.send("POST", f"{url}/{kept['id']}/download", status=400)["error"])
        for body, message in [
            ({"url": "/etc/passwd"}, "is not a link"),
            ({"url": "https://example.com/x", "download": "yes"}, "'download' must be true or false"),
            ({"url": "https://example.com/x", "kind": "photo"}, "'kind' must be one of"),
            ({"link": "https://example.com/x"}, "unknown field(s): link"),
        ]:
            with self.subTest(body=body):
                self.assertIn(message, self.send("POST", url, body, 400)["error"])
        self.send("POST", "/api/parts/999/attachments", {"url": "https://example.com/x"}, 404)

    def test_remove_attachment(self):
        other = self.send("POST", "/api/parts", {"name": "LM358"}, 201)["id"]
        attachment = self.upload(self.ne555, {"file": (io.BytesIO(b"x"), "a.txt")})
        path = self.inventory().get_attachment(attachment["id"]).path
        # Only through the part it belongs to, so a wrong part id can't remove it.
        self.send("DELETE", f"/api/parts/{other}/attachments/{attachment['id']}", status=404)
        self.send("POST", f"/api/parts/{other}/attachments/{attachment['id']}/download", status=404)
        self.assertTrue(path.exists())
        self.send("DELETE", f"/api/parts/{self.ne555}/attachments/{attachment['id']}", status=204)
        self.assertFalse(path.exists())
        self.assertEqual(self.inventory().get_part(self.ne555).attachments, [])
        self.send("DELETE", f"/api/parts/{self.ne555}/attachments/{attachment['id']}", status=404)

    def test_read_only_copy(self):
        (Path(self.tmp.name) / "partstrack-copy.json").write_text('{"kind": "server", "source": "http://a/"}')
        error = self.send("POST", "/api/parts", {"name": "LM358"}, 409)["error"]
        self.assertIn("read-only copy", error)
        self.send("POST", "/api/parts", {"name": "LM358"}, 401, token=False)  # no token: still 401


if __name__ == "__main__":
    unittest.main()
