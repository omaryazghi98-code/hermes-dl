from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    CRAWL4AI_AVAILABLE = True
except Exception:
    CRAWL4AI_AVAILABLE = False


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "watcher_state.json"
ACTIVITY_FILE = DATA_DIR / "activity.log"

DEFAULT_CONFIG = {
    "download_root": r"D:\_TEMP\IDM",
    "rar_temp": r"D:\_TEMP\RAR",
    "automation_root": r"D:\_Automation\IDM-AutoExtract",
    "game_root": r"D:\PS5\Games\Incoming",
    "inbox_root": r"D:\_INBOX",
    "watch_interval": 15,
    "stable_seconds": 30,
}

app = Flask(__name__, static_folder="static", template_folder="templates")

PPSA_RE = re.compile(r"(?i)\bPPSA[-_ ]?(\d{5})\b")
PART_RE = re.compile(r"(?i)\.part(\d+)\.rar$")
LEGACY_RE = re.compile(r"(?i)\.r(\d{2,})$")
RAR_RE = re.compile(r"(?i)\.rar$")
ZIP_RE = re.compile(r"(?i)\.zip$")


@dataclass
class ArchiveGroup:
    key: str
    folder: str
    base_name: str
    archive_type: str
    root_file: str
    files: list[str]
    parts: list[int]


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp.replace(path)


def load_config() -> dict[str, Any]:
    data = load_json(CONFIG_FILE, {})
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        cfg.update({k: data[k] for k in DEFAULT_CONFIG.keys() if k in data})
    return cfg


CONFIG = load_config()


def ensure_layout() -> None:
    paths = [
        CONFIG["download_root"],
        CONFIG["rar_temp"],
        CONFIG["automation_root"],
        CONFIG["game_root"],
        CONFIG["inbox_root"],
        str(Path(CONFIG["automation_root"]) / "covers"),
        str(Path(CONFIG["automation_root"]) / "logs"),
    ]
    for value in paths:
        try:
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


ensure_layout()


