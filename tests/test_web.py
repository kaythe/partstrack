import io
import tempfile
import unittest
from pathlib import Path

from partstrack.store import Inventory

from .fileserver import serve

try:
    from partstrack.web.app import create_app
except ImportError:  # the optional web dependencies are not installed
    create_app = None


@unittest.skipIf(create_app is None, "needs the 'web' extra (Flask)")
class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "parts.db"
        self.client = create_app(self.db).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def inventory(self):
        return Inventory(self.db)

    def add_part(self, **form):
        data = {"name": "NE555", "category": "IC", "tags": "timer, tht", "attributes": "vcc = 4.5-16V",
                "quantity": "20", "location": "Cabinet A/Drawer 1", **form}
        return self.client.post("/parts/new", data=data, content_type="multipart/form-data")

    def test_add_part_with_upload_and_show_it(self):
        routes = {"/ne555.jpg": (200, "image/jpeg", b"JPEG")}
        with serve(routes) as base:
            response = self.add_part(
                datasheets=[(io.BytesIO(b"%PDF-1.4"), "ne555 Größe.pdf")],
                images=[(io.BytesIO(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), "..\\..\\pinout.svg")],
                links=f"{base}/ne555.jpg {base}/shop/ne555",
            )
        self.assertEqual(response.status_code, 302)
        with self.inventory() as inv:
            part = inv.get_part(inv.find_part("NE555"))
        self.assertEqual(part.tags, ["tht", "timer"])
        self.assertEqual(part.attributes, {"vcc": "4.5-16V"})
        self.assertEqual(part.total_quantity, 20)
        names = sorted(a.original_name for a in part.attachments)
        self.assertEqual(names, ["ne555", "ne555 Größe.pdf", "ne555.jpg", "pinout.svg"])
        jpg = next(a for a in part.attachments if a.original_name == "ne555.jpg")
        self.assertEqual((jpg.url, jpg.source_url, jpg.path.read_bytes()), (None, f"{base}/ne555.jpg", b"JPEG"))

        page = self.client.get(f"/parts/{part.id}").get_data(as_text=True)
        self.assertIn("Cabinet A/Drawer 1", page)
        self.assertIn("4.5-16V", page)

        svg = next(a for a in part.attachments if a.original_name == "pinout.svg")
        response = self.client.get(f"/attachments/{svg.id}")
        self.assertEqual(response.headers["Content-Security-Policy"], "sandbox")
        pdf = next(a for a in part.attachments if a.kind == "datasheet")
        response = self.client.get(f"/attachments/{pdf.id}")
        self.assertEqual((response.mimetype, response.data), ("application/pdf", b"%PDF-1.4"))
        response.close()

    def test_failed_add_keeps_form_values_and_saves_nothing(self):
        response = self.add_part(quantity="5", location="", description="kept")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("please choose where the parts are stored", page)
        self.assertIn('value="kept"', page)
        with self.inventory() as inv:
            self.assertEqual(inv.list_parts(), [])

    def test_list_search_and_escaping(self):
        self.add_part(name="<b>evil</b>")
        page = self.client.get("/?q=evil").get_data(as_text=True)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;", page)
        self.assertNotIn("<b>evil</b>", page)
        self.assertIn("No parts found", self.client.get("/?q=nothing").get_data(as_text=True))

    def test_stock_actions(self):
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        url = f"/parts/{part_id}/stock"
        self.client.post(url, data={"location": "/Cabinet A/Drawer 1", "amount": "3", "op": "take"})
        self.client.post(f"/parts/{part_id}/move",
                         data={"source": "/Cabinet A/Drawer 1", "destination": "Bench", "quantity": "7"})
        response = self.client.post(url, data={"location": "/Bench", "amount": "99", "op": "take"},
                                    headers={"Referer": f"http://localhost/parts/{part_id}"})
        self.assertEqual(response.headers["Location"], f"http://localhost/parts/{part_id}")
        with self.inventory() as inv:
            stock = {s.location: s.quantity for s in inv.get_part(part_id).stock}
        self.assertEqual(stock, {"Bench": 7, "Cabinet A/Drawer 1": 10})
        page = self.client.get(f"/parts/{part_id}").get_data(as_text=True)
        self.assertIn("only 7 in", page)  # the failed take was reported

    def test_edit_part_replaces_tags_and_attributes(self):
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.client.post(f"/parts/{part_id}/edit", data={"name": "NE555P", "tags": "timer", "attributes": ""})
        with self.inventory() as inv:
            part = inv.get_part(part_id)
        self.assertEqual((part.name, part.tags, part.attributes), ("NE555P", ["timer"], {}))

    def test_locations(self):
        self.client.post("/locations", data={"path": "Lab/Cabinet A", "description": ""})
        self.client.post("/locations/edit?path=/Lab/Cabinet A",
                         data={"name": "Cabinet B", "description": "tall", "parent": ""})
        with self.inventory() as inv:
            self.assertEqual([l.path for l in inv.list_locations()], ["Cabinet B", "Lab"])
        self.client.post("/locations/delete", data={"path": "/Lab"})
        page = self.client.get("/locations").get_data(as_text=True)
        self.assertIn("Cabinet B", page)
        self.assertNotIn(">Lab<", page)

    def test_csv_round_trip_without_server_files(self):
        self.add_part(datasheets=[(io.BytesIO(b"%PDF"), "ds.pdf")])
        exported = self.client.get("/export.csv?delimiter=;&bom=1").data
        self.assertTrue(exported.startswith("﻿name;".encode()))

        # A file path in an uploaded CSV must not attach files from the server.
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("secret")
        csv_text = f"name,quantity,location,datasheets\nLM358,5,Box,{secret}\n"
        response = self.client.post("/import", data={"file": (io.BytesIO(csv_text.encode()), "x.csv")},
                                    content_type="multipart/form-data")
        self.assertIn("skipped 1 file attachment", response.get_data(as_text=True))
        with self.inventory() as inv:
            part = inv.get_part(inv.find_part("LM358"))
        self.assertEqual((part.total_quantity, part.attachments), (5, []))

        response = self.client.post("/import", data={"file": (io.BytesIO(exported), "parts.csv"), "preview": "1"},
                                    content_type="multipart/form-data")
        self.assertIn("Nothing was saved yet", response.get_data(as_text=True))

    def test_store_link_locally_button(self):
        with serve({"/ds.pdf": (200, "application/pdf", b"%PDF")}) as base:
            with self.inventory() as inv:
                part_id = inv.add_part("LM358")
                attachment_id = inv.add_attachment(part_id, f"{base}/ds.pdf")
            page = self.client.get(f"/parts/{part_id}").get_data(as_text=True)
            self.assertIn("Store locally", page)
            self.client.post(f"/attachments/{attachment_id}/download")
        page = self.client.get(f"/parts/{part_id}").get_data(as_text=True)
        self.assertIn("Stored ds.pdf locally", page)
        self.assertIn("original link", page)
        self.assertNotIn("Store locally", page)

    def test_theme_switch(self):
        self.assertNotIn("data-theme", self.client.get("/locations").get_data(as_text=True))
        response = self.client.post("/theme", data={"theme": "dark"},
                                    headers={"Referer": "http://localhost/locations"})
        self.assertEqual(response.headers["Location"], "http://localhost/locations")
        page = self.client.get("/locations").get_data(as_text=True)
        self.assertIn('<html lang="en" data-theme="dark">', page)
        self.assertIn('value="dark" aria-pressed="true"', page)
        self.client.post("/theme", data={"theme": "auto"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("data-theme", page)
        self.assertIn('value="auto" aria-pressed="true"', page)
        self.client.post("/theme", data={"theme": "<script>"})
        self.assertNotIn("data-theme", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get("/import").status_code, 302)

    def test_missing_part_is_404(self):
        self.assertEqual(self.client.get("/parts/42").status_code, 404)


if __name__ == "__main__":
    unittest.main()
