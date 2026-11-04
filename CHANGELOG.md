# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The parts list in the web interface is split into pages, with links to the
  previous and next page above and below the table. *Show 25 / 50 / 100 / All*
  sets how many parts a page shows (50 by default), and the browser remembers
  the choice. Only the shown page is loaded, so long inventories open quickly.
- In the web interface, *Change all shown* and *Labels for all shown* are now
  called *Change all matching* and *Labels for all matching*. They still act on
  every part the search and filters find, on all pages.

### Fixed

- Numbers written with characters other than the digits 0–9, such as `²`, no
  longer crash the web interface (error 500) or the CLI. In the web interface
  they are ignored, as other text is: in a part, page or copy field of a URL
  or form. The API answers them with "must be a whole number", and
  `partstrack show '#²'` reports that there is no such part.

## [1.1.1] - 2026-11-03

### Changed

- New attachments (added files, uploads and downloaded links) are stored
  under file names that every system accepts: characters such as `:` become
  `_`, device names like `CON` get a leading `_`, and a name that differs
  from an existing one only in case gets a number. The attachment still shows
  its original name.

### Fixed

- Downloading a copy of a server or opening a backup no longer fails with
  "the archive contains something unexpected" when an attachment's file name
  contains characters that Windows doesn't allow, such as `:`. Such files
  (and device names like `CON`, or names that only differ in case) are
  renamed in the copy; the attachments still show their original names.

## [1.1.0] - 2026-11-02

### Added

- Minimum quantity per part: a part with less than that in total counts as
  low on stock. Set it with `add`/`edit --min` (`edit --no-min` removes it) or
  in the part forms, and list low parts with `partstrack ls --low` or the
  "Low stock" filter in the web interface and desktop app. CSV files have a
  new `min_quantity` column.
- Stock history: every stock change is recorded with time, location, change,
  new quantity and an optional note (`stock -m`, `move -m`, or the note fields
  in the web interface and desktop app). Show it with `partstrack history`,
  on a part's page, on the web interface's new History page, and in the
  desktop app's part view. Existing stock is recorded as `initial` entries
  when the database is upgraded.
- Labels with QR codes for locations and parts, printed in batches from the
  web interface (checkboxes on the Locations and Parts pages) or the desktop
  app (File → Print labels…, also as PDF). Scanning a label opens the
  location's parts or the part in the web interface. Layouts for plain paper
  with cut lines, A4 sticker sheets and label printers, plus custom sizes and
  a start position for partly used sheets. `partstrack-web --url` sets the
  address in the codes. The `web` and `desktop` extras now include `segno`.
- Duplicating a part: `partstrack copy NE555 NE556`, the *Duplicate* button
  on a part's web page, or Part → Duplicate… (Ctrl+D) in the desktop app. The
  copy gets the fields, tags, attributes and minimum quantity, but no stock
  or attachments; the web and desktop forms let you change it before saving.
- Searching by attribute: search words like `resistance>=10k`,
  `capacitance=100n`, `vcc:16` or `vcc:` filter on attributes, comparing
  numbers with SI prefixes and units (`4k7`, `100nF`, `3,3V`). This works in
  the CLI, the web interface and the desktop app.
- Bulk changes in the web interface: tick parts in the parts list (or take
  all that the current filters show) to add or remove tags, set the category,
  move all their stock (optionally only from one location) or delete them in
  one go.
- A read-only JSON API in the web interface under `/api`: parts (with the
  same search and filters as the parts page), single parts, locations, tags,
  categories and the stock history.
- Read-only copies of a server's inventory in the desktop app: File →
  Download copy from server… downloads the database and attachments (served
  at `/api/snapshot`) into a folder, and opens them read-only, so no changes
  get made that would be lost. A later download replaces the copy.
- File → Open backup… in the desktop app opens a backup from the backup
  script (or a saved snapshot) read-only, to look up an earlier state of the
  inventory without restoring it.

