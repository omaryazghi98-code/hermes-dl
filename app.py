from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
PROJECTS_FILE = DATA_DIR / "projects.json"
DATA_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")

LINK_RE = re.compile(r"https?://[^\s<>\"']+")
MF_FILE_RE = re.compile(r"/file(?:_premium)?/[^/]+/([^/]+)/file", re.I)
PART_RE = re.compile(r"\.part(\d+)\.rar$", re.I)


def load_projects():
    if not PROJECTS_FILE.exists():
        return {}
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_projects(data):
    PROJECTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def filename_from_url(url: str) -> str:
    m = MF_FILE_RE.search(urllib.parse.urlparse(url).path)
    if m:
        return urllib.parse.unquote(m.group(1))
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return Path(path).name or "download.bin"


def part_number(name: str):
    m = PART_RE.search(name)
    return int(m.group(1)) if m else None


def normalize_links(text: str):
    links = LINK_RE.findall(text or "")
    # preserve user ordering but remove exact duplicates
    out, seen = [], set()
    for u in links:
        u = u.strip().rstrip(",.;")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def find_idm():
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Internet Download Manager" / "IDMan.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Internet Download Manager" / "IDMan.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Internet Download Manager" / "IDMan.exe",
    ]
    for p in candidates:
        if p and p.exists():
            return str(p)
    path = shutil.which("IDMan.exe") or shutil.which("idman.exe")
    return path


def local_state(folder: str, filename: str, direct_url: str | None = None, expected_size: int | None = None):
    if not folder:
        return {"status": "unknown", "size": 0}
    p = Path(folder) / filename
    if not p.exists():
        return {"status": "missing", "size": 0}
    try:
        size = p.stat().st_size
    except OSError:
        return {"status": "unknown", "size": 0}
    if size == 0:
        return {"status": "partial", "size": 0}
    if expected_size is None and direct_url:
        expected_size = remote_size(direct_url)
    if expected_size is not None:
        if size == expected_size:
            return {"status": "present", "size": size, "verified": True}
        return {"status": "partial", "size": size, "expected_size": expected_size, "verified": False}
    return {"status": "present", "size": size, "verified": False}


def remote_size(url: str):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            v = r.headers.get("Content-Length")
            return int(v) if v and v.isdigit() else None
    except Exception:
        return None


def resolve_requests(page_url: str):
    req = urllib.request.Request(
        page_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")

    patterns = [
        r'<a[^>]+id=["\']downloadButton["\'][^>]+href=["\']([^"\']+)["\']',
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']downloadButton["\']',
        r'https://download\d+\.mediafire\.com/[^"\'<>\\ ]+',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I | re.S)
        if not m:
            continue
        candidate = m.group(1) if m.lastindex else m.group(0)
        candidate = urllib.parse.urljoin(page_url, candidate)
        candidate = candidate.replace("&amp;", "&")
        if re.match(r"^https://download\d+\.mediafire\.com/", candidate, re.I):
            return candidate
    raise RuntimeError("MediaFire direct link was not present in the page HTML")


def resolve_playwright(page_url: str):
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run setup.bat first.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            selectors = [
                "#downloadButton",
                "a#downloadButton",
                "a.download_link",
            ]
            href = None
            for selector in selectors:
                loc = page.locator(selector).first
                if loc.count():
                    href = loc.get_attribute("href")
                    if href:
                        break
            if not href:
                # Some page revisions render the download anchor after JS work.
                page.wait_for_timeout(3000)
                for selector in selectors:
                    loc = page.locator(selector).first
                    if loc.count():
                        href = loc.get_attribute("href")
                        if href:
                            break
            if not href:
                raise RuntimeError("MediaFire download button was not found")
            href = urllib.parse.urljoin(page_url, href)
            if not re.match(r"^https://download\d+\.mediafire\.com/", href, re.I):
                raise RuntimeError("MediaFire returned a non-direct download URL")
            return href
        finally:
            browser.close()


def resolve_url(page_url: str, mode="auto"):
    errors = []
    if mode in ("auto", "http"):
        try:
            return resolve_requests(page_url), "http"
        except Exception as e:
            errors.append(str(e))
    if mode in ("auto", "browser"):
        try:
            return resolve_playwright(page_url), "browser"
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError(" | ".join(errors) or "Unable to resolve URL")


def idm_add(idm, direct_url, folder, filename):
    if not idm:
        raise RuntimeError("IDM.exe was not found")
    Path(folder).mkdir(parents=True, exist_ok=True)
    cmd = [idm, "/n", "/a", "/d", direct_url, "/p", folder, "/f", filename]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def idm_start(idm):
    if not idm:
        raise RuntimeError("IDM.exe was not found")
    subprocess.run([idm, "/s"], check=True, capture_output=True, text=True)


@app.get("/")
def index():
    return render_template("index.html")



def safe_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\\\|?*]+', " ", (name or "").strip())
    name = re.sub(r"\\s+", " ", name).strip().strip(".")
    return name[:120] or "Untitled Download"


