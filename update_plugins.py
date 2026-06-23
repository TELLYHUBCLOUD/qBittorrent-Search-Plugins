#!/usr/bin/env python3
"""
qBittorrent Search Plugins Auto-Updater
Designed for GitHub Actions workflow — zero config, just run.

Usage:
    python update_plugins.py

Environment:
    GITHUB_TOKEN  — auto-provided by GitHub Actions (higher rate limits)
"""

import os
import re
import json
import time
import hashlib
import shutil
import zipfile
import sqlite3
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — matches workflow expectations exactly
# ═══════════════════════════════════════════════════════════════════════════════
PUBLIC_DIR  = Path("public_sites")
PRIVATE_DIR = Path("private_sites")
ENGINES_DIR = Path("engines")
ZIP_NAME    = Path("qBittorrent-Search-Plugins-Complete.zip")
DB_PATH     = "/tmp/qbt_plugins.db"          # /tmp — never committed

GITHUB_API   = "https://api.github.com"
GITLAB_API   = "https://gitlab.com/api/v4"
CODEBERG_API = "https://codeberg.org/api/v1"
WIKI_URL     = "https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins"

THREADS = 8
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

SKIP_FILES = frozenset({
    "__init__.py", "helpers.py", "helpers_106.py", "helpers_122.py",
    "novaprinter.py", "novaprinter_107.py", "novaprinter_125.py",
    "nova2.py", "nova2dl.py", "sgmllib3.py", "socks.py",
    "validate_plugin.py", "setup.py", "requirements.txt",
    "test_bitsearch.py", "test_bitsearch_real.py",
    "final_test.py", "main.py", "ripper.py", "sqliteplugin.py",
    "telegram_torrent_bot.py", "update_plugins.py",
})

# ── Known plugin repos (unique) ───────────────────────────────────────────────
KNOWN_REPOS = [
    "LightDestory/qBittorrent-Search-Plugins",
    "BurningMop/qBittorrent-Search-Plugins",
    "nklido/qBittorrent_search_engines",
    "hannsen/qbittorrent_search_plugins",
    "Cc050511/qBit-search-plugins",
    "nindogo/qbtSearchScripts",
    "imDMG/qBittorrent_Search_Plugins",
    "iordic/qbittorrent-search-plugins",
    "tolotp/qbittorrent-search-plugins",
    "444995/qbittorrent-search-plugins",
    "bugsbringer/qbit-plugins",
    "AlaaBrahim/qBitTorrent-animetosho-search-plugin",
    "TuckerWarlock/qbittorrent-search-plugins",
    "galaris/BTDigg-qBittorrent-plugin",
    "MarcBresson/cpasbien",
    "ZH1637/dmhy",
    "lazulyra/qbittorrent-yts-plugin",
    "Bioux1/qbittorrent-search-plugins",
    "bebetoh/qbittorrent-search-plugins",
    "Cycloctane/qbittorrent-mikan-plugin",
    "vt-idiot/qbittorrent-sukebei-plugin",
    "elazar/qbittorrent-cloudtorrents-plugin",
    "joseeloren/qbittorrent-search-plugins",
    "menegop/qbittorrent-search-plugins",
    "msagca/qbittorrent-plugins",
    "caiocinel/qbittorrent-onlinefix-plugin",
    "kli885/qbittorrent-subsplease-plugin",
    "Ashalda/qbittorrent-sktorrent-plugin",
    "BrunoReX/qbittorrent-search-plugins",
    "YGGverse/qbittorrent-yggtracker-plugin",
    "OptimusKoala/qbittorrent-c411-plugin",
    "RaresPNet/qbittorrent-filelist-plugin",
    "Ooggle/qbittorrent-gazelle-games-plugin",
    "txtsd/qB-IPT",
    "darktohka/qbittorrent-ncore-plugin",
    "TainakaDrums/qbittorrent-pornolab-plugin",
    "swannie-eire/qbittorrent-prowlarr-plugin",
    "Evyd13/qbittorrent-redacted-plugin",
    "nbusseneau/qBittorrent-RuTracker-plugin",
    "etn/qbittorrent-sharewood-plugin",
    "miIiano/qbittorrent-speedapp-plugin",
    "MjKey/qbittorrent-tapochek-plugin",
    "PlayDay/qbittorrent-gurtom-plugin",
    "CrimsonKoba/qbittorrent-unionfansub-plugin",
    "Laiteux/qbittorrent-yggapi-plugin",
    "CravateRouge/qbittorrent-yggtorrent-plugin",
    "Necrosis/qbittorrent-zamunda-plugin",
    "DrPurp/qbittorrent-eztv-plugin",
    "scadams/qbittorrent-search-plugins",
    "v1k1/magnetdl-qbittorrent-plugin",
    "hdvinnie/qBittorrent-Search-Plugins",
    "bnlf/qbittorrent-search-plugin",
    "LevyFialho/qbittorrent-search-plugins",
    "quorums/qbittorrent-search-plugins",
    "balansse/qbittorrent-piratebay-plugin",
    "jivoi/qbittorrent-torrserver-plugin",
    "freecoder76/qBittorrent-search-plugins",
    "ddd9898/qbittorrent-plugin",
    "soyYo/torrentbytes-qbt-plugin",
    "GomorrA1/qbittorrent-search-plugins",
    "HiItsD/jackett-search-plugin",
    "noxs1de/prowlarr-qbittorrent-plugin",
    "alessandro-ooo/one-click-qbittorrent-searchplugins",
]

