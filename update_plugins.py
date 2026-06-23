#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  qBittorrent Search Plugin ULTIMATE Collector v4.0 — Full-Net Edition       ║
║                                                                              ║
║  SOURCES:  Wiki · GitHub REST · GitLab API · Codeberg API · Awesome-lists   ║
║  FEATURES: Validation · Dedup · Threading · SQLite · Rate-limit · ZIP       ║
║                                                                              ║
║  Usage:                                                                      ║
║    python qbt_plugin_collector.py                        # default run       ║
║    python qbt_plugin_collector.py --github-token TOKEN   # higher limits     ║
║    python qbt_plugin_collector.py --gitlab-token TOKEN   # GitLab auth       ║
║    python qbt_plugin_collector.py --no-discovery         # wiki only         ║
║    python qbt_plugin_collector.py --threads 16           # concurrency       ║
║    python qbt_plugin_collector.py --out ./my-plugins     # custom output     ║
║                                                                              ║
║  Environment:  GITHUB_TOKEN / GITLAB_TOKEN                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import re
import json
import time
import hashlib
import argparse
import sqlite3
import shutil
import zipfile
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
DEFAULT_OUT   = "qBittorrent-Search-Plugins-Repo"
GITHUB_API    = "https://api.github.com"
GITLAB_API    = "https://gitlab.com/api/v4"
CODEBERG_API  = "https://codeberg.org/api/v1"
WIKI_URL      = "https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

SKIP_FILES = frozenset({
    "__init__.py", "helpers.py", "helpers_106.py", "helpers_122.py",
    "novaprinter.py", "novaprinter_107.py", "novaprinter_125.py",
    "nova2.py", "nova2dl.py", "sgmllib3.py", "socks.py",
    "validate_plugin.py", "test_bitsearch.py", "test_bitsearch_real.py",
    "final_test.py", "main.py", "ripper.py", "sqliteplugin.py",
    "telegram_torrent_bot.py", "setup.py", "requirements.txt",
})

# ── Search queries ────────────────────────────────────────────────────────────
GITHUB_REPO_QUERIES = [
    "qBittorrent search plugin language:python",
    "qbittorrent search engine python",
    "qbittorrent search plugin torrent",
    "qbittorrent-plugin python torrent",
    "qbittorrent-search-plugins",
    "qbt search plugin python",
    "python torrent search plugin qbittorrent",
]

GITHUB_TOPIC_QUERIES = [
    "qbittorrent-search-plugin", "qbittorrent-plugin",
    "qbittorrent-search", "qbittorrent",
]

GITHUB_CODE_QUERIES = [
    "class SearchEngine filename:.py qbittorrent",
    "noSearchResult qbittorrent filename:.py",
    "prettyPrinting filename:.py qbittorrent",
    "supported_categories qbittorrent filename:.py",
    "url_dl qbittorrent filename:.py",
]

GITLAB_QUERIES = [
    "qbittorrent search plugin python",
    "qbittorrent plugin",
    "qbt search engine",
]

CODEBERG_QUERIES = [
    "qbittorrent search plugin",
    "qbt plugin python",
]

AWESOME_LIST_URLS = [
    "https://raw.githubusercontent.com/qbittorrent/search-plugins/master/README.md",
    "https://raw.githubusercontent.com/Ravencentric/awesome-qbittorrent/main/README.md",
]

