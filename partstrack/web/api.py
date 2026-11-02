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

"""A read-only JSON API for scripts and other programs, under /api.

Every route is a GET, so nothing here can change the inventory; the web
interface's protection against forged requests (see app.py) only needs to
cover its forms. No CORS headers are sent, so other websites can't read the
inventory through a visitor's browser. Attachments are given as links to the
web interface, never as file paths on the server.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from .. import __version__
from ..store import Attachment, InventoryError, Location, NotFoundError, Part, StockChange
from .app import inventory

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
def _http_error(e: HTTPException):
    """Unknown URLs and methods: JSON under /api (no route matched, so the
    blueprint's own handlers don't apply), the usual pages elsewhere."""
    if request.path != "/api" and not request.path.startswith("/api/"):
        return e
    message = "no such API endpoint; see /api" if e.code == 404 else "the API is read-only; only GET is allowed"
    return jsonify(error=message), e.code


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


def _number(name: str) -> int | None:
    text = request.args.get(name, "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise InventoryError(f"'{name}' must be a whole number, got '{text}'")
    return int(text)


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