COLLECTION_REPOS = [
    "https://github.com/alessandro-ooo/one-click-qbittorrent-searchplugins",
    "https://github.com/nklido/qBittorrent_search_engines",
    "https://github.com/darktohka/qbittorrent-plugins",
    "https://github.com/LightDestory/qBittorrent-Search-Plugins",
    "https://github.com/BurningMop/qBittorrent-Search-Plugins",
    "https://github.com/hdvvine/qBittorrent-Search-Plugins",
    "https://github.com/freecoder76/qBittorrent-search-plugins",
]

GITHUB_REPO_QUERIES = [
    "qBittorrent search plugin language:python",
    "qbittorrent search engine python",
    "qbittorrent search plugin torrent",
    "qbittorrent-plugin python torrent",
    "qbt search plugin python",
]

GITHUB_TOPIC_QUERIES = [
    "qbittorrent-search-plugin", "qbittorrent-plugin",
    "qbittorrent-search", "qbittorrent",
]

GITHUB_CODE_QUERIES = [
    "class SearchEngine filename:.py qbittorrent",
    "noSearchResult qbittorrent filename:.py",
    "supported_categories qbittorrent filename:.py",
]

# ═══════════════════════════════════════════════════════════════════════════════
# PLUGIN VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════
PLUGIN_SIGS = [
    b"noSearchResult", b"class SearchEngine", b"prettyPrinting",
    b"supported_categories", b"url_dl", b"CORE_SITE",
    b"magnetdl", b"qbittorrentapi",
]
BAD_SIGS = [b"<!DOCTYPE", b"<html", b"404: Not Found", b"Not Found"]

def is_valid_plugin(content: bytes) -> bool:
    if not (400 <= len(content) <= 512_000):
        return False
    for bad in BAD_SIGS:
        if bad in content[:300]:
            return False
    for sig in PLUGIN_SIGS:
        if sig in content:
            return True
    return b"def search(" in content and b"class " in content