# ── Known plugin repos (deduplicated) ─────────────────────────────────────────
KNOWN_REPOS = {
    "LightDestory":  "LightDestory/qBittorrent-Search-Plugins",
    "BurningMop":    "BurningMop/qBittorrent-Search-Plugins",
    "nklido":        "nklido/qBittorrent_search_engines",
    "hannsen":       "hannsen/qbittorrent_search_plugins",
    "Cc050511":      "Cc050511/qBit-search-plugins",
    "nindogo":       "nindogo/qbtSearchScripts",
    "imDMG":         "imDMG/qBittorrent_Search_Plugins",
    "iordic":        "iordic/qbittorrent-search-plugins",
    "tolotp":        "tolotp/qbittorrent-search-plugins",
    "444995":        "444995/qbittorrent-search-plugins",
    "bugsbringer":   "bugsbringer/qbit-plugins",
    "AlaaBrahim":    "AlaaBrahim/qBitTorrent-animetosho-search-plugin",
    "TuckerWarlock": "TuckerWarlock/qbittorrent-search-plugins",
    "galaris":       "galaris/BTDigg-qBittorrent-plugin",
    "MarcBresson":   "MarcBresson/cpasbien",
    "ZH1637":        "ZH1637/dmhy",
    "lazulyra":      "lazulyra/qbittorrent-yts-plugin",
    "Bioux1":        "Bioux1/qbittorrent-search-plugins",
    "bebetoh":       "bebetoh/qbittorrent-search-plugins",
    "Cycloctane":    "Cycloctane/qbittorrent-mikan-plugin",
    "vt-idiot":      "vt-idiot/qbittorrent-sukebei-plugin",
    "elazar":        "elazar/qbittorrent-cloudtorrents-plugin",
    "joseeloren":    "joseeloren/qbittorrent-search-plugins",
    "menegop":       "menegop/qbittorrent-search-plugins",
    "msagca":        "msagca/qbittorrent-plugins",
    "caiocinel":     "caiocinel/qbittorrent-onlinefix-plugin",
    "kli885":        "kli885/qbittorrent-subsplease-plugin",
    "Ashalda":       "Ashalda/qbittorrent-sktorrent-plugin",
    "BrunoReX":      "BrunoReX/qbittorrent-search-plugins",
    "YGGverse":      "YGGverse/qbittorrent-yggtracker-plugin",
    "OptimusKoala":  "OptimusKoala/qbittorrent-c411-plugin",
    "RaresPNet":     "RaresPNet/qbittorrent-filelist-plugin",
    "Ooggle":        "Ooggle/qbittorrent-gazelle-games-plugin",
    "txtsd":         "txtsd/qB-IPT",
    "darktohka":     "darktohka/qbittorrent-ncore-plugin",
    "TainakaDrums":  "TainakaDrums/qbittorrent-pornolab-plugin",
    "swannie-eire":  "swannie-eire/qbittorrent-prowlarr-plugin",
    "Evyd13":        "Evyd13/qbittorrent-redacted-plugin",
    "nbusseneau":    "nbusseneau/qBittorrent-RuTracker-plugin",
    "etn":           "etn/qbittorrent-sharewood-plugin",
    "miIiano":       "miIiano/qbittorrent-speedapp-plugin",
    "MjKey":         "MjKey/qbittorrent-tapochek-plugin",
    "PlayDay":       "PlayDay/qbittorrent-gurtom-plugin",
    "CrimsonKoba":   "CrimsonKoba/qbittorrent-unionfansub-plugin",
    "Laiteux":       "Laiteux/qbittorrent-yggapi-plugin",
    "CravateRouge":  "CravateRouge/qbittorrent-yggtorrent-plugin",
    "Necrosis":      "Necrosis/qbittorrent-zamunda-plugin",
    "DrPurp":        "DrPurp/qbittorrent-eztv-plugin",
    "scadams":       "scadams/qbittorrent-search-plugins",
    "MagnetDL":      "v1k1/magnetdl-qbittorrent-plugin",
    "hdvinnie":      "hdvinnie/qBittorrent-Search-Plugins",
    "bnlf":          "bnlf/qbittorrent-search-plugin",
    "LevyFialho":    "LevyFialho/qbittorrent-search-plugins",
    "quorums":       "quorums/qbittorrent-search-plugins",
    "balansse":      "balansse/qbittorrent-piratebay-plugin",
    "jivoi":         "jivoi/qbittorrent-torrserver-plugin",
    "freecoder76":   "freecoder76/qBittorrent-search-plugins",
    "ddd9898":       "ddd9898/qbittorrent-plugin",
    "soyYo":         "soyYo/torrentbytes-qbt-plugin",
    "GomorrA1":      "GomorrA1/qbittorrent-search-plugins",
    "HiItsD":        "HiItsD/jackett-search-plugin",
    "noxs1de":       "noxs1de/prowlarr-qbittorrent-plugin",
    "Prowlarr":      "Prowlarr/Prowlarr",
    "Jackett":       "Jackett/Jackett",
    "alessandro":    "alessandro-ooo/one-click-qbittorrent-searchplugins",
}

# ── Collection repos to scrape for .py links ──────────────────────────────────
COLLECTION_REPOS = [
    "https://github.com/alessandro-ooo/one-click-qbittorrent-searchplugins",
    "https://github.com/nklido/qBittorrent_search_engines",
    "https://github.com/darktohka/qbittorrent-plugins",
    "https://github.com/HazukiShiro/qBittorrent-Search-Plugins",
    "https://github.com/LightDestory/qBittorrent-Search-Plugins",
    "https://github.com/BurningMop/qBittorrent-Search-Plugins",
    "https://github.com/hdvinnie/qBittorrent-Search-Plugins",
    "https://github.com/freecoder76/qBittorrent-search-plugins",
]

