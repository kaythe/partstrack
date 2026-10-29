# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/kaythe/partstrack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kaythe/partstrack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kaythe/partstrack/releases/tag/v0.1.0
