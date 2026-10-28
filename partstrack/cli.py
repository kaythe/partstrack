"""Command-line interface for the parts inventory."""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

from . import __version__
from .csvio import decode_csv, export_csv, import_csv
from .store import ATTACHMENT_KINDS, EDITABLE_FIELDS, Inventory, InventoryError, Part


def default_db_path() -> Path:
    if env := os.environ.get("PARTSTRACK_DB"):
        return Path(env)
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "partstrack" / "parts.db"


# -- output helpers -----------------------------------------------------------


def print_table(headers: list[str], rows: list[list[object]]) -> None:
    cells = [[str(c) for c in row] for row in rows]
    widths = [max([len(h)] + [len(row[i]) for row in cells]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())


def print_part(part: Part) -> None:
    print(f"#{part.id}  {part.name}")
    for label, value in [
        ("Description", part.description),
        ("Manufacturer", part.manufacturer),
        ("MPN", part.mpn),
        ("Category", part.category),
        ("Package", part.package),
        ("Tags", ", ".join(part.tags)),
    ]:
        if value:
            print(f"  {label + ':':<14}{value}")
    if part.attributes:
        print("  Attributes:")
        for key, value in part.attributes.items():
            print(f"    {key}: {value}")
    print(f"  Stock (total {part.total_quantity}):")
    for entry in part.stock:
        print(f"    {entry.location}: {entry.quantity}")
    if part.attachments:
        print("  Attachments:")
        for a in part.attachments:
            source = f"  (from {a.source_url})" if a.source_url else ""
            print(f"    [{a.id}] {a.kind:<9}  {a.target}{source}")
    if part.notes:
        print("  Notes:")
        for line in part.notes.splitlines():
            print(f"    {line}")
    print(f"  Added {part.created_at}, updated {part.updated_at} (UTC)")


def report_downloads(results) -> int:
    """Print the outcome of Inventory.download_links(); returns the number of failures."""
    failures = 0
    for attachment, error in results:
        if error is None:
            print(f"Downloaded {attachment.original_name} [{attachment.id}]")
        else:
            failures += 1
            print(f"warning: kept [{attachment.id}] as a link: {error}", file=sys.stderr)
    return failures


def parse_attribute(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise InventoryError(f"attribute must look like KEY=VALUE, got '{text}'")
    return key.strip(), value.strip()


# -- commands -----------------------------------------------------------------


def cmd_add(inv: Inventory, args: argparse.Namespace) -> None:
    if args.qty and not args.location:
        raise InventoryError("--qty requires --location")
    attributes = [parse_attribute(a) for a in args.attr]
    with inv.transaction():
        part_id = inv.add_part(
            args.name,
            description=args.description,
            manufacturer=args.manufacturer,
            mpn=args.mpn,
            category=args.category,
            package=args.package,
            notes=args.notes,
        )
        if args.tag:
            inv.add_tags(part_id, args.tag)
        for key, value in attributes:
            inv.set_attribute(part_id, key, value)
        if args.qty:
            inv.set_stock(part_id, args.location, args.qty)
        attachment_ids = [inv.add_attachment(part_id, source, "datasheet") for source in args.datasheet]
        attachment_ids += [inv.add_attachment(part_id, source, "image") for source in args.image]
        attachment_ids += [inv.add_attachment(part_id, source) for source in args.attach]
    print(f"Added part #{part_id} '{args.name}'")
    if not args.link:
        report_downloads(inv.download_links(attachment_ids))


def cmd_list(inv: Inventory, args: argparse.Namespace) -> None:
    parts = inv.list_parts(
        " ".join(args.query), tag=args.tag, location=args.location, category=args.category
    )
    if not parts:
        print("No parts found.")
        return
    print_table(
        ["ID", "Name", "Qty", "Category", "Package", "Location(s)", "Tags"],
        [
            [
                p.id,
                p.name,
                p.total_quantity,
                p.category,
                p.package,
                ", ".join(f"{s.location} ({s.quantity})" for s in p.stock),
                ", ".join(p.tags),
            ]
            for p in parts
        ],
    )


def cmd_show(inv: Inventory, args: argparse.Namespace) -> None:
    print_part(inv.get_part(inv.find_part(args.part)))


def cmd_edit(inv: Inventory, args: argparse.Namespace) -> None:
    part_id = inv.find_part(args.part)
    fields = {name: getattr(args, name) for name in EDITABLE_FIELDS if getattr(args, name) is not None}
    attributes = [parse_attribute(a) for a in args.attr]
    with inv.transaction():
        inv.update_part(part_id, **fields)
        if args.tag:
            inv.add_tags(part_id, args.tag)
        if args.untag:
            inv.remove_tags(part_id, args.untag)
        for key, value in attributes:
            inv.set_attribute(part_id, key, value)
        for key in args.unattr:
            inv.remove_attribute(part_id, key)
    print_part(inv.get_part(part_id))


def cmd_rm(inv: Inventory, args: argparse.Namespace) -> None:
    part = inv.get_part(inv.find_part(args.part))
    if not args.yes:
        answer = input(f"Delete part #{part.id} '{part.name}' and its attachments? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return
    inv.delete_part(part.id)
    print(f"Deleted part #{part.id} '{part.name}'")


def cmd_stock(inv: Inventory, args: argparse.Namespace) -> None:
    part_id = inv.find_part(args.part)
    amount = args.amount.strip()
    try:
        number = int(amount.lstrip("+="))
    except ValueError:
        raise InventoryError(f"amount must be +N, -N or N, got '{amount}'") from None
    with inv.transaction():
        if amount.startswith(("+", "-")):
            new = inv.adjust_stock(part_id, args.location, number)
        else:
            inv.set_stock(part_id, args.location, number)
            new = number
        location = inv.location_path(args.location) if new else args.location
    part = inv.get_part(part_id)
    print(f"{part.name}: {new} in '{location}', {part.total_quantity} total")


def cmd_move(inv: Inventory, args: argparse.Namespace) -> None:
    part_id = inv.find_part(args.part)
    with inv.transaction():
        moved = inv.move_stock(part_id, args.source, args.destination, args.qty)
        destination = inv.location_path(args.destination)
    print(f"Moved {moved} from '{args.source}' to '{destination}'")


def cmd_attach(inv: Inventory, args: argparse.Namespace) -> None:
    part_id = inv.find_part(args.part)
    attachment_id = inv.add_attachment(part_id, args.source, args.kind)
    results = [] if args.link else inv.download_links([attachment_id])
    if results and results[0][1] is None:
        attachment = results[0][0]
        print(f"Downloaded {attachment.kind} [{attachment.id}]: {attachment.target}")
        return
    report_downloads(results)
    attachment = inv.get_attachment(attachment_id)
    print(f"Added {attachment.kind} [{attachment.id}]: {attachment.target}")


def cmd_download(inv: Inventory, args: argparse.Namespace) -> None:
    part_id = inv.find_part(args.part) if args.part else None
    candidates = inv.downloadable_attachments(part_id)
    if not candidates:
        print("No links to files found.")
        return
    if args.dry_run:
        for a in candidates:
            print(f"[{a.id}] {inv.get_part(a.part_id).name}: {a.url}")
        return
    failures = report_downloads(inv.download_links(a.id for a in candidates))
    if failures:
        raise InventoryError(f"{failures} of {len(candidates)} download(s) failed; those stay links")
    print(f"Stored {len(candidates)} file(s) locally.")


def cmd_detach(inv: Inventory, args: argparse.Namespace) -> None:
    inv.remove_attachment(args.attachment_id)
    print(f"Removed attachment [{args.attachment_id}]")


def cmd_locations(inv: Inventory, args: argparse.Namespace) -> None:
    locations = inv.list_locations()
    if not locations:
        print("No locations yet.")
        return
    print_table(
        ["Location", "Parts", "Items", "Description"],
        [["  " * l.depth + l.name, l.part_count, l.total_quantity, l.description] for l in locations],
    )


def cmd_location_add(inv: Inventory, args: argparse.Namespace) -> None:
    location_id = inv.add_location(args.location, args.description)
    print(f"Added location '{inv.location_path(location_id)}'")


def cmd_location_edit(inv: Inventory, args: argparse.Namespace) -> None:
    inv.update_location(
        args.location,
        new_name=args.rename,
        description=args.description,
        parent=args.parent,
        top_level=args.top_level,
    )
    print("Updated location.")


def cmd_location_rm(inv: Inventory, args: argparse.Namespace) -> None:
    path = inv.location_path(args.location)
    inv.delete_location(args.location)
    print(f"Deleted location '{path}'")


def cmd_tags(inv: Inventory, args: argparse.Namespace) -> None:
    tags = inv.list_tags()
    if not tags:
        print("No tags yet.")
        return
    print_table(["Tag", "Parts"], [[name, count] for name, count in tags])


def cmd_export(inv: Inventory, args: argparse.Namespace) -> None:
    encoding = "utf-8-sig" if args.bom else "utf-8"
    if args.output is None:
        sys.stdout.reconfigure(encoding=encoding, newline="")
        export_csv(inv, sys.stdout, delimiter=args.delimiter)
        return
    with open(args.output, "w", encoding=encoding, newline="") as f:
        count = export_csv(inv, f, delimiter=args.delimiter)
    print(f"Exported {count} part(s) to {args.output}")


def cmd_import(inv: Inventory, args: argparse.Namespace) -> None:
    try:
        data = args.file.read_bytes()
    except OSError as e:
        raise InventoryError(f"cannot read {args.file}: {e.strerror}") from None
    result = import_csv(
        inv,
        io.StringIO(decode_csv(data), newline=""),
        base_dir=args.file.parent,
        add_stock=args.add_stock,
        default_location=args.location,
        delimiter=args.delimiter,
        dry_run=args.dry_run,
    )
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if not args.link:
        report_downloads(inv.download_links(result.link_ids))
    print(
        f"{'Checked' if args.dry_run else 'Imported'} {result.rows} row(s):"
        f" {len(result.created)} new part(s), {len(result.updated)} updated"
        + (" (dry run, nothing was saved)" if args.dry_run else "")
    )


# -- argument parsing ---------------------------------------------------------

LINK_HELP = "keep links as links instead of downloading the files they point to"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partstrack",
        description="Keep track of your electronic parts.",
        epilog="Parts can be referred to by name or id; use '#ID' to force an id lookup. "
               "Locations are paths like 'Cabinet A/Drawer 3'; the last part ('Drawer 3') "
               "is enough if it is unambiguous, and a leading '/' means a full path.",
    )
    parser.add_argument("--db", type=Path, default=None,
                        help=f"database file (default: $PARTSTRACK_DB or {default_db_path()})")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("add", help="add a new part")
    p.add_argument("name")
    p.add_argument("-d", "--description", default="")
    p.add_argument("-m", "--manufacturer", default="")
    p.add_argument("--mpn", default="", help="manufacturer part number")
    p.add_argument("-c", "--category", default="")
    p.add_argument("-p", "--package", default="", help="e.g. 0805, SOIC-8, TO-220")
    p.add_argument("-n", "--notes", default="")
    p.add_argument("-q", "--qty", type=int, default=0, help="initial quantity (needs --location)")
    p.add_argument("-l", "--location", help="where the initial quantity is stored")
    p.add_argument("-t", "--tag", action="append", default=[], help="tag (repeatable)")
    p.add_argument("-a", "--attr", action="append", default=[], metavar="KEY=VALUE",
                   help="custom attribute, e.g. resistance=10k (repeatable)")
    p.add_argument("--datasheet", action="append", default=[], metavar="FILE|URL",
                   help="datasheet file or link (repeatable)")
    p.add_argument("--image", action="append", default=[], metavar="FILE|URL",
                   help="picture file or link (repeatable)")
    p.add_argument("--attach", action="append", default=[], metavar="FILE|URL",
                   help="other attachment; kind guessed from extension (repeatable)")
    p.add_argument("--link", action="store_true", help=LINK_HELP)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", aliases=["ls", "search"], help="list or search parts")
    p.add_argument("query", nargs="*", help="words that must all match (name, MPN, tags, attributes, ...)")
    p.add_argument("-t", "--tag", help="only parts with this tag")
    p.add_argument("-l", "--location", help="only parts stored at this location or inside it")
    p.add_argument("-c", "--category", help="only parts in this category")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show all details of a part")
    p.add_argument("part")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("edit", help="change a part's fields, tags or attributes")
    p.add_argument("part")
    p.add_argument("--name")
    p.add_argument("-d", "--description")
    p.add_argument("-m", "--manufacturer")
    p.add_argument("--mpn")
    p.add_argument("-c", "--category")
    p.add_argument("-p", "--package")
    p.add_argument("-n", "--notes")
    p.add_argument("-t", "--tag", action="append", default=[], help="add tag (repeatable)")
    p.add_argument("--untag", action="append", default=[], metavar="TAG", help="remove tag (repeatable)")
    p.add_argument("-a", "--attr", action="append", default=[], metavar="KEY=VALUE",
                   help="set attribute (repeatable)")
    p.add_argument("--unattr", action="append", default=[], metavar="KEY", help="remove attribute (repeatable)")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("rm", help="delete a part")
    p.add_argument("part")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("stock", help="change the quantity of a part at a location",
                       description="AMOUNT is +N to add, -N to take, or N to set the quantity. "
                                   "Locations are created automatically.")
    p.add_argument("part")
    p.add_argument("location")
    p.add_argument("amount")
    p.set_defaults(func=cmd_stock)

    p = sub.add_parser("move", help="move a part from one location to another")
    p.add_argument("part")
    p.add_argument("source")
    p.add_argument("destination")
    p.add_argument("qty", type=int, nargs="?", help="how many (default: all)")
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("attach", help="attach a datasheet, picture or other file/link")
    p.add_argument("part")
    p.add_argument("source", metavar="FILE|URL", help="files are copied into the database folder")
    p.add_argument("-k", "--kind", choices=ATTACHMENT_KINDS, help="default: guessed from extension")
    p.add_argument("--link", action="store_true", help=LINK_HELP)
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("download", help="replace links to files with downloaded copies",
                       description="Download the files that link attachments point to (links ending in a "
                                   "file name, e.g. .../ne555.pdf) and store them locally. Links to web "
                                   "pages stay links, as do links whose download fails.")
    p.add_argument("part", nargs="?", help="only this part (default: all parts)")
    p.add_argument("--dry-run", action="store_true", help="only list the links that would be downloaded")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("detach", help="remove an attachment (id as shown by 'show')")
    p.add_argument("attachment_id", type=int)
    p.set_defaults(func=cmd_detach)

    p = sub.add_parser("locations", aliases=["locs"], help="show the tree of storage locations")
    p.set_defaults(func=cmd_locations)

    p = sub.add_parser("location", aliases=["loc"], help="add, edit or remove a storage location")
    loc_sub = p.add_subparsers(dest="location_command", required=True, metavar="ACTION")
    lp = loc_sub.add_parser("add", help="add a location, e.g. 'Cabinet A/Drawer 3' (parents are created too)")
    lp.add_argument("location")
    lp.add_argument("-d", "--description", default="")
    lp.set_defaults(func=cmd_location_add)
    lp = loc_sub.add_parser("edit", help="rename, describe or move a location")
    lp.add_argument("location")
    lp.add_argument("--rename", metavar="NEW_NAME")
    lp.add_argument("-d", "--description")
    where = lp.add_mutually_exclusive_group()
    where.add_argument("--parent", metavar="LOCATION", help="move it (and its contents) inside LOCATION")
    where.add_argument("--top-level", action="store_true", help="move it to the top level")
    lp.set_defaults(func=cmd_location_edit)
    lp = loc_sub.add_parser("rm", help="remove an empty location")
    lp.add_argument("location")
    lp.set_defaults(func=cmd_location_rm)

    p = sub.add_parser("tags", help="list tags")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("export", help="export all parts as CSV")
    p.add_argument("-o", "--output", type=Path, help="CSV file to write (default: standard output)")
    p.add_argument("--delimiter", default=",", help="field separator (default: ','; German Excel wants ';')")
    p.add_argument("--bom", action="store_true", help="start the file with a UTF-8 BOM so Excel detects the encoding")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="import parts from CSV (all or nothing)",
                       description="Parts are matched by name: new ones are created, existing ones updated. "
                                   "Empty cells leave existing values alone. Quantities replace the stock at "
                                   "the row's location unless --add-stock is given. Columns: "
                                   "name, description, manufacturer, mpn, category, package, location, quantity, "
                                   "tags, datasheets, images, other_attachments, notes and attr:<name>; "
                                   "only 'name' is required. Lists within a cell are separated by ';'.")
    p.add_argument("file", type=Path)
    p.add_argument("--add-stock", action="store_true", help="add quantities to the existing stock instead of replacing it")
    p.add_argument("-l", "--location", help="location for rows that have a quantity but no location")
    p.add_argument("--delimiter", help="field separator (default: guessed from the header)")
    p.add_argument("--dry-run", action="store_true", help="check the file without saving anything")
    p.add_argument("--link", action="store_true", help=LINK_HELP)
    p.set_defaults(func=cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with Inventory(args.db or default_db_path()) as inv:
            args.func(inv, args)
    except InventoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
