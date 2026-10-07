# IDM Queue Studio

A local Windows web app for turning batches of file-page URLs into a visual download checklist and queueing the missing items in Internet Download Manager.

## Features

- Paste a whole block of links at once.
- Extracts filenames and part numbers automatically.
- Saves multiple queues/projects locally.
- Rechecks the destination folder and marks already-present parts.
- Resolves MediaFire pages to direct download URLs when possible.
- Browser fallback powered by Playwright for pages where plain HTTP resolution fails.
- Sends downloads to IDM using its documented command-line integration (`/a`, `/p`, `/f`).
- Starts the IDM scheduler/queue using `/s`.

## Run

Double-click `start.bat`.

The first run creates `.venv`, installs the dependencies, and installs Chromium for the browser resolver. The app then opens at `http://127.0.0.1:8765`.

## IDM

IDM should be installed normally. The app searches the standard Windows IDM locations for `IDMan.exe`.

## Notes

The app itself does not extract RAR archives. It manages the checklist and hands the actual downloads to IDM. MediaFire may still require a logged-in browser or other anti-abuse checks; the browser resolver is intended to cover the normal JavaScript-rendered download page, not bypass access controls.
