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

"""A JSON API for scripts and other programs (e.g. a phone app), under /api.

Reading needs nothing, like the web pages. Every change (POST, PATCH, DELETE)
needs an API token, created with 'partstrack token add', in the header
'Authorization: Bearer <token>'. Browsers never send that header on their own,
and other websites can't add it to a request here without a CORS preflight,
which is never answered; so token requests can't be forged by other websites,
and app.py's check for that doesn't apply to API routes. No CORS headers are
sent, so other websites can't read the inventory through a visitor's browser
either. Attachments are given as links to the web interface, never as file
paths on the server.

Requests with a body take a JSON object (file uploads: multipart/form-data);
unknown fields are refused, so a typo can't go unnoticed. Locations in bodies are full paths ('Cabinet/Drawer 3', as
the API returns them); missing ones are created where stock is put. In URLs,
parts and locations are given by id.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, g, jsonify, request, send_file, url_for
from werkzeug.exceptions import HTTPException

from .. import __version__
from ..snapshot import write_snapshot
from ..store import (
    ATTACHMENT_KINDS,
    EDITABLE_FIELDS,
    NEW_PART_NOTE,
    Attachment,
    InventoryError,
    Location,
    NotFoundError,
    Part,
    StockChange,
    is_url,
    whole_number,
)
from .app import SAFE_METHODS, _save_uploads, inventory

api = Blueprint("api", __name__, url_prefix="/api")
TRUE = ("1", "true", "yes", "on")


@api.errorhandler(NotFoundError)
def _not_found(e: NotFoundError):
    return jsonify(error=str(e)), 404


@api.errorhandler(InventoryError)
def _bad_request(e: InventoryError):  # e.g. an attribute filter that isn't a number
    return jsonify(error=str(e)), 400


@api.app_errorhandler(404)
@api.app_errorhandler(405)
@api.app_errorhandler(413)
@api.app_errorhandler(500)
def _http_error(e: HTTPException):
    """Unknown URLs and methods, and unexpected errors (e.g. a locked database):
    JSON under /api (no route matched, or the error came from outside a route,
    so the blueprint's own handlers don't apply), the usual pages elsewhere."""
    if request.path != "/api" and not request.path.startswith("/api/"):
        return e
    if e.code == 404:
        message = "no such API endpoint; see /api"
    elif e.code == 413:
        message = f"the request is larger than {current_app.config['MAX_CONTENT_LENGTH'] >> 20} MB"
    elif e.code == 500:  # Flask has logged the details; don't show internals
        message = "the server could not complete the request (see its log)"
    else:
        message = f"{request.method} is not allowed here; allowed: {', '.join(sorted(e.valid_methods or []))}"
    response = jsonify(error=message)
    response.status_code = e.code
    if e.code == 405 and e.valid_methods:
        response.headers["Allow"] = ", ".join(sorted(e.valid_methods))
    return response


@api.before_request
def _require_token():
    """Changes need a valid API token; reading needs none."""
    if request.method in SAFE_METHODS:
        return None
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return _unauthorized(
            "changes need an API token (see 'partstrack token add') in the header 'Authorization: Bearer <token>'"
        )
    inv = inventory()
    g.api_token = inv.check_token(token)
    if g.api_token is None:
        return _unauthorized("unknown or revoked API token", ', error="invalid_token"')
    if inv.read_only:
        return jsonify(error="this server shows a read-only copy; changes can't be saved"), 409
    return None


def _unauthorized(message: str, details: str = "") -> tuple[Response, int, dict]:
    return jsonify(error=message), 401, {"WWW-Authenticate": f'Bearer realm="partstrack"{details}'}


@api.get("")
def index():
    return jsonify(
        version=__version__,
        endpoints={
            "/api/parts": "parts; filters: q (search, e.g. 'smd resistance>=10k'), tag, location, category, low",
            "/api/parts/<id>": "one part",
            "/api/locations": "all locations, parents before their children",
            "/api/tags": "tags with the number of parts that have them",
            "/api/categories": "categories in use",
            "/api/history": "stock changes, newest first; filters: part (id), location, limit (default 50, 0 for all)",
            "/api/locations/<id>": "one location",
            "/api/snapshot": "the database and attachments as .tar.gz, for the desktop app's read-only copies",
        },
        authentication="changes need an API token (see 'partstrack token add') "
                       "in the header 'Authorization: Bearer <token>'",
        changes={
            "POST /api/parts": "add a part: name (required), " + ", ".join(EDITABLE_FIELDS[1:])
                               + ", tags (list), attributes (object), min_quantity, "
                                 "stock (list of {location, quantity})",
            "PATCH /api/parts/<id>": "change the fields given; tags and attributes replace all; "
                                     "min_quantity null removes it",
            "DELETE /api/parts/<id>": "delete a part with its stock, history and attachments",
            "POST /api/parts/<id>/attachments": "attach a link as JSON: url, kind (" + ", ".join(ATTACHMENT_KINDS)
                                                + "; default: from the name), download (default true: store a "
                                                  "copy of a file a link points to); or upload a file as "
                                                  "multipart/form-data: file, kind",
            "POST /api/parts/<id>/attachments/<id>/download": "store a copy of the file a link points to",
            "DELETE /api/parts/<id>/attachments/<id>": "remove an attachment",
            "POST /api/parts/<id>/stock": "location and either quantity (the new amount there) "
                                          "or delta (added or, if negative, taken); note",
            "POST /api/parts/<id>/move": "to, and from with an optional quantity (default: all there); "
                                         "without from, all stock moves; note",
            "POST /api/locations": "add a location: path (parents are created too), description",
            "PATCH /api/locations/<id>": "name, description, parent (a path, or null for the top level)",
            "DELETE /api/locations/<id>": "delete an empty location",
        },
    )


@api.get("/parts")
def parts():
    args = request.args
    found = inventory().list_parts(
        args.get("q", ""),
        tag=args.get("tag") or None,
        location=args.get("location") or None,
        category=args.get("category") or None,
        low=args.get("low", "").lower() in TRUE,
    )
    return jsonify([_part(p) for p in found])


@api.get("/parts/<int:part_id>")
def part(part_id: int):
    return jsonify(_part(inventory().get_part(part_id)))


@api.get("/locations")
def locations():
    return jsonify([_location(l) for l in inventory().list_locations()])


@api.get("/locations/<int:location_id>")
def location(location_id: int):
    return jsonify(_location(inventory().get_location(location_id)))


@api.get("/tags")
def tags():
    return jsonify([{"name": name, "parts": count} for name, count in inventory().list_tags()])


@api.get("/categories")
def categories():
    return jsonify(inventory().list_categories())


@api.get("/history")
def history():
    part_id = _number("part")
    limit = _number("limit")
    changes = inventory().stock_history(
        part_id, location=request.args.get("location") or None, limit=50 if limit is None else (limit or None)
    )
    return jsonify([_change(c) for c in changes])


@api.get("/snapshot")
def snapshot():
    """The whole inventory as a .tar.gz (see partstrack.snapshot). It is built in an
    anonymous temporary file, so large attachment folders don't have to fit in
    memory; the system deletes the file when the server closes it after sending.
    (A named file would stay behind: Werkzeug skips call_on_close for send_file.)"""
    archive = tempfile.TemporaryFile(prefix="partstrack-snapshot-")
    try:
        write_snapshot(inventory(), archive)
        archive.seek(0)
    except BaseException:
        archive.close()
        raise
    return send_file(archive, mimetype="application/gzip", as_attachment=True,
                     download_name=datetime.now(timezone.utc).strftime("partstrack-%Y-%m-%d_%H%M%S.tar.gz"))


# -- changes ------------------------------------------------------------------

PART_FIELDS = {*EDITABLE_FIELDS, "tags", "attributes", "min_quantity"}


@api.post("/parts")
def add_part():
    data = _body(PART_FIELDS | {"stock"}, required=("name",))
    stock = [
        (_location_ref(entry, "location", "stock"), _int(entry, "quantity", "stock"))
        for entry in _objects(data, "stock", {"location", "quantity"}, ("location", "quantity"))
    ]
    inv = inventory()
    with inv.transaction():
        part_id = inv.add_part(_text(data, "name"), **_part_fields(data, exclude="name"))
        _set_part_details(part_id, data)
        for location, quantity in stock:
            inv.set_stock(part_id, location, quantity, note=NEW_PART_NOTE)
    response = jsonify(_part(inv.get_part(part_id)))
    return response, 201, {"Location": url_for("api.part", part_id=part_id, _external=True)}


@api.patch("/parts/<int:part_id>")
def update_part(part_id: int):
    data = _body(PART_FIELDS)
    inv = inventory()
    with inv.transaction():
        inv.update_part(part_id, **_part_fields(data))
        _set_part_details(part_id, data)
    return jsonify(_part(inv.get_part(part_id)))


@api.delete("/parts/<int:part_id>")
def delete_part(part_id: int):
    inventory().delete_part(part_id)
    return "", 204


@api.post("/parts/<int:part_id>/attachments")
def add_attachment(part_id: int):
    """A link (JSON) or an uploaded file (multipart/form-data). Links to files are
    downloaded after the commit, like everywhere else; if that fails, the link
    is kept, and the answer says why in 'download_error'."""
    inv = inventory()
    if request.mimetype == "multipart/form-data":
        _check_keys(request.form, {"kind"}, ())
        _check_keys(request.files, {"file"}, ("file",))
        uploads = request.files.getlist("file")
        if len(uploads) > 1:
            raise InventoryError("send one file per request")
        if not uploads[0].filename:
            raise InventoryError("the uploaded file needs a file name")
        kind = _kind(request.form.get("kind"))
        with inv.transaction():
            inv.get_part(part_id)  # 404 before saving the upload for a missing part
            (attachment_id,) = _save_uploads(inv, part_id, uploads, kind)
        return jsonify(_attachment(inv.get_attachment(attachment_id))), 201

    data = _body({"url", "kind", "download"}, required=("url",))
    url = _text(data, "url")
    if not is_url(url):
        raise InventoryError(f"'{url}' is not a link (http://, https:// or ftp://)")
    kind = _kind(data.get("kind"))
    download = data.get("download", True)
    if not isinstance(download, bool):
        raise InventoryError("'download' must be true or false")
    attachment_id = inv.add_attachment(part_id, url, kind)
    result = _attachment(inv.get_attachment(attachment_id))
    if download:
        for stored, error in inv.download_links([attachment_id]):
            result = _attachment(stored)
            if error is not None:
                result["download_error"] = str(error)
    return jsonify(result), 201


# Both pass the part, so a mistyped part id can't change another part's
# attachment; the store checks it in the same statement that makes the change.
@api.post("/parts/<int:part_id>/attachments/<int:attachment_id>/download")
def download_attachment(part_id: int, attachment_id: int):
    return jsonify(_attachment(inventory().download_attachment(attachment_id, part_id)))


@api.delete("/parts/<int:part_id>/attachments/<int:attachment_id>")
def remove_attachment(part_id: int, attachment_id: int):
    inventory().remove_attachment(attachment_id, part_id)
    return "", 204


def _kind(value: object) -> str | None:
    if value is None or value == "":
        return None
    if value not in ATTACHMENT_KINDS:
        raise InventoryError(f"'kind' must be one of {', '.join(ATTACHMENT_KINDS)}")
    return value


@api.post("/parts/<int:part_id>/stock")
def change_stock(part_id: int):
    data = _body({"location", "quantity", "delta", "note"}, required=("location",))
    if ("quantity" in data) == ("delta" in data):
        raise InventoryError("give either 'quantity' (the new amount) or 'delta' (the change), not both")
    location, note = _location_ref(data, "location"), _text(data, "note")
    inv = inventory()
    with _locations_from_body(part_id), inv.transaction():
        if "quantity" in data:
            inv.set_stock(part_id, location, _int(data, "quantity"), note=note)
        else:
            inv.adjust_stock(part_id, location, _int(data, "delta"), note=note)
    return jsonify(_part(inv.get_part(part_id)))


@api.post("/parts/<int:part_id>/move")
def move_stock(part_id: int):
    data = _body({"from", "to", "quantity", "note"}, required=("to",))
    destination, note = _location_ref(data, "to"), _text(data, "note")
    inv = inventory()
    with _locations_from_body(part_id), inv.transaction():
        if "from" in data:
            quantity = _int(data, "quantity", nullable=True) if "quantity" in data else None
            inv.move_stock(part_id, _location_ref(data, "from"), destination, quantity, note=note)
        elif "quantity" in data:
            raise InventoryError("'quantity' needs 'from', the location to take them from")
        else:
            inv.move_all_stock(part_id, destination, note=note)
    return jsonify(_part(inv.get_part(part_id)))


@api.post("/locations")
def add_location():
    data = _body({"path", "description"}, required=("path",))
    inv = inventory()
    location_id = inv.add_location(_location_ref(data, "path"), _text(data, "description"))
    response = jsonify(_location(inv.get_location(location_id)))
    return response, 201, {"Location": url_for("api.location", location_id=location_id, _external=True)}


@api.patch("/locations/<int:location_id>")
def update_location(location_id: int):
    data = _body({"name", "description", "parent"})
    inv = inventory()
    parent = None
    if "parent" in data and data["parent"] is not None:
        parent = _location_ref(data, "parent")
    inv.update_location(
        location_id,  # the id, not its path: the location may be renamed meanwhile
        new_name=_text(data, "name") if "name" in data else None,
        description=_text(data, "description") if "description" in data else None,
        parent=parent,
        top_level="parent" in data and data["parent"] is None,
    )
    return jsonify(_location(inv.get_location(location_id)))


@api.delete("/locations/<int:location_id>")
def delete_location(location_id: int):
    inventory().delete_location(location_id)
    return "", 204


def _set_part_details(part_id: int, data: dict) -> None:
    """Tags, attributes and the minimum quantity, for those given in `data`."""
    inv = inventory()
    if "tags" in data:
        inv.set_tags(part_id, _texts(data, "tags"))
    if "attributes" in data:
        inv.set_attributes(part_id, _text_object(data, "attributes"))
    if "min_quantity" in data:
        inv.set_min_quantity(part_id, _int(data, "min_quantity", nullable=True))


@contextmanager
def _locations_from_body(part_id: int) -> Iterator[None]:
    """A location in the request body that doesn't exist is the client's input
    error (400); 404 is only for things named in the URL, such as the part."""
    try:
        yield
    except NotFoundError as e:
        inventory().get_part(part_id)  # a missing part is still a 404
        raise InventoryError(str(e)) from None


# -- request bodies -----------------------------------------------------------
# Each check names the field, so the message says what is wrong where.


def _body(allowed: set[str], required: tuple[str, ...] = ()) -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise InventoryError("the request body must be a JSON object, sent with 'Content-Type: application/json'")
    _check_keys(data, allowed, required)
    return data


def _check_keys(data: dict, allowed: set[str], required: tuple[str, ...], where: str = "") -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise InventoryError(f"unknown field(s){where}: {', '.join(unknown)}")
    missing = [key for key in required if key not in data]
    if missing:
        raise InventoryError(f"missing field(s){where}: {', '.join(missing)}")


def _part_fields(data: dict, exclude: str | None = None) -> dict[str, str]:
    return {f: _text(data, f) for f in EDITABLE_FIELDS if f in data and f != exclude}


def _text(data: dict, key: str, where: str | None = None) -> str:
    """A string field, without surrounding white space; '' if it is missing."""
    value = data.get(key, "")
    if not isinstance(value, str):
        raise InventoryError(f"'{_name(key, where)}' must be a string")
    return value.strip()


def _int(data: dict, key: str, where: str | None = None, *, nullable: bool = False) -> int | None:
    value = data.get(key)
    if value is None and nullable:
        return None
    if not isinstance(value, int) or isinstance(value, bool):  # True is an int in Python
        raise InventoryError(f"'{_name(key, where)}' must be a whole number{' or null' if nullable else ''}")
    return value


def _texts(data: dict, key: str) -> list[str]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise InventoryError(f"'{key}' must be a list of strings")
    return value


def _text_object(data: dict, key: str) -> dict[str, str]:
    value = data[key]
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise InventoryError(f"'{key}' must be an object whose values are strings")
    return value


def _objects(data: dict, key: str, allowed: set[str], required: tuple[str, ...]) -> list[dict]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise InventoryError(f"'{key}' must be a list of objects")
    for entry in value:
        _check_keys(entry, allowed, required, f" in '{key}'")
    return value


def _location_ref(data: dict, key: str, where: str | None = None) -> str:
    """A location path from a request as an exact reference ('/' + full path), so
    a partial path can't match some other location that ends the same way."""
    return "/" + _text(data, key, where).lstrip("/")


