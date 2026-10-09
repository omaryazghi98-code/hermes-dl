from __future__ import annotations

import asyncio
import html
import ftplib
import ipaddress
import json
import os
import posixpath
import re
import shutil
import socket
import hashlib
import subprocess
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, render_template, request, send_file

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
    "ftp_local_root": r"D:\PS5\Packages\Incoming",
    "idm_exe_path": "",
    "orbit_zero_exe_path": "",
    "ps5upload_exe_path": "",
    "filezilla_exe_path": "",
    "ps5_ip": "",
    "ps5upload_engine_url": "http://127.0.0.1:19113",
    "ftp_host": "",
    "ftp_port": 2122,
    "ftp_user": "anonymous",
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
        CONFIG.get("ftp_local_root", CONFIG["inbox_root"]),
        str(Path(CONFIG["automation_root"]) / "covers"),
        str(Path(CONFIG["automation_root"]) / "logs"),
        str(DATA_DIR / "payloads"),
    ]
    for value in paths:
        try:
            Path(value).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


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


def prosperopatches_url(ppsa: str) -> str:
    return "https://prosperopatches.com/" + ppsa


def ppsa_url(ppsa: str) -> str:
    return prosperopatches_url(ppsa)


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


def extract_page_title(page_html: str, ppsa: str) -> str | None:
    candidates = [
        extract_meta_content(page_html, "og:title"),
        extract_meta_content(page_html, "twitter:title"),
        html_h1(page_html),
    ]

    title_tag = re.search(
        r"<title[^>]*>\s*(.*?)\s*</title>",
        page_html or "",
        flags=re.I | re.S,
    )
    if title_tag:
        candidates.append(
            html.unescape(re.sub(r"<[^>]+>", " ", title_tag.group(1))).strip()
        )

    for candidate in candidates:
        value = re.sub(r"\s+", " ", html.unescape(candidate or "")).strip()
        if not value:
            continue
        if "javascript is required" in value.lower():
            continue
        if "playstation 5 game update database" in value.lower():
            continue
        if "404" in value.lower() and "not found" in value.lower():
            continue

        value = re.sub(
            rf"\s*[:\-|–—]\s*{re.escape(ppsa)}\b",
            "",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"\s*[\|\-–—]\s*PROSPERO[Pp]atches\.com.*$",
            "",
            value,
            flags=re.I,
        )
        value = value.replace(ppsa, "").strip(" :-|–—")
        value = safe_name(value)
        if value and value != ppsa:
            return value

    return None


def extract_page_image(page_html: str) -> str | None:
    for key in ("og:image", "twitter:image", "twitter:image:src"):
        candidate = extract_meta_content(page_html, key)
        if candidate and candidate.startswith(("http://", "https://")):
            if not re.search(
                r"logo|favicon|icon|avatar|sprite|banner|tracking",
                candidate,
                re.I,
            ):
                return candidate
    return None


def resolve_prosperopatches_http(
    ppsa: str,
) -> tuple[str | None, str | None, str]:
    url = prosperopatches_url(ppsa)

    response = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/154 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=30,
    )
    response.raise_for_status()

    page_html = response.text or ""
    title = extract_page_title(page_html, ppsa)
    cover = extract_page_image(page_html)
    return title, cover, url


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


def extract_meta_content(page_html: str, property_name: str) -> str | None:
    pattern = r"<meta[^>]+(?:property|name)=[\"']" + re.escape(property_name) + r"[\"'][^>]+content=[\"'](.*?)[\"']"
    match = re.search(pattern, page_html or "", flags=re.I | re.S)
    if match:
        return html.unescape(match.group(1)).strip() or None

    reverse_pattern = r"<meta[^>]+content=[\"'](.*?)[\"'][^>]+(?:property|name)=[\"']" + re.escape(property_name) + r"[\"']"
    match = re.search(reverse_pattern, page_html or "", flags=re.I | re.S)
    if match:
        return html.unescape(match.group(1)).strip() or None

    return None


def choose_prosperopatches_image(page_html: str, media_images: list[Any]) -> str | None:
    # First choice: explicit image metadata from the ProsperoPatches page.
    for key in ("og:image", "twitter:image", "twitter:image:src"):
        candidate = extract_meta_content(page_html, key)
        if candidate and candidate.startswith(("http://", "https://")):
            if not re.search(
                r"logo|favicon|icon|avatar|sprite|banner|tracking",
                candidate,
                re.I,
            ):
                return candidate

    # Second choice: images Crawl4AI found on the same page.
    candidates: list[tuple[int, str]] = []

    for image in media_images:
        if not isinstance(image, dict):
            continue

        src = str(image.get("src") or "").strip()
        if not src.startswith(("http://", "https://")):
            continue

        alt = str(
            image.get("alt")
            or image.get("desc")
            or image.get("title")
            or ""
        )
        text = (src + " " + alt).lower()

        score = 0
        if "cover" in text or "box" in text or "art" in text:
            score += 12
        if "ps5" in text:
            score += 8
        if re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", text):
            score += 2
        if re.search(
            r"logo|favicon|icon|avatar|sprite|banner|tracking|discord|telegram",
            text,
            re.I,
        ):
            score -= 50

        candidates.append((score, src))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates[0][0] > -20 else None


