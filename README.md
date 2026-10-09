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

## Integrated download and PS5 tools

Hermes is the control panel; it does not attempt to replace specialist tools.

- **IDM:** Download Center can send a direct HTTP/HTTPS URL to Internet Download Manager through its command-line interface. Choose queue or start, then let IDM handle retries and download management. Downloads placed in the configured IDM staging folder are handled by the existing watcher.
- **Orbit Store:** the Console & Transfers page checks the PS5 web interface at `http://<ps5-ip>:34177/` and opens it in Hermes' visible browser for the documented pairing-code workflow. Enter the code shown on your PS5 in Orbit itself; Hermes does not bypass pairing.
- **Orbit Zero:** optionally launch the Orbit Zero desktop app by configuring its executable path in Settings. Orbit Zero is the computer-side download-and-transfer route; it is separate from Orbit Store's PS5-side service. Get it from the [official Orbit Store repository](https://github.com/saawant12/orbit-store-ps5).
- **PS5Upload:** configure its desktop executable and local engine origin (default `http://127.0.0.1:19113`). Hermes can check engine/console connectivity, queue a single-file transfer to an absolute PS5 path, poll transfer status, inspect local PKG metadata (title, Title ID, category, version and Content ID), and submit installation of a local single-file `.pkg`. The integration calls the existing [PS5Upload engine](https://github.com/phantomptr/ps5upload) rather than reimplementing its AVA1 protocol.
- **FTP:** browse a compatible FTP server, upload a selected local file, or download a remote file to the configured local FTP folder. FileZilla may be launched as an optional external client.
- **Payload Sender:** keep an ordered local playlist of your own `.elf`, `.bin` or `.payload` files and payload assets from the official latest-release APIs for [etaHEN](https://github.com/etaHEN/etaHEN/releases) and [kstuff](https://github.com/EchoStretch/kstuff/releases). Official assets are downloaded into `data/payloads/` and SHA-256 checked when GitHub provides a digest. Each send is a separate, explicitly confirmed TCP transfer to the chosen PS5 LAN IP and loader port (9020/9021 are common ports). Removing a playlist entry does not delete its file.

### Payload sender notes

- Hermes resolves the upstream **latest stable release** live; it does not assume that a version number is published. At the time of the last upstream check, etaHEN's official latest-stable API returned **2.5B**, not 2.6; check the live list for future changes.
- The upstream kstuff **v1.6.7** release notes describe firmware 3.00–10.01. That is outside the previously reported PS5 firmware 13.60; do not send that build to 13.60 unless a trusted official source explicitly confirms support for that firmware.
- A payload loader must already be listening at the selected IP and port. A completed send means bytes were written to the TCP connection; Hermes cannot verify that the payload ran, nor does it guarantee firmware compatibility.
- The sender only accepts private IPv4 LAN destinations, payload extensions `.elf`, `.bin`, and `.payload`, and files up to 256 MiB. Local custom payloads are referenced in place rather than copied. Downloaded official assets remain on disk even if their playlist entries are removed.

### Safety and connection notes

- The PS5, computer, Orbit interface, FTP service and PS5Upload engine must be on a trusted network where they can reach one another.
- PS5Upload's engine is powerful and does not provide its own password authentication. Hermes accepts only a loopback PS5Upload engine URL by default; do not expose that engine to the internet.
- Set the PS5's LAN IPv4 address in Console & Transfers and run **Check connections** before transferring or installing.
- Local transfer/install sources must be inside one of Hermes' configured library folders. Configure `FTP downloads to this local folder` under Settings for additional staging space.
- The PKG installer requires an ordinary `.pkg` file and complete package metadata; Hermes refuses installation if it cannot verify the Content ID, Title ID and category. Split-package workflows should be handled in the PS5Upload client until Hermes adds explicit split-set support. PS5 system packages with `NPXS` identifiers are not part of this installer workflow.
- Hermes leaves PS5Upload's destructive-reinstall option disabled and does not request deletion of the local PKG. Nevertheless, package installation can affect existing console content; confirm the title and package type before installing.
- Classic FTP is unencrypted. Use it only on a trusted LAN; usernames, passwords and file contents are not protected in transit. Prefer PS5Upload for large or sensitive transfers.
- FTP credentials are used only for the current connection request and are not saved in Hermes configuration. Hermes will not overwrite an existing local or remote file and has no remote-delete action.
- Large downloads and extractions on external storage should remain sequential by default, particularly when a disk has previously shown I/O retry/reset errors.

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