# ═══════════════════════════════════════════════════════════════════════════════
# PLUGIN VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════
PLUGIN_SIGNATURES = [
    b"noSearchResult", b"class SearchEngine", b"prettyPrinting",
    b"supported_categories", b"url_dl", b"CORE_SITE",
    b"magnetdl", b"qbittorrentapi",
]

PLUGIN_BLACKLIST = [b"<!DOCTYPE", b"<html", b"404: Not Found", b"Not Found"]
MIN_PLUGIN_BYTES = 400
MAX_PLUGIN_BYTES = 512_000

def is_valid_plugin(content: bytes) -> bool:
    """Return True if content looks like a real qBittorrent search plugin."""
    if not (MIN_PLUGIN_BYTES <= len(content) <= MAX_PLUGIN_BYTES):
        return False
    for bad in PLUGIN_BLACKLIST:
        if bad in content[:300]:
            return False
    for sig in PLUGIN_SIGNATURES:
        if sig in content:
            return True
    if b"def search(" in content and b"class " in content:
        return True
    return False

def classify_plugin(content: bytes) -> bool:
    """Return True if plugin appears to be for a private tracker."""
    low = content.lower()
    private_signals = [
        b"passkey", b"api_key", b"apikey", b"auth_token",
        b"bearer", b"username", b"password", b"private",
        b"cookie", b"login", b"authenticate",
    ]
    score = sum(1 for s in private_signals if s in low)
    return score >= 2

# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE DATABASE
# ═══════════════════════════════════════════════════════════════════════════════
class PluginDB:
    DDL = """
    CREATE TABLE IF NOT EXISTS plugins (
        content_hash  TEXT PRIMARY KEY,
        filename      TEXT NOT NULL,
        engine_name   TEXT DEFAULT '',
        engine_site   TEXT DEFAULT '',
        is_public     INTEGER DEFAULT 1,
        first_seen    TEXT,
        last_seen     TEXT,
        byte_size     INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sources (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        content_hash TEXT REFERENCES plugins(content_hash),
        source_url   TEXT UNIQUE,
        repo         TEXT DEFAULT '',
        author       TEXT DEFAULT '',
        version      TEXT DEFAULT '',
        last_update  TEXT DEFAULT '',
        forge        TEXT DEFAULT 'unknown',
        discovered   TEXT
    );
    CREATE TABLE IF NOT EXISTS failures (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT,
        url    TEXT,
        reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(content_hash);
    CREATE INDEX IF NOT EXISTS idx_plugins_public ON plugins(is_public);
    """

    def __init__(self, path: str):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.con.executescript(self.DDL)
        self.con.commit()

    def has_content(self, h: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM plugins WHERE content_hash=?", (h,)
        ).fetchone() is not None

    def has_source(self, url: str) -> bool:
        return self.con.execute(
            "SELECT 1 FROM sources WHERE source_url=?", (url,)
        ).fetchone() is not None

    def upsert_plugin(self, ch, filename, engine_name="", engine_site="",
                      is_public=True, byte_size=0):
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            existing = self.con.execute(
                "SELECT first_seen FROM plugins WHERE content_hash=?", (ch,)
            ).fetchone()
            if existing:
                self.con.execute(
                    "UPDATE plugins SET last_seen=?, byte_size=? WHERE content_hash=?",
                    (now, byte_size, ch))
            else:
                self.con.execute(
                    "INSERT INTO plugins VALUES (?,?,?,?,?,?,?,?)",
                    (ch, filename, engine_name, engine_site,
                     int(is_public), now, now, byte_size))
            self.con.commit()

    def upsert_source(self, ch, source_url, repo="", author="",
                      version="", last_update="", forge="unknown"):
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            if not self.has_source(source_url):
                self.con.execute(
                    "INSERT INTO sources (content_hash,source_url,repo,author,"
                    "version,last_update,forge,discovered) VALUES (?,?,?,?,?,?,?,?)",
                    (ch, source_url, repo, author, version, last_update, forge, now))
                self.con.commit()

    def log_failure(self, url: str, reason: str):
        with self.lock:
            self.con.execute(
                "INSERT INTO failures(ts,url,reason) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(), url, reason))
            self.con.commit()

    def stats(self) -> dict:
        p = self.con.execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
        s = self.con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        f = self.con.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        pub = self.con.execute("SELECT COUNT(*) FROM plugins WHERE is_public=1").fetchone()[0]
        priv = p - pub
        return {"plugins": p, "sources": s, "failures": f,
                "public": pub, "private": priv}

    def get_failures(self) -> list:
        return self.con.execute(
            "SELECT url, reason FROM failures ORDER BY id"
        ).fetchall()

    def get_all_plugin_info(self) -> list:
        return self.con.execute(
            "SELECT p.filename, p.engine_name, p.engine_site, p.is_public, "
            "p.byte_size, s.source_url, s.repo, s.author, s.version, "
            "s.last_update, s.forge "
            "FROM plugins p LEFT JOIN sources s ON p.content_hash = s.content_hash "
            "ORDER BY p.is_public, p.engine_name"
        ).fetchall()

    def close(self):
        self.con.close()

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════
def http_get(url: str, headers: dict = None, timeout: int = 20,
             retries: int = 4, accept_html: bool = False) -> bytes | None:
    """Robust GET with exponential back-off and rate-limit awareness."""
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                remaining = resp.headers.get("X-RateLimit-Remaining", "999")
                reset_ts = resp.headers.get("X-RateLimit-Reset")
                if remaining == "0" and reset_ts:
                    wait = max(0, int(reset_ts) - int(time.time())) + 5
                    print(f"    Rate-limited — sleeping {wait}s...")
                    time.sleep(wait)
                data = resp.read()
                if not accept_html and (
                    data[:15].lstrip().startswith(b"<!DOCTYPE")
                    or data[:6].lstrip().startswith(b"<html")
                ):
                    return None
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** (attempt + 3)
                print(f"    HTTP 429 — sleeping {wait}s...")
                time.sleep(wait)
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

def fetch_text(url: str, timeout: int = 15) -> str | None:
    data = http_get(url, timeout=timeout, accept_html=True)
    return data.decode("utf-8", errors="ignore") if data else None

def api_get(url: str, token: str = None, timeout: int = 20):
    """GET JSON from any forge API."""
    h = {"Accept": "application/json"}
    if token and "github" in url:
        h["Authorization"] = f"token {token}"
        h["Accept"] = "application/vnd.github+json"
    elif token and ("gitlab" in url or "codeberg" in url):
        h["Authorization"] = f"Bearer {token}"
    data = http_get(url, headers=h, timeout=timeout)
    if data is None:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:20]

