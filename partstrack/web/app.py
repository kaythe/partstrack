"""Web interface for the parts inventory.

Needs the optional 'web' dependencies (Flask and waitress):
    pip install -e '.[web]'
    partstrack-web --port 8080

Every request opens its own Inventory, because an SQLite connection may only
be used by the thread that created it and waitress serves requests from a
thread pool. Pages are rendered on the server; forms post to an action URL
and redirect back (post/redirect/get), with messages passed via flash().
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from collections.abc import Callable
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.datastructures import FileStorage

from .. import LOGO_PATH, REPOSITORY_URL, __version__
from ..cli import default_db_path
from ..csvio import decode_csv, export_csv, import_csv
from ..store import (
    ATTACHMENT_KINDS,
    EDITABLE_FIELDS,
    Inventory,
    InventoryError,
    NotFoundError,
    Part,
    downloadable_name,
    is_url,
)

bp = Blueprint("ui", __name__)


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__)
    app.config.update(
        PARTSTRACK_DB=str(db_path),
        # Only used to sign the cookie that carries flash messages.
        SECRET_KEY=os.environ.get("PARTSTRACK_SECRET") or os.urandom(32),
        MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    )
    app.register_blueprint(bp)
    app.add_template_global(downloadable_name)
    app.teardown_appcontext(_close_inventory)
    return app


def inventory() -> Inventory:
    if "inventory" not in g:
        g.inventory = Inventory(current_app.config["PARTSTRACK_DB"])
    return g.inventory


def _close_inventory(exc: BaseException | None) -> None:
    inv = g.pop("inventory", None)
    if inv is not None:
        inv.close()


THEMES = ("light", "dark")
THEME_COOKIE = "theme"


@bp.app_context_processor
def _theme():
    """The colour theme chosen with the switch in the header, or None to follow the device."""
    theme = request.cookies.get(THEME_COOKIE)
    return {"theme": theme if theme in THEMES else None}


@bp.post("/theme")
def set_theme():
    response = redirect(_previous_page())
    theme = request.form.get("theme")
    if theme in THEMES:
        response.set_cookie(THEME_COOKIE, theme, max_age=365 * 24 * 3600, samesite="Lax")
    else:
        response.delete_cookie(THEME_COOKIE)
    return response


@bp.app_errorhandler(NotFoundError)
def _not_found(e: NotFoundError):
    return render_template("error.html", message=str(e)), 404


def action(view: Callable) -> Callable:
    """For form posts: show an InventoryError as a message and go back."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except InventoryError as e:
            flash(str(e), "error")
            return redirect(_previous_page())

    return wrapper


def _previous_page() -> str:
    referrer = request.referrer
    if referrer and referrer.startswith(request.host_url):
        return referrer
    return url_for("ui.parts")


# -- form helpers -------------------------------------------------------------


def _text(name: str) -> str:
    return request.form.get(name, "").replace("\r\n", "\n").strip()


def _parse_int(text: str, what: str, *, allow_empty: bool = False) -> int | None:
    if not text and allow_empty:
        return None
    try:
        number = int(text)
    except ValueError:
        raise InventoryError(f"{what} must be a whole number") from None
    if number < 0:
        raise InventoryError(f"{what} must not be negative")
    return number


