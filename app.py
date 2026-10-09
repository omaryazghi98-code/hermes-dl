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
    pattern = rf'<meta[^>]+(?:property|name)=["\\']{re.escape(property_name)}["\\'][^>]+content=["\\'](.*?)["\\']'
    match = re.search(pattern, page_html or "", flags=re.I | re.S)
    if match:
        return html.unescape(match.group(1)).strip() or None

    reverse_pattern = rf'<meta[^>]+content=["\\'](.*?)["\\'][^>]+(?:property|name)=["\\']{re.escape(property_name)}["\\']'
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
        {"name": key[0], "bytes": key[1], "paths": paths}
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