def scan_library(root: str):
    if not root:
        raise ValueError("Root folder is required")
    base = Path(root).expanduser()
    if not base.exists():
        return {"root": str(base), "folders": [], "sets": []}

    folders = []
    sets = {}
    try:
        for child in base.iterdir():
            if child.is_dir():
                folders.append({"name": child.name, "path": str(child)})
    except OSError:
        pass

    locations = [base] + [Path(x["path"]) for x in folders]
    for folder in locations:
        try:
            for f in folder.iterdir():
                if not f.is_file():
                    continue
                m = PART_RE.search(f.name)
                if not m:
                    continue
                prefix = f.name[:m.start()]
                key = f"{folder.resolve()}::{prefix}"
                entry = sets.setdefault(key, {
                    "folder": str(folder),
                    "name": prefix.rstrip(". -_") or folder.name,
                    "parts": [],
                    "count": 0,
                    "total_bytes": 0,
                })
                entry["parts"].append(f.name)
                entry["count"] += 1
                try:
                    entry["total_bytes"] += f.stat().st_size
                except OSError:
                    pass
        except OSError:
            continue

    for entry in sets.values():
        entry["parts"].sort(key=lambda n: (
            part_number(n) if part_number(n) is not None else 999999, n.lower()
        ))
    return {
        "root": str(base),
        "folders": sorted(folders, key=lambda x: x["name"].lower()),
        "sets": list(sets.values())
    }


@app.get("/api/config")
def config():
    return jsonify({
        "idm": find_idm(),
        "playwright": sync_playwright is not None,
        "projects": load_projects(),
    })



@app.get("/api/library/scan")
def library_scan():
    root = (request.args.get("root") or "").strip()
    try:
        return jsonify(scan_library(root))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/library/folder")
def library_folder():
    data = request.get_json(force=True) or {}
    root = (data.get("root") or "").strip()
    name = safe_folder_name(data.get("name") or "")
    if not root:
        return jsonify({"error": "Root folder is required"}), 400
    target = Path(root).expanduser() / name
    try:
        target.mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True, "path": str(target)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/projects")
def create_project():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "Untitled Queue").strip()
    folder = (data.get("folder") or "").strip()
    if data.get("auto_folder"):
        root = (data.get("root") or r"D:\JB PS5 Backups").strip()
        folder = str(Path(root).expanduser() / safe_folder_name(name))
        Path(folder).mkdir(parents=True, exist_ok=True)
    links = normalize_links(data.get("links") or "")
    if not links:
        return jsonify({"error": "No URLs found."}), 400

    items = []
    for idx, url in enumerate(links, 1):
        fn = filename_from_url(url)
        items.append({
            "id": f"item-{idx}",
            "source": url,
            "filename": fn,
            "part": part_number(fn),
            "direct": None,
            "remote_size": None,
            "status": "pending",
            "message": "Ready to resolve",
        })
    projects = load_projects()
    project_id = f"p-{int(time.time()*1000)}"
    projects[project_id] = {"id": project_id, "name": name, "folder": folder, "items": items, "created": int(time.time())}
    save_projects(projects)
    return jsonify(projects[project_id])


@app.get("/api/projects/<pid>")
def get_project(pid):
    project = load_projects().get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    return jsonify(project)


@app.delete("/api/projects/<pid>")
def delete_project(pid):
    projects = load_projects()
    if pid in projects:
        del projects[pid]
        save_projects(projects)
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/scan")
def scan_project(pid):
    projects = load_projects()
    project = projects.get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    folder = project.get("folder", "")
    for item in project["items"]:
        st = local_state(folder, item["filename"], item.get("direct"), item.get("remote_size"))
        if st["status"] == "present":
            item["status"] = "complete"
            item["message"] = "Complete — size verified" if st.get("verified") else "File exists — remote size unavailable"
        elif st["status"] == "partial":
            item["status"] = "partial"
            item["message"] = "Partial/empty local file"
        else:
            item["status"] = "missing"
            item["message"] = "Not downloaded"
    save_projects(projects)
    return jsonify(project)


@app.post("/api/projects/<pid>/resolve")
def resolve_project(pid):
    projects = load_projects()
    project = projects.get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    data = request.get_json(force=True) or {}
    mode = data.get("mode", "auto")
    ids = set(data.get("ids") or [x["id"] for x in project["items"]])
    for item in project["items"]:
        if item["id"] not in ids:
            continue
        if item["status"] == "complete":
            continue
        try:
            direct, resolver = resolve_url(item["source"], mode)
            item["direct"] = direct
            item["remote_size"] = remote_size(direct)
            item["status"] = "ready"
            item["message"] = f"Resolved via {resolver}"
        except Exception as e:
            item["status"] = "error"
            item["message"] = str(e)
    save_projects(projects)
    return jsonify(project)


@app.post("/api/projects/<pid>/queue")
def queue_project(pid):
    projects = load_projects()
    project = projects.get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    data = request.get_json(force=True) or {}
    ids = set(data.get("ids") or [])
    include_all = not ids
    idm = find_idm()
    queued = []
    failures = []
    for item in project["items"]:
        if item["status"] == "complete":
            continue
        if not include_all and item["id"] not in ids:
            continue
        if not item.get("direct"):
            failures.append({"filename": item["filename"], "error": "Not resolved yet"})
            continue
        try:
            idm_add(idm, item["direct"], project["folder"], item["filename"])
            item["status"] = "queued"
            item["message"] = "Added to IDM queue"
            queued.append(item["filename"])
        except Exception as e:
            item["status"] = "error"
            item["message"] = str(e)
            failures.append({"filename": item["filename"], "error": str(e)})
    save_projects(projects)
    return jsonify({"project": project, "queued": queued, "failures": failures, "idm": idm})


@app.post("/api/projects/<pid>/start")
def start_project(pid):
    project = load_projects().get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    try:
        idm_start(find_idm())
        return jsonify({"ok": True, "message": "IDM queue started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/projects/<pid>/open-folder")
def open_folder(pid):
    project = load_projects().get(pid)
    if not project:
        return jsonify({"error": "Project not found"}), 404
    folder = project.get("folder")
    if not folder:
        return jsonify({"error": "No destination folder configured"}), 400
    Path(folder).mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(folder)
    else:
        subprocess.Popen(["xdg-open", folder])
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("IDM Queue Studio running at http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False)
