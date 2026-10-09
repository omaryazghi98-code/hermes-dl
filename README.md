# Hermes Manager

Hermes Manager is a local-first Windows desktop organizer for downloads, PS5 archives, game metadata, and storage cleanup reviews. The existing Flask-based download watcher remains the backend; the desktop shell is Electron, with an embedded visible Chromium browser for sites that need JavaScript or manual interaction.

## Launch the desktop app

1. Install **Python 3.11+** and **Node.js LTS** on Windows.
2. Make sure WinRAR is installed if you want archive testing and extraction.
3. Run `launch-desktop.bat`.
4. On first run, the launcher creates the Python environment, installs Python dependencies, and installs Electron. Subsequent launches reuse the environment.
5. Hermes opens as a desktop window. The browser panel uses a persistent Electron session.

The existing browser-based version is still available with `start.bat` at `http://127.0.0.1:8765`.

## Features

- **Download queue:** watches the configured IDM staging folder, groups multipart RAR files, waits for files to stop changing, tests archives with WinRAR, and extracts verified sets.
- **PPSA identification:** detects title IDs such as `PPSA22327` and resolves game title and cover metadata using the canonical ProsperoPatches page.
- **Visible browser fallback:** the Browser tab is a real embedded Chromium browser in the Electron desktop app. Navigate to a ProsperoPatches title page, then use **Use current browser page metadata** on the PS5 Library page if automated lookup does not work.
- **PS5 library view:** shows recognized jobs, title IDs, artwork, and destinations.
- **Storage scanner:** read-only inventory of a selected folder or drive, including file-type counts, largest files, and possible duplicate candidates based on matching filename and size.
- **Folder shortcuts and settings:** open configured folders and update the staging, temporary, game, and inbox paths.
- **Activity log:** recent watcher and metadata activity.

## Default folder layout

```
D:\\
├── _TEMP\\
│   ├── IDM\\
│   └── RAR\\
├── _Automation\\
│   └── IDM-AutoExtract\\
├── _INBOX\\
└── PS5\\
    └── Games\\
        └── Incoming\\
```

Paths can be changed from Settings. The scanner defaults to `D:\\` and inspects up to 10,000 files per scan by default; the limit is configurable up to 50,000. A partial scan is clearly reported when the limit is reached.

## Safety rules

- Hermes does **not** delete source archives after extraction.
- Storage scans are read-only. Duplicate candidates are only files sharing a filename and size; they are not proof of identical contents.
- Nothing is moved, renamed, or deleted by the storage scanner.
- Archive sets must pass a WinRAR integrity test before extraction.
- Unknown/unresolved metadata falls back to the configured inbox.
- Use the scanner's results as a review list, not as automatic cleanup instructions.

## Metadata source

The canonical title page format is:

`https://prosperopatches.com/PPSAxxxxx`

Hermes first tries ordinary HTTP metadata and the existing Crawl4AI fallback. When a site requires a visible browser session, use the integrated Browser tab and import the rendered page metadata manually.

## Local data

Runtime configuration, watcher state, and activity logs are stored under the local `data/` directory, which is ignored by Git. Cover images are cached under the configured automation directory.

## Troubleshooting

- **Python not found:** install Python 3.11+ and ensure the `py` launcher is available.
- **Node/npm not found:** install Node.js LTS and rerun `launch-desktop.bat`.
- **WinRAR missing:** install WinRAR; extraction and archive tests need it.
- **Desktop window cannot connect:** check that port 8765 is not already occupied by another service.
- **Browser panel unavailable:** the embedded browser works in the Electron desktop app; when opening the Flask UI in an ordinary browser, the page provides a fallback message instead.