def to_raw_url(url: str) -> str:
    """Convert web URLs → raw content URLs."""
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

def safe_filename(name: str, url: str) -> str:
    url_file = os.path.basename(url.split("?")[0].split("#")[0])
    if url_file.endswith(".py") and len(url_file) > 3 and url_file not in SKIP_FILES:
        return url_file
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
        self.cur_cell = {"text": "", "links": []}
        self.cur_row = []
        self.public_rows = []
        self.private_rows = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and "name" in d:
            n = d["name"].lower()
            if "public" in n:
                self.section = "public"
            elif "private" in n:
                self.section = "private"
        if tag in ("h1", "h2", "h3", "h4") and "id" in d:
            i = d["id"].lower()
            if "public" in i:
                self.section = "public"
            elif "private" in i:
                self.section = "private"
        if tag == "table" and self.section:
            self.in_table = True
        if self.in_table:
            if tag == "tr":
                self.cur_row = []
            elif tag in ("td", "th"):
                self.in_cell = True
                self.cur_cell = {"text": "", "links": []}
            elif tag == "a" and self.in_cell and "href" in d:
                self.cur_cell["links"].append(d["href"])

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "tr":
            if self.cur_row and len(self.cur_row) >= 5:
                first = self.cur_row[0]["text"].lower()
                if "search engine" not in first:
                    if self.section == "public":
                        self.public_rows.append(self.cur_row)
                    elif self.section == "private":
                        self.private_rows.append(self.cur_row)
        elif tag in ("td", "th"):
            self.in_cell = False
            self.cur_cell["text"] = self.cur_cell["text"].strip()
            self.cur_row.append(self.cur_cell)

    def handle_data(self, data):
        if self.in_cell:
            self.cur_cell["text"] += data