def is_private_plugin(content: bytes) -> bool:
    low = content.lower()
    signals = [b"passkey", b"api_key", b"apikey", b"auth_token",
               b"bearer", b"username", b"password", b"private",
               b"cookie", b"login", b"authenticate"]
    return sum(1 for s in signals if s in low) >= 2

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════════════════════
def http_get(url, timeout=20, retries=4, accept_html=False):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                rem = resp.headers.get("X-RateLimit-Remaining", "999")
                rst = resp.headers.get("X-RateLimit-Reset")
                if rem == "0" and rst:
                    w = max(0, int(rst) - int(time.time())) + 5
                    print(f"        Rate-limited — sleeping {w}s")
                    time.sleep(w)
                data = resp.read()
                if not accept_html and (
                    data[:15].lstrip().startswith(b"<!DOCTYPE")
                    or data[:6].lstrip().startswith(b"<html")
                ):
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** (attempt + 3))
            elif e.code in (404, 451):
                return None
            elif attempt == retries - 1:
                return None
            else:
                time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt)
    return None

def fetch_text(url, timeout=15):
    d = http_get(url, timeout=timeout, accept_html=True)
    return d.decode("utf-8", errors="ignore") if d else None

def api_get(url, token=None, timeout=20):
    h = {"Accept": "application/json"}
    if token and "github" in url:
        h["Authorization"] = f"token {token}"
        h["Accept"] = "application/vnd.github+json"
    elif token and ("gitlab" in url or "codeberg" in url):
        h["Authorization"] = f"Bearer {token}"
    d = http_get(url, headers=h, timeout=timeout)
    if d is None:
        return None
    try:
        return json.loads(d)
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def chash(data):
    return hashlib.sha256(data).hexdigest()[:20]

def to_raw(url):
    if not url:
        return ""
    url = url.split("#")[0]
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    if "gitlab.com" in url and "/-/blob/" in url:
        url = re.sub(r"/-/blob/", "/-/raw/", url)
    if "codeberg.org" in url and "/src/branch/" in url:
        url = url.replace("/src/branch/", "/raw/branch/")
    return url

def safe_name(name, url):
    uf = os.path.basename(url.split("?")[0].split("#")[0])
    if uf.endswith(".py") and len(uf) > 3 and uf not in SKIP_FILES:
        return uf
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "", name.replace(" ", "_")).lower()
    return f"{clean}.py" if clean else "unknown.py"

# ═══════════════════════════════════════════════════════════════════════════════
# WIKI PARSER
# ═══════════════════════════════════════════════════════════════════════════════
class WikiParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = None
        self.in_table = False
        self.in_cell = False
        self.cell = {"text": "", "links": []}
        self.row = []
        self.public = []
        self.private = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and "name" in d:
            n = d["name"].lower()
            if "public" in n: self.section = "public"
            elif "private" in n: self.section = "private"
        if tag in ("h1","h2","h3","h4") and "id" in d:
            i = d["id"].lower()
            if "public" in i: self.section = "public"
            elif "private" in i: self.section = "private"
        if tag == "table" and self.section:
            self.in_table = True
        if self.in_table:
            if tag == "tr":
                self.row = []
            elif tag in ("td","th"):
                self.in_cell = True
                self.cell = {"text":"","links":[]}
            elif tag == "a" and self.in_cell and "href" in d:
                self.cell["links"].append(d["href"])

    def handle_endtag(self, tag):
        if not self.in_table: return
        if tag == "table":
            self.in_table = False
        elif tag == "tr":
            if self.row and len(self.row) >= 5:
                if "search engine" not in self.row[0]["text"].lower():
                    if self.section == "public": self.public.append(self.row)
                    elif self.section == "private": self.private.append(self.row)
        elif tag in ("td","th"):
            self.in_cell = False
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)

    def handle_data(self, data):
        if self.in_cell:
            self.cell["text"] += data

