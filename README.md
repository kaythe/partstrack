# partstrack

![Tests](https://github.com/kaythe/partstrack/actions/workflows/tests.yml/badge.svg)

A small inventory for electronic parts, backed by SQLite. It records what you
have, how many, where they are stored (in nested locations), and attributes
such as datasheets, pictures, tags and free-form key/value attributes. Parts can
be imported from and exported to CSV.

The command-line app uses only the Python standard library (Python 3.10+). The
optional web interface needs Flask and waitress, and the optional desktop app
needs PySide6 (Qt).

## Install / run

```sh
python3 -m partstrack --help          # run from the source folder
pip install -e .                      # or install the `partstrack` command
```

The database lives at `~/.local/share/partstrack/parts.db` by default. Override
it with `--db FILE` or the `PARTSTRACK_DB` environment variable. Attached files
are copied into an `attachments/` folder next to the database, so back up that
whole folder.

## Examples

```sh
partstrack add NE555 -d "Precision timer" -m TI --mpn NE555P -c IC -p DIP-8 \
    -q 20 -l "Drawer A3" -t timer -t tht -a vcc=4.5-16V \
    --datasheet ~/Downloads/ne555.pdf --image https://example.com/ne555.jpg

partstrack stock NE555 "Drawer A3" -3     # take 3   (+N adds, N sets)
partstrack move NE555 "Drawer A3" Bench 5 # move 5 (omit the number to move all)
partstrack ls                             # list everything
partstrack search 0805 10k                # all words must match
partstrack ls -t smd -l "Drawer A3"       # filter by tag / location / category (-c)
partstrack show NE555
partstrack edit NE555 -t opamp --untag tht -a package-marking=NE555
partstrack attach NE555 photo.jpg         # kind guessed from extension, or -k
partstrack attach NE555 https://example.com/ne555.pdf  # downloaded and stored
partstrack detach 3                       # attachment id from `show`
partstrack download                       # store all linked files locally
partstrack download NE555 --dry-run       # ... or one part's; just list them
partstrack tags
partstrack rm NE555
```

Links that end in a file name (`…/ne555.pdf`, `…/pinout.png?v=2`) are
downloaded and stored like uploaded files, and the original link is
remembered. This happens in `add`, `attach`, `import` and the web interface.
Links to web pages (`…/products/ne555`, `…/index.html`) stay links. So do links
whose download fails; you get a warning and can retry later with
`partstrack download`. Use `--link` to keep a link to a file as a link. For
safety, files are never downloaded from the server itself or from link-local
addresses.

Parts are referred to by name (case-insensitive) or by id. Use `#12` to force
an id lookup when a part's name is itself a number. For that reason, part
names cannot start with `#`.

## Locations

Locations can be nested and are written as paths:

```sh
partstrack loc add "Lab/Cabinet A/Drawer 3" -d "SMD ICs"  # parents are created too
partstrack stock NE555 "Drawer 3" +10   # the last part is enough if it's unambiguous
partstrack stock NE555 "/Drawer 3" +10  # leading '/': exactly this top-level path
partstrack locs                         # show the tree
partstrack ls -l "Cabinet A"            # parts in Cabinet A or anywhere inside it
partstrack loc edit "Cabinet A" --rename "Cabinet 1" --parent Storeroom
partstrack loc edit "Cabinet 1" --top-level
partstrack loc rm "Drawer 3"            # only when empty
```

Locations are created automatically when you first put stock into them.

## CSV import and export

```sh
partstrack export -o parts.csv                      # or to stdout without -o
partstrack export -o parts.csv --delimiter ";" --bom  # for German-locale Excel
partstrack import parts.csv --dry-run               # check only
partstrack import parts.csv
partstrack import order.csv --add-stock -l Incoming # e.g. a distributor order
```

The CSV has one row per part and location. Columns are `name`, `description`,
`manufacturer`, `mpn`, `category`, `package`, `location`, `quantity`, `tags`,
`datasheets`, `images`, `other_attachments`, `notes`, plus one `attr:<name>`
column per attribute. Lists within a cell are separated by `;`. Exported
locations are full paths starting with `/`, so importing them never lands in a
different location that merely ends with the same name.

When importing:
- Only `name` is required. Unknown columns are ignored with a warning, and a
  few common header names (`Qty`, `Part Number`, ...) are recognised.
- Files may be UTF-8 or Windows-1252 (what Excel writes in Western European
  locales).
- Parts are matched by name. New parts are created and existing ones updated.
  Empty cells never clear existing values.
- Quantities replace the stock at that location, or are added to it with
  `--add-stock`. Rows without a location use `-l/--location`.
- Attachment paths may be relative to the CSV file. Attachments that are
  already present are not added again, so export → edit → import is safe.
- The import is all-or-nothing. If any row has an error, nothing is saved.

## Web interface

A browser interface for use on a local network, served by the machine that
holds the database:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[web]'
.venv/bin/partstrack-web                    # http://<server>:8080/, all interfaces
.venv/bin/partstrack-web --port 9000 --db /srv/partstrack/parts.db
.venv/bin/partstrack-web --host 127.0.0.1   # this machine only
```

It covers the same features as the CLI: searching and filtering, adding and
editing parts, taking, adding and moving stock, uploading pictures and
datasheets (pictures are shown on the part's page), the location tree, and CSV
import/export. The header has a switch for light mode, dark mode, or following
the device's setting, and an About page shows the version. The CLI and the web interface can be used on the same database
at the same time.

**There is no login.** Anyone who can reach the port can view and change the
inventory, so only run it on a network you trust. For the same reason, CSV
imports through the browser only accept links as attachments, never file paths
on the server.

To run it as a service with systemd, use a unit like this (adjust user and
paths):

```ini
[Unit]
Description=partstrack web interface
After=network.target

[Service]
User=partstrack
ExecStart=/opt/partstrack/.venv/bin/partstrack-web --db /srv/partstrack/parts.db --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Desktop app

A native window for Windows and Linux, working directly on the database:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[desktop]'      # Windows: .venv\Scripts\pip install -e .[desktop]
.venv/bin/partstrack-desktop                 # or: partstrack-desktop --db D:\parts\parts.db
```

The Parts tab has a searchable, filterable and sortable table, with the
selected part's details, stock, pictures and attachments next to it.
Double-click a part to edit it, a stock entry to change it, or an attachment
to open it. The Locations tab shows the location tree. CSV import shows what a
file contains before anything is saved. **File → Open database…** switches to
another database file (or creates one), and the app remembers it. **View →
Theme** chooses light, dark or the system's setting.

The desktop app, the CLI and the web interface can all be used on the same
database at the same time. The desktop app doesn't notice changes made
elsewhere until you press F5 (View → Refresh).

## Layout

- `partstrack/store.py` holds the `Inventory` class with all database logic.
  It returns dataclasses (`Part`, `Location`, `Attachment`, ...) and raises
  `InventoryError` for user errors. It never prints, so a GUI can use it
  directly. Wrap several calls in `with inv.transaction():` to save them
  all-or-nothing. Transactions nest, and a failed inner block is undone on its
  own.
- `partstrack/csvio.py` contains CSV import/export on top of `Inventory`.
- `partstrack/cli.py` is the command-line front end, a thin layer over `Inventory`.
- `partstrack/web/` holds the web front end: a Flask app in `app.py`, plus
  Jinja templates and a stylesheet.
- `partstrack/desktop/app.py` is the desktop front end (PySide6).
- Schema changes go into `MIGRATIONS` in `store.py`. They are applied
  automatically using SQLite's `user_version`.

## Tests

```sh
python3 -m unittest -v          # web and desktop tests are skipped without Flask / PySide6
.venv/bin/python -m unittest -v # everything, with the web and desktop extras installed
```