def _name(key: str, where: str | None) -> str:
    return f"{where}.{key}" if where else key


def _number(name: str) -> int | None:
    text = request.args.get(name, "").strip()
    if not text:
        return None
    number = whole_number(text)
    if number is None:
        raise InventoryError(f"'{name}' must be a whole number, got '{text}'")
    return number


# -- conversion to JSON -------------------------------------------------------


def _part(part: Part) -> dict:
    return {
        "id": part.id,
        "name": part.name,
        "description": part.description,
        "manufacturer": part.manufacturer,
        "mpn": part.mpn,
        "category": part.category,
        "package": part.package,
        "notes": part.notes,
        "min_quantity": part.min_quantity,
        "total_quantity": part.total_quantity,
        "low": part.is_low,
        "stock": [{"location": s.location, "quantity": s.quantity} for s in part.stock],
        "tags": part.tags,
        "attributes": part.attributes,
        "attachments": [_attachment(a) for a in part.attachments],
        "created_at": part.created_at,
        "updated_at": part.updated_at,
        "web_url": url_for("ui.part", part_id=part.id, _external=True),
        "api_url": url_for("api.part", part_id=part.id, _external=True),
    }


def _attachment(attachment: Attachment) -> dict:
    return {
        "id": attachment.id,
        "kind": attachment.kind,
        "name": attachment.original_name,
        "stored": attachment.url is None,  # a file kept with the database, or only a link
        "url": attachment.url or url_for("ui.attachment", attachment_id=attachment.id, _external=True),
        "source_url": attachment.source_url,
        "added_at": attachment.added_at,
    }


def _location(location: Location) -> dict:
    return {
        "id": location.id,
        "name": location.name,
        "path": location.path,
        "parent": location.path.rpartition("/")[0] or None,
        "description": location.description,
        "part_count": location.part_count,
        "total_quantity": location.total_quantity,
        "parts_url": url_for("api.parts", location="/" + location.path, _external=True),
        "api_url": url_for("api.location", location_id=location.id, _external=True),
    }


def _change(change: StockChange) -> dict:
    return {
        "id": change.id,
        "at": change.at,
        "part_id": change.part_id,
        "part_name": change.part_name,
        "location": change.location,
        "delta": change.delta,
        "quantity": change.quantity,
        "action": change.action,
        "note": change.note,
    }