# ═══════════════════════════════════════════════════════════════════════════════
# STORE — thread-safe, dedup, tracks metadata
# ═══════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen = set()
        self.results = []
        self.counts = {"new": 0, "dup": 0, "invalid": 0, "skip": 0, "fail": 0}
        self.failures = []
        for d in [PUBLIC_DIR, PRIVATE_DIR, ENGINES_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    def add(self, content, filename, url, engine_name="", engine_site="",
            author="", repo="", version="", last_update="", comments="",
            forge="wiki", wiki_private=False):
        if filename in SKIP_FILES:
            self.counts["skip"] += 1
            return "skip"
        if not is_valid_plugin(content):
            self.counts["invalid"] += 1
            return "invalid"

        h = chash(content)
        with self.lock:
            if h in self.seen:
                self.counts["dup"] += 1
                return "dup"
            self.seen.add(h)

        priv = wiki_private or is_private_plugin(content)
        target = PRIVATE_DIR if priv else PUBLIC_DIR
        fp = target / filename

        # collision
        if fp.exists():
            stem = fp.stem
            c = 1
            while fp.exists():
                fp = target / f"{stem}_{c}.py"
                c += 1
            filename = fp.name

        fp.write_bytes(content)
        self.counts["new"] += 1

        # also in engines/
        ep = ENGINES_DIR / filename
        if not ep.exists():
            ep.write_bytes(content)

        self.results.append({
            "engine_name": engine_name, "engine_site": engine_site,
            "author_name": author, "repo_link": repo,
            "version": version, "last_update": " ".join(last_update.split()),
            "filename": filename, "download_url": url,
            "comments": " ".join(comments.split()), "file_size": len(content),
            "is_private": priv, "forge": forge,
        })
        return "new"

    def log_fail(self, url, reason):
        self.failures.append({"url": url, "reason": reason})
        self.counts["fail"] += 1

# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD + STORE
# ═══════════════════════════════════════════════════════════════════════════════
def dl(url, store, filename="", engine_name="", engine_site="",
       author="", repo="", version="", last_update="", comments="",
       forge="unknown", wiki_private=False):
    raw = to_raw(url)
    if not raw:
        store.log_fail(url, "no_url")
        return "fail"
    content = http_get(raw, timeout=15)
    if content is None:
        store.log_fail(raw, "download_failed")
        return "fail"
    if not filename:
        filename = safe_name(engine_name, raw)
    return store.add(content, filename, raw, engine_name, engine_site,
                     author, repo, version, last_update, comments,
                     forge, wiki_private)

# ═══════════════════════════════════════════════════════════════════════════════
# GITHUB
# ═══════════════════════════════════════════════════════════════════════════════
def gh_api(path, params=None):
    url = f"{GITHUB_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return api_get(url, GITHUB_TOKEN)

def gh_search_repos(query, max_pages=2):
    found = []
    for page in range(1, max_pages + 1):
        data = gh_api("/search/repositories", {
            "q": query, "sort": "updated", "per_page": 100, "page": page
        })
        if not data or not data.get("items"): break
        for item in data["items"]:
            fn = item["full_name"]
            if fn not in KNOWN_REPOS:
                KNOWN_REPOS.append(fn)
                found.append(fn)
        if len(data["items"]) < 100: break
        time.sleep(2)
    return found

def gh_search_topics(topic):
    data = gh_api("/search/repositories", {
        "q": f"topic:{topic}", "sort": "updated", "per_page": 100
    })
    if not data: return []
    found = []
    for item in data.get("items", []):
        fn = item["full_name"]
        if fn not in KNOWN_REPOS:
            KNOWN_REPOS.append(fn)
            found.append(fn)
    return found

def gh_search_code(query, max_pages=1):
    items = []
    for page in range(1, max_pages + 1):
        data = gh_api("/search/code", {
            "q": query + " extension:py", "per_page": 100, "page": page
        })
        if not data or not data.get("items"): break
        for item in data["items"]:
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            html_url = item.get("html_url", "")
            if html_url and path.endswith(".py") and path not in SKIP_FILES:
                items.append({"repo": repo, "path": path, "url": html_url})
        if len(data["items"]) < 100: break
        time.sleep(3)
    return items

def gh_list_files(full_name):
    files = []
    owner, repo = full_name.split("/", 1)
    for sub in ["", "engines", "plugins", "search_engines", "src"]:
        data = gh_api(f"/repos/{owner}/{repo}/contents/{sub}")
        if not data or not isinstance(data, list): continue
        for item in data:
            if (item.get("type") == "file"
                    and item["name"].endswith(".py")
                    and item["name"] not in SKIP_FILES):
                dl_url = item.get("download_url", "")
                if dl_url:
                    files.append({
                        "repo": full_name, "path": item["path"],
                        "url": dl_url, "name": item["name"],
                    })
        time.sleep(0.3)
    return files

# ═══════════════════════════════════════════════════════════════════════════════
# GITLAB
# ═══════════════════════════════════════════════════════════════════════════════
def gl_list_files(pid):
    files = []
    for sub in ["", "engines", "plugins"]:
        url = (f"{GITLAB_API}/projects/{urllib.parse.quote(pid, safe='')}"
               f"/repository/tree?path={sub}&per_page=100")
        data = api_get(url, GITLAB_TOKEN)
        if not data or not isinstance(data, list): continue
        for item in data:
            if (item.get("type") == "blob"
                    and item["name"].endswith(".py")
                    and item["name"] not in SKIP_FILES):
                raw = (f"{GITLAB_API}/projects/{urllib.parse.quote(pid, safe='')}"
                       f"/repository/files/{urllib.parse.quote(item['path'], safe='')}"
                       f"/raw?ref=main")
                files.append({"repo": pid, "path": item["path"],
                              "url": raw, "name": item["name"]})
        time.sleep(0.5)
    return files

# ═══════════════════════════════════════════════════════════════════════════════
# CODEBERG
# ═══════════════════════════════════════════════════════════════════════════════
def cb_list_files(full_name):
    files = []
    owner, repo = full_name.split("/", 1)
    for sub in ["", "engines", "plugins"]:
        url = f"{CODEBERG_API}/repos/{owner}/{repo}/contents/{sub}"
        data = api_get(url)
        if not data or not isinstance(data, list): continue
        for item in data:
            if (item.get("type") == "file"
                    and item["name"].endswith(".py")
                    and item["name"] not in SKIP_FILES):
                dl_url = item.get("download_url", "")
                if dl_url:
                    files.append({"repo": full_name, "path": item["path"],
                                  "url": dl_url, "name": item["name"]})
        time.sleep(0.5)
    return files

# ═══════════════════════════════════════════════════════════════════════════════
# BULK DOWNLOAD HELPER
# ═══════════════════════════════════════════════════════════════════════════════
def bulk_download(files, store, forge="github"):
    if not files: return 0
    new = 0
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futs = {}
        for fi in files:
            f = pool.submit(dl, fi["url"], store, filename=fi.get("name", ""),
                            repo=fi.get("repo", ""), forge=forge)
            futs[f] = fi["url"]
        for f in as_completed(futs):
            try:
                if f.result() == "new":
                    new += 1
            except Exception:
                pass
    return new

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — WIKI
# ═══════════════════════════════════════════════════════════════════════════════
def phase_wiki(store):
    print("[1/4] Wiki...")
    html = fetch_text(WIKI_URL, timeout=20)
    if not html:
        print("      Wiki fetch FAILED")
        return

    wp = WikiParser()
    wp.feed(html)
    print(f"      Rows: {len(wp.public)} public, {len(wp.private)} private")

    # Build tasks
    tasks = []
    for rows, priv in [(wp.public, False), (wp.private, True)]:
        for row in rows:
            ename = row[0]["text"].strip()
            esite = row[0]["links"][0] if row[0]["links"] else ""
            aname = row[1]["text"].strip()
            rlink = row[1]["links"][0] if row[1]["links"] else ""
            ver = row[2]["text"] if len(row) > 2 else "1.0"
            lup = row[3]["text"] if len(row) > 3 else "N/A"
            dlc = row[4] if len(row) > 4 else None
            comm = row[5]["text"] if len(row) > 5 else ""
            raw = to_raw(dlc["links"][0] if dlc and dlc["links"] else "")
            if not raw:
                store.log_fail("", f"Wiki: {ename} — no URL")
                continue
            fname = safe_name(ename, raw)
            tasks.append((raw, fname, ename, esite, aname, rlink,
                          ver, lup, comm, priv))

    ok = 0
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futs = {}
        for (url, fn, en, es, an, rl, v, lu, c, p) in tasks:
            f = pool.submit(dl, url, store, filename=fn, engine_name=en,
                            engine_site=es, author=an, repo=rl, version=v,
                            last_update=lu, comments=c, forge="wiki",
                            wiki_private=p)
            futs[f] = en
        for f in as_completed(futs):
            try:
                if f.result() == "new":
                    ok += 1
            except Exception:
                pass
    print(f"      Downloaded: {ok}")

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — GITHUB
# ═══════════════════════════════════════════════════════════════════════════════
def phase_github(store):
    print("[2/4] GitHub API...")

    # Repo search
    for q in GITHUB_REPO_QUERIES:
        found = gh_search_repos(q, max_pages=2)
        if found:
            print(f"      Repo '{q[:35]}...' → +{len(found)}")

    # Topic search
    for topic in GITHUB_TOPIC_QUERIES:
        found = gh_search_topics(topic)
        if found:
            print(f"      Topic '{topic}' → +{len(found)}")

    # Code search (only with token)
    code_files = []
    if GITHUB_TOKEN:
        for q in GITHUB_CODE_QUERIES:
            code_files.extend(gh_search_code(q, max_pages=1))

    # Scan all repos
    all_files = []
    total_repos = len(KNOWN_REPOS)
    print(f"      Scanning {total_repos} repos...")
    for i, repo in enumerate(KNOWN_REPOS):
        all_files.extend(gh_list_files(repo))
        if (i + 1) % 25 == 0 or (i + 1) == total_repos:
            print(f"        {i+1}/{total_repos} — {len(all_files)} files found")
        time.sleep(0.25)

    new1 = bulk_download(all_files, store, "github")
    print(f"      GitHub repos: {new1} new")

    if code_files:
        new2 = bulk_download(code_files, store, "github-code")
        print(f"      GitHub code: {new2} new")

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — GITLAB + CODEBERG + COLLECTIONS
# ═══════════════════════════════════════════════════════════════════════════════
def phase_other(store):
    print("[3/4] GitLab / Codeberg / Collections...")

    # GitLab
    for q in ["qbittorrent search plugin", "qbittorrent plugin"]:
        url = f"{GITLAB_API}/projects?search={urllib.parse.quote(q)}&per_page=50"
        data = api_get(url, GITLAB_TOKEN)
        if data and isinstance(data, list):
            for proj in data:
                pid = proj["path_with_namespace"]
                desc = (proj.get("description") or "").lower()
                if "qbittorrent" in desc or "plugin" in desc or "torrent" in desc:
                    files = gl_list_files(pid)
                    if files:
                        n = bulk_download(files, store, "gitlab")
                        if n:
                            print(f"      GitLab {pid}: +{n}")
        time.sleep(1)

    # Codeberg
    for q in ["qbittorrent search plugin", "qbt plugin"]:
        data = api_get(
            f"{CODEBERG_API}/repos/search?q={urllib.parse.quote(q)}&limit=50"
        )
        if data and isinstance(data, dict):
            for r in data.get("data", []):
                fn = r.get("full_name", "")
                if fn:
                    files = cb_list_files(fn)
                    if files:
                        n = bulk_download(files, store, "codeberg")
                        if n:
                            print(f"      Codeberg {fn}: +{n}")
        time.sleep(1)

    # Collection repos (HTML scrape)
    tasks = []
    for repo_url in COLLECTION_REPOS:
        html = fetch_text(repo_url, timeout=10)
        if not html: continue
        for match in re.findall(r'href="([^"]+\.py)"', html):
            full = urllib.parse.urljoin(repo_url, match)
            full = to_raw(full)
            name = os.path.basename(full)
            if name not in SKIP_FILES:
                tasks.append((full, name, repo_url))

    if tasks:
        print(f"      Collection links: {len(tasks)}")
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futs = {pool.submit(dl, u, store, filename=n, repo=r, forge="collection"): u
                    for u, n, r in tasks}
            new = sum(1 for f in as_completed(futs)
                      if f.result() == "new")
        print(f"      Collections: +{new}")

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — OUTPUT
# ═══════════════════════════════════════════════════════════════════════════════
def phase_output(store):
    print("[4/4] Generating outputs...")
    c = store.counts
    print(f"      {c['new']} new | {c['dup']} dup | "
          f"{c['invalid']} invalid | {c['skip']} skip | {c['fail']} fail")

    pub_count  = len(list(PUBLIC_DIR.glob("*.py")))
    priv_count = len(list(PRIVATE_DIR.glob("*.py")))
    eng_count  = len(list(ENGINES_DIR.glob("*.py")))

    pub_r  = [r for r in store.results if not r["is_private"]]
    priv_r = [r for r in store.results if r["is_private"]]

    # ── README.md ─────────────────────────────────────────────────────────────
    def rows(items, start=1):
        lines = []
        for i, it in enumerate(items, start):
            en = f"[{it['engine_name']}]({it['engine_site']})" if it['engine_site'] else it['engine_name']
            au = f"[{it['author_name']}]({it['repo_link']})" if it['repo_link'] else it['author_name']
            dl = f"[`{it['filename']}`]({it['download_url']})"
            lines.append(f"| {i} | {en} | {au} | {it['version']} | {it['last_update']} | {dl} | `{it.get('forge','wiki')}` | {it['comments']} |")
        return "\n".join(lines)

    repo_env = os.environ.get("GITHUB_REPOSITORY", "user/qBittorrent-Search-Plugins")

    readme = f"""# qBittorrent Search Plugins — Complete Collection

[![Auto-Update](https://github.com/{repo_env}/actions/workflows/auto-update.yml/badge.svg)](https://github.com/{repo_env}/actions/workflows/auto-update.yml)

Aggregates **all** unofficial qBittorrent search engine plugins from the
[Official Wiki](https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins),
GitHub, GitLab, Codeberg, and community collections.

> **DISCLAIMER**: Unofficial plugins. All credits to original authors. Use at your own risk.

## Stats

| Metric | Count |
|--------|-------|
| **Total plugins** | {eng_count} |
| Public sites | {pub_count} |
| Private sites | {priv_count} |

## Download

- **[qBittorrent-Search-Plugins-Complete.zip](https://raw.githubusercontent.com/{repo_env}/main/qBittorrent-Search-Plugins-Complete.zip)**
- `public_sites/` — Public torrent site plugins
- `private_sites/` — Private tracker plugins (may need credentials)
- `engines/` — All plugins combined

## Install

1. Open **qBittorrent** → **Search** tab
2. Click **Search plugins...** → **Install a new one**
3. Select **Local file** and pick `.py` files

## Public Sites ({pub_count})

| # | Search Engine | Author | Version | Updated | Download | Source | Comments |
|---|---------------|--------|---------|---------|----------|--------|----------|
{rows(pub_r)}

## Private Sites ({priv_count})

| # | Search Engine | Author | Version | Updated | Download | Source | Comments |
|---|---------------|--------|---------|---------|----------|--------|----------|
{rows(priv_r, len(pub_r) + 1)}

## Testing

```bash
python3 nova2.py <plugin_name> all "ubuntu"
