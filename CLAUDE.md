# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

partstrack is an inventory of electronic parts: what you have, how many, where it is stored (nested locations), and tags, key/value attributes and attachments (datasheets, pictures). It is backed by SQLite (Python ≥ 3.10). There are two front ends: a CLI that uses only the standard library, and a web interface for the local network (Flask + waitress, installed as the optional `web` extra). `README.md` documents user-facing behaviour, including the CSV format and how to run the web server.

## Commands

```sh
python3 -m unittest -v                                                  # all tests
python3 -m unittest tests.test_store                                    # one module
python3 -m unittest tests.test_store.InventoryTest.test_nested_locations  # one test
python3 -m partstrack --help                                            # run the CLI from source
PARTSTRACK_DB=/tmp/x/parts.db python3 -m partstrack ...                 # use a scratch database

python3 -m venv .venv && .venv/bin/pip install -e '.[web,desktop]'      # web and desktop dependencies
.venv/bin/python -m unittest                                            # all tests incl. test_web, test_desktop
.venv/bin/partstrack-web --db /tmp/x/parts.db --port 8765 --debug        # web dev server with reload
.venv/bin/partstrack-desktop --db /tmp/x/parts.db                        # desktop app
```

Without `--db` or `PARTSTRACK_DB`, the front ends use the user's real database at `~/.local/share/partstrack/parts.db`, so always point manual testing at a scratch path. The version number lives only in `partstrack/__init__.py` (`pyproject.toml` reads it from there); user-visible changes go into the Unreleased section of `CHANGELOG.md`. The project is licensed GPL-3.0-or-later: every source file (Python, JS, CSS, templates, scripts, systemd units) starts with the same license notice as a comment in its own syntax (`#`, `/* */`, `{# … -#}` in templates so no blank line is output, after the shebang in scripts). Copy it from an existing file of the same type when adding one. No linter, formatter or pytest is configured. Tests use `unittest` and temporary directories. Plain `python3 -m unittest` skips the web and desktop tests when Flask or PySide6 is missing. That only works because `partstrack/web/__init__.py` and `partstrack/desktop/__init__.py` import nothing; test discovery imports every package, so keep Flask and Qt imports in the `app.py` modules.

## Architecture

There are three layers, and dependencies only point downwards:

- `partstrack/store.py`: `Inventory` holds **all** domain logic and database access. It returns dataclasses (`Part`, `Location`, `StockEntry`, `Attachment`) and raises `InventoryError` (or `NotFoundError`), whose messages are meant to be shown to users as they are. It never prints or prompts.
- `partstrack/csvio.py`: CSV import and export, built only on the public `Inventory` API. Its module docstring specifies the format and the import semantics.
- `partstrack/cli.py`: an argparse front end. Each `cmd_*` function parses input, calls `Inventory` or `csvio`, and formats output. `main()` turns `InventoryError` into `error: …` and exit code 1.
- `partstrack/web/app.py`: a Flask front end (see "Web interface" below), with Jinja templates in `web/templates/` and CSS in `web/static/`.
- `partstrack/desktop/app.py`: a Qt (PySide6) front end (see "Desktop app" below).

Logic that more than one front end needs belongs in `store.py` or `csvio.py`, never in a front end.

### Things that span several files

