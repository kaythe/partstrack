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

        self.client.post(f"/parts/{part.id}/attachments", content_type="multipart/form-data",
                         data={"files": [(io.BytesIO(b"%PDF-1.4"), "ds 10:30.pdf")]})
        with self.inventory() as inv:
            uploaded = next(a for a in inv.get_part(part.id).attachments if a.original_name == "ds 10:30.pdf")
        self.assertEqual((uploaded.path.name, uploaded.path.read_bytes()), ("ds 10_30.pdf", b"%PDF-1.4"))

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

    def test_parts_list_pages(self):
        with self.inventory() as inv:
            for i in range(60):
                part_id = inv.add_part(f"R{i:02}")
                if i % 2:
                    inv.add_tags(part_id, ["odd"])
        page = self.client.get("/").get_data(as_text=True)  # 50 per page unless chosen
        self.assertIn("Parts 1–50 of 60", page)
        self.assertIn(">R49</a>", page)
        self.assertNotIn(">R50</a>", page)
        self.assertIn("Page 1 of 2", page)
        self.assertIn("Change all 60 matching", page)

        page = self.client.get("/?page=2").get_data(as_text=True)
        self.assertIn("Parts 51–60 of 60", page)
        self.assertIn(">R59</a>", page)
        self.assertNotIn(">R49</a>", page)
        self.assertEqual(page, self.client.get("/?page=9").get_data(as_text=True))  # past the end: last page

        response = self.client.get("/?per_page=25&tag=odd")
        self.assertIn("per_page=25", response.headers["Set-Cookie"])
        page = response.get_data(as_text=True)
        self.assertIn("Parts 1–25 of 30", page)
        self.assertIn('href="/?page=2&amp;tag=odd"', page)
        self.assertIn("Labels for all 30 matching", page)
        self.assertIn("Page 2 of 3", self.client.get("/?page=2").get_data(as_text=True))  # remembered

        self.client.get("/?per_page=all")
        page = self.client.get("/?page=2").get_data(as_text=True)
        self.assertIn(">R00</a>", page)
        self.assertIn(">R59</a>", page)
        self.assertNotIn("Page 1", page)

        self.client.get("/?per_page=50")
        page = self.client.get("/parts/bulk?shown=1&filter_tag=odd").get_data(as_text=True)
        self.assertIn("R59", page)  # all that match, not only the first page

    def test_numbers_that_are_not_ascii(self):
        self.add_part()
        for url in ["/?page=%C2%B2", "/parts/bulk?part=%C2%B2", "/labels?part=%C2%B2",
                    "/parts/new?copy=%C2%B2", "/history?part=%C2%B2"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn(">NE555</a>", self.client.get("/?page=%C2%B2").get_data(as_text=True))
        response = self.client.post("/parts/bulk", data={"part": "²", "op": "add_tags", "tags": "x"})
        self.assertEqual(response.status_code, 302)

    def test_numbers_beyond_the_database_range(self):
        self.add_part()
        huge = "9" * 30
        for url in [f"/parts/{huge}", f"/parts/{huge}/edit", f"/attachments/{huge}"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for url in [f"/history?part={huge}", f"/parts/new?copy={huge}", f"/parts/bulk?part={huge}",
                    f"/labels?part={huge}"]:
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (200, 302, 404))
        response = self.add_part(name="LM358", quantity=huge, description="kept")
        self.assertEqual(response.status_code, 200)
        self.assertIn("must not be larger than", response.get_data(as_text=True))
        self.assertIn('value="kept"', response.get_data(as_text=True))
        response = self.add_part(name="LM358", min_quantity=huge)
        self.assertIn("must not be larger than", response.get_data(as_text=True))
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        for path, data in [(f"/parts/{part_id}/stock", {"op": "add", "amount": huge, "location": "/Cabinet A/Drawer 1"}),
                           ("/parts/bulk", {"part": huge, "op": "add_tags", "tags": "x"}),
                           (f"/parts/{huge}/attachments", {"links": "https://example.com/x"})]:
            with self.subTest(path=path):
                self.assertIn(self.client.post(path, data=data).status_code, (302, 404))
        with self.inventory() as inv:
            self.assertEqual(inv.get_part(part_id).total_quantity, 20)
            self.assertEqual(len(inv.list_parts()), 1)

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

    def test_minimum_quantity_and_low_filter(self):
        self.add_part(min_quantity="30")
        self.add_part(name="LM358", min_quantity="")
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
            self.assertIsNone(inv.get_part(inv.find_part("LM358")).min_quantity)
        page = self.client.get("/?low=1").get_data(as_text=True)
        self.assertIn(">NE555</a>", page)
        self.assertNotIn(">LM358</a>", page)
        self.assertIn("below the minimum of 30", self.client.get(f"/parts/{part_id}").get_data(as_text=True))
        edit = self.client.get(f"/parts/{part_id}/edit").get_data(as_text=True)
        self.assertIn('name="min_quantity" min="0" value="30"', edit)
        self.client.post(f"/parts/{part_id}/edit", data={"name": "NE555", "min_quantity": ""})
        self.assertNotIn(">NE555</a>", self.client.get("/?low=1").get_data(as_text=True))
        response = self.client.post(f"/parts/{part_id}/edit", data={"name": "NE555", "min_quantity": "-2"})
        self.assertIn("must not be negative", response.get_data(as_text=True))

    def test_stock_history(self):
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.client.post(f"/parts/{part_id}/stock",
                         data={"location": "/Cabinet A/Drawer 1", "amount": "3", "op": "take", "note": "<b>amp</b>"})
        self.client.post(f"/parts/{part_id}/move",
                         data={"source": "/Cabinet A/Drawer 1", "destination": "Bench", "note": "testing"})
        with self.inventory() as inv:
            self.assertEqual([c.note for c in inv.stock_history(part_id)], ["testing", "testing", "<b>amp</b>", "new part"])
        page = self.client.get(f"/parts/{part_id}").get_data(as_text=True)
        self.assertIn("&lt;b&gt;amp&lt;/b&gt;", page)
        self.assertNotIn("Full history", page)
        page = self.client.get("/history?location=/Bench").get_data(as_text=True)
        self.assertIn("testing", page)
        self.assertNotIn("amp", page)
        self.assertIn("NE555", self.client.get(f"/history?part={part_id}").get_data(as_text=True))
        self.assertEqual(self.client.get("/history?part=999").status_code, 404)
        self.assertIn("no location", self.client.get("/history?location=/Nowhere").get_data(as_text=True))

    def test_labels(self):
        self.add_part(package="DIP-8", description="<b>timer</b>")
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
            inv.add_location("Cabinet A/Drawer 2")
            inv.add_location("Shelf")
        self.assertIn('form="labels"', self.client.get("/locations").get_data(as_text=True))
        self.assertIn('name="part" value="%d" form="selection"' % part_id, self.client.get("/").get_data(as_text=True))

        response = self.client.get(f"/labels?location=/Cabinet A&sublocations=1&part={part_id}"
                                   "&layout=sheet-3x8-70x37&start=3")
        page = response.get_data(as_text=True)
        self.assertEqual(page.count('<svg class="qr"'), 4)
        self.assertIn("left: 0.0mm; top: 37.5mm", page)  # the third place is the first of row 2
        self.assertIn("&lt;b&gt;timer&lt;/b&gt;", page)
        self.assertIn("DIP-8", page)
        self.assertIn("@page { size: 210.0mm 297.0mm; margin: 0; }", page)
        self.assertIn("only works on this computer", page)  # the test client is localhost
        self.assertIn("sheet-3x8-70x37", response.headers["Set-Cookie"])

        page = self.client.get("/labels?all_locations=1").get_data(as_text=True)  # layout from the cookie
        self.assertEqual(page.count('<svg class="qr"'), 4)
        self.assertIn('value="sheet-3x8-70x37" selected', page)
        page = self.client.get("/labels?shown=1&filter_q=555").get_data(as_text=True)
        self.assertEqual(page.count('<svg class="qr"'), 1)
        page = self.client.get("/labels?location=/Shelf&layout=printer-62x29").get_data(as_text=True)
        self.assertIn("@page { size: 62.0mm 29.0mm; margin: 0; }", page)

        page = self.client.get("/labels?location=/Shelf&layout=custom-sheet&columns=9").get_data(as_text=True)
        self.assertIn("more than the page", page)
        page = self.client.get("/labels?location=/Shelf&start=99").get_data(as_text=True)
        self.assertIn("must be between 1 and", page)
        self.assertIn("No labels selected", self.client.get("/labels").get_data(as_text=True))
        self.assertEqual(self.client.get("/labels?part=999").status_code, 404)

    def test_labels_use_the_public_url(self):
        client = create_app(self.db, "http://parts.lan:8080").test_client()
        with self.inventory() as inv:
            inv.add_location("Shelf")
        page = client.get("/labels?location=/Shelf").get_data(as_text=True)
        self.assertIn("The QR codes link to http://parts.lan:8080/.", page)
        self.assertNotIn("only works on this computer", page)

    def test_duplicate_part(self):
        self.add_part(package="DIP-8", min_quantity="5")
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.assertIn(f"/parts/new?copy={part_id}", self.client.get(f"/parts/{part_id}").get_data(as_text=True))
        form = self.client.get(f"/parts/new?copy={part_id}").get_data(as_text=True)
        self.assertIn("A copy of", form)
        for expected in ['value="NE555"', 'value="DIP-8"', 'value="tht, timer"', "vcc = 4.5-16V", 'value="5"']:
            self.assertIn(expected, form)
        self.assertNotIn('name="quantity" min="0" value="20"', form)  # no stock
        self.assertIn(f'<a href="/parts/{part_id}">Cancel</a>', form)
        self.assertEqual(self.client.get("/parts/new?copy=999").status_code, 404)

        response = self.client.post(f"/parts/new?copy={part_id}", data={"name": "ne555", "package": "SOIC-8"})
        self.assertIn("already exists", response.get_data(as_text=True))
        self.assertIn("A copy of", response.get_data(as_text=True))
        self.add_part(name="NE556")
        with self.inventory() as inv:
            self.assertEqual(len(inv.list_parts()), 2)

    def test_attribute_search(self):
        self.add_part(attributes="resistance = 4k7")
        self.add_part(name="R2", attributes="resistance = 10k")
        page = self.client.get("/?q=resistance>=10k").get_data(as_text=True)
        self.assertIn(">R2</a>", page)
        self.assertNotIn(">NE555</a>", page)
        self.assertIn("is not a number", self.client.get("/?q=resistance<lots").get_data(as_text=True))

    def test_bulk_changes(self):
        self.add_part()  # 20 in Cabinet A/Drawer 1
        self.add_part(name="LM358", quantity="5", location="Bench")
        self.add_part(name="BC547", quantity="", category="Transistor")
        with self.inventory() as inv:
            ids = {p.name: p.id for p in inv.list_parts()}
        selected = [ids["NE555"], ids["LM358"]]

        page = self.client.get("/parts/bulk", query_string={"part": selected}).get_data(as_text=True)
        self.assertIn("Change 2 parts", page)
        self.assertNotIn(">BC547</a>", page)
        page = self.client.get("/parts/bulk?shown=1&filter_category=Transistor").get_data(as_text=True)
        self.assertIn("Change 1 part<", page)
        self.assertIn(">BC547</a>", page)
        self.assertIn("No parts selected", self.client.get("/parts/bulk").get_data(as_text=True))

        def post(**form):
            return self.client.post("/parts/bulk", data={"part": selected, **form},
                                    headers={"Referer": "http://localhost/parts/bulk"})

        response = post(op="add_tags", tags="opamp, smd")
        self.assertEqual(response.headers["Location"], f"/parts/bulk?part={ids['LM358']}&part={ids['NE555']}")  # by name
        post(op="remove_tags", tags="tht")
        post(op="category", category="Analog")
        post(op="move", destination="Box", note="tidying up")
        with self.inventory() as inv:
            for name in ("NE555", "LM358"):
                part = inv.get_part(ids[name])
                self.assertEqual((part.category, [s.location for s in part.stock]), ("Analog", ["Box"]))
                self.assertIn("opamp", part.tags)
                self.assertNotIn("tht", part.tags)
            self.assertEqual(inv.get_part(ids["NE555"]).total_quantity, 20)
            self.assertEqual(inv.stock_history(ids["LM358"])[0].note, "tidying up")
            self.assertEqual(inv.get_part(ids["BC547"]).category, "Transistor")

        response = post(op="move", destination="")  # the error goes back to the bulk page
        self.assertEqual(response.headers["Location"], "http://localhost/parts/bulk")
        response = post(op="delete")
        self.assertEqual(response.headers["Location"], "/")
        with self.inventory() as inv:
            self.assertEqual([p.name for p in inv.list_parts()], ["BC547"])
        self.assertEqual(self.client.post("/parts/bulk", data={"part": [ids["BC547"]], "op": "x"}).status_code, 400)

    def test_cross_site_requests_are_refused(self):
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")

        def delete(**headers):
            return self.client.post("/parts/bulk", data={"part": [part_id], "op": "delete"}, headers=headers)

        for headers in [{"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "http://localhost:3000"},
                        {"Origin": "https://localhost"}, {"Origin": "http://localhost:99999"},
                        {"Referer": "http://evil.example/page"}, {"Referer": "https://localhost/parts/bulk"}]:
            with self.subTest(headers=headers):
                response = delete(**headers)
                self.assertEqual(response.status_code, 403)
                self.assertIn("sent from another website", response.get_data(as_text=True))
        self.assertEqual(self.client.post("/theme", data={"theme": "dark"},
                                          headers={"Origin": "http://evil.example"}).status_code, 403)
        with self.inventory() as inv:
            self.assertEqual(len(inv.list_parts()), 1)
        self.assertEqual(self.client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200)

        self.assertEqual(delete(**{"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"}).status_code, 302)
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.assertEqual(delete(Origin="http://LOCALHOST:80").status_code, 302)  # the same origin
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.assertEqual(delete(Referer="http://localhost/parts/bulk").status_code, 302)

    def test_public_url_counts_as_this_site(self):
        client = create_app(self.db, "http://parts.lan:8080").test_client()
        response = client.post("/theme", data={"theme": "dark"}, headers={"Origin": "http://parts.lan:8080"})
        self.assertEqual(response.status_code, 302)
        response = client.post("/theme", data={"theme": "dark"}, headers={"Origin": "http://parts.lan:9999"})
        self.assertEqual(response.status_code, 403)
        response = client.post("/theme", data={"theme": "dark"}, headers={"Origin": "https://parts.lan:8080"})
        self.assertEqual(response.status_code, 403)
        # Behind an HTTPS proxy, --url says which origin the browser sees.
        client = create_app(self.db, "https://parts.lan").test_client()
        response = client.post("/theme", data={"theme": "dark"}, headers={"Origin": "https://parts.lan"})
        self.assertEqual(response.status_code, 302)

    def test_edit_part_replaces_tags_and_attributes(self):
        self.add_part()
        with self.inventory() as inv:
            part_id = inv.find_part("NE555")
        self.client.post(f"/parts/{part_id}/edit", data={"name": "NE555P", "tags": "timer", "attributes": ""})
        with self.inventory() as inv:
            part = inv.get_part(part_id)
        self.assertEqual((part.name, part.tags, part.attributes), ("NE555P", ["timer"], {}))

    def test_tag_suggestions_are_offered(self):
        self.add_part(tags="timer, it's <b>")
        page = self.client.get("/parts/new").get_data(as_text=True)
        self.assertIn("""data-tags='["it\\u0027s \\u003cb\\u003e", "timer"]'""", page)
        self.assertIn("/static/tags.js", page)
        with self.client.get("/static/tags.js") as response:
            self.assertEqual(response.status_code, 200)

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

    def test_about_page(self):
        from partstrack import __version__
        page = self.client.get("/about").get_data(as_text=True)
        self.assertIn(f"<dd>{__version__}</dd>", page)
        self.assertIn('href="https://github.com/kaythe/partstrack"', page)
        self.assertIn('href="/about" aria-current="page"', page)
        self.assertIn('href="https://github.com/kaythe/partstrack/blob/main/LICENSE"', page)

    def test_logo(self):
        with self.client.get("/logo.svg") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/svg+xml")
            self.assertTrue(response.get_data().startswith(b"<svg"))
        self.assertIn('rel="icon" href="/logo.svg"', self.client.get("/about").get_data(as_text=True))

    def test_missing_part_is_404(self):
        self.assertEqual(self.client.get("/parts/42").status_code, 404)


if __name__ == "__main__":
    unittest.main()