def resolve_title_with_crawl4ai(ppsa: str) -> tuple[str | None, str | None, str | None]:
    # A title imported from the visible browser is a trusted local override and
    # avoids repeating a failed headless lookup for this title ID.
    overrides = load_json(DATA_DIR / "title_overrides.json", {})
    saved = overrides.get(ppsa) if isinstance(overrides, dict) else None
    if isinstance(saved, dict) and saved.get("title"):
        return str(saved["title"]), None, str(saved.get("source") or prosperopatches_url(ppsa))

    # ProsperoPatches is authoritative for a PPSA. Try its canonical page directly
    # first; only fall back to a browser crawl if the normal HTTP response does not
    # expose the metadata.
    url = prosperopatches_url(ppsa)

    try:
        title, cover, _ = resolve_prosperopatches_http(ppsa)
        if title or cover:
            return title, cover, url
    except Exception as exc:
        activity(
            "ProsperoPatches HTTP lookup failed for "
            + ppsa
            + ": "
            + str(exc)
        )

    if not CRAWL4AI_AVAILABLE:
        return None, None, url

    try:
        result = run_crawl(url)
        page_html = getattr(result, "html", "") or ""
        markdown = getattr(result, "markdown", "") or ""

        title = extract_page_title(page_html, ppsa)

        if not title:
            for line in [line.strip() for line in markdown.splitlines()]:
                if line.startswith("# "):
                    candidate = safe_name(line[2:].strip())
                    if candidate and candidate.upper() != ppsa:
                        title = candidate
                        break

        media = getattr(result, "media", None) or {}
        images = media.get("images", []) if isinstance(media, dict) else []
        cover_url = extract_page_image(page_html) or choose_prosperopatches_image(
            page_html, images
        )

        return title, cover_url, url

    except Exception as exc:
        activity(
            "ProsperoPatches browser lookup failed for "
            + ppsa
            + ": "
            + str(exc)
        )
        return None, None, url


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

    for path in cover_dir.glob(ppsa + ".*"):
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return str(path), path.name

    if not preferred:
        activity("No cover exposed by ProsperoPatches for " + ppsa)
        return None, None

    target: Path | None = None

    try:
        response = requests.get(
            preferred,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/154 Safari/537.36"
                )
            },
            timeout=30,
            stream=True,
        )
        response.raise_for_status()

        content_type = (
            response.headers.get("Content-Type") or ""
        ).split(";")[0].lower()

        suffix_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }

        suffix = suffix_map.get(content_type)

        if not suffix:
            guessed = Path(
                urllib.parse.urlparse(preferred).path
            ).suffix.lower()
            if guessed in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = ".jpg" if guessed == ".jpeg" else guessed

        if not suffix:
            activity(
                "ProsperoPatches cover rejected for "
                + ppsa
                + " (not a recognized image)."
            )
            return None, None

        target = cover_dir / (ppsa + suffix)
        total = 0

        with target.open("wb") as handle:
            for chunk in response.iter_content(128 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 15 * 1024 * 1024:
                    raise ValueError("Cover exceeds 15 MB")
                handle.write(chunk)

        if total < 10 * 1024:
            target.unlink(missing_ok=True)
            activity("ProsperoPatches cover was unexpectedly small for " + ppsa)
            return None, None

        return str(target), target.name

    except Exception as exc:
        if target:
            target.unlink(missing_ok=True)

        activity(
            "ProsperoPatches cover download failed for "
            + ppsa
            + ": "
            + str(exc)
        )
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
        "resolver": "Crawl4AI + ProsperoPatches",
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

    cached_cover = metadata.get("cover_path")
    if cached_cover:
        try:
            source_cover = Path(cached_cover)
            if source_cover.exists():
                local_cover = destination / ("cover" + source_cover.suffix.lower())
                shutil.copy2(source_cover, local_cover)
                metadata["local_cover"] = local_cover.name
        except OSError as exc:
            activity("Could not copy cover into game folder: " + str(exc))

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
        if key in {"watch_interval", "stable_seconds", "ftp_port"}:
            try:
                number = int(new_config[key])
                if key == "ftp_port":
                    if not 1 <= number <= 65535:
                        continue
                    merged[key] = number
                else:
                    merged[key] = max(5, number)
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


def find_idm() -> str | None:
    configured = str(CONFIG.get("idm_exe_path") or "").strip()
    candidates = [Path(configured)] if configured else []
    for base in (
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if base:
            candidates.append(Path(base) / "Internet Download Manager" / "IDMan.exe")
            candidates.append(Path(base) / "Programs" / "Internet Download Manager" / "IDMan.exe")
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.suffix.lower() == ".exe":
                return str(candidate.resolve())
        except OSError:
            continue
    return shutil.which("IDMan.exe")


def private_lan_ip(value: Any, allow_loopback: bool = False) -> str:
    text = str(value or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError("Enter the PS5's local IPv4 address, for example 192.168.1.40.") from exc
    if not addr.is_private or addr.is_multicast or addr.is_unspecified:
        raise ValueError("For safety, Hermes only connects to private LAN IP addresses.")
    if addr.is_loopback and not allow_loopback:
        raise ValueError("Enter the PS5's LAN address, not 127.0.0.1.")
    if addr.version != 4:
        raise ValueError("Enter an IPv4 address for this integration.")
    return str(addr)


def validate_engine_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("PS5Upload engine URL must be a plain HTTP URL on your trusted local machine.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Use only the engine origin, for example http://127.0.0.1:19113.")
    host = parsed.hostname.lower()
    if host != "localhost":
        try:
            addr = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("For safety, the PS5Upload engine must use localhost or a loopback IP.") from exc
        if not addr.is_loopback:
            raise ValueError("For safety, keep the unauthenticated PS5Upload engine on this PC (localhost).")
    return url


def find_companion_path(component: str) -> Path | None:
    key_for_component = {
        "orbit_zero": "orbit_zero_exe_path",
        "ps5upload": "ps5upload_exe_path",
        "filezilla": "filezilla_exe_path",
    }
    if component == "idm":
        value = find_idm()
    else:
        key = key_for_component.get(component)
        if not key:
            return None
        value = str(CONFIG.get(key) or "").strip()
        if not value and component == "filezilla":
            for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
                if base:
                    candidate = Path(base) / "FileZilla FTP Client" / "filezilla.exe"
                    if candidate.is_file():
                        value = str(candidate)
                        break
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        return path.resolve() if path.is_file() and path.suffix.lower() == ".exe" else None
    except OSError:
        return None


def ftp_remote_path(value: Any) -> str:
    path = str(value or "/").strip()
    if "\r" in path or "\n" in path or "\x00" in path:
        raise ValueError("Remote path contains invalid characters.")
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError("Parent-directory traversal is not allowed in FTP paths.")
    normalized = posixpath.normpath(path or "/")
    return normalized if normalized.startswith("/") else "/" + normalized


def open_ftp(data: dict[str, Any]) -> tuple[ftplib.FTP, dict[str, Any]]:
    host = private_lan_ip(data.get("host") or CONFIG.get("ftp_host") or CONFIG.get("ps5_ip"))
    try:
        port = int(data.get("port") or CONFIG.get("ftp_port") or 2122)
    except (TypeError, ValueError) as exc:
        raise ValueError("FTP port must be a number between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("FTP port must be between 1 and 65535.")
    username = str(data.get("username") or CONFIG.get("ftp_user") or "anonymous").strip() or "anonymous"
    password = str(data.get("password") or "")
    ftp = ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=8)
        ftp.login(username, password)
        ftp.set_pasv(True)
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise
    return ftp, {"host": host, "port": port, "username": username, "password": password}


FTP_JOB_LOCK = threading.Lock()
FTP_JOBS: dict[str, dict[str, Any]] = {}


def update_ftp_job(job_id: str, **fields: Any) -> None:
    with FTP_JOB_LOCK:
        if job_id in FTP_JOBS:
            FTP_JOBS[job_id].update(fields)
            FTP_JOBS[job_id]["updated_at"] = time.time()


def allowed_local_transfer_file(value: Any) -> Path:
    raw = Path(str(value or "").strip()).expanduser()
    if not str(value or "").strip() or raw.is_symlink() or not raw.is_file():
        raise ValueError("Choose an existing local file. Symbolic links are not accepted.")
    resolved = raw.resolve()
    allowed_roots = [
        Path(CONFIG["download_root"]),
        Path(CONFIG["game_root"]),
        Path(CONFIG["inbox_root"]),
        Path(CONFIG.get("ftp_local_root", CONFIG["inbox_root"])),
    ]
    for root in allowed_roots:
        try:
            root_resolved = root.expanduser().resolve()
            if resolved.is_relative_to(root_resolved):
                return resolved
        except (OSError, ValueError):
            continue
    raise ValueError("For safety, choose a file inside IDM staging, PS5 Games, the Inbox, or the configured FTP local folder.")


def ftp_transfer_worker(job_id: str, mode: str, data: dict[str, Any], local_file: Path | None = None) -> None:
    ftp: ftplib.FTP | None = None
    temp_path: Path | None = None
    try:
        ftp, credentials = open_ftp(data)
        if mode == "upload":
            if local_file is None:
                raise ValueError("Local upload file is missing.")
            remote_dir = ftp_remote_path(data.get("remote_dir") or "/")
            remote_name = safe_name(str(data.get("remote_name") or local_file.name), max_len=180)
            if not remote_name or remote_name in {".", ".."}:
                raise ValueError("Remote filename is invalid.")
            ftp.cwd(remote_dir)
            try:
                existing = ftp.nlst()
            except ftplib.all_errors as exc:
                raise RuntimeError("Could not check remote filenames, so Hermes refused to risk overwriting a file: " + str(exc)) from exc
            existing_names = {posixpath.basename(item.rstrip("/")) for item in existing}
            if remote_name in existing_names:
                raise FileExistsError("A remote file with that name already exists. Rename it or choose another destination; Hermes never overwrites it.")
            total = local_file.stat().st_size
            update_ftp_job(job_id, status="transferring", total_bytes=total, local_path=str(local_file), remote_path=posixpath.join(remote_dir, remote_name))
            transferred = 0
            last_update = 0
            def upload_progress(block: bytes) -> None:
                nonlocal transferred, last_update
                transferred += len(block)
                if transferred - last_update >= 4 * 1024 * 1024 or transferred >= total:
                    update_ftp_job(job_id, bytes_done=transferred)
                    last_update = transferred
            with local_file.open("rb") as handle:
                ftp.storbinary("STOR " + remote_name, handle, blocksize=1024 * 1024, callback=upload_progress)
            update_ftp_job(job_id, status="completed", bytes_done=total, message="Upload finished. Remote listing was not modified beyond the new file.")
        elif mode == "download":
            remote_path = ftp_remote_path(data.get("remote_path"))
            remote_name = safe_name(posixpath.basename(remote_path), max_len=180)
            if not remote_name or remote_name in {".", ".."}:
                raise ValueError("Choose a remote file, not a directory.")
            target_dir = Path(CONFIG.get("ftp_local_root", CONFIG["inbox_root"])).expanduser()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / remote_name
            if target.exists():
                raise FileExistsError("A local file with that name already exists. Hermes never overwrites it.")
            try:
                total_raw = ftp.size(remote_path)
                total = int(total_raw) if total_raw is not None else 0
            except ftplib.all_errors:
                total = 0
            if total and shutil.disk_usage(target_dir).free < total:
                raise OSError("There is not enough free space in the configured FTP local folder.")
            temp_path = target_dir / (".hermes-partial-" + uuid.uuid4().hex + ".tmp")
            transferred = 0
            last_update = 0
            update_ftp_job(job_id, status="transferring", total_bytes=total, remote_path=remote_path, local_path=str(target))
            temp_path.touch(exist_ok=False)
            with temp_path.open("wb") as handle:
                def download_progress(block: bytes) -> None:
                    nonlocal transferred, last_update
                    handle.write(block)
                    transferred += len(block)
                    if transferred - last_update >= 4 * 1024 * 1024 or (total and transferred >= total):
                        update_ftp_job(job_id, bytes_done=transferred, total_bytes=total)
                        last_update = transferred
                ftp.retrbinary("RETR " + remote_path, download_progress, blocksize=1024 * 1024)
                handle.flush()
                os.fsync(handle.fileno())
            if total and transferred != total:
                raise IOError("FTP download size mismatch: expected " + str(total) + " bytes, received " + str(transferred) + ". The incomplete temporary file was not promoted.")
            if target.exists():
                raise FileExistsError("The local destination appeared during transfer; original downloaded bytes were kept in a temporary file.")
            temp_path.replace(target)
            temp_path = None
            update_ftp_job(job_id, status="completed", bytes_done=transferred, total_bytes=total or transferred, local_path=str(target), message="Download finished and moved into place.")
        else:
            raise ValueError("Unknown FTP transfer type.")
        activity("FTP " + mode + " completed (job " + job_id + ").")
    except Exception as exc:
        update_ftp_job(job_id, status="failed", error=str(exc))
        activity("FTP " + mode + " failed (job " + job_id + "): " + str(exc)[:200])
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


def queue_ftp_transfer(mode: str, data: dict[str, Any], local_file: Path | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    entry = {
        "id": job_id, "mode": mode, "status": "queued",
        "bytes_done": 0, "total_bytes": 0, "created_at": time.time(),
        "updated_at": time.time(), "local_path": str(local_file) if local_file else None,
        "remote_path": str(data.get("remote_path") or data.get("remote_dir") or ""),
        "error": None, "message": "Waiting for FTP worker.",
    }
    with FTP_JOB_LOCK:
        FTP_JOBS[job_id] = entry
        if len(FTP_JOBS) > 100:
            oldest = sorted(FTP_JOBS, key=lambda key: FTP_JOBS[key].get("created_at", 0))[:20]
            for key in oldest:
                FTP_JOBS.pop(key, None)
    threading.Thread(target=ftp_transfer_worker, args=(job_id, mode, dict(data), local_file), name="HermesFTP-" + job_id, daemon=True).start()
    return job_id


@app.get("/api/integrations/status")
def api_integrations_status():
    return jsonify({
        "idm": {"available": bool(find_idm()), "path": find_idm()},
        "orbit_zero": {"configured": bool(find_companion_path("orbit_zero")), "path": str(find_companion_path("orbit_zero") or "")},
        "ps5upload": {"configured": bool(find_companion_path("ps5upload")), "path": str(find_companion_path("ps5upload") or "")},
        "filezilla": {"configured": bool(find_companion_path("filezilla")), "path": str(find_companion_path("filezilla") or "")},
        "ps5_ip": CONFIG.get("ps5_ip", ""),
        "ps5upload_engine_url": CONFIG.get("ps5upload_engine_url", "http://127.0.0.1:19113"),
        "ftp_host": CONFIG.get("ftp_host", ""),
        "ftp_port": CONFIG.get("ftp_port", 2122),
        "ftp_user": CONFIG.get("ftp_user", "anonymous"),
        "ftp_local_root": CONFIG.get("ftp_local_root", CONFIG["inbox_root"]),
    })


@app.post("/api/integrations/check")
def api_integrations_check():
    data = request.get_json(force=True) or {}
    try:
        raw_ip = data.get("ps5_ip", CONFIG.get("ps5_ip", ""))
        raw_engine_url = data.get("ps5upload_engine_url", CONFIG.get("ps5upload_engine_url", "http://127.0.0.1:19113"))
        ps5_ip = private_lan_ip(raw_ip, allow_loopback=False) if str(raw_ip or "").strip() else ""
        engine_url = validate_engine_url(raw_engine_url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    update_config({"ps5_ip": ps5_ip, "ps5upload_engine_url": engine_url})

    idm_path = find_idm()
    result: dict[str, Any] = {
        "idm": {"ok": bool(idm_path), "path": idm_path, "message": "IDM found." if idm_path else "IDM was not detected. Set its executable path in Settings."},
        "orbit": {"ok": False, "url": "http://" + ps5_ip + ":34177/" if ps5_ip else "", "message": "Enter your PS5's LAN IP." if not ps5_ip else "Not checked."},
        "ps5upload": {"engine_ok": False, "console_ok": False, "engine_url": engine_url, "message": "Engine not checked."},
    }

    if ps5_ip:
        orbit_url = "http://" + ps5_ip + ":34177/"
        try:
            response = requests.get(orbit_url, timeout=3, stream=True, allow_redirects=False)
            result["orbit"] = {
                "ok": response.status_code in (200, 301, 302, 303, 307, 308),
                "url": orbit_url,
                "http_status": response.status_code,
                "message": "Orbit Store web UI responded." if response.status_code in (200, 301, 302, 303, 307, 308) else "Orbit Store did not return a normal web response.",
            }
            response.close()
        except requests.RequestException as exc:
            result["orbit"] = {"ok": False, "url": orbit_url, "message": "No response from Orbit Store at port 34177: " + str(exc)}

    try:
        engine_response = requests.get(engine_url + "/api/jobs", timeout=3)
        result["ps5upload"]["engine_ok"] = engine_response.ok
        if engine_response.ok:
            try:
                jobs_data = engine_response.json()
                result["ps5upload"]["job_count"] = len(jobs_data) if isinstance(jobs_data, list) else None
            except ValueError:
                pass
            if ps5_ip:
                console_response = requests.get(engine_url + "/api/ps5/status", params={"addr": ps5_ip}, timeout=6)
                result["ps5upload"]["console_http_status"] = console_response.status_code
                try:
                    body = console_response.json()
                except ValueError:
                    body = {}
                result["ps5upload"]["console_ok"] = console_response.ok and body.get("ok", True) is not False
                result["ps5upload"]["console_response"] = body
                result["ps5upload"]["message"] = "Engine is online; console status request completed." if result["ps5upload"]["console_ok"] else "Engine is online, but the console did not return a successful status. Check jailbreak, helper and pairing."
            else:
                result["ps5upload"]["message"] = "Engine is online. Enter the PS5 LAN IP to check the console."
        else:
            result["ps5upload"]["message"] = "Engine returned HTTP " + str(engine_response.status_code) + "."
    except requests.RequestException as exc:
        result["ps5upload"]["message"] = "Engine not reachable at " + engine_url + ": " + str(exc)

    activity("Integration check completed.")
    return jsonify(result)


@app.post("/api/integrations/launch")
def api_integrations_launch():
    data = request.get_json(force=True) or {}
    component = str(data.get("component") or "")
    path = find_companion_path(component)
    if path is None:
        return jsonify({"error": "Executable not found. Set the path in Settings first."}), 404
    try:
        subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True)
    except Exception as exc:
        return jsonify({"error": "Could not launch " + component + ": " + str(exc)}), 500
    activity("Launched companion application: " + component)
    return jsonify({"ok": True, "component": component, "path": str(path)})


@app.post("/api/downloads/idm")
def api_download_with_idm():
    data = request.get_json(force=True) or {}
    raw_url = str(data.get("url") or "").strip()
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return jsonify({"error": "Enter a direct HTTP or HTTPS download URL without embedded credentials."}), 400
    destination_key = str(data.get("destination") or "download_root")
    allowed = {"download_root": CONFIG["download_root"], "game_root": CONFIG["game_root"], "inbox_root": CONFIG["inbox_root"]}
    destination_value = allowed.get(destination_key)
    if not destination_value:
        return jsonify({"error": "Choose a valid destination folder."}), 400
    destination = Path(destination_value)
    filename = safe_name(str(data.get("filename") or "").strip() or posixpath.basename(parsed.path) or "download.bin")
    if filename in {"", ".", ".."}:
        return jsonify({"error": "Filename is invalid."}), 400
    try:
        destination.mkdir(parents=True, exist_ok=True)
        if (destination / filename).exists():
            return jsonify({"error": "That filename already exists in the destination. Rename it or choose another folder; Hermes will not overwrite it."}), 409
    except OSError as exc:
        return jsonify({"error": "Cannot access the destination folder: " + str(exc)}), 400
    idm_path = find_idm()
    if not idm_path:
        return jsonify({"error": "IDM was not found. Set IDMan.exe in Settings."}), 404
    action = str(data.get("action") or "queue").lower()
    if action not in {"queue", "start"}:
        return jsonify({"error": "Action must be queue or start."}), 400
    args = ["/d", raw_url, "/p", str(destination), "/f", filename]
    if action == "queue":
        args.append("/a")
    else:
        args.insert(0, "/n")
    try:
        subprocess.Popen([idm_path, *args], cwd=str(Path(idm_path).parent), close_fds=True)
    except Exception as exc:
        return jsonify({"error": "Could not start IDM: " + str(exc)}), 500
    activity("Sent a download to IDM (" + action + "): " + filename)
    return jsonify({"ok": True, "action": action, "filename": filename, "destination": str(destination), "idm_path": idm_path, "message": "Request sent to IDM. Verify the item in IDM's own queue."}), 202


def ps5upload_origin(data: dict[str, Any] | None = None) -> str:
    data = data or {}
    raw = data.get("engine_url") or CONFIG.get("ps5upload_engine_url", "http://127.0.0.1:19113")
    return validate_engine_url(raw)


def remote_ps5_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or "\x00" in path or "\r" in path or "\n" in path:
        raise ValueError("Enter an absolute PS5 destination path beginning with /.")
    parts = path.split("/")
    if any(part == ".." for part in parts):
        raise ValueError("Parent-directory traversal is not allowed in PS5 paths.")
    if len(path) > 700:
        raise ValueError("Remote path is too long.")
    return posixpath.normpath(path)


def ps5upload_request(method: str, route: str, *, engine_url: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, timeout: int = 12) -> requests.Response:
    if not route.startswith("/api/") and not route.startswith("/pkg-host/"):
        raise ValueError("Invalid PS5Upload engine API route.")
    response = requests.request(
        method,
        engine_url + route,
        params=params,
        json=body,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    return response


@app.post("/api/ps5upload/transfer")
def api_ps5upload_transfer():
    data = request.get_json(force=True) or {}
    try:
        engine_url = ps5upload_origin(data)
        ps5_ip = private_lan_ip(data.get("ps5_ip") or CONFIG.get("ps5_ip"))
        local_file = allowed_local_transfer_file(data.get("local_path"))
        remote_directory = remote_ps5_path(data.get("remote_path"))
        remote_path = posixpath.join(remote_directory, local_file.name)
        update_config({"ps5_ip": ps5_ip, "ps5upload_engine_url": engine_url})
        if local_file.stat().st_size <= 0:
            return jsonify({"error": "The selected local file is empty."}), 400
        response = ps5upload_request(
            "POST",
            "/api/transfer/file",
            engine_url=engine_url,
            body={"addr": ps5_ip, "src": str(local_file), "dest": remote_path},
            timeout=20,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"error": response.text[:500]}
        if not response.ok:
            return jsonify({"error": result.get("error") or result.get("detail") or "PS5Upload rejected the transfer.", "engine_response": result}), response.status_code
        job_id = result.get("job_id") or result.get("job") or result.get("id")
        if not job_id:
            return jsonify({"error": "PS5Upload did not return a transfer job ID.", "engine_response": result}), 502
        activity("PS5Upload transfer queued: " + local_file.name + " → " + remote_path)
        return jsonify({"ok": True, "job_id": str(job_id), "kind": "transfer", "local_path": str(local_file), "remote_path": remote_path, "engine_url": engine_url}), 202
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": "PS5Upload engine request failed: " + str(exc)}), 502


@app.post("/api/ps5upload/pkg/inspect")
def api_ps5upload_pkg_inspect():
    data = request.get_json(force=True) or {}
    try:
        engine_url = ps5upload_origin(data)
        local_file = allowed_local_transfer_file(data.get("local_path"))
        if local_file.suffix.lower() != ".pkg":
            return jsonify({"error": "Choose a single .pkg file. Split package sets are not supported by this inspector."}), 400
        response = ps5upload_request("POST", "/api/pkg/parse", engine_url=engine_url, body={"path": str(local_file)}, timeout=30)
        try:
            metadata = response.json()
        except ValueError:
            metadata = {"error": response.text[:500]}
        if not response.ok:
            return jsonify({"error": metadata.get("error") or "PS5Upload could not inspect this package.", "metadata": metadata}), response.status_code
        return jsonify({"ok": True, "local_path": str(local_file), "metadata": metadata})
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": "PS5Upload package inspection failed: " + str(exc)}), 502


@app.post("/api/ps5upload/pkg/install")
def api_ps5upload_pkg_install():
    data = request.get_json(force=True) or {}
    try:
        engine_url = ps5upload_origin(data)
        ps5_ip = private_lan_ip(data.get("ps5_ip") or CONFIG.get("ps5_ip"))
        local_file = allowed_local_transfer_file(data.get("local_path"))
        update_config({"ps5_ip": ps5_ip, "ps5upload_engine_url": engine_url})
        if local_file.suffix.lower() != ".pkg":
            return jsonify({"error": "Choose a single .pkg file. Split package sets need to be selected through PS5Upload's own package workflow."}), 400
        if local_file.stat().st_size <= 0:
            return jsonify({"error": "The selected PKG file is empty."}), 400
        inspect_response = ps5upload_request(
            "POST",
            "/api/pkg/parse",
            engine_url=engine_url,
            body={"path": str(local_file)},
            timeout=30,
        )
        try:
            metadata = inspect_response.json()
        except ValueError:
            metadata = {}
        if not inspect_response.ok:
            return jsonify({"error": metadata.get("error") or "PS5Upload could not read this package's metadata."}), inspect_response.status_code
        content_id = str(metadata.get("content_id") or "").strip()
        title_id = str(metadata.get("title_id") or "").strip()
        category = str(metadata.get("category") or "").strip()
        app_ver = str(metadata.get("app_ver") or "").strip()
        title = str(metadata.get("title") or "").strip()
        if not content_id or not title_id or not category:
            return jsonify({"error": "Package metadata is incomplete (Content ID, Title ID or category missing). Hermes refused to install it so it cannot skip the installer's safety checks.", "metadata": metadata}), 400
        if title_id.upper().startswith("NPXS") or content_id.upper().startswith("NPXS"):
            return jsonify({"error": "System packages (NPXS titles) are not supported by this workflow. Use the console's own Package Installer workflow.", "metadata": metadata}), 400
        payload = {
            "ps5_addr": ps5_ip,
            "source": {"host_file": str(local_file)},
            "content_id": content_id,
            "title_id": title_id,
            "category": category,
            "package_app_ver": app_ver or None,
            "options": {
                "delete_source_copy_after": False,
                "allow_destructive_reinstall": False,
                "force_stream": False,
                "console_path_fallback": False,
                "proxy_link": False,
                "insecure_tls": False,
            },
        }
        response = ps5upload_request(
            "POST",
            "/api/pkg/install",
            engine_url=engine_url,
            body=payload,
            timeout=20,
        )
        try:
            result = response.json()
        except ValueError:
            result = {"error": response.text[:500]}
        if not response.ok or result.get("ok") is False or not result.get("job"):
            return jsonify({"error": result.get("error") or "PS5Upload did not accept the package installation.", "engine_response": result}), response.status_code if not response.ok else 502
        activity("PS5Upload install queued: " + local_file.name + " on " + ps5_ip)
        return jsonify({"ok": True, "job_id": str(result["job"]), "kind": "install", "local_path": str(local_file), "engine_url": engine_url, "metadata": {"title": title, "content_id": content_id, "title_id": title_id, "category": category, "app_ver": app_ver}}), 202
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": "PS5Upload engine request failed: " + str(exc)}), 502


@app.get("/api/ps5upload/jobs/<path:job_id>")
def api_ps5upload_job(job_id: str):
    kind = str(request.args.get("kind") or "transfer").lower()
    try:
        engine_url = ps5upload_origin()
        if not job_id or len(job_id) > 120 or any(ch in job_id for ch in "\\/?#"):
            return jsonify({"error": "Invalid job ID."}), 400
        if kind == "install":
            response = ps5upload_request("GET", "/api/pkg/install/status", engine_url=engine_url, params={"job": job_id}, timeout=8)
        elif kind == "transfer":
            response = ps5upload_request("GET", "/api/jobs/" + urllib.parse.quote(job_id, safe=""), engine_url=engine_url, timeout=8)
        else:
            return jsonify({"error": "Job kind must be transfer or install."}), 400
        try:
            result = response.json()
        except ValueError:
            result = {"error": response.text[:500]}
        return jsonify({"ok": response.ok, "kind": kind, "job": result}), response.status_code
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": "PS5Upload job status request failed: " + str(exc)}), 502


@app.post("/api/ftp/list")
def api_ftp_list():
    data = request.get_json(force=True) or {}
    ftp = None
    try:
        remote_path = ftp_remote_path(data.get("path") or "/")
        ftp, _ = open_ftp(data)
        ftp.cwd(remote_path)
        current = ftp.pwd()
        items: list[dict[str, Any]] = []
        try:
            for name, facts in ftp.mlsd():
                if name in {".", ".."}:
                    continue
                kind = facts.get("type", "unknown").lower()
                if kind in {"cdir", "pdir"}:
                    continue
                try:
                    size = int(facts.get("size", "0"))
                except (ValueError, TypeError):
                    size = None
                items.append({"name": name, "path": posixpath.join(current, name), "is_dir": kind == "dir", "size": size})
        except ftplib.all_errors:
            ftp.cwd(current)
            names = ftp.nlst()
            for raw_name in names:
                name = posixpath.basename(raw_name.rstrip("/"))
                if not name or name in {".", ".."}:
                    continue
                item_path = raw_name if raw_name.startswith("/") else posixpath.join(current, raw_name)
                is_dir = False
                try:
                    ftp.cwd(item_path)
                    is_dir = True
                    ftp.cwd(current)
                except ftplib.all_errors:
                    try:
                        ftp.cwd(current)
                    except ftplib.all_errors:
                        pass
                    try:
                        size = ftp.size(item_path)
                    except ftplib.all_errors:
                        size = None
                else:
                    size = None
                items.append({"name": name, "path": item_path, "is_dir": is_dir, "size": size})
        items.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return jsonify({"ok": True, "host": CONFIG.get("ftp_host") or data.get("host") or CONFIG.get("ps5_ip"), "path": current, "items": items})
    except Exception as exc:
        return jsonify({"error": "FTP browse failed: " + str(exc)}), 502
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


@app.post("/api/ftp/upload")
def api_ftp_upload():
    data = request.get_json(force=True) or {}
    try:
        local_file = allowed_local_transfer_file(data.get("local_path"))
        remote_dir = ftp_remote_path(data.get("remote_dir") or "/")
        data["remote_dir"] = remote_dir
        job_id = queue_ftp_transfer("upload", data, local_file)
        return jsonify({"ok": True, "job_id": job_id, "message": "FTP upload queued."}), 202
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/ftp/download")
def api_ftp_download():
    data = request.get_json(force=True) or {}
    try:
        data["remote_path"] = ftp_remote_path(data.get("remote_path"))
        job_id = queue_ftp_transfer("download", data)
        return jsonify({"ok": True, "job_id": job_id, "destination": CONFIG.get("ftp_local_root"), "message": "FTP download queued."}), 202
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/ftp/jobs")
def api_ftp_jobs():
    with FTP_JOB_LOCK:
        jobs = [dict(value) for value in FTP_JOBS.values()]
    jobs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return jsonify(jobs[:20])


# ---------------- PS5 Payload Sender ----------------
PAYLOAD_PLAYLIST_FILE = DATA_DIR / "payload_playlist.json"
PAYLOAD_DOWNLOAD_DIR = DATA_DIR / "payloads"
PAYLOAD_EXTENSIONS = {".elf", ".bin", ".payload"}
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
PAYLOAD_RELEASE_SOURCES = {
    "etaHEN/etaHEN": {
        "label": "etaHEN",
        "api_url": "https://api.github.com/repos/etaHEN/etaHEN/releases/latest",
        "repo_url": "https://github.com/etaHEN/etaHEN",
    },
    "EchoStretch/kstuff": {
        "label": "kstuff",
        "api_url": "https://api.github.com/repos/EchoStretch/kstuff/releases/latest",
        "repo_url": "https://github.com/EchoStretch/kstuff",
    },
}
PAYLOAD_JOBS: dict[str, dict[str, Any]] = {}
PAYLOAD_JOB_LOCK = threading.Lock()


def load_payload_playlist() -> list[dict[str, Any]]:
    raw = load_json(PAYLOAD_PLAYLIST_FILE, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("id") and item.get("path")]


def save_payload_playlist(items: list[dict[str, Any]]) -> None:
    save_json(PAYLOAD_PLAYLIST_FILE, items)


def payload_item_for_response(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    path = Path(str(item.get("path", "")))
    result["exists"] = path.is_file()
    if result["exists"]:
        try:
            result["size"] = path.stat().st_size
        except OSError:
            result["size"] = item.get("size", 0)
    return result


def payload_job_update(job_id: str, **updates: Any) -> None:
    with PAYLOAD_JOB_LOCK:
        job = PAYLOAD_JOBS.get(job_id)
        if job is not None:
            job.update(updates)
            job["updated_at"] = time.time()


def payload_sender_worker(job_id: str, source_path: str, target_ip: str, target_port: int) -> None:
    path = Path(source_path)
    try:
        total_bytes = path.stat().st_size
        payload_job_update(job_id, status="connecting", total_bytes=total_bytes, bytes_sent=0)
        with socket.create_connection((target_ip, target_port), timeout=6) as client:
            client.settimeout(15)
            payload_job_update(job_id, status="sending")
            bytes_sent = 0
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    client.sendall(chunk)
                    bytes_sent += len(chunk)
                    payload_job_update(job_id, bytes_sent=bytes_sent)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        payload_job_update(
            job_id,
            status="sent",
            bytes_sent=bytes_sent,
            completed_at=time.time(),
            message="All payload bytes were written to the TCP connection. This does not confirm that the PS5 loader executed the payload.",
        )
        activity(f"PAYLOAD SENT: {path.name} -> {target_ip}:{target_port} ({bytes_sent} bytes written)")
    except Exception as exc:
        payload_job_update(job_id, status="failed", error=str(exc), completed_at=time.time())
        activity(f"PAYLOAD SEND FAILED: {path.name} -> {target_ip}:{target_port}: {exc}")


def fetch_latest_payload_release(repository: str) -> dict[str, Any]:
    source = PAYLOAD_RELEASE_SOURCES.get(repository)
    if not source:
        raise ValueError("Only the configured official etaHEN and EchoStretch/kstuff repositories are supported.")
    response = requests.get(
        source["api_url"],
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Hermes-Payload-Sender"},
        timeout=15,
    )
    if response.status_code == 404:
        raise ValueError(f"No published release was found for {repository}.")
    response.raise_for_status()
    release = response.json()
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("The upstream API did not return a stable latest release.")
    assets = []
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        if Path(name).suffix.lower() not in PAYLOAD_EXTENSIONS:
            continue
        if not asset.get("browser_download_url") or not asset.get("size"):
            continue
        assets.append({
            "name": name,
            "size": int(asset["size"]),
            "download_count": int(asset.get("download_count") or 0),
            "digest": asset.get("digest"),
        })
    if not assets:
        raise ValueError(f"The latest release for {repository} has no .elf, .bin or .payload asset.")
    tag = str(release.get("tag_name") or "")
    notes = str(release.get("body") or "")
    compatibility_note = ""
    if repository == "EchoStretch/kstuff":
        if "10.01" in notes and re.search(r"3[.]00", notes):
            compatibility_note = f"The {tag} release notes describe support for PS5 firmware 3.00–10.01. That does not document support for firmware 13.60; do not send this build there unless an official source explicitly confirms compatibility."
        else:
            compatibility_note = f"Verify the exact firmware range for {tag} in the official release notes before sending. Do not assume that the newest release supports every firmware."
    elif repository == "etaHEN/etaHEN":
        if not re.search(r"(^|[^0-9])v?2[.]?6([bB]?)([^0-9]|$)", tag):
            compatibility_note = f"The official GitHub latest-stable endpoint currently returns {tag}, not an etaHEN 2.6 tag. Check the official release page if you expected a newer build, and verify firmware compatibility before sending."
    return {
        "repository": repository,
        "label": source["label"],
        "release_name": release.get("name") or tag,
        "tag": tag,
        "published_at": release.get("published_at"),
        "url": release.get("html_url") or source["repo_url"],
        "repo_url": source["repo_url"],
        "release_notes": notes[:1600],
        "compatibility_note": compatibility_note,
        "assets": assets,
    }


@app.get("/api/payloads")
def api_payloads_list():
    items = [payload_item_for_response(item) for item in load_payload_playlist()]
    with PAYLOAD_JOB_LOCK:
        jobs = sorted(
            (dict(job) for job in PAYLOAD_JOBS.values()),
            key=lambda item: float(item.get("created_at", 0)),
            reverse=True,
        )[:20]
    return jsonify({"items": items, "jobs": jobs})


@app.get("/api/payloads/releases")
def api_payloads_latest_releases():
    releases = []
    errors = {}
    for repository in PAYLOAD_RELEASE_SOURCES:
        try:
            releases.append(fetch_latest_payload_release(repository))
        except Exception as exc:
            errors[repository] = str(exc)
    return jsonify({
        "releases": releases,
        "errors": errors,
        "checked_at": time.time(),
    })


@app.post("/api/payloads/add-local")
def api_payloads_add_local():
    data = request.get_json(force=True) or {}
    raw_path = str(data.get("path") or data.get("local_path") or "").strip().strip('"')
    if not raw_path:
        return jsonify({"error": "Choose a local payload file first."}), 400
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("The selected payload path is not a file.")
        if path.suffix.lower() not in PAYLOAD_EXTENSIONS:
            raise ValueError("Use a .elf, .bin or .payload file.")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("The selected payload file is empty.")
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload files larger than 256 MiB are not accepted.")
    except (OSError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    items = load_payload_playlist()
    existing = next((item for item in items if str(Path(str(item.get("path", ""))).resolve()) == str(path)), None)
    if existing:
        return jsonify({"ok": True, "already_added": True, "item": payload_item_for_response(existing)})
    item = {
        "id": uuid.uuid4().hex,
        "name": str(data.get("name") or path.stem).strip()[:100] or path.stem,
        "filename": path.name,
        "path": str(path),
        "size": size,
        "source": "local",
        "added_at": time.time(),
    }
    items.append(item)
    save_payload_playlist(items)
    activity(f"PAYLOAD ADDED: {path.name} ({size} bytes)")
    return jsonify({"ok": True, "item": payload_item_for_response(item)}), 201


@app.post("/api/payloads/releases/download")
def api_payloads_download_latest():
    data = request.get_json(force=True) or {}
    repository = str(data.get("repository") or "")
    asset_name = str(data.get("asset_name") or "")
    if repository not in PAYLOAD_RELEASE_SOURCES or not asset_name or Path(asset_name).name != asset_name:
        return jsonify({"error": "Select a payload asset from a listed official release."}), 400
    try:
        release = fetch_latest_payload_release(repository)
        asset_meta = next((asset for asset in release["assets"] if asset["name"] == asset_name), None)
        if not asset_meta:
            return jsonify({"error": "That asset is no longer part of the latest official release. Refresh the release list."}), 409
        if asset_meta["size"] > MAX_PAYLOAD_BYTES:
            return jsonify({"error": "The upstream asset is larger than the 256 MiB safety limit."}), 400
        source = PAYLOAD_RELEASE_SOURCES[repository]
        api_response = requests.get(
            source["api_url"],
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Hermes-Payload-Sender"},
            timeout=15,
        )
        api_response.raise_for_status()
        latest = api_response.json()
        if str(latest.get("tag_name") or "") != str(release.get("tag") or ""):
            return jsonify({"error": "The upstream latest release changed while you were downloading. Refresh the release list and try again."}), 409
        remote_asset = next(
            (asset for asset in latest.get("assets", []) if asset.get("name") == asset_name),
            None,
        )
        if not remote_asset or Path(str(remote_asset.get("name") or "")).suffix.lower() not in PAYLOAD_EXTENSIONS:
            return jsonify({"error": "Could not re-verify the asset against the official release API."}), 409
        if int(remote_asset.get("size") or -1) != int(asset_meta["size"]):
            return jsonify({"error": "The upstream asset metadata changed. Refresh the release list and try again."}), 409
        download_url = str(remote_asset.get("browser_download_url") or "")
        parsed_url = urllib.parse.urlparse(download_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "github.com" or not parsed_url.path.startswith(f"/{repository}/releases/download/"):
            return jsonify({"error": "The official release returned an unexpected download URL."}), 502
        target_dir = PAYLOAD_DOWNLOAD_DIR / repository
        target_dir = target_dir / re.sub(r"[^A-Za-z0-9._-]+", "_", str(release["tag"]))
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / asset_name
        temp_path = target_dir / (asset_name + "." + uuid.uuid4().hex + ".part")
        digest = hashlib.sha256()
        size_written = 0
        try:
            with requests.get(download_url, headers={"User-Agent": "Hermes-Payload-Sender"}, stream=True, timeout=(12, 30)) as download:
                download.raise_for_status()
                with temp_path.open("wb") as handle:
                    for chunk in download.iter_content(chunk_size=128 * 1024):
                        if not chunk:
                            continue
                        size_written += len(chunk)
                        if size_written > MAX_PAYLOAD_BYTES:
                            raise ValueError("The downloaded asset exceeded the 256 MiB safety limit.")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if size_written != int(remote_asset.get("size") or -1):
                raise ValueError(f"Download size mismatch: expected {remote_asset.get('size')} bytes, received {size_written}.")
            expected_digest = remote_asset.get("digest")
            actual_digest = "sha256:" + digest.hexdigest()
            if expected_digest and expected_digest.lower() != actual_digest.lower():
                raise ValueError("SHA-256 verification failed for the official release asset.")
            if target_path.exists():
                existing_digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
                if existing_digest != digest.hexdigest():
                    target_path = target_dir / (Path(asset_name).stem + "-" + digest.hexdigest()[:8] + Path(asset_name).suffix)
            os.replace(temp_path, target_path) if not target_path.exists() else temp_path.unlink(missing_ok=True)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        items = load_payload_playlist()
        existing = next((item for item in items if item.get("path") == str(target_path)), None)
        if existing:
            return jsonify({"ok": True, "already_added": True, "item": payload_item_for_response(existing), "release": release})
        item = {
            "id": uuid.uuid4().hex,
            "name": f"{release['label']} {release['tag']}",
            "filename": target_path.name,
            "path": str(target_path),
            "size": size_written,
            "source": "official",
            "repository": repository,
            "version": release["tag"],
            "release_url": release["url"],
            "sha256": digest.hexdigest(),
            "added_at": time.time(),
        }
        items.append(item)
        save_payload_playlist(items)
        activity(f"PAYLOAD DOWNLOADED: {repository}@{release['tag']} / {asset_name} ({size_written} bytes; SHA-256 verified)")
        return jsonify({"ok": True, "item": payload_item_for_response(item), "release": release}), 201
    except requests.RequestException as exc:
        return jsonify({"error": f"GitHub download failed: {exc}"}), 502
    except (OSError, ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/payloads/reorder")
def api_payloads_reorder():
    data = request.get_json(force=True) or {}
    ordered_ids = data.get("ids")
    if not isinstance(ordered_ids, list) or any(not isinstance(item_id, str) for item_id in ordered_ids):
        return jsonify({"error": "Send an ordered list of playlist IDs."}), 400
    items = load_payload_playlist()
    current_ids = [str(item.get("id")) for item in items]
    if len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
        return jsonify({"error": "Playlist order must include every existing entry exactly once."}), 400
    by_id = {str(item.get("id")): item for item in items}
    reordered = [by_id[item_id] for item_id in ordered_ids]
    save_payload_playlist(reordered)
    return jsonify({"ok": True, "items": [payload_item_for_response(item) for item in reordered]})


@app.post("/api/payloads/remove")
def api_payloads_remove():
    data = request.get_json(force=True) or {}
    item_id = str(data.get("id") or "")
    items = load_payload_playlist()
    remaining = [item for item in items if str(item.get("id")) != item_id]
    if len(remaining) == len(items):
        return jsonify({"error": "Payload entry not found."}), 404
    save_payload_playlist(remaining)
    # Intentionally keep all files on disk, including downloaded release assets.
    return jsonify({"ok": True, "items": [payload_item_for_response(item) for item in remaining]})


@app.post("/api/payloads/send")
def api_payloads_send():
    data = request.get_json(force=True) or {}
    item_id = str(data.get("id") or "")
    raw_ip = str(data.get("ip") or CONFIG.get("ps5_ip") or "").strip()
    try:
        target_ip = private_lan_ip(raw_ip)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        target_port = int(data.get("port") or 9021)
        if not 1 <= target_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a TCP port between 1 and 65535 (common PS5 payload-loader ports are 9020 and 9021)."}), 400
    items = load_payload_playlist()
    item = next((entry for entry in items if str(entry.get("id")) == item_id), None)
    if item is None:
        return jsonify({"error": "Choose a payload from the playlist."}), 404
    try:
        path = Path(str(item.get("path") or "")).resolve(strict=True)
        if not path.is_file():
            raise ValueError("Payload file not found on this PC.")
        if path.suffix.lower() not in PAYLOAD_EXTENSIONS:
            raise ValueError("Only .elf, .bin and .payload files can be sent.")
        size = path.stat().st_size
        if size <= 0 or size > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload must be non-empty and no larger than 256 MiB.")
    except (OSError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        "id": job_id,
        "payload_id": item_id,
        "name": item.get("name") or path.name,
        "filename": path.name,
        "target_ip": target_ip,
        "target_port": target_port,
        "status": "queued",
        "bytes_sent": 0,
        "total_bytes": size,
        "message": "Waiting for the sender thread.",
        "created_at": now,
        "updated_at": now,
    }
    with PAYLOAD_JOB_LOCK:
        PAYLOAD_JOBS[job_id] = job
    threading.Thread(
        target=payload_sender_worker,
        args=(job_id, str(path), target_ip, target_port),
        name=f"HermesPayload-{job_id[:8]}",
        daemon=True,
    ).start()
    activity(f"PAYLOAD SEND QUEUED: {path.name} -> {target_ip}:{target_port}")
    return jsonify({"ok": True, "job": dict(job)}), 202


@app.get("/api/payloads/jobs/<job_id>")
def api_payloads_job(job_id: str):
    with PAYLOAD_JOB_LOCK:
        job = PAYLOAD_JOBS.get(job_id)
        if job is not None:
            return jsonify(dict(job))
    return jsonify({"error": "Payload send job not found; restart may have cleared the in-memory history."}), 404


@app.get("/manager")
def manager():
    return render_template("manager.html")


@app.get("/api/storage/drive")
def api_storage_drive():
    try:
        root = Path(CONFIG["game_root"])
        usage = shutil.disk_usage(root.anchor or root)
        return jsonify({"total": usage.total, "used": usage.used, "free": usage.free})
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/storage/scan")
def api_storage_scan():
    data = request.get_json(force=True) or {}
    root_text = str(data.get("root") or "").strip()
    try:
        root = Path(root_text).expanduser()
        if not root_text or not root.exists() or not root.is_dir():
            return jsonify({"error": "Choose an existing folder or drive."}), 400
        limit = max(100, min(50000, int(data.get("limit", 10000))))
    except (ValueError, OSError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    excluded = {
        "$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION", ".GIT",
        ".VENV", "NODE_MODULES", "__PYCACHE__", ".NEXT",
        "WINDOWSAPPS", "WINSXS",
    }
    extensions: dict[str, dict[str, int]] = {}
    largest: list[dict[str, Any]] = []
    same_name_size: dict[tuple[str, int], list[str]] = {}
    total_bytes = 0
    file_count = 0
    directory_count = 0
    inspected = 0
    truncated = False

    def remember_large(path: Path, size: int) -> None:
        largest.append({"path": str(path), "bytes": size})
        largest.sort(key=lambda item: item["bytes"], reverse=True)
        del largest[30:]

    try:
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = [name for name in dirs if name.upper() not in excluded and not Path(current, name).is_symlink()]
            directory_count += len(dirs)
            for filename in files:
                if inspected >= limit:
                    truncated = True
                    break
                inspected += 1
                path = Path(current) / filename
                try:
                    if path.is_symlink():
                        continue
                    size = path.stat().st_size
                except OSError:
                    continue
                file_count += 1
                total_bytes += size
                ext = path.suffix.lower() or "[no extension]"
                bucket = extensions.setdefault(ext, {"count": 0, "bytes": 0})
                bucket["count"] += 1
                bucket["bytes"] += size
                key = (filename.lower(), size)
                same_name_size.setdefault(key, []).append(str(path))
                remember_large(path, size)
            if truncated:
                break
    except OSError as exc:
        return jsonify({"error": "Could not scan folder: " + str(exc)}), 500

    duplicate_candidates = [
        {"name": key[0], "bytes": key[1], "paths": paths[:10], "matches": len(paths)}
        for key, paths in same_name_size.items()
        if len(paths) > 1
    ]
    duplicate_candidates.sort(key=lambda item: (len(item["paths"]), item["bytes"]), reverse=True)

    return jsonify({
        "root": str(root.resolve()),
        "entries": inspected,
        "files": file_count,
        "folders": directory_count,
        "bytes": total_bytes,
        "truncated": truncated,
        "extensions": extensions,
        "large": largest,
        "duplicate_candidates": duplicate_candidates[:50],
    })


@app.get("/covers/<path:name>")
def cover_file(name: str):
    filename = Path(name).name
    cover_dir = Path(CONFIG["automation_root"]) / "covers"
    path = cover_dir / filename
    if not path.exists() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"error": "Cover not found."}), 404
    return send_file(path)


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


@app.post("/api/resolve-from-browser")
def api_resolve_from_browser():
    data = request.get_json(force=True) or {}
    ppsa = extract_ppsa(data.get("ppsa") or "")
    if not ppsa:
        return jsonify({"error": "Enter a valid PPSA, for example PPSA18089."}), 400

    source_url = str(data.get("url") or "")
    if urllib.parse.urlparse(source_url).hostname not in {"prosperopatches.com", "www.prosperopatches.com"}:
        return jsonify({"error": "Metadata can only be imported from ProsperoPatches."}), 400

    raw_title = str(data.get("title") or "").strip()
    if not raw_title or len(raw_title) > 250:
        return jsonify({"error": "The browser page did not expose a usable game title."}), 400
    title = safe_name(raw_title)
    if title.upper() in {ppsa, "UNKNOWN DOWNLOAD", "PROSPEROPATCHES.COM"}:
        return jsonify({"error": "The browser page did not expose a usable game title."}), 400

    preferred_cover = str(data.get("cover") or "").strip() or None
    if preferred_cover and urllib.parse.urlparse(preferred_cover).scheme not in {"http", "https"}:
        preferred_cover = None

    _, cover_name = download_cover(title, ppsa, preferred_cover)
    override_file = DATA_DIR / "title_overrides.json"
    overrides = load_json(override_file, {})
    if not isinstance(overrides, dict):
        overrides = {}
    overrides[ppsa] = {
        "title": title,
        "source": prosperopatches_url(ppsa),
        "cover": cover_name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "resolver": "visible-browser",
    }
    save_json(override_file, overrides)
    activity("Saved visible-browser metadata override for " + ppsa + " → " + title)
    return jsonify({
        "title_id": ppsa,
        "title": title,
        "source": prosperopatches_url(ppsa),
        "cover": cover_name,
        "resolver": "visible-browser",
    })


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


def open_browser() -> None:
    if os.environ.get("HERMES_DESKTOP") == "1":
        return
    time.sleep(1.0)
    try:
        webbrowser.open("http://127.0.0.1:8765")
    except Exception:
        pass


if __name__ == "__main__":
    ensure_layout()
    WATCHER.start()
    threading.Thread(target=open_browser, name="HermesBrowser", daemon=True).start()
    print("Hermes DL running at http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False)