# ═══════════════════════════════════════════════════════════════════════════════
# THREAD-SAFE WRITER WITH DEDUP
# ═══════════════════════════════════════════════════════════════════════════════
class PluginStore:
    """Thread-safe plugin storage with hash-based deduplication."""

    def __init__(self, base_dir: str, db: PluginDB):
        self.base_dir = base_dir
        self.db = db
        self.lock = threading.Lock()
        self.seen_hashes = set()
        self.counters = {"new": 0, "dup": 0, "skip": 0, "invalid": 0}
        self.results = []  # metadata for README/JSON

        self.public_dir = Path(base_dir) / "public_sites"
        self.private_dir = Path(base_dir) / "private_sites"
        self.engines_dir = Path(base_dir) / "engines"
        for d in [self.public_dir, self.private_dir, self.engines_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def add(self, content: bytes, filename: str, download_url: str,
            engine_name: str = "", engine_site: str = "", author: str = "",
            repo: str = "", version: str = "", last_update: str = "",
            comments: str = "", forge: str = "wiki", is_private_wiki: bool = False):
        """Add a plugin. Thread-safe. Returns status string."""
        if filename in SKIP_FILES:
            self.counters["skip"] += 1
            return "skip"

        if not is_valid_plugin(content):
            self.counters["invalid"] += 1
            return "invalid"

        h = content_hash(content)

        with self.lock:
            if h in self.seen_hashes:
                self.counters["dup"] += 1
                # Still record source in DB
                self.db.upsert_source(h, download_url, repo, author,
                                      version, last_update, forge)
                return "dup"
            self.seen_hashes.add(h)

        is_private = is_private_wiki or classify_plugin(content)
        target = self.private_dir if is_private else self.public_dir
        filepath = target / filename

        # Handle filename collisions
        if filepath.exists():
            stem = filepath.stem
            counter = 1
            while filepath.exists():
                filepath = target / f"{stem}_{counter}.py"
                counter += 1
            filename = filepath.name

        filepath.write_bytes(content)
        self.counters["new"] += 1

        # Also put in engines/
        engines_path = self.engines_dir / filename
        if not engines_path.exists():
            engines_path.write_bytes(content)

        # DB
        self.db.upsert_plugin(h, filename, engine_name, engine_site,
                              not is_private, len(content))
        self.db.upsert_source(h, download_url, repo, author,
                              version, last_update, forge)

        # Collect metadata
        self.results.append({
            "engine_name": engine_name, "engine_site": engine_site,
            "author_name": author, "repo_link": repo,
            "version": version, "last_update": " ".join(last_update.split()),
            "filename": filename, "download_url": download_url,
            "comments": " ".join(comments.split()), "file_size": len(content),
            "is_private": is_private, "forge": forge,
        })
        return "new"

    def summary(self) -> str:
        c = self.counters
        return (f"New: {c['new']} | Duplicates: {c['dup']} | "
                f"Invalid: {c['invalid']} | Skipped: {c['skip']}")

# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD WORKER
# ═══════════════════════════════════════════════════════════════════════════════
def download_and_store(url: str, store: PluginStore, filename: str = "",
                       engine_name: str = "", engine_site: str = "",
                       author: str = "", repo: str = "", version: str = "",
                       last_update: str = "", comments: str = "",
                       forge: str = "unknown", is_private_wiki: bool = False):
    """Download a URL, validate, and store. Returns status."""
    raw = to_raw_url(url)
    if not raw:
        return "no_url"

    content = http_get(raw, timeout=15)
    if content is None:
        return "download_fail"

    if not filename:
        filename = safe_filename(engine_name, raw)

    return store.add(content, filename, raw, engine_name, engine_site,
                     author, repo, version, last_update, comments,
                     forge, is_private_wiki)

# ═══════════════════════════════════════════════════════════════════════════════
# GITHUB SEARCHER
# ═══════════════════════════════════════════════════════════════════════════════
class GitHubSearcher:
    def __init__(self, token: str = None):
        self.token = token
        self._seen = set(KNOWN_REPOS.values())

    def _api(self, path: str, params: dict = None):
        url = f"{GITHUB_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return api_get(url, self.token)

    def search_repos(self, query: str, max_pages: int = 3) -> list:
        found = []
        for page in range(1, max_pages + 1):
            data = self._api("/search/repositories", {
                "q": query, "sort": "updated", "per_page": 100, "page": page
            })
            if not data or not data.get("items"):
                break
            for item in data["items"]:
                fn = item["full_name"]
                if fn not in self._seen:
                    self._seen.add(fn)
                    found.append(fn)
            if len(data["items"]) < 100:
                break
            time.sleep(2)
        return found

    def search_topics(self, topic: str) -> list:
        data = self._api("/search/repositories", {
            "q": f"topic:{topic}", "sort": "updated", "per_page": 100
        })
        if not data:
            return []
        found = []
        for item in data.get("items", []):
            fn = item["full_name"]
            if fn not in self._seen:
                self._seen.add(fn)
                found.append(fn)
        return found

    def search_code(self, query: str, max_pages: int = 2) -> list:
        items = []
        for page in range(1, max_pages + 1):
            data = self._api("/search/code", {
                "q": query + " extension:py", "per_page": 100, "page": page
            })
            if not data or not data.get("items"):
                break
            for item in data["items"]:
                repo = item.get("repository", {}).get("full_name", "")
                path = item.get("path", "")
                dl = item.get("html_url", "")
                if dl and path.endswith(".py") and path not in SKIP_FILES:
                    items.append({"repo": repo, "path": path, "url": dl})
            if len(data["items"]) < 100:
                break
            time.sleep(3)  # code search is heavily rate-limited
        return items

    def list_repo_files(self, full_name: str, subdirs: list = None) -> list:
        """List .py files in a repo, searching subdirs."""
        if subdirs is None:
            subdirs = ["", "engines", "plugins", "search_engines", "src"]
        files = []
        owner, repo = full_name.split("/", 1)
        for sub in subdirs:
            data = self._api(f"/repos/{owner}/{repo}/contents/{sub}")
            if not data or not isinstance(data, list):
                continue
            for item in data:
                if (item.get("type") == "file"
                        and item["name"].endswith(".py")
                        and item["name"] not in SKIP_FILES):
                    dl = item.get("download_url", "")
                    if dl:
                        files.append({
                            "repo": full_name, "path": item["path"],
                            "url": dl, "name": item["name"],
                            "size": item.get("size", 0),
                        })
            time.sleep(0.5)
        return files

# ═══════════════════════════════════════════════════════════════════════════════
# GITLAB SEARCHER
# ═══════════════════════════════════════════════════════════════════════════════
class GitLabSearcher:
    def __init__(self, token: str = None):
        self.token = token

    def search_projects(self, query: str) -> list:
        data = api_get(f"{GITLAB_API}/projects", self.token, timeout=15)
        if not data:
            return []
        return [p["path_with_namespace"] for p in data
                if ".py" in str(p.get("description", "")) or
                "qbittorrent" in p.get("description", "").lower()]

    def list_repo_files(self, pid: str, ref: str = "main") -> list:
        files = []
        for sub in ["", "engines", "plugins"]:
            url = f"{GITLAB_API}/projects/{urllib.parse.quote(pid, safe='')}/repository/tree"
            data = api_get(url, self.token)
            if not data or not isinstance(data, list):
                continue
            for item in data:
                if (item.get("type") == "blob"
                        and item["name"].endswith(".py")
                        and item["name"] not in SKIP_FILES):
                    raw = f"{GITLAB_API}/projects/{urllib.parse.quote(pid, safe='')}/repository/files/{urllib.parse.quote(item['path'], safe='')}/raw?ref={ref}"
                    files.append({
                        "repo": pid, "path": item["path"],
                        "url": raw, "name": item["name"],
                    })
            time.sleep(0.5)
        return files

# ═══════════════════════════════════════════════════════════════════════════════
# CODEBERG SEARCHER
# ═══════════════════════════════════════════════════════════════════════════════
class CodebergSearcher:
    def __init__(self, token: str = None):
        self.token = token

    def search_repos(self, query: str) -> list:
        data = api_get(
            f"{CODEBERG_API}/repos/search?q={urllib.parse.quote(query)}&limit=50",
            self.token
        )
        if not data or not isinstance(data, dict):
            return []
        return [r["full_name"] for r in data.get("data", [])]

    def list_repo_files(self, full_name: str) -> list:
        files = []
        owner, repo = full_name.split("/", 1)
        for sub in ["", "engines", "plugins"]:
            url = f"{CODEBERG_API}/repos/{owner}/{repo}/contents/{sub}"
            data = api_get(url, self.token)
            if not data or not isinstance(data, list):
                continue
            for item in data:
                if (item.get("type") == "file"
                        and item["name"].endswith(".py")
                        and item["name"] not in SKIP_FILES):
                    dl = item.get("download_url", "")
                    if dl:
                        files.append({
                            "repo": full_name, "path": item["path"],
                            "url": dl, "name": item["name"],
                        })
            time.sleep(0.5)
        return files

# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPE COLLECTION REPOS (HTML parsing for .py links)
# ═══════════════════════════════════════════════════════════════════════════════
def scrape_collection_repos(store: PluginStore, threads: int = 8):
    """Scrape known collection repo pages for .py file links."""
    print("[+] Scraping collection repos...")
    tasks = []

    for repo_url in COLLECTION_REPOS:
        html = fetch_text(repo_url, timeout=10)
        if not html:
            continue
        for match in re.findall(r'href="([^"]+\.py)"', html):
            full = urllib.parse.urljoin(repo_url, match)
            full = to_raw_url(full)
            name = os.path.basename(full)
            if name not in SKIP_FILES:
                tasks.append((full, name, repo_url))

    print(f"    Found {len(tasks)} .py links in collection repos")
    downloaded = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futs = {}
        for url, name, repo in tasks:
            f = pool.submit(download_and_store, url, store, filename=name,
                            repo=repo, forge="collection")
            futs[f] = (url, name)
        for f in as_completed(futs):
            url, name = futs[f]
            try:
                status = f.result()
                if status == "new":
                    downloaded += 1
            except Exception:
                pass
    print(f"    Downloaded {downloaded} new from collections")

# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPE AWESOME LISTS
# ═══════════════════════════════════════════════════════════════════════════════
def scrape_awesome_lists(store: PluginStore):
    """Parse awesome-list markdown for .py links."""
    print("[+] Scraping awesome-lists...")
    found = 0
    for url in AWESOME_LIST_URLS:
        md = fetch_text(url, timeout=10)
        if not md:
            continue
        for match in re.findall(r'\[([^\]]*?)\]\((https?://[^)]+\.py)\)', md):
            name, link = match
            link = to_raw_url(link)
            fname = os.path.basename(link)
            if fname not in SKIP_FILES:
                status = download_and_store(link, store, filename=fname,
                                            engine_name=name, forge="awesome-list")
                if status == "new":
                    found += 1
    print(f"    Found {found} from awesome-lists")

# ═══════════════════════════════════════════════════════════════════════════════
# PROCESS WIKI ROWS
# ═══════════════════════════════════════════════════════════════════════════════
def process_wiki_rows(rows: list, store: PluginStore, is_private: bool,
                      threads: int = 8):
    """Download and store plugins from wiki table rows."""
    tasks = []
    for row in rows:
        engine_cell = row[0]
        author_cell = row[1]
        version = row[2]["text"] if len(row) > 2 else "1.0"
        last_update = row[3]["text"] if len(row) > 3 else "N/A"
        dl_cell = row[4] if len(row) > 4 else None
        comments = row[5]["text"] if len(row) > 5 else ""

        engine_name = engine_cell["text"].strip()
        engine_site = engine_cell["links"][0] if engine_cell["links"] else ""
        author_name = author_cell["text"].strip()
        repo_link = author_cell["links"][0] if author_cell["links"] else ""
        raw_url = dl_cell["links"][0] if dl_cell and dl_cell["links"] else ""
        download_url = to_raw_url(raw_url)

        if not download_url:
            store.db.log_failure("", f"Wiki: {engine_name} — no URL")
            continue

        filename = safe_filename(engine_name, download_url)
        tasks.append((download_url, filename, engine_name, engine_site,
                       author_name, repo_link, version, last_update, comments,
                       is_private))

    ok = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futs = {}
        for (url, fname, ename, esite, aname, rlink, ver, lup, comm, priv) in tasks:
            f = pool.submit(download_and_store, url, store, filename=fname,
                            engine_name=ename, engine_site=esite, author=aname,
                            repo=rlink, version=ver, last_update=lup,
                            comments=comm, forge="wiki", is_private_wiki=priv)
            futs[f] = (url, ename)
        for f in as_completed(futs):
            url, ename = futs[f]
            try:
                status = f.result()
                if status == "new":
                    ok += 1
                elif status in ("download_fail", "no_url"):
                    fail += 1
                    store.db.log_failure(url, f"Wiki: {ename}")
            except Exception as e:
                fail += 1
                store.db.log_failure(url, str(e)[:100])
    return ok, fail

# ═══════════════════════════════════════════════════════════════════════════════
# PROCESS REPO FILES (from API search)
# ═══════════════════════════════════════════════════════════════════════════════
def process_repo_files(files: list, store: PluginStore, forge: str = "github",
                       threads: int = 8):
    """Download a list of file dicts from API search results."""
    if not files:
        return 0
    downloaded = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futs = {}
        for f_info in files:
            f = pool.submit(
                download_and_store, f_info["url"], store,
                filename=f_info.get("name", ""),
                repo=f_info.get("repo", ""),
                forge=forge
            )
            futs[f] = f_info["url"]
        for f in as_completed(futs):
            url = futs[f]
            try:
                if f.result() == "new":
                    downloaded += 1
            except Exception:
                store.db.log_failure(url, "repo file download error")
    return downloaded

# ═══════════════════════════════════════════════════════════════════════════════
# FULL DISCOVERY PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════
def run_discovery(store: PluginStore, gh_token: str, gl_token: str,
                  threads: int = 8):
    """Run all API-based discovery methods."""
    all_repo_files = []
    all_code_files = []

    # ── GitHub repo search ────────────────────────────────────────────────────
    print("[+] GitHub repo search...")
    gh = GitHubSearcher(gh_token)
    for q in GITHUB_REPO_QUERIES:
        repos = gh.search_repos(q, max_pages=2)
        if repos:
            print(f"    '{q[:40]}...' → {len(repos)} new repos")

    # ── GitHub topic search ───────────────────────────────────────────────────
    print("[+] GitHub topic search...")
    for topic in GITHUB_TOPIC_QUERIES:
        repos = gh.search_topics(topic)
        if repos:
            print(f"    topic:{topic} → {len(repos)} repos")

    # ── GitHub code search (only with token — rate-limited) ───────────────────
    if gh_token:
        print("[+] GitHub code search...")
        for q in GITHUB_CODE_QUERIES:
            items = gh.search_code(q, max_pages=1)
            all_code_files.extend(items)
        print(f"    Found {len(all_code_files)} code results")

    # ── List files from all discovered GitHub repos ───────────────────────────
    print(f"[+] Scanning {len(gh._seen)} GitHub repos for .py files...")
    scan_count = 0
    for repo_full in list(gh._seen):
        files = gh.list_repo_files(repo_full)
        all_repo_files.extend(files)
        scan_count += 1
        if scan_count % 20 == 0:
            print(f"    Scanned {scan_count}/{len(gh._seen)} repos, "
                  f"found {len(all_repo_files)} files so far")
        time.sleep(0.3)
    print(f"    Total: {len(all_repo_files)} .py files from GitHub repos")

    # ── Download GitHub files ─────────────────────────────────────────────────
    print("[+] Downloading GitHub files...")
    dl = process_repo_files(all_repo_files, store, "github", threads)
    print(f"    {dl} new from GitHub repos")

    if all_code_files:
        print("[+] Downloading GitHub code-search results...")
        dl2 = process_repo_files(all_code_files, store, "github-code", threads)
        print(f"    {dl2} new from code search")

    # ── GitLab search ─────────────────────────────────────────────────────────
    print("[+] GitLab search...")
    gl = GitLabSearcher(gl_token)
    for q in GITLAB_QUERIES:
        # Use projects search endpoint with query
        url = f"{GITLAB_API}/projects?search={urllib.parse.quote(q)}&per_page=50"
        data = api_get(url, gl_token)
        if data and isinstance(data, list):
            for proj in data:
                pid = proj["path_with_namespace"]
                desc = proj.get("description", "") or ""
                if "qbittorrent" in desc.lower() or "plugin" in desc.lower():
                    files = gl.list_repo_files(pid)
                    if files:
                        dl = process_repo_files(files, store, "gitlab", threads)
                        if dl:
                            print(f"    GitLab {pid}: {dl} new")
        time.sleep(1)

    # ── Codeberg search ───────────────────────────────────────────────────────
    print("[+] Codeberg search...")
    cb = CodebergSearcher()
    for q in CODEBERG_QUERIES:
        repos = cb.search_repos(q)
        for repo in repos:
            files = cb.list_repo_files(repo)
            if files:
                dl = process_repo_files(files, store, "codeberg", threads)
                if dl:
                    print(f"    Codeberg {repo}: {dl} new")
        time.sleep(1)

    # ── Collection repos ──────────────────────────────────────────────────────
    scrape_collection_repos(store, threads)

    # ── Awesome lists ─────────────────────────────────────────────────────────
    scrape_awesome_lists(store)

# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════
def generate_outputs(store: PluginStore, output_dir: str):
    """Generate README.md, plugins.json, and ZIP."""
    base = Path(output_dir)
    pub_count = len(list((base / "public_sites").glob("*.py")))
    priv_count = len(list((base / "private_sites").glob("*.py")))
    eng_count = len(list((base / "engines").glob("*.py")))

    # ── Split results by public/private ───────────────────────────────────────
    public_results = [r for r in store.results if not r["is_private"]]
    private_results = [r for r in store.results if r["is_private"]]

    # ── README ────────────────────────────────────────────────────────────────
    def fmt_rows(items, start=1):
        lines = []
        for i, item in enumerate(items, start):
            engine = (f"[{item['engine_name']}]({item['engine_site']})"
                      if item['engine_site'] else item['engine_name'])
            author = (f"[{item['author_name']}]({item['repo_link']})"
                      if item['repo_link'] else item['author_name'])
            dl = f"[`{item['filename']}`]({item['download_url']})"
            forge_badge = f"`{item.get('forge', 'wiki')}`"
            lines.append(
                f"| {i} | {engine} | {author} | {item['version']} | "
                f"{item['last_update']} | {dl} | {forge_badge} | {item['comments']} |"
            )
        return "\n".join(lines)

    repo_env = os.environ.get("GITHUB_REPOSITORY",
                               "user/qBittorrent-Search-Plugins")
    db_stats = store.db.stats()