- **Transactions.** Every write in `Inventory` runs inside `self.transaction()`. Transactions nest: the outermost level is an explicit `BEGIN`/commit, and inner levels are SAVEPOINTs, so a failed inner block is undone on its own. Front ends wrap multi-step operations (e.g. `cmd_add`, `import_csv`) in `inv.transaction()` so they are all-or-nothing. File-system side effects are not transactional: register them with `_after_commit` (e.g. deleting files) or `_after_rollback` (e.g. removing a copied file) instead of doing them directly. Never use `with self.conn:` for new code.
- **Location references** are strings, not ids: `Cabinet A/Drawer 3`. A trailing part (`Drawer 3`) matches if it is unique; if several match, the call raises `InventoryError`. A leading `/` means an exact full path. Stock operations create missing locations automatically. `Location.path` and `StockEntry.location` are full paths *without* a leading `/`. When code passes a known location back in, it must prefix `/` (see `csvio.export_csv`); otherwise suffix matching can pick a different location.
- **Part references.** `find_part()` accepts a name (case-insensitive) or an id, and `#12` forces an id lookup, which is why part names may not start with `#`. Programmatic code should use part ids, and `part_id_by_name()` for exact name lookups.
- **Case-insensitivity.** Names (parts, locations, tags, attribute keys) use SQLite `COLLATE NOCASE`, which folds ASCII letters only. Python-side comparisons that must agree with the database's uniqueness must use `store.nocase()`, not `.lower()` or `.casefold()`. `casefold` is used only for display sort order.
- **Part fields.** `EDITABLE_FIELDS` is the single list of text columns on `parts`. Search and `cmd_edit` derive from it. Adding a field also needs a migration, an option in the `add` and `edit` subparsers, a keyword in `cmd_add`, and an entry in `csvio.COLUMNS`.
- **Attachments.** Local files are copied to `<db dir>/attachments/<part_id>/` and stored as paths relative to that folder. URLs are stored as they are. `Attachment.path` is absolute and `Attachment.target` is the path or URL to open or show. Back up the database folder, not only the `.db` file.
- **Downloads.** `download_attachment()` turns a link that `downloadable_name()` accepts (the URL path ends in a file name that isn't a page like `.html`) into a stored file, and keeps the link in `source_url`. It runs network I/O, so call it (or `download_links()`) **outside** `transaction()`; an open transaction would block other writers for the whole download. Front ends add links inside their transaction, collect the new attachment ids (`ImportResult.link_ids` for CSV), and call `download_links()` after the commit; failures leave the link and become warnings. `_check_download_url` refuses loopback, link-local and reserved addresses, including on every redirect, so the web interface can't be used to reach services that only the server can see. Tests serve files with `tests/fileserver.py`, which patches `Inventory.allow_local_downloads`. Never let tests hit the internet.

### Schema migrations

`PRAGMA user_version` holds the schema version, and `MIGRATIONS` in `store.py` is applied in order on open. Entries are SQL scripts or functions taking the connection. Only append, never edit existing entries. During migrations foreign keys are off (so tables can be rebuilt), each migration runs in its own transaction, and `PRAGMA foreign_key_check` must pass before commit. Upgrades from older versions are tested by building the old schema from `MIGRATIONS[:n]` (see `MigrationTest`).

## Web interface

`partstrack/web/app.py` is a Flask app built by `create_app(db_path)`, with all routes on the `ui` blueprint. In production, `main()` (the `partstrack-web` entry point) serves it with waitress, by default on `0.0.0.0:8080`. Its conventions:

- **One `Inventory` per request**, created lazily by `inventory()` and closed on teardown. An `sqlite3` connection may only be used by the thread that created it, and waitress uses a thread pool. Never keep an `Inventory` in module or app state. `main()` opens the database once before serving, so migrations don't run from concurrent requests.
- **Pages are server-rendered.** JavaScript is only an optional extra: `confirm()` on deletes, and `static/tags.js`, which suggests existing tags in fields marked `data-tags='{{ tags|tojson }}'` (single quotes, because `tojson` output contains double quotes). Every page must work without JavaScript. Forms post to an action route that redirects (post/redirect/get). The `@action` decorator turns `InventoryError` into a flashed message and a redirect back to the referrer. Add and edit forms don't use it; they re-render with the submitted values so input isn't lost. `NotFoundError` on a GET renders `error.html` with a 404.
- **Theme.** Colours are CSS custom properties in `static/style.css`. The dark values appear twice: once for `data-theme="dark"`, and once for "device prefers dark and no theme chosen". Keep both blocks identical. The header switch posts to `/theme`, which stores a `theme` cookie (`light`/`dark`; absent means follow the device). A context processor exposes it as `theme` in templates, so `<html data-theme>` is set server-side and pages never flash the wrong theme.
- **Identity.** Templates put part ids and `/`-prefixed location paths (`"/" ~ l.path`) into URLs and hidden fields, never bare names, because bare names may resolve to a different location by suffix matching.
- **Multi-step form handling** goes inside `inv.transaction()`, so a failure in any step saves nothing. `Inventory.set_tags` and `set_attributes` exist for the edit form's replace-all semantics.
- **Security.** There is no authentication by design (trusted local network). Keep these protections: Jinja autoescaping (never use `|safe` on stored data); uploaded file names reduced to a bare name by `_safe_filename`; attachments served with `Content-Security-Policy: sandbox` (except PDFs) so uploaded HTML or SVG can't run scripts; and `import_csv(..., allow_files=False)` for uploaded CSVs, so a CSV can't attach arbitrary files from the server.
- **Performance.** `list_parts()` still runs about 4 queries per part (`get_part` per row, plus a recursive location CTE each time), and the parts page calls it on every load. If large inventories feel slow, replace it with bulk loading: one query per table keyed by `part_id`, and location paths computed once.
- **Dependencies.** The CLI must stay dependency-free. Web and desktop dependencies live only in the `web` and `desktop` extras in `pyproject.toml`. New template or static file types must match the `package-data` patterns there, or they won't be installed.

## Desktop app

`partstrack/desktop/app.py` is a PySide6 app (the `desktop` extra, entry point `partstrack-desktop`, a GUI script so Windows opens no console). It follows the same rules as the web interface, adapted to Qt:

- **Threads.** `MainWindow.inv` is used only by the GUI thread. Downloads run in `Downloader`, a plain `threading.Thread` that opens its own `Inventory` and reports through a Qt signal connected to a `MainWindow` method, so the slot runs in the GUI thread. Never connect a worker's signal to a lambda; it would run in the worker thread.
- **Refreshing.** There are no change notifications, so every write is followed by `refresh()`, which re-reads the filter combos, the parts table, the location tree and the shown part. `attempt()` runs a write, shows an `InventoryError` and refreshes either way. The part view is rebuilt from scratch on every refresh.
- **Dialogs** subclass `SavingDialog`: `save()` does the work inside `inv.transaction()`, and `accept()` shows an `InventoryError` and keeps the dialog open with the input. Results the window needs afterwards (new part id, link ids to download) are stored on the dialog only after the transaction commits. Show every dialog and message box with `run_dialog()`, never `exec()` directly: it deletes the dialog afterwards. A dialog left alive as a hidden child of the window makes the interpreter crash on exit (segfault or bus error).
- **Identity.** `LocationBox` returns known locations as `/`-paths and typed text as it is; table and tree items keep part ids and location paths in `Qt.ItemDataRole.UserRole`.
- **Theme.** `apply_theme()` asks the platform for a colour scheme and, where the platform ignores the request, falls back to Fusion with `dark_palette()`. The choice is stored in `QSettings`, as are the window geometry, the last database opened and the last folder used.
- **Tests** (`tests/test_desktop.py`) run with `QT_QPA_PLATFORM=offscreen`, drive dialogs by setting widget values and calling `accept()`, and replace the module functions `show_error` and `confirm` so no message box blocks. Pass a `QSettings` file in the temporary directory to `MainWindow`, so tests never touch the user's settings. For a visual check, `widget.grab().save("x.png")` works offscreen too.
