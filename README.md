# Hermes DL

Hermes DL is now a local Windows download organizer for large PS5/game archives.

It watches an IDM staging folder, groups multipart RAR sets, waits for the set to stop changing, tests the archive with WinRAR, resolves PS5 PPSA title IDs with Crawl4AI, downloads a cover, and extracts each game into its own folder.

## Default layout

```
D:\
├── _TEMP\
│   ├── IDM\
│   └── RAR\
├── _Automation\
│   └── IDM-AutoExtract\
├── _INBOX\
└── PS5\
    └── Games\
        └── Incoming\
```

The watcher and extracted metadata stay outside the IDM/RAR staging folders. The RAR staging directory is reserved for archive work/WinRAR temporary files, not the watcher itself.

## What the watcher does

For a set such as:

```
[DLPSGAME.COM]-PPSA22327.part01.rar
[DLPSGAME.COM]-PPSA22327.part02.rar
...
[DLPSGAME.COM]-PPSA22327.part20.rar
```

Hermes groups the parts together using the archive base name, waits for the files to become stable, then runs a WinRAR integrity test against the root archive.

Only a successful WinRAR test is allowed to proceed to extraction.

For a detected PPSA, Hermes opens the canonical ProsperoPatches page:

https://prosperopatches.com/PPSAxxxxx

It uses the page's own title/heading and image metadata first, then falls back to a Crawl4AI browser crawl when the normal HTTP response does not expose the metadata. Covers are cached under:

```
D:\_Automation\IDM-AutoExtract\covers\
```

The final game folder becomes:

```
D:\PS5\Games\Incoming\Game Name [PPSAxxxxx]\
├── cover.jpg / cover.png / cover.webp
├── metadata.json
├── .hermes-complete
└── extracted game contents...
```

If a PPSA cannot be resolved, the archive is still handled, but it falls back to:

```
D:\_INBOX\
```

## Installation

Run:

```
start.bat
```

The first run creates a Python virtual environment, installs the pinned Crawl4AI release, and installs its browser dependencies.

Crawl4AI's current open-source release used here is v0.9.4.

## WinRAR

Hermes searches the standard Windows WinRAR installation locations and passes its temporary work directory as:

```
D:\_TEMP\RAR
```

so the system drive is not used as the archive workspace.

## Important

Hermes deliberately does **not** delete source archives after extraction. This is intentional: completed archives remain available until you verify the extracted result.

It also keeps failed/incomplete multipart sets in the IDM staging folder and retries them when the set changes or the retry window expires.

## UI

Open:

```
http://127.0.0.1:8765
```

The dashboard shows:

- watcher status
- WinRAR / Crawl4AI status
- staging paths
- recent archive jobs
- activity log
- manual PPSA resolver
- manual scan/start/stop controls