def _parse_tags(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _parse_attributes(text: str) -> dict[str, str]:
    attributes = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise InventoryError(f"attributes must look like 'name = value', got '{line.strip()}'")
        attributes[key.strip()] = value.strip()
    return attributes


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip().lstrip(".")
    return name or "upload"


def _save_uploads(inv: Inventory, part_id: int, uploads: list[FileStorage], kind: str | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for upload in uploads:
            if not upload or not upload.filename:
                continue
            path = Path(tmp) / _safe_filename(upload.filename)
            upload.save(path)
            inv.add_attachment(part_id, str(path), kind)


def _save_links(inv: Inventory, part_id: int, text: str, kind: str | None) -> list[int]:
    attachment_ids = []
    for link in text.split():
        if not is_url(link):
            raise InventoryError(f"'{link}' is not a link (http://, https:// or ftp://)")
        attachment_ids.append(inv.add_attachment(part_id, link, kind))
    return attachment_ids


def _download_links(inv: Inventory, attachment_ids: list[int]) -> None:
    """Store the files behind new links locally (outside any transaction);
    links whose download fails are kept, with a message."""
    for _, error in inv.download_links(attachment_ids):
        if error is not None:
            flash(f"Kept a link instead of the file: {error}", "error")


def _form_values(part: Part | None) -> dict[str, str]:
    """Initial values for the part form."""
    if part is None:
        return {}
    values = {name: getattr(part, name) for name in EDITABLE_FIELDS}
    values["tags"] = ", ".join(part.tags)
    values["attributes"] = "\n".join(f"{k} = {v}" for k, v in part.attributes.items())
    return values


def _render_part_form(part: Part | None, values: dict[str, str]):
    inv = inventory()
    return render_template(
        "part_form.html",
        part=part,
        values=values,
        locations=inv.list_locations(),
        categories=inv.list_categories(),
        tags=[name for name, _ in inv.list_tags()],
        kinds=ATTACHMENT_KINDS,
    )


# -- parts --------------------------------------------------------------------


@bp.get("/")
def parts():
    inv = inventory()
    filters = {name: request.args.get(name, "").strip() for name in ("q", "tag", "location", "category")}
    try:
        found = inv.list_parts(
            filters["q"],
            tag=filters["tag"] or None,
            location=filters["location"] or None,
            category=filters["category"] or None,
        )
    except InventoryError as e:
        flash(str(e), "error")
        found = []
    return render_template(
        "parts.html",
        parts=found,
        filters=filters,
        tags=inv.list_tags(),
        locations=inv.list_locations(),
        categories=inv.list_categories(),
    )


@bp.route("/parts/new", methods=["GET", "POST"])
def new_part():
    inv = inventory()
    if request.method == "GET":
        return _render_part_form(None, {})
    try:
        with inv.transaction():
            part_id = inv.add_part(_text("name"), **{f: _text(f) for f in EDITABLE_FIELDS if f != "name"})
            inv.set_tags(part_id, _parse_tags(_text("tags")))
            inv.set_attributes(part_id, _parse_attributes(_text("attributes")))
            quantity = _parse_int(_text("quantity"), "Quantity", allow_empty=True)
            if quantity:
                if not _text("location"):
                    raise InventoryError("please choose where the parts are stored")
                inv.set_stock(part_id, _text("location"), quantity)
            _save_uploads(inv, part_id, request.files.getlist("datasheets"), "datasheet")
            _save_uploads(inv, part_id, request.files.getlist("images"), "image")
            link_ids = _save_links(inv, part_id, _text("links"), None)
    except InventoryError as e:
        flash(str(e), "error")
        return _render_part_form(None, request.form)
    flash(f"Added {_text('name')}.", "success")
    _download_links(inv, link_ids)
    return redirect(url_for("ui.part", part_id=part_id))


@bp.get("/parts/<int:part_id>")
def part(part_id: int):
    inv = inventory()
    return render_template(
        "part.html", part=inv.get_part(part_id), locations=inv.list_locations(), kinds=ATTACHMENT_KINDS
    )


@bp.route("/parts/<int:part_id>/edit", methods=["GET", "POST"])
def edit_part(part_id: int):
    inv = inventory()
    current = inv.get_part(part_id)
    if request.method == "GET":
        return _render_part_form(current, _form_values(current))
    try:
        with inv.transaction():
            inv.update_part(part_id, **{f: _text(f) for f in EDITABLE_FIELDS})
            inv.set_tags(part_id, _parse_tags(_text("tags")))
            inv.set_attributes(part_id, _parse_attributes(_text("attributes")))
    except InventoryError as e:
        flash(str(e), "error")
        return _render_part_form(current, request.form)
    flash("Saved.", "success")
    return redirect(url_for("ui.part", part_id=part_id))


@bp.post("/parts/<int:part_id>/delete")
@action
def delete_part(part_id: int):
    inv = inventory()
    name = inv.get_part(part_id).name
    inv.delete_part(part_id)
    flash(f"Deleted {name}.", "success")
    return redirect(url_for("ui.parts"))


@bp.post("/parts/<int:part_id>/stock")
@action
def change_stock(part_id: int):
    inv = inventory()
    location = _text("location")
    if not location:
        raise InventoryError("please choose a location")
    amount = _parse_int(_text("amount"), "Amount")
    match request.form.get("op"):
        case "add":
            inv.adjust_stock(part_id, location, amount)
        case "take":
            inv.adjust_stock(part_id, location, -amount)
        case "set":
            inv.set_stock(part_id, location, amount)
        case _:
            abort(400)
    return redirect(url_for("ui.part", part_id=part_id))


@bp.post("/parts/<int:part_id>/move")
@action
def move_stock(part_id: int):
    inv = inventory()
    quantity = _parse_int(_text("quantity"), "Quantity", allow_empty=True)
    moved = inv.move_stock(part_id, _text("source"), _text("destination"), quantity)
    flash(f"Moved {moved}.", "success")
    return redirect(url_for("ui.part", part_id=part_id))


@bp.post("/parts/<int:part_id>/attachments")
@action
def add_attachments(part_id: int):
    inv = inventory()
    kind = request.form.get("kind") or None
    if kind is not None and kind not in ATTACHMENT_KINDS:
        abort(400)
    with inv.transaction():
        inv.get_part(part_id)  # 404 before saving uploads for a missing part
        _save_uploads(inv, part_id, request.files.getlist("files"), kind)
        link_ids = _save_links(inv, part_id, _text("links"), kind)
    _download_links(inv, link_ids)
    return redirect(url_for("ui.part", part_id=part_id))


@bp.get("/attachments/<int:attachment_id>")
def attachment(attachment_id: int):
    found = inventory().get_attachment(attachment_id)
    if found.url is not None:
        return redirect(found.url)
    if not found.path.is_file():
        abort(404)
    response = send_file(
        found.path,
        download_name=found.original_name or found.path.name,
        as_attachment=request.args.get("download") == "1",
    )
    if response.mimetype != "application/pdf":
        # Uploaded HTML or SVG files must not be able to run scripts on this site.
        # (PDFs are exempt because browsers' PDF viewers refuse sandboxed pages.)
        response.headers["Content-Security-Policy"] = "sandbox"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.post("/attachments/<int:attachment_id>/download")
@action
def download_attachment(attachment_id: int):
    stored = inventory().download_attachment(attachment_id)
    flash(f"Stored {stored.original_name} locally.", "success")
    return redirect(url_for("ui.part", part_id=stored.part_id))


@bp.post("/attachments/<int:attachment_id>/delete")
@action
def delete_attachment(attachment_id: int):
    inv = inventory()
    part_id = inv.get_attachment(attachment_id).part_id
    inv.remove_attachment(attachment_id)
    return redirect(url_for("ui.part", part_id=part_id))


# -- locations ----------------------------------------------------------------


@bp.get("/locations")
def locations():
    return render_template("locations.html", locations=inventory().list_locations())


@bp.post("/locations")
@action
def add_location():
    inv = inventory()
    location_id = inv.add_location(_text("path"), _text("description"))
    flash(f"Added {inv.location_path(location_id)}.", "success")
    return redirect(url_for("ui.locations"))


@bp.route("/locations/edit", methods=["GET", "POST"])
def edit_location():
    inv = inventory()
    path = inv.location_path(request.args.get("path", ""))
    location = next(l for l in inv.list_locations() if l.path == path)
    parent = path.rpartition("/")[0]
    if request.method == "POST":
        new_parent = _text("parent").strip("/")
        try:
            inv.update_location(
                "/" + path,
                new_name=_text("name"),
                description=_text("description"),
                parent=("/" + new_parent) if new_parent else None,
                top_level=not new_parent,
            )
        except InventoryError as e:
            flash(str(e), "error")
            return render_template("location_form.html", location=location, values=request.form,
                                   locations=inv.list_locations())
        flash("Saved.", "success")
        return redirect(url_for("ui.locations"))
    values = {"name": location.name, "description": location.description, "parent": parent}
    return render_template("location_form.html", location=location, values=values, locations=inv.list_locations())


@bp.post("/locations/delete")
@action
def delete_location():
    inv = inventory()
    path = inv.location_path(_text("path"))
    inv.delete_location("/" + path)
    flash(f"Deleted {path}.", "success")
    return redirect(url_for("ui.locations"))


# -- CSV ----------------------------------------------------------------------


@bp.get("/transfer")
def transfer():
    return render_template("transfer.html", result=None, locations=inventory().list_locations())


@bp.get("/export.csv")
def export():
    out = io.StringIO()
    delimiter = request.args.get("delimiter", ",")
    if delimiter not in (",", ";", "\t"):
        abort(400)
    export_csv(inventory(), out, delimiter=delimiter)
    encoding = "utf-8-sig" if request.args.get("bom") else "utf-8"
    return Response(
        out.getvalue().encode(encoding),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=parts.csv"},
    )


@bp.get("/import")
def import_page():
    return redirect(url_for("ui.transfer"))


@bp.post("/import")
def import_():
    inv = inventory()
    upload = request.files.get("file")
    dry_run = "preview" in request.form
    try:
        if not upload or not upload.filename:
            raise InventoryError("please choose a CSV file")
        result = import_csv(
            inv,
            io.StringIO(decode_csv(upload.read()), newline=""),
            add_stock="add_stock" in request.form,
            default_location=_text("location") or None,
            dry_run=dry_run,
            allow_files=False,
        )
    except InventoryError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.transfer"))
    _download_links(inv, result.link_ids)
    return render_template("transfer.html", result=result, dry_run=dry_run, locations=inv.list_locations())


# -- about --------------------------------------------------------------------


@bp.get("/about")
def about():
    return render_template("about.html", version=__version__, repository=REPOSITORY_URL)


@bp.get("/logo.svg")
def logo():
    return send_file(LOGO_PATH, mimetype="image/svg+xml", max_age=86400)


# -- server -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="partstrack-web", description="Serve the parts inventory as a website.")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"database file (default: $PARTSTRACK_DB or {default_db_path()})")
    parser.add_argument("--host", default="0.0.0.0",
                        help="address to listen on (default: all interfaces; use 127.0.0.1 for this machine only)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true", help="use Flask's auto-reloading development server")
    args = parser.parse_args(argv)

    db_path = args.db or default_db_path()
    try:
        Inventory(db_path).close()  # create or upgrade the database once, before serving
    except InventoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    app = create_app(db_path)
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        from waitress import serve

        print(f"Serving {db_path} at http://{args.host}:{args.port}/", flush=True)
        serve(app, host=args.host, port=args.port)
    return 0