### Changed

- Listing and searching parts is much faster for large inventories (about
  ten times for 2000 parts), in all front ends and the CSV export.
- Search words containing `=`, `<`, `>` or `:` are now attribute filters
  (see above) instead of plain text, and quotes group words: `"10k smd"`
  matches that phrase.

### Security

- The web interface refuses changes sent from other websites. Before, a
  website you visited could make your browser post forms to partstrack, for
  example to delete parts (cross-site request forgery). Behind an HTTPS
  reverse proxy, start `partstrack-web` with `--url https://…`, so that
  changes from older browsers are still accepted.

## [1.0.0] - 2026-10-30

First stable release.

### Changed

- partstrack is now licensed under the GNU General Public License, version 3
  or later. The license is included in the packages and named on the About
  page of the web interface, in the desktop app's About dialog, and in
  `partstrack --help` and `--version`.

## [0.3.0] - 2026-10-29

### Added

- Backup script and systemd timers in `contrib/systemd/` (user and system
  variants): daily backups of the database and attachments, keeping the
  newest 5.
- Logo, shown as the web interface's favicon and in its header and About
  page, and as the desktop app's window and taskbar icon (on Windows too,
  instead of Python's icon).

## [0.2.0] - 2026-10-29

### Added

- Desktop app for Windows and Linux (`partstrack-desktop`, optional `desktop`
  extra with PySide6). It covers searching and filtering, adding and editing
  parts (with suggestions of existing tags), stock changes and moves,
  attachments with picture previews, storing links locally, the location tree,
  CSV import/export with a preview, and a light/dark theme.
- Suggestions of existing tags while typing tags in the web interface.
- About page in the web interface with the version number and links to the
  GitHub repository, changelog and issue tracker.

### Changed

- The version number is defined only in `partstrack/__init__.py`; the package
  metadata reads it from there and now includes repository and changelog
  links.

## [0.1.0] - 2026-10-28

First release.

### Added

- Parts inventory in an SQLite database. Each part has a name, description,
  manufacturer, MPN, category, package, notes, tags, key/value attributes,
  attachments, and stock per storage location. Schema upgrades are applied
  automatically when the database is opened.
- `partstrack` command-line app using only the Python standard library:
  `add`, `list`/`search`, `show`, `edit`, `rm`, `stock`, `move`, `attach`,
  `detach`, `download`, `tags`, `export` and `import`.
- Nested storage locations written as paths (`Lab/Cabinet A/Drawer 3`). A
  unique last part such as `Drawer 3` is enough, and a leading `/` means an
  exact path. Locations are created automatically when stock is first put
  into them. Location commands: `locations`, `location add|edit|rm`.
- CSV import and export with one row per part and location, `attr:<name>`
  columns for attributes, and `;`-separated lists. Imports are all-or-nothing,
  support `--dry-run`, `--add-stock` and a default location, recognise common
  header names, and read UTF-8 or Windows-1252 files. Exports can use another
  delimiter and a BOM for Excel.
- Attachments (datasheets, images, other files) are copied into an
  `attachments/` folder next to the database, or stored as links.
- Links that end in a file name are downloaded and stored locally, keeping the
  original link. `partstrack download` converts existing links, and `--link`
  keeps a link as a link. Downloads from the server itself or from link-local
  and reserved addresses are refused.
- Web interface for the local network (`partstrack-web`, optional `web`
  extra with Flask and waitress). It covers searching and filtering, adding
  and editing parts, stock changes and moves, uploads with an image gallery,
  storing links locally, the location tree, and CSV import/export.
- Light/dark theme switch in the web interface, with an "Auto" setting that
  follows the device.

[Unreleased]: https://github.com/kaythe/partstrack/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/kaythe/partstrack/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/kaythe/partstrack/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/kaythe/partstrack/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/kaythe/partstrack/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kaythe/partstrack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kaythe/partstrack/releases/tag/v0.1.0