def activity(message: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " | " + message
    try:
        log_dir = Path(CONFIG["automation_root"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with ACTIVITY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def find_winrar() -> str | None:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "WinRAR" / "WinRAR.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "WinRAR" / "WinRAR.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WinRAR" / "WinRAR.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    return shutil.which("WinRAR.exe") or shutil.which("rar.exe")


def safe_name(value: str, max_len: int = 150) -> str:
    name = (value or "").strip()
    name = re.sub(r'[<>:"/\\\\|?*]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = "Unknown Download"
    return name[:max_len].rstrip(" .")


def extract_ppsa(value: str) -> str | None:
    match = PPSA_RE.search(value or "")
    return "PPSA" + match.group(1) if match else None


def ppsa_number(ppsa: str) -> str:
    return re.sub(r"(?i)^PPSA", "", ppsa)


def ppsa_url(ppsa: str) -> str:
    return "https://www.serialstation.com/titles/PPSA/" + ppsa_number(ppsa)


def archive_info(path: Path) -> tuple[str, str, int] | None:
    name = path.name

    match = PART_RE.search(name)
    if match:
        base = name[:match.start()].rstrip(" .-_")
        return base, "RAR", int(match.group(1))

    match = LEGACY_RE.search(name)
    if match:
        base = name[:match.start()].rstrip(" .-_")
        return base, "RAR", int(match.group(1)) + 1

    if RAR_RE.search(name):
        return name[:-4], "RAR", 1

    if ZIP_RE.search(name):
        return name[:-4], "ZIP", 1

    return None


def group_archives(root: Path) -> list[ArchiveGroup]:
    groups: dict[str, dict[str, Any]] = {}

    if not root.exists():
        return []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        info = archive_info(path)
        if not info:
            continue

        base, archive_type, part = info
        folder = str(path.parent.resolve())
        key = folder.lower() + "|" + base.lower() + "|" + archive_type.lower()

        entry = groups.setdefault(
            key,
            {
                "folder": folder,
                "base_name": base,
                "archive_type": archive_type,
                "files": [],
            },
        )
        entry["files"].append((part, str(path)))

    result: list[ArchiveGroup] = []

    for key, entry in groups.items():
        files_sorted = sorted(entry["files"], key=lambda item: (item[0], item[1].lower()))
        file_paths = [item[1] for item in files_sorted]
        parts = [item[0] for item in files_sorted]
        root_file = ""

        if entry["archive_type"] == "RAR":
            candidates = [
                item[1]
                for item in files_sorted
                if Path(item[1]).name.lower() == (entry["base_name"] + ".part1.rar").lower()
            ]
            if candidates:
                root_file = candidates[0]
            else:
                legacy = [
                    item[1]
                    for item in files_sorted
                    if Path(item[1]).name.lower() == (entry["base_name"] + ".rar").lower()
                ]
                if legacy:
                    root_file = legacy[0]
        else:
            root_file = file_paths[0] if file_paths else ""

        result.append(
            ArchiveGroup(
                key=key,
                folder=entry["folder"],
                base_name=entry["base_name"],
                archive_type=entry["archive_type"],
                root_file=root_file,
                files=file_paths,
                parts=parts,
            )
        )

    return sorted(result, key=lambda item: item.base_name.lower())


def file_signature(group: ArchiveGroup) -> str:
    parts: list[str] = []
    for path_str in group.files:
        path = Path(path_str)
        try:
            stat = path.stat()
            parts.append(
                path.name + ":" + str(stat.st_size) + ":" + str(stat.st_mtime_ns)
            )
        except OSError:
            parts.append(path.name + ":missing")
    return "|".join(parts)


def stable_enough(state: dict[str, Any], group: ArchiveGroup) -> bool:
    signature = file_signature(group)
    now = time.time()
    previous = state.get(group.key)

    if not previous:
        state[group.key] = {
            "signature": signature,
            "first_stable_at": now,
            "status": "waiting_for_stability",
            "name": group.base_name,
            "parts": len(group.files),
        }
        return False

    if previous.get("signature") != signature:
        previous.update(
            {
                "signature": signature,
                "first_stable_at": now,
                "status": "waiting_for_stability",
                "parts": len(group.files),
            }
        )
        return False

    stable_for = now - float(previous.get("first_stable_at", now))
    return stable_for >= int(CONFIG.get("stable_seconds", 30))


def update_state(state: dict[str, Any], group: ArchiveGroup, **fields: Any) -> None:
    entry = state.setdefault(group.key, {})
    entry.update(fields)
    entry["name"] = group.base_name
    entry["parts"] = len(group.files)
    entry["updated_at"] = time.time()
    save_json(STATE_FILE, state)


def run_winrar(args: list[str]) -> subprocess.CompletedProcess[str]:
    winrar = find_winrar()
    if not winrar:
        raise RuntimeError("WinRAR.exe was not found.")

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [winrar, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        timeout=60 * 60 * 12,
    )


def test_archive(group: ArchiveGroup) -> tuple[bool, str]:
    if not group.root_file:
        return False, "Part 1 / root archive is not present."

    args = [
        "t",
        "-ibck",
        "-inul",
        group.root_file,
    ]

    try:
        result = run_winrar(args)
    except subprocess.TimeoutExpired:
        return False, "WinRAR test timed out."
    except Exception as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, "Archive test passed."

    details = (result.stderr or result.stdout or "").strip()
    if not details:
        details = "WinRAR returned exit code " + str(result.returncode)
    return False, details[-1200:]


def extract_archive(group: ArchiveGroup, destination: Path) -> tuple[bool, str]:
    destination.mkdir(parents=True, exist_ok=True)

    args = [
        "x",
        "-ibck",
        "-inul",
        "-y",
        "-o+",
        "-w" + str(Path(CONFIG["rar_temp"])),
        group.root_file,
        str(destination),
    ]

    try:
        result = run_winrar(args)
    except subprocess.TimeoutExpired:
        return False, "WinRAR extraction timed out."
    except Exception as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, "Extraction completed."

    details = (result.stderr or result.stdout or "").strip()
    if not details:
        details = "WinRAR returned exit code " + str(result.returncode)
    return False, details[-1600:]


async def crawl_page(url: str) -> Any:
    if not CRAWL4AI_AVAILABLE:
        raise RuntimeError("Crawl4AI is not installed.")

    browser_config = BrowserConfig(headless=True)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        scan_full_page=False,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        return await crawler.arun(url=url, config=run_config)


def run_crawl(url: str) -> Any:
    return asyncio.run(crawl_page(url))


def html_h1(html_text: str) -> str | None:
    match = re.search(
        r"<h1[^>]*>\s*(.*?)\s*</h1>",
        html_text or "",
        flags=re.I | re.S,
    )
    if not match:
        return None
    value = re.sub(r"<[^>]+>", " ", match.group(1))
    value = html.unescape(re.sub(r"\s+", " ", value)).strip()
    return value or None


def resolve_title_with_crawl4ai(ppsa: str) -> tuple[str | None, str | None, str | None]:
    url = ppsa_url(ppsa)

    try:
        result = run_crawl(url)
        page_html = getattr(result, "html", "") or ""
        markdown = getattr(result, "markdown", "") or ""

        title = html_h1(page_html)
        if not title:
            lines = [line.strip() for line in markdown.splitlines()]
            for line in lines:
                if line.startswith("# ") and not PPSA_RE.search(line):
                    title = line[2:].strip()
                    break

        title = safe_name(title) if title else None
        if title:
            images = []
            media = getattr(result, "media", None) or {}
            images.extend(media.get("images", []) if isinstance(media, dict) else [])

            cover_url = None
            for image in images:
                source = image.get("src") if isinstance(image, dict) else None
                if source and not re.search(r"icon|logo|avatar|favicon", source, re.I):
                    cover_url = source
                    break

            return title, cover_url, url

    except Exception as exc:
        activity("Crawl4AI title lookup failed for " + ppsa + ": " + str(exc))

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        response.raise_for_status()
        page = response.text
        title = html_h1(page)
        if not title:
            match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.I | re.S)
            if match:
                title = re.sub(r"<[^>]+>", " ", match.group(1))
                title = re.sub(r"\s+", " ", html.unescape(title)).strip()
        if title:
            return safe_name(title), None, url
    except Exception as exc:
        activity("HTTP title fallback failed for " + ppsa + ": " + str(exc))

    return None, None, None


def image_candidates_from_bing(title: str, ppsa: str) -> list[dict[str, str]]:
    query = urllib.parse.quote_plus(title + " PS5 cover box art " + ppsa)
    url = "https://www.bing.com/images/search?q=" + query
    candidates: list[dict[str, str]] = []

    try:
        result = run_crawl(url)
        page_html = getattr(result, "html", "") or ""

        for match in re.finditer(r'"murl":"(.*?)"', page_html):
            source = match.group(1).replace("\\/", "/").replace('\\"', '"')
            source = source.replace("\\u002f", "/")
            if source.startswith("http"):
                candidates.append({"src": source, "alt": ""})

        media = getattr(result, "media", None) or {}
        images = media.get("images", []) if isinstance(media, dict) else []
        for image in images:
            if not isinstance(image, dict):
                continue
            source = image.get("src")
            if not source:
                continue
            candidates.append(
                {
                    "src": str(source),
                    "alt": str(image.get("alt", "") or "") + " " + str(image.get("desc", "") or ""),
                }
            )
    except Exception as exc:
        activity("Cover search failed for " + ppsa + ": " + str(exc))

    unique: list[dict[str, str]] = []
    seen: set[str] = set()

    for candidate in candidates:
        source = candidate.get("src", "")
        key = source.lower().split("?")[0]
        if not source.startswith(("http://", "https://")):
            continue
        if key in seen:
            continue
        if re.search(
            r"logo|icon|favicon|avatar|profile|sprite|banner|advert|tracking",
            source + " " + candidate.get("alt", ""),
            re.I,
        ):
            continue
        seen.add(key)
        unique.append(candidate)

    def score(item: dict[str, str]) -> int:
        text = (item.get("src", "") + " " + item.get("alt", "")).lower()
        value = 0
        if "cover" in text:
            value += 8
        if "boxart" in text or "box art" in text:
            value += 7
        if "ps5" in text:
            value += 4
        if "forza" in text and "horizon" in text:
            value += 2
        if re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", text):
            value += 2
        return value

    return sorted(unique, key=score, reverse=True)[:20]


def download_cover(title: str, ppsa: str, preferred: str | None = None) -> tuple[str | None, str | None]:
    cover_dir = Path(CONFIG["automation_root"]) / "covers"
    cover_dir.mkdir(parents=True, exist_ok=True)

    existing = list(cover_dir.glob(ppsa + ".*"))
    for path in existing:
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return str(path), path.name

    candidates: list[dict[str, str]] = []
    if preferred:
        candidates.append({"src": preferred, "alt": "metadata cover"})
    candidates.extend(image_candidates_from_bing(title, ppsa))

    for candidate in candidates:
        source = candidate.get("src", "")
        try:
            response = requests.get(
                source,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=25,
                stream=True,
            )
            if response.status_code != 200:
                continue

            content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
            if not content_type.startswith("image/"):
                continue

            suffix_map = {
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }
            suffix = suffix_map.get(content_type)
            if not suffix:
                continue

            target = cover_dir / (ppsa + suffix)
            total = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(1024 * 128):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 15 * 1024 * 1024:
                        raise ValueError("Image is larger than 15 MB")
                    handle.write(chunk)

            if total < 10 * 1024:
                target.unlink(missing_ok=True)
                continue

            return str(target), target.name

        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            continue

    return None, None


def resolve_metadata(ppsa: str, archive_name: str) -> dict[str, Any]:
    title, preferred_cover, source_url = resolve_title_with_crawl4ai(ppsa)
    if not title:
        title = safe_name(
            PPSA_RE.sub("", archive_name).strip(" []-_") or ppsa
        )

    cover_path, cover_name = download_cover(title, ppsa, preferred_cover)

    return {
        "title": title,
        "title_id": ppsa,
        "platform": "PS5",
        "source": source_url,
        "cover": cover_name,
        "cover_path": cover_path,
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resolver": "Crawl4AI + SerialStation + Bing Images",
    }


def destination_for(group: ArchiveGroup, metadata: dict[str, Any]) -> Path:
    ppsa = metadata.get("title_id")
    title = metadata.get("title")

    if ppsa and title:
        return Path(CONFIG["game_root"]) / (safe_name(title) + " [" + ppsa + "]")

    return Path(CONFIG["inbox_root"]) / safe_name(group.base_name)


def process_group(state: dict[str, Any], group: ArchiveGroup) -> None:
    entry = state.get(group.key, {})
    if entry.get("status") == "completed":
        marker = Path(entry.get("destination", "")) / ".hermes-complete"
        if marker.exists():
            return

    if group.archive_type == "RAR" and not group.root_file:
        update_state(
            state,
            group,
            status="waiting_for_part1",
            error="Part 1 / root archive not found.",
        )
        return

    activity(
        "Checking " + group.base_name + " (" + str(len(group.files)) + " archive file(s))"
    )

    ok, message = test_archive(group)
    if not ok:
        update_state(
            state,
            group,
            status="waiting_or_failed_test",
            error=message,
            last_test=time.time(),
        )
        activity("Archive test failed: " + group.base_name + " | " + message[:300])
        return

    ppsa = extract_ppsa(group.base_name)
    metadata: dict[str, Any] = {
        "title": safe_name(group.base_name),
        "title_id": ppsa,
        "platform": "PS5" if ppsa else "Unknown",
        "source": None,
        "cover": None,
        "cover_path": None,
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resolver": None,
    }

    if ppsa:
        activity("Resolving " + ppsa + " for " + group.base_name)
        try:
            metadata = resolve_metadata(ppsa, group.base_name)
            activity(
                "Resolved " + ppsa + " → " + str(metadata.get("title"))
            )
        except Exception as exc:
            activity("Metadata resolver failed for " + ppsa + ": " + str(exc))

    destination = destination_for(group, metadata)
    marker = destination / ".hermes-complete"

    if marker.exists():
        update_state(
            state,
            group,
            status="completed",
            destination=str(destination),
            metadata=metadata,
        )
        return

    activity("Extracting " + group.base_name + " → " + str(destination))

    ok, message = extract_archive(group, destination)
    if not ok:
        update_state(
            state,
            group,
            status="extraction_failed",
            error=message,
            destination=str(destination),
            metadata=metadata,
        )
        activity("Extraction failed: " + group.base_name + " | " + message[:300])
        return

    metadata_file = destination / "metadata.json"
    metadata_file.write_text(
        json.dumps(
            {
                **metadata,
                "archive": {
                    "type": group.archive_type,
                    "base_name": group.base_name,
                    "part_count": len(group.files),
                    "parts": group.parts,
                    "source_folder": group.folder,
                    "tested": True,
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    marker.write_text(
        "Hermes DL completed " + time.strftime("%Y-%m-%d %H:%M:%S"),
        encoding="utf-8",
    )

    update_state(
        state,
        group,
        status="completed",
        destination=str(destination),
        metadata=metadata,
        completed_at=time.time(),
        error=None,
    )
    activity("DONE: " + group.base_name)


class OrganizerWatcher:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.running = False
        self.last_scan = 0.0
        self.last_error: str | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._loop,
            name="HermesWatcher",
            daemon=True,
        )
        self.thread.start()
        activity("Watcher started.")

    def stop(self) -> None:
        self.stop_event.set()
        self.running = False
        activity("Watcher stopped.")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.scan_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                activity("Watcher error: " + str(exc))
            self.stop_event.wait(int(CONFIG.get("watch_interval", 15)))

    def scan_once(self) -> dict[str, Any]:
        root = Path(CONFIG["download_root"])
        state = load_json(STATE_FILE, {})
        groups = group_archives(root)

        processed = 0

        for group in groups:
            if not stable_enough(state, group):
                continue

            entry = state.get(group.key, {})
            signature = file_signature(group)
            previous_signature = entry.get("processed_signature")

            if entry.get("status") in {
                "completed",
                "extraction_failed",
                "waiting_or_failed_test",
            } and previous_signature == signature:
                retry_at = float(entry.get("retry_after", 0))
                if time.time() < retry_at:
                    continue

            process_group(state, group)
            state[group.key]["processed_signature"] = signature
            state[group.key]["retry_after"] = time.time() + 60
            processed += 1

        save_json(STATE_FILE, state)
        self.last_scan = time.time()

        return {
            "groups": len(groups),
            "processed": processed,
            "last_scan": self.last_scan,
        }


WATCHER = OrganizerWatcher()


def update_config(new_config: dict[str, Any]) -> dict[str, Any]:
    global CONFIG

    merged = dict(CONFIG)

    for key in DEFAULT_CONFIG:
        if key not in new_config:
            continue
        if key in {"watch_interval", "stable_seconds"}:
            try:
                merged[key] = max(5, int(new_config[key]))
            except (ValueError, TypeError):
                continue
        else:
            value = str(new_config[key]).strip()
            if value:
                merged[key] = value

    CONFIG = merged
    save_json(CONFIG_FILE, CONFIG)
    ensure_layout()
    return CONFIG


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "watcher": {
                "running": WATCHER.running,
                "last_scan": WATCHER.last_scan,
                "last_error": WATCHER.last_error,
            },
            "config": CONFIG,
            "winrar": find_winrar(),
            "crawl4ai": CRAWL4AI_AVAILABLE,
        }
    )


@app.get("/api/config")
def api_config():
    return jsonify(CONFIG)


@app.post("/api/config")
def api_update_config():
    data = request.get_json(force=True) or {}
    return jsonify(update_config(data))


@app.post("/api/watcher/start")
def api_start_watcher():
    WATCHER.start()
    return jsonify({"ok": True, "running": WATCHER.running})


@app.post("/api/watcher/stop")
def api_stop_watcher():
    WATCHER.stop()
    return jsonify({"ok": True, "running": WATCHER.running})


@app.post("/api/watcher/scan")
def api_scan():
    try:
        return jsonify({"ok": True, **WATCHER.scan_once()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/activity")
def api_activity():
    lines: list[str] = []
    try:
        if ACTIVITY_FILE.exists():
            lines = ACTIVITY_FILE.read_text(encoding="utf-8").splitlines()[-100:]
    except OSError:
        pass
    return jsonify({"lines": lines})


@app.get("/api/jobs")
def api_jobs():
    state = load_json(STATE_FILE, {})
    jobs = []

    for key, entry in state.items():
        item = dict(entry)
        item["key"] = key
        jobs.append(item)

    jobs.sort(key=lambda item: float(item.get("updated_at", 0)), reverse=True)
    return jsonify(jobs[:100])


@app.post("/api/resolve")
def api_resolve():
    data = request.get_json(force=True) or {}
    ppsa = extract_ppsa(data.get("ppsa") or "")
    if not ppsa:
        return jsonify({"error": "Enter a valid PPSA, for example PPSA22327."}), 400

    title, cover, source = resolve_title_with_crawl4ai(ppsa)
    if not title:
        return jsonify({"error": "PPSA could not be resolved.", "title_id": ppsa}), 404

    cover_path, cover_name = download_cover(title, ppsa, cover)

    return jsonify(
        {
            "title_id": ppsa,
            "title": title,
            "source": source,
            "cover": cover_name,
            "cover_path": cover_path,
        }
    )


@app.post("/api/open-folder")
def api_open_folder():
    data = request.get_json(force=True) or {}
    target = str(data.get("target") or "").strip()

    allowed = {
        "download_root": CONFIG["download_root"],
        "rar_temp": CONFIG["rar_temp"],
        "automation_root": CONFIG["automation_root"],
        "game_root": CONFIG["game_root"],
        "inbox_root": CONFIG["inbox_root"],
    }

    folder = allowed.get(target)
    if not folder:
        return jsonify({"error": "Unknown folder."}), 400

    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)

    try:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"ok": True, "path": str(path)})


@app.get("/api/library-summary")
def api_library_summary():
    roots = {
        "download": Path(CONFIG["download_root"]),
        "games": Path(CONFIG["game_root"]),
        "inbox": Path(CONFIG["inbox_root"]),
    }

    summary: dict[str, Any] = {}
    for key, root in roots.items():
        try:
            files = 0
            folders = 0
            bytes_total = 0

            if root.exists():
                for item in root.rglob("*"):
                    if item.is_dir():
                        folders += 1
                    elif item.is_file():
                        files += 1
                        try:
                            bytes_total += item.stat().st_size
                        except OSError:
                            pass

            summary[key] = {
                "path": str(root),
                "files": files,
                "folders": folders,
                "bytes": bytes_total,
            }
        except OSError as exc:
            summary[key] = {"path": str(root), "error": str(exc)}

    return jsonify(summary)


if __name__ == "__main__":
    ensure_layout()
    WATCHER.start()
    print("Hermes DL running at http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False)
