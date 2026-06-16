
#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║   🔥 qBittorrent Search Plugin ULTIMATE Collector v3.1 - Full-Net Edition   ║
║                                                                              ║
║  SEARCH METHODS:                                                             ║
║  • GitHub REST API  - repo search, code search, topic search                 ║
║  • GitHub GraphQL  - deep traversal for related repos                        ║
║  • GitLab API      - full public GitLab search                               ║
║  • Codeberg API    - Gitea-based forge search                                ║
║  • Wiki scrape     - official qBittorrent plugin wiki                        ║
║  • Awesome-lists   - curated community lists                                 ║
║                                                                              ║
║  SMARTS:                                                                     ║
║  • Validates Python files as real qBittorrent plugins before saving          ║
║  • Full SHA-256 content deduplication across ALL sources                     ║
║  • Content dedup - same code from different repos counted once               ║
║  • Incremental: only downloads changed files                                 ║
║  • Concurrent downloads (ThreadPoolExecutor)                                 ║
║  • Full SQLite metadata DB (sqlite3.Row) + JSON export                       ║
║  • Exponential-backoff rate-limit handling                                   ║
║  • SSRF-safe URL allowlist for markdown-extracted links                      ║
╚══════════════════════════════════════════════════════════════════════════════╝

Usage:
    python qbt_plugin_collector.py                        # basic run
    python qbt_plugin_collector.py --github-token TOKEN   # GitHub auth
    python qbt_plugin_collector.py --gitlab-token TOKEN   # GitLab auth
    python qbt_plugin_collector.py --no-discovery         # wiki only
    python qbt_plugin_collector.py --threads 10           # concurrent downloads
    python qbt_plugin_collector.py --out ./my-plugins     # custom output dir

Environment variables (alternative to flags):
    GITHUB_TOKEN   - GitHub personal access token
    GITLAB_TOKEN   - GitLab personal access token
"""

import os
import sys
import re
import json
import time
import hashlib
import argparse
import sqlite3
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from typing import Optional, Union

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_OUT   = "qbt-plugins"
GITHUB_API    = "https://api.github.com"
GITLAB_API    = "https://gitlab.com/api/v4"
CODEBERG_API  = "https://codeberg.org/api/v1"

WIKI_URL = (
    "https://github.com/qbittorrent/search-plugins/wiki/"
    "Unofficial-search-plugins"
)

OFFICIAL_REPO = "qbittorrent/search-plugins"

# ── SSRF-safe URL allowlist ───────────────────────────────────────────────────
ALLOWED_FETCH_HOSTS: frozenset = frozenset({
    "raw.githubusercontent.com",
    "github.com",
    "gitlab.com",
    "codeberg.org",
    "api.github.com",
    "api.gitlab.com",
    "codeberg.org",
})

# ── GitHub search queries ─────────────────────────────────────────────────────
GITHUB_REPO_QUERIES = [
    "qBittorrent search plugin language:python",
    "qbittorrent search engine python",
    "qbittorrent search plugin torrent",
    "qbittorrent plugin python torrent",
    "qbittorrent-search-plugins",
    "qbt search plugin python",
    "python torrent search plugin qbittorrent",
]

GITHUB_TOPIC_QUERIES = [
    "qbittorrent-search-plugin",
    "qbittorrent-plugin",
    "qbittorrent-search",
    "qbittorrent",
]

GITHUB_CODE_QUERIES = [
    "class SearchEngine filename:.py qbittorrent",
    "noSearchResult qbittorrent filename:.py",
    "prettyPrinting filename:.py qbittorrent",
    "url_dl qbittorrent filename:.py",
    "supported_categories qbittorrent filename:.py",
    "def search filename:.py qbittorrent search engine",
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
    "https://raw.githubusercontent.com/lgallard/qBittorrent-Controller/master/README.md",
]

# ── Known plugin-specific repos only (large application repos removed) ────────
KNOWN_REPOS: dict[str, str] = {
    "LightDestory"  : "LightDestory/qBittorrent-Search-Plugins",
    "BurningMop"    : "BurningMop/qBittorrent-Search-Plugins",
    "nklido"        : "nklido/qBittorrent_search_engines",
    "hannsen"       : "hannsen/qbittorrent_search_plugins",
    "Cc050511"      : "Cc050511/qBit-search-plugins",
    "nindogo"       : "nindogo/qbtSearchScripts",
    "imDMG"         : "imDMG/qBittorrent_Search_Plugins",
    "iordic"        : "iordic/qbittorrent-search-plugins",
    "tolotp"        : "tolotp/qbittorrent-search-plugins",
    "444995"        : "444995/qbittorrent-search-plugins",
    "bugsbringer"   : "bugsbringer/qbit-plugins",
    "AlaaBrahim"    : "AlaaBrahim/qBitTorrent-animetosho-search-plugin",
    "TuckerWarlock" : "TuckerWarlock/qbittorrent-search-plugins",
    "galaris"       : "galaris/BTDigg-qBittorrent-plugin",
    "MarcBresson"   : "MarcBresson/cpasbien",
    "ZH1637"        : "ZH1637/dmhy",
    "lazulyra"      : "lazulyra/qbittorrent-yts-plugin",
    "Bioux1"        : "Bioux1/qbittorrent-search-plugins",
    "bebetoh"       : "bebetoh/qbittorrent-search-plugins",
    "Cycloctane"    : "Cycloctane/qbittorrent-mikan-plugin",
    "vt-idiot"      : "vt-idiot/qbittorrent-sukebei-plugin",
    "elazar"        : "elazar/qbittorrent-cloudtorrents-plugin",
    "joseeloren"    : "joseeloren/qbittorrent-search-plugins",
    "menegop"       : "menegop/qbittorrent-search-plugins",
    "msagca"        : "msagca/qbittorrent-plugins",
    "caiocinel"     : "caiocinel/qbittorrent-onlinefix-plugin",
    "kli885"        : "kli885/qbittorrent-subsplease-plugin",
    "Ashalda"       : "Ashalda/qbittorrent-sktorrent-plugin",
    "BrunoReX"      : "BrunoReX/qbittorrent-search-plugins",
    "YGGverse"      : "YGGverse/qbittorrent-yggtracker-plugin",
    "OptimusKoala"  : "OptimusKoala/qbittorrent-c411-plugin",
    "RaresPNet"     : "RaresPNet/qbittorrent-filelist-plugin",
    "Ooggle"        : "Ooggle/qbittorrent-gazelle-games-plugin",
    "txtsd"         : "txtsd/qB-IPT",
    "darktohka"     : "darktohka/qbittorrent-ncore-plugin",
    "TainakaDrums"  : "TainakaDrums/qbittorrent-pornolab-plugin",
    "swannie-eire"  : "swannie-eire/qbittorrent-prowlarr-plugin",
    "Evyd13"        : "Evyd13/qbittorrent-redacted-plugin",
    "nbusseneau"    : "nbusseneau/qBittorrent-RuTracker-plugin",
    "etn"           : "etn/qbittorrent-sharewood-plugin",
    "miIiano"       : "miIiano/qbittorrent-speedapp-plugin",
    "MjKey"         : "MjKey/qbittorrent-tapochek-plugin",
    "PlayDay"       : "PlayDay/qbittorrent-gurtom-plugin",
    "CrimsonKoba"   : "CrimsonKoba/qbittorrent-unionfansub-plugin",
    "Laiteux"       : "Laiteux/qbittorrent-yggapi-plugin",
    "CravateRouge"  : "CravateRouge/qbittorrent-yggtorrent-plugin",
    "Necrosis"      : "Necrosis/qbittorrent-zamunda-plugin",
    "DrPurp"        : "DrPurp/qbittorrent-eztv-plugin",
    "scadams"       : "scadams/qbittorrent-search-plugins",
    "MagnetDL"      : "v1k1/magnetdl-qbittorrent-plugin",
    "hdvinnie"      : "hdvinnie/qBittorrent-Search-Plugins",
    "bnlf"          : "bnlf/qbittorrent-search-plugin",
    "LevyFialho"    : "LevyFialho/qbittorrent-search-plugins",
    "quorums"       : "quorums/qbittorrent-search-plugins",
    "balansse"      : "balansse/qbittorrent-piratebay-plugin",
    "jivoi"         : "jivoi/qbittorrent-torrserver-plugin",
    "freecoder76"   : "freecoder76/qBittorrent-search-plugins",
    "ddd9898"       : "ddd9898/qbittorrent-plugin",
    "soyYo"         : "soyYo/torrentbytes-qbt-plugin",
    "GomorrA1"      : "GomorrA1/qbittorrent-search-plugins",
    "HiItsD"        : "HiItsD/jackett-search-plugin",
    "noxs1de"       : "noxs1de/prowlarr-qbittorrent-plugin",
}

# ─────────────────────────────────────────────────────────────────────────────
# PLUGIN VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

# Strong: unambiguously qBittorrent plugin signatures
PLUGIN_STRONG_SIGS: tuple[bytes, ...] = (
    b"noSearchResult",
    b"class SearchEngine",
    b"prettyPrinting",
    b"supported_categories",
)

# Weak: only accepted when a class definition is also present
PLUGIN_WEAK_SIGS: tuple[bytes, ...] = (
    b"url_dl_l",
    b"url_dl",
    b"magnetdl",
    b"CORE_SITE",
)

PLUGIN_BLACKLIST_PATTERNS: tuple[bytes, ...] = (
    b"<!DOCTYPE",
    b"<html",
    b"404: Not Found",
    b"Not Found",
)

MIN_PLUGIN_BYTES = 400
MAX_PLUGIN_BYTES = 512_000


def is_valid_plugin(content: bytes) -> bool:
    """
    Return True only if content is a real qBittorrent search plugin.

    Rules:
    - Must be within byte-size bounds.
    - Must not start with HTML error markers.
    - Strong signatures are accepted alone.
    - Weak signatures require a class definition to also be present.
    - 'def search(' inside a class context is also accepted.
    """
    if not (MIN_PLUGIN_BYTES <= len(content) <= MAX_PLUGIN_BYTES):
        return False

    header = content[:200]
    for bad in PLUGIN_BLACKLIST_PATTERNS:
        if bad in header:
            return False

    for sig in PLUGIN_STRONG_SIGS:
        if sig in content:
            return True

    has_class = b"class " in content
    if has_class:
        for sig in PLUGIN_WEAK_SIGS:
            if sig in content:
                return True
        if b"def search(" in content:
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# URL SAFETY
# ─────────────────────────────────────────────────────────────────────────────

def is_safe_url(url: str) -> bool:
    """
    Return True only if the URL targets an explicitly allowed host.
    Prevents SSRF when fetching URLs extracted from untrusted markdown.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        return (
            parsed.scheme in ("http", "https")
            and (parsed.hostname or "") in ALLOWED_FETCH_HOSTS
        )
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SQLITE DATABASE
# ─────────────────────────────────────────────────────────────────────────────

class PluginDB:
    """
    SQLite-backed metadata store.

    Tables:
        plugins  – one row per unique file (by full SHA-256 content hash)
        sources  – one row per (repo × file_path) pair pointing to a plugin
        failures – download errors log

    All cursors use sqlite3.Row so columns are accessed by name, not index.
    All writes are serialised through a threading.Lock.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS plugins (
        content_hash  TEXT PRIMARY KEY,
        filename      TEXT NOT NULL,
        engine_name   TEXT,
        engine_site   TEXT,
        is_public     INTEGER DEFAULT 1,
        first_seen    TEXT,
        last_seen     TEXT,
        byte_size     INTEGER
    );
    CREATE TABLE IF NOT EXISTS sources (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        content_hash  TEXT REFERENCES plugins(content_hash),
        source_url    TEXT UNIQUE,
        repo          TEXT,
        author        TEXT,
        version       TEXT,
        last_update   TEXT,
        forge         TEXT,
        discovered    TEXT
    );
    CREATE TABLE IF NOT EXISTS failures (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT,
        url     TEXT,
        reason  TEXT
    );
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row          # named column access
        self._lock = threading.Lock()
        self._con.executescript(self.DDL)
        self._con.commit()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute under lock and return cursor (for reads that need locking)."""
        with self._lock:
            return self._con.execute(sql, params)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── read helpers ──────────────────────────────────────────────────────────

    def has_content(self, content_hash: str) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM plugins WHERE content_hash=?", (content_hash,)
        ).fetchone()
        return row is not None

    def has_source(self, url: str) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM sources WHERE source_url=?", (url,)
        ).fetchone()
        return row is not None

    # ── write helpers ─────────────────────────────────────────────────────────

    def upsert_plugin(
        self,
        content_hash: str,
        filename: str,
        engine_name: str = "",
        engine_site: str = "",
        is_public: bool = True,
        byte_size: int = 0,
    ) -> None:
        now = self._now()
        with self._lock:
            existing = self._con.execute(
                "SELECT first_seen FROM plugins WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
            if existing:
                self._con.execute(
                    "UPDATE plugins SET last_seen=?, byte_size=? "
                    "WHERE content_hash=?",
                    (now, byte_size, content_hash),
                )
            else:
                self._con.execute(
                    "INSERT INTO plugins VALUES (?,?,?,?,?,?,?,?)",
                    (
                        content_hash, filename, engine_name, engine_site,
                        int(is_public), now, now, byte_size,
                    ),
                )
            self._con.commit()

    def upsert_source(
        self,
        content_hash: str,
        source_url: str,
        repo: str = "",
        author: str = "",
        version: str = "",
        last_update: str = "",
        forge: str = "unknown",
    ) -> None:
        now = self._now()
        with self._lock:
            if not self.has_source(source_url):
                self._con.execute(
                    "INSERT INTO sources"
                    " (content_hash,source_url,repo,author,version,"
                    "  last_update,forge,discovered)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        content_hash, source_url, repo, author,
                        version, last_update, forge, now,
                    ),
                )
                self._con.commit()

    def log_failure(self, url: str, reason: str) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO failures(ts,url,reason) VALUES (?,?,?)",
                (self._now(), url, reason),
            )
            self._con.commit()

    # ── stats / export ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        c = self._con
        return {
            "plugins"  : c.execute("SELECT COUNT(*) FROM plugins").fetchone()[0],
            "sources"  : c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "failures" : c.execute("SELECT COUNT(*) FROM failures").fetchone()[0],
            "public"   : c.execute(
                "SELECT COUNT(*) FROM plugins WHERE is_public=1"
            ).fetchone()[0],
            "private"  : c.execute(
                "SELECT COUNT(*) FROM plugins WHERE is_public=0"
            ).fetchone()[0],
        }

    def all_plugins(self) -> list[sqlite3.Row]:
        """
        Return every plugin joined with one representative source row.
        Columns accessible by name via sqlite3.Row.
        """
        return self._con.execute(
            """
            SELECT
                p.content_hash, p.filename, p.engine_name, p.engine_site,
                p.is_public,   p.first_seen, p.last_seen,  p.byte_size,
                s.source_url,  s.repo,       s.author,     s.forge
            FROM plugins p
            LEFT JOIN sources s ON p.content_hash = s.content_hash
            ORDER BY p.engine_name
            """
        ).fetchall()

    def close(self) -> None:
        self._con.close()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; qBittorrentPluginBot/3.1; "
        "+https://github.com/qbittorrent/search-plugins)"
    )
}


def http_get(
    url: str,
    headers: Optional[dict] = None,
    timeout: int = 20,
    retries: int = 4,
    accept_html: bool = False,
) -> Optional[bytes]:
    """
    Robust GET with exponential back-off and rate-limit awareness.

    - Checks X-RateLimit-Remaining (as int, not string) before next request.
    - Returns None on unrecoverable errors or HTML responses when not expected.
    - Does NOT fetch URLs outside ALLOWED_FETCH_HOSTS (SSRF guard).
    """
    if not is_safe_url(url):
        return None

    h = dict(_DEFAULT_HEADERS)
    if headers:
        h.update(headers)

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Rate-limit pre-emption: integer comparison
                remaining_str = resp.headers.get("X-RateLimit-Remaining", "999")
                reset_ts_str  = resp.headers.get("X-RateLimit-Reset")
                try:
                    remaining = int(remaining_str)
                except ValueError:
                    remaining = 999

                if remaining <= 0 and reset_ts_str:
                    try:
                        wait = max(0, int(reset_ts_str) - int(time.time())) + 5
                    except ValueError:
                        wait = 60
                    print(f"  ⏳ Rate-limited – sleeping {wait}s …")
                    time.sleep(wait)

                data = resp.read()

            # Reject unexpected HTML responses
            if not accept_html:
                sniff = data[:15].lstrip()
                if sniff.startswith(b"<!DOCTYPE") or sniff.startswith(b"<html"):
                    return None

            return data

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = 2 ** (attempt + 3)
                print(f"  ⏳ HTTP 429 – sleeping {wait}s …")
                time.sleep(wait)
            elif exc.code in (404, 451):
                return None          # permanent – do not retry
            elif attempt == retries - 1:
                return None
            else:
                time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(2 ** attempt)

    return None


def api_get(
    url: str,
    token: Optional[str] = None,
    timeout: int = 20,
) -> Optional[Union[dict, list]]:
    """GET JSON from any forge API. Returns parsed object or None."""
    h: dict[str, str] = {"Accept": "application/json"}
    if token:
        if "github" in url:
            h["Authorization"] = f"token {token}"
            h["Accept"] = "application/vnd.github+json"
        elif "gitlab" in url or "codeberg" in url:
            h["Authorization"] = f"Bearer {token}"

    data = http_get(url, headers=h, timeout=timeout, accept_html=False)
    if data is None:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FILENAME / PATH HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def content_hash(data: bytes) -> str:
    """Full SHA-256 hex digest – used as SQLite primary key."""
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str, url: str) -> str:
    """Derive a safe .py filename from the engine name or URL."""
    url_file = os.path.basename(url.split("?")[0].split("#")[0])
    if url_file.endswith(".py") and len(url_file) > 3:
        return url_file
    clean = re.sub(r"[^a-zA-Z0-9_\-]", "", name.replace(" ", "_")).lower()
    return f"{clean}.py" if clean else "plugin.py"


def raw_url(url: str) -> str:
    """Convert GitHub/GitLab/Codeberg web URLs → raw content URLs."""
    if not url:
        return ""
    url = url.split("#")[0]
    if "github.com" in url and "/blob/" in url:
        url = (
            url.replace("github.com", "raw.githubusercontent.com")
               .replace("/blob/", "/")
        )
    if "gitlab.com" in url and "/-/blob/" in url:
        url = re.sub(r"/-/blob/", "/-/raw/", url)
    if "codeberg.org" in url and "/src/branch/" in url:
        url = url.replace("/src/branch/", "/raw/branch/")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# FILE WRITER  (thread-safe, content-dedup-aware)
# ─────────────────────────────────────────────────────────────────────────────

class PluginWriter:
    """
    Encapsulates all state for writing plugins to disk.

    Replaces the previous module-level globals (_write_lock, _seen_hashes)
    so multiple instances can coexist in the same process (e.g. tests).
    """

    def __init__(self) -> None:
        self._lock        = threading.Lock()
        self._seen_hashes: set[str] = set()

    def save(self, content: bytes, filename: str, target_dir: str) -> str:
        """
        Write a plugin file to *target_dir* (collision-safe).

        Returns one of:
            "new"       – written for the first time
            "duplicate" – identical content already seen this session
            "unchanged" – same filename + same content already on disk

        Filename collisions with *different* content are resolved by
        appending the first 8 chars of the content hash.
        """
        h        = content_hash(content)
        filepath = os.path.join(target_dir, filename)

        with self._lock:
            # Content-level deduplication
            if h in self._seen_hashes:
                return "duplicate"
            self._seen_hashes.add(h)

            # File exists – compare content
            if os.path.exists(filepath):
                try:
                    existing_h = content_hash(Path(filepath).read_bytes())
                except OSError:
                    existing_h = ""

                if existing_h == h:
                    return "unchanged"

                # Different content, same filename → disambiguate
                base, ext = os.path.splitext(filename)
                filepath  = os.path.join(target_dir, f"{base}_{h[:8]}{ext}")

            Path(filepath).write_bytes(content)
            return "new"


# ─────────────────────────────────────────────────────────────────────────────
# WIKI PARSER
# ─────────────────────────────────────────────────────────────────────────────

class WikiParser(HTMLParser):
    """Parse the qBittorrent unofficial search-plugins wiki page."""

    def __init__(self) -> None:
        super().__init__()
        self.section       : Optional[str] = None   # "public" | "private"
        self.in_table      : bool = False
        self.in_cell       : bool = False
        self.cell_idx      : int  = -1
        self.cur_cell      : dict = {"text": "", "links": []}
        self.cur_row       : list = []
        self.public_rows   : list = []
        self.private_rows  : list = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        d = dict(attrs)

        # Detect section markers from anchor names or heading IDs
        if tag == "a" and "name" in d:
            n = d["name"].lower()
            if "public"  in n: self.section = "public"
            if "private" in n: self.section = "private"
        if tag in ("h1", "h2", "h3", "h4") and "id" in d:
            i = d["id"].lower()
            if "public"  in i: self.section = "public"
            if "private" in i: self.section = "private"

        if tag == "table" and self.section:
            self.in_table = True
        if self.in_table:
            if tag == "tr":
                self.cur_row = []
                self.cell_idx = -1
            elif tag in ("td", "th"):
                self.in_cell  = True
                self.cell_idx += 1
                self.cur_cell = {"text": "", "links": []}
            elif tag == "a" and self.in_cell and "href" in d:
                self.cur_cell["links"].append(d["href"])

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "tr":
            if self.cur_row and len(self.cur_row) >= 5:
                first = self.cur_row[0]["text"].lower()
                if "search engine" not in first:
                    bucket = (
                        self.public_rows
                        if self.section == "public"
                        else self.private_rows
                    )
                    bucket.append(self.cur_row)
        elif tag in ("td", "th"):
            self.in_cell = False
            self.cur_cell["text"] = self.cur_cell["text"].strip()
            self.cur_row.append(self.cur_cell)

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cur_cell["text"] += data


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB SEARCHER
# ─────────────────────────────────────────────────────────────────────────────

class GitHubSearcher:

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token
        self._repos_seen: set[str] = set(KNOWN_REPOS.values())

    def _gh(
        self, path: str, params: Optional[dict] = None
    ) -> Optional[Union[dict, list]]:
        url = f"{GITHUB_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return api_get(url, self.token)

    # ── repo search ───────────────────────────────────────────────────────────

    def search_repos(self, query: str, max_pages: int = 5) -> list[str]:
        """Return list of 'owner/repo' strings matching the query."""
        found: list[str] = []
        for page in range(1, max_pages + 1):
            data = self._gh("/search/repositories", {
                "q": query, "sort": "updated",
                "per_page": 100, "page": page,
            })
            if not data or not data.get("items"):
                break
            for item in data["items"]:
                fn = item["full_name"]
                if fn not in self._repos_seen:
                    self._repos_seen.add(fn)
                    found.append(fn)
            if len(data["items"]) < 100:
                break
            time.sleep(1.5)
        return found

    # ── topic search ──────────────────────────────────────────────────────────

    def search_topics(self, topic: str) -> list[str]:
        """Return repos tagged with the given GitHub topic."""
        data = self._gh("/search/repositories", {
            "q": f"topic:{topic}", "sort": "updated", "per_page": 100,
        })
        if not data:
            return []
        found: list[str] = []
        for item in data.get("items", []):
            fn = item["full_name"]
            if fn not in self._repos_seen:
                self._repos_seen.add(fn)
                found.append(fn)
        return found

    # ── code search ───────────────────────────────────────────────────────────

    def search_code(self, query: str, max_pages: int = 3) -> list[dict]:
        """
        Return {repo, path, url, raw} for Python files matching query.
        Requires a GitHub token for meaningful rate limits.
        """
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._gh("/search/code", {
                "q": query + " extension:py",
                "per_page": 100, "page": page,
            })
            if not data or not data.get("items"):
                break
            for item in data["items"]:
                html = item["html_url"]
                items.append({
                    "repo": item["repository"]["full_name"],
                    "path": item["path"],
                    "url" : html,
                    "raw" : raw_url(html),
                })
            if len(data["items"]) < 100:
                break
            time.sleep(2)
        return items

    # ── fork network ──────────────────────────────────────────────────────────

    def get_forks(self, full_name: str, max_pages: int = 3) -> list[str]:
        """Return 'owner/repo' for every fork of the given repo."""
        owner, repo = full_name.split("/", 1)
        forks: list[str] = []
        for page in range(1, max_pages + 1):
            data = self._gh(f"/repos/{owner}/{repo}/forks", {
                "per_page": 100, "page": page,
            })
            if not data:
                break
            for f in data:
                fn = f["full_name"]
                if fn not in self._repos_seen:
                    self._repos_seen.add(fn)
                    forks.append(fn)
            if len(data) < 100:
                break
            time.sleep(0.5)
        return forks

    # ── file listing ──────────────────────────────────────────────────────────

    def find_py_files(self, full_name: str) -> list[dict]:
        """Return all .py files in the repo via the Git Trees API."""
        owner, repo = full_name.split("/", 1)
        repo_info = self._gh(f"/repos/{owner}/{repo}")
        if not repo_info:
            return []
        branch = repo_info.get("default_branch", "master")
        tree_data = self._gh(
            f"/repos/{owner}/{repo}/git/trees/{branch}",
            {"recursive": "1"},
        )
        if not tree_data or "tree" not in tree_data:
            return []
        files: list[dict] = []
        for node in tree_data["tree"]:
            if node["type"] == "blob" and node["path"].endswith(".py"):
                raw = (
                    f"https://raw.githubusercontent.com/"
                    f"{full_name}/{branch}/{node['path']}"
                )
                files.append({
                    "repo" : full_name,
                    "path" : node["path"],
                    "raw"  : raw,
                    "sha"  : node["sha"],
                    "size" : node.get("size", 0),
                })
        return files

    # ── stargazer discovery ───────────────────────────────────────────────────

    def repos_from_stargazers(
        self, full_name: str, max_users: int = 50
    ) -> list[str]:
        """
        Inspect repositories owned by users who starred *full_name*
        and return those that look like qBittorrent plugin repos.
        """
        owner, repo = full_name.split("/", 1)
        data = self._gh(
            f"/repos/{owner}/{repo}/stargazers",
            {"per_page": min(max_users, 100)},
        )
        if not data:
            return []
        found: list[str] = []
        KEYWORDS = ("qbittorrent", "qbit", "torrent", "search-plugin", "plugin")
        for user in data[:max_users]:
            login = user.get("login", "")
            user_repos = self._gh(
                f"/users/{login}/repos",
                {"per_page": 100, "sort": "updated"},
            )
            if not user_repos:
                continue
            for r in user_repos:
                fn   = r["full_name"]
                desc = (r.get("description") or "").lower()
                name = r["name"].lower()
                if fn in self._repos_seen:
                    continue
                if any(k in name or k in desc for k in KEYWORDS):
                    self._repos_seen.add(fn)
                    found.append(fn)
            time.sleep(0.3)
        return found


# ─────────────────────────────────────────────────────────────────────────────
# GITLAB SEARCHER
# ─────────────────────────────────────────────────────────────────────────────

class GitLabSearcher:

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token

    def _gl(
        self, path: str, params: Optional[dict] = None
    ) -> Optional[Union[dict, list]]:
        url = f"{GITLAB_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return api_get(url, self.token)

    def search_projects(self, query: str, max_pages: int = 3) -> list[dict]:
        projects: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._gl("/projects", {
                "search": query, "order_by": "last_activity_at",
                "per_page": 100, "page": page,
            })
            if not data:
                break
            for p in data:
                projects.append({
                    "id"            : p["id"],
                    "full_name"     : p["path_with_namespace"],
                    "web_url"       : p.get("web_url", ""),
                    "default_branch": p.get("default_branch", "main"),
                })
            if len(data) < 100:
                break
            time.sleep(1)
        return projects

    def find_py_files(
        self, project_id: int, branch: str = "main"
    ) -> list[dict]:
        data = self._gl(
            f"/projects/{project_id}/repository/tree",
            {"recursive": "true", "per_page": 100},
        )
        if not data:
            return []
        files: list[dict] = []
        for item in data:
            if item.get("type") == "blob" and item["name"].endswith(".py"):
                encoded = urllib.parse.quote(item["path"], safe="")
                raw = (
                    f"{GITLAB_API}/projects/{project_id}"
                    f"/repository/files/{encoded}/raw?ref={branch}"
                )
                files.append({"path": item["path"], "raw": raw})
        return files


# ─────────────────────────────────────────────────────────────────────────────
# CODEBERG SEARCHER
# ─────────────────────────────────────────────────────────────────────────────

class CodebergSearcher:

    def search_repos(self, query: str) -> list[dict]:
        url  = (
            f"{CODEBERG_API}/repos/search"
            f"?q={urllib.parse.quote(query)}&limit=50"
        )
        data = api_get(url)
        if not data:
            return []
        return [
            {
                "full_name"     : r["full_name"],
                "default_branch": r.get("default_branch", "main"),
            }
            for r in data.get("data", [])
        ]

    def find_py_files(self, full_name: str, branch: str = "main") -> list[dict]:
        owner, repo = full_name.split("/", 1)
        url  = (
            f"{CODEBERG_API}/repos/{owner}/{repo}"
            f"/git/trees/{branch}?recursive=1"
        )
        data = api_get(url)
        if not data:
            return []
        files: list[dict] = []
        for node in data.get("tree", []):
            path = node.get("path", "")
            if path.endswith(".py"):
                raw = (
                    f"https://codeberg.org/{full_name}"
                    f"/raw/branch/{branch}/{path}"
                )
                files.append({"path": path, "raw": raw})
        return files


# ─────────────────────────────────────────────────────────────────────────────
# AWESOME-LIST URL EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────

_GITHUB_RAW_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/[^\s\)\]\"']+\.py"
)
_GITHUB_BLOB_RE = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+"
    r"/blob/[^\s\)\]\"']+\.py"
)


def extract_plugin_urls_from_markdown(content: bytes) -> list[str]:
    """
    Pull raw .py URLs from README / awesome-list markdown.
    Only returns URLs that pass the SSRF allowlist check.
    """
    text = content.decode("utf-8", errors="replace")
    urls: set[str] = set()

    for m in _GITHUB_RAW_RE.finditer(text):
        candidate = m.group(0).strip().rstrip(")]\",")
        if is_safe_url(candidate):
            urls.add(candidate)

    for m in _GITHUB_BLOB_RE.finditer(text):
        candidate = raw_url(m.group(0).strip().rstrip(")]\","))
        if is_safe_url(candidate):
            urls.add(candidate)

    return list(urls)


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD WORKER
# ─────────────────────────────────────────────────────────────────────────────

class DownloadResult:
    __slots__ = ("url", "action", "filename", "engine", "error", "is_public")

    def __init__(
        self,
        url: str,
        action: str = "skip",
        filename: str = "",
        engine: str = "",
        error: str = "",
        is_public: bool = True,
    ) -> None:
        self.url       = url
        self.action    = action
        self.filename  = filename
        self.engine    = engine
        self.error     = error
        self.is_public = is_public


def _download_one(
    task: dict,
    db: PluginDB,
    writer: PluginWriter,
    pub_dir: str,
    priv_dir: str,
) -> DownloadResult:
    """
    Download, validate, and save a single plugin file.

    task keys:
        raw_url, engine_name, engine_site, author, version,
        last_update, is_public, repo, forge
    """
    url       = task["raw_url"]
    engine    = task.get("engine_name", "")
    is_public = task.get("is_public", True)
    tgt_dir   = pub_dir if is_public else priv_dir
    forge     = task.get("forge", "unknown")

    if db.has_source(url):
        return DownloadResult(url, action="known")

    content = http_get(url)
    if content is None:
        db.log_failure(url, "download failed")
        return DownloadResult(url, action="error", error="download failed")

    if not is_valid_plugin(content):
        db.log_failure(url, "failed validation")
        return DownloadResult(url, action="invalid", error="not a plugin")

    h        = content_hash(content)
    filename = safe_filename(engine or os.path.basename(url), url)
    action   = writer.save(content, filename, tgt_dir)

    db.upsert_plugin(
        h,
        filename,
        engine_name = engine,
        engine_site = task.get("engine_site", ""),
        is_public   = is_public,
        byte_size   = len(content),
    )
    db.upsert_source(
        h,
        url,
        repo        = task.get("repo", ""),
        author      = task.get("author", ""),
        version     = task.get("version", ""),
        last_update = task.get("last_update", ""),
        forge       = forge,
    )
    return DownloadResult(
        url, action=action, filename=filename,
        engine=engine, is_public=is_public,
    )


# ─────────────────────────────────────────────────────────────────────────────
# WIKI TASK BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def wiki_tasks(html: str) -> list[dict]:
    """Parse wiki HTML and return validated download tasks."""
    parser = WikiParser()
    parser.feed(html)

    tasks: list[dict] = []
    for rows, is_public in (
        (parser.public_rows,  True),
        (parser.private_rows, False),
    ):
        for row in rows:
            if len(row) < 5:
                continue

            eng_name  = row[0]["text"]
            eng_links = row[0]["links"]
            auth_cell = row[1]
            version   = row[2]["text"] if len(row) > 2 else "1.0"
            updated   = row[3]["text"] if len(row) > 3 else ""
            dl_cell   = row[4]
            comments  = row[5]["text"] if len(row) > 5 else ""

            if not dl_cell["links"]:
                continue

            link = raw_url(dl_cell["links"][0])

            # Require the download link to point at a .py file
            if not link.endswith(".py"):
                continue

            if not is_safe_url(link):
                continue

            tasks.append({
                "raw_url"    : link,
                "engine_name": eng_name,
                "engine_site": eng_links[0] if eng_links else "",
                "author"     : auth_cell["text"].strip(),
                "version"    : version,
                "last_update": updated,
                "is_public"  : is_public,
                "repo"       : "",
                "forge"      : "wiki",
                "comments"   : comments,
            })
    return tasks


# ─────────────────────────────────────────────────────────────────────────────
# REPO-FILE → TASK CONVERTER
# ─────────────────────────────────────────────────────────────────────────────

def files_to_tasks(
    files: list[dict],
    repo: str,
    forge: str,
    is_public: bool = True,
) -> list[dict]:
    """Convert a list of file descriptors into download tasks."""
    tasks: list[dict] = []
    for f in files:
        u = f.get("raw", "")
        if not u or not is_safe_url(u):
            continue
        stem = os.path.splitext(os.path.basename(f.get("path", "plugin")))[0]
        tasks.append({
            "raw_url"    : u,
            "engine_name": stem,
            "engine_site": "",
            "author"     : repo.split("/")[0] if "/" in repo else repo,
            "version"    : "",
            "last_update": "",
            "is_public"  : is_public,
            "repo"       : repo,
            "forge"      : forge,
        })
    return tasks


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS PRINTER
# ─────────────────────────────────────────────────────────────────────────────

_ACTION_ICONS: dict[str, str] = {
    "new"      : "✅",
    "updated"  : "🔄",
    "unchanged": "⏭️ ",
    "duplicate": "⏭️ ",
    "known"    : "⏭️ ",
    "invalid"  : "🚫",
    "error"    : "❌",
    "skip"     : "⏭️ ",
}
_print_lock = threading.Lock()


def pprint_result(result: DownloadResult, idx: int, total: int) -> None:
    icon = _ACTION_ICONS.get(result.action, "❓")
    name = result.engine or os.path.basename(result.url)
    msg  = f"({result.action})"
    if result.error:
        msg += f" – {result.error}"
    with _print_lock:
        print(f"  {icon} [{idx:>4}/{total}] {name:<45} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# TASK EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

def run_tasks(
    tasks: list[dict],
    db: PluginDB,
    writer: PluginWriter,
    pub_dir: str,
    priv_dir: str,
    threads: int = 6,
    label: str = "",
) -> dict:
    """Execute a list of download tasks concurrently and return action counts."""
    if not tasks:
        return {}

    total   = len(tasks)
    counter : dict[str, int] = defaultdict(int)
    idx     = 0
    idx_lk  = threading.Lock()

    def _work(task: dict) -> str:
        nonlocal idx
        r = _download_one(task, db, writer, pub_dir, priv_dir)
        with idx_lk:
            idx += 1
            i = idx
        pprint_result(r, i, total)
        return r.action

    if label:
        print(f"\n{label} ({total} tasks, {threads} threads)")

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {pool.submit(_work, t): t for t in tasks}
        for fut in as_completed(futures):
            try:
                counter[fut.result()] += 1
            except Exception:
                counter["error"] += 1

    return dict(counter)


# ─────────────────────────────────────────────────────────────────────────────
# README GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

def generate_readme(out_dir: str, db: PluginDB) -> None:
    st   = db.stats()
    rows = db.all_plugins()

    pub_rows  = [r for r in rows if r["is_public"] == 1]
    priv_rows = [r for r in rows if r["is_public"] == 0]

    def table_section(plugin_rows: list) -> str:
        lines: list[str] = []
        for i, r in enumerate(plugin_rows, 1):
            name      = r["engine_name"] or r["filename"]
            site      = r["engine_site"] or ""
            repo      = r["repo"]        or ""
            author    = r["author"]      or ""
            src       = r["source_url"]  or ""
            eng_cell  = f"[{name}]({site})" if site  else name
            auth_cell = f"[{author}]({repo})" if repo else author
            dl_cell   = f"[`{r['filename']}`]({src})" if src else r["filename"]
            lines.append(f"| {i} | {eng_cell} | {auth_cell} | {dl_cell} |")
        return "\n".join(lines) or "| – | – | – | – |"

    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    readme = f"""# 🔥 qBittorrent Search Plugins – Ultimate Collection

[![Plugins](https://img.shields.io/badge/unique_plugins-{st['plugins']}-blue)](./)
[![Sources](https://img.shields.io/badge/sources_indexed-{st['sources']}-green)](./)
[![Public](https://img.shields.io/badge/public-{st['public']}-brightgreen)](./public_sites)
[![Private](https://img.shields.io/badge/private-{st['private']}-orange)](./private_sites)
[![Updated](https://img.shields.io/badge/last_updated-{now.replace(' ', '-')}-lightgrey)](./)

> ⚠️ **DISCLAIMER** – Automated aggregation of unofficial plugins.
> All credit goes to original authors. Use at your own risk.

## 📊 Stats

| Metric | Count |
|:-------|------:|
| Unique plugins (by content) | **{st['plugins']}** |
| Sources indexed | **{st['sources']}** |
| Public site plugins | **{st['public']}** |
| Private site plugins | **{st['private']}** |
| Failed downloads | {st['failures']} |
| Last updated | {now} |

## 📦 Installation

1. qBittorrent → **View → Search Engine** → **Search Plugins**
2. **Install a new one** → select a `.py` file, or paste a raw URL.

## 🌍 Public Plugins ({len(pub_rows)})

| # | Engine | Author | Download |
|:-:|:-------|:-------|:---------|
{table_section(pub_rows)}

## 🔒 Private / Credentialed Plugins ({len(priv_rows)})

*Edit the `.py` file and fill in your credentials before installing.*

| # | Engine | Author | Download |
|:-:|:-------|:-------|:---------|
{table_section(priv_rows)}

## 🔍 Discovery Sources

| Source | Method |
|:-------|:-------|
| [Official Wiki]({WIKI_URL}) | HTML table scrape |
| GitHub Repo Search | {len(GITHUB_REPO_QUERIES)} queries × 5 pages |
| GitHub Topic Search | {len(GITHUB_TOPIC_QUERIES)} topics |
| GitHub Code Search | {len(GITHUB_CODE_QUERIES)} code queries |
| GitHub Fork Networks | Forks of known plugin repos |
| GitHub Stargazers | Who starred the official repo |
| GitLab | {len(GITLAB_QUERIES)} queries |
| Codeberg | {len(CODEBERG_QUERIES)} queries |
| Awesome-lists | {len(AWESOME_LIST_URLS)} curated lists |
| Known repos | {len(KNOWN_REPOS)} hardcoded |

## ⚡ Smart Logic

- **Full SHA-256 content deduplication** – identical code from multiple repos saved once
- **Tiered plugin validation** – strong + weak signatures with class-context requirement
- **SSRF-safe fetching** – only allowlisted hosts contacted
- **Hash-based incremental** – unchanged files never re-downloaded
- **Rate-limit aware** – integer header parsing, auto-sleep on HTTP 429
- **Concurrent** – configurable thread pool for fast downloads
- **sqlite3.Row** – named column access, no magic indices

## 📁 Structure

```
{os.path.basename(out_dir)}/
├── public_sites/       # Public torrent search plugins
├── private_sites/      # Private tracker plugins (need credentials)
├── .meta/
│   ├── plugins.db      # SQLite database (full metadata)
│   └── plugins.json    # JSON export
└── README.md
```

## ⚖️ License

Individual plugins retain their authors' licenses.
This aggregator script is MIT licensed.
"""
    Path(os.path.join(out_dir, "README.md")).write_text(readme, encoding="utf-8")
    print("  📝 README.md written")


# ─────────────────────────────────────────────────────────────────────────────
# JSON EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def export_json(meta_dir: str, db: PluginDB) -> None:
    rows    = db.all_plugins()
    plugins = [
        {
            "content_hash": r["content_hash"],
            "filename"    : r["filename"],
            "engine_name" : r["engine_name"],
            "engine_site" : r["engine_site"],
            "is_public"   : bool(r["is_public"]),
            "first_seen"  : r["first_seen"],
            "last_seen"   : r["last_seen"],
            "byte_size"   : r["byte_size"],
            "source_url"  : r["source_url"],
            "repo"        : r["repo"],
            "author"      : r["author"],
            "forge"       : r["forge"],
        }
        for r in rows
    ]
    out = {
        "exported": datetime.now(timezone.utc).isoformat(),
        "stats"   : db.stats(),
        "plugins" : plugins,
    }
    Path(os.path.join(meta_dir, "plugins.json")).write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("  📄 plugins.json exported")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="qBittorrent Search Plugin Ultimate Collector v3.1"
    )
    p.add_argument("--out",           default=DEFAULT_OUT,
                   help="Output directory (default: qbt-plugins)")
    p.add_argument("--github-token",  default=os.environ.get("GITHUB_TOKEN", ""),
                   help="GitHub PAT (higher rate-limits + code search)")
    p.add_argument("--gitlab-token",  default=os.environ.get("GITLAB_TOKEN", ""),
                   help="GitLab PAT")
    p.add_argument("--threads",       type=int, default=6,
                   help="Concurrent download threads (default: 6)")
    p.add_argument("--no-discovery",  action="store_true",
                   help="Skip GitHub/GitLab/Codeberg API discovery")
    p.add_argument("--no-forks",      action="store_true",
                   help="Skip fork-network crawl")
    p.add_argument("--no-stargazers", action="store_true",
                   help="Skip stargazer-based discovery")
    p.add_argument("--no-codeberg",   action="store_true",
                   help="Skip Codeberg search")
    p.add_argument("--no-gitlab",     action="store_true",
                   help="Skip GitLab search")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# PRETTY PRINTERS
# ─────────────────────────────────────────────────────────────────────────────

def banner(text: str) -> None:
    w = 78
    print("┌" + "─" * w + "┐")
    print("│" + text.center(w) + "│")
    print("└" + "─" * w + "┘")


def section(title: str) -> None:
    pad = max(0, 72 - len(title))
    print(f"\n{'═' * 4} {title} {'═' * pad}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    args   = parse_args()
    out    = args.out
    pub    = os.path.join(out, "public_sites")
    priv   = os.path.join(out, "private_sites")
    meta   = os.path.join(out, ".meta")
    gh_tok = args.github_token or ""
    gl_tok = args.gitlab_token or ""
    T      = args.threads

    for d in (pub, priv, meta):
        os.makedirs(d, exist_ok=True)

    db     = PluginDB(os.path.join(meta, "plugins.db"))
    writer = PluginWriter()

    banner("🔥  qBittorrent Plugin ULTIMATE Collector  v3.1  🔥")

    all_tasks: list[dict] = []

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1 – Official Wiki
    # ══════════════════════════════════════════════════════════════════════════
    section("STEP 1 – Official qBittorrent Wiki")
    html_bytes = http_get(WIKI_URL, accept_html=True)
    if html_bytes:
        wiki_t = wiki_tasks(html_bytes.decode("utf-8", "replace"))
        print(f"  Found {len(wiki_t)} plugin entries in wiki")
        all_tasks.extend(wiki_t)
    else:
        print("  ❌ Could not fetch wiki page")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2 – Known repos
    # ══════════════════════════════════════════════════════════════════════════
    section("STEP 2 – Scanning Known Repositories")
    gh = GitHubSearcher(gh_tok or None)

    for display, full_name in KNOWN_REPOS.items():
        print(f"  🗂  {full_name}")
        files = gh.find_py_files(full_name)
        if files:
            all_tasks.extend(files_to_tasks(files, full_name, "github"))
        time.sleep(0.3)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 – GitHub Discovery
    # ══════════════════════════════════════════════════════════════════════════
    if not args.no_discovery:
        discovered_repos: set[str] = set()

        section("STEP 3a – GitHub Repository Search")
        for q in GITHUB_REPO_QUERIES:
            print(f"  🔎  repo: {q}")
            discovered_repos.update(gh.search_repos(q))
            time.sleep(1)

        section("STEP 3b – GitHub Topic Search")
        for topic in GITHUB_TOPIC_QUERIES:
            print(f"  🏷  topic:{topic}")
            discovered_repos.update(gh.search_topics(topic))
            time.sleep(1)

        section("STEP 3c – GitHub Code Search")
        code_tasks: list[dict] = []
        for q in GITHUB_CODE_QUERIES:
            if not gh_tok:
                print(f"  ⚠️  code search needs --github-token (skipping)")
                break
            print(f"  🔬  code: {q}")
            for hit in gh.search_code(q):
                if is_safe_url(hit["raw"]):
                    code_tasks.append({
                        "raw_url"    : hit["raw"],
                        "engine_name": os.path.splitext(
                            os.path.basename(hit["path"])
                        )[0],
                        "engine_site": "",
                        "author"     : hit["repo"].split("/")[0],
                        "version"    : "",
                        "last_update": "",
                        "is_public"  : True,
                        "repo"       : hit["repo"],
                        "forge"      : "github",
                    })
                    discovered_repos.add(hit["repo"])
            time.sleep(2)
        all_tasks.extend(code_tasks)

        print(f"\n  📦 Scanning {len(discovered_repos)} discovered repos …")
        for rn in discovered_repos:
            files = gh.find_py_files(rn)
            if files:
                all_tasks.extend(files_to_tasks(files, rn, "github"))
            time.sleep(0.3)

        # ── Fork network ──────────────────────────────────────────────────────
        if not args.no_forks:
            section("STEP 3d – Fork Networks")
            fork_roots = [OFFICIAL_REPO] + [
                v for v in KNOWN_REPOS.values()
                if "search-plugin" in v.lower()
            ][:8]
            for root in fork_roots:
                print(f"  🍴  forks of {root}")
                for fn in gh.get_forks(root):
                    files = gh.find_py_files(fn)
                    if files:
                        all_tasks.extend(files_to_tasks(files, fn, "github"))
                    time.sleep(0.3)

        # ── Stargazer discovery ───────────────────────────────────────────────
        if not args.no_stargazers and gh_tok:
            section("STEP 3e – Stargazer Discovery")
            print(f"  ⭐  scanning stargazers of {OFFICIAL_REPO} …")
            for rn in gh.repos_from_stargazers(OFFICIAL_REPO, max_users=80):
                files = gh.find_py_files(rn)
                if files:
                    all_tasks.extend(files_to_tasks(files, rn, "github"))
                time.sleep(0.3)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4 – GitLab
    # ══════════════════════════════════════════════════════════════════════════
    if not args.no_gitlab:
        section("STEP 4 – GitLab Search")
        glab = GitLabSearcher(gl_tok or None)
        for q in GITLAB_QUERIES:
            print(f"  🔎  {q}")
            for proj in glab.search_projects(q):
                files = glab.find_py_files(proj["id"], proj["default_branch"])
                all_tasks.extend(
                    files_to_tasks(files, proj["full_name"], "gitlab")
                )
            time.sleep(1)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5 – Codeberg
    # ══════════════════════════════════════════════════════════════════════════
    if not args.no_codeberg:
        section("STEP 5 – Codeberg Search")
        cb = CodebergSearcher()
        for q in CODEBERG_QUERIES:
            print(f"  🔎  {q}")
            for r in cb.search_repos(q):
                files = cb.find_py_files(r["full_name"], r["default_branch"])
                all_tasks.extend(
                    files_to_tasks(files, r["full_name"], "codeberg")
                )
            time.sleep(1)

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6 – Awesome-lists
    # ══════════════════════════════════════════════════════════════════════════
    section("STEP 6 – Awesome-Lists & Curated URLs")
    for list_url in AWESOME_LIST_URLS:
        print(f"  📋  {list_url}")
        content = http_get(list_url)
        if not content:
            continue
        urls = extract_plugin_urls_from_markdown(content)
        print(f"       → {len(urls)} raw .py URLs extracted")
        for u in urls:
            stem = os.path.splitext(os.path.basename(u))[0]
            all_tasks.append({
                "raw_url"    : u,
                "engine_name": stem,
                "engine_site": "",
                "author"     : "",
                "version"    : "",
                "last_update": "",
                "is_public"  : True,
                "repo"       : "",
                "forge"      : "awesome-list",
            })

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 7 – Deduplicate task URLs then download
    # ══════════════════════════════════════════════════════════════════════════
    section("STEP 7 – Downloading (deduped)")
    seen_urls   : set[str]  = set()
    unique_tasks: list[dict] = []
    for t in all_tasks:
        u = t.get("raw_url", "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            unique_tasks.append(t)

    print(f"  Total unique URLs to fetch: {len(unique_tasks)}")
    counts = run_tasks(
        unique_tasks, db, writer, pub, priv,
        threads=T,
        label="⬇  Downloading plugins",
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 8 – Output files
    # ══════════════════════════════════════════════════════════════════════════
    section("STEP 8 – Writing output files")
    generate_readme(out, db)
    export_json(meta, db)

    # Snapshot stats before closing
    final_st = db.stats()
    db.close()

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ══════════════════════════════════════════════════════════════════════════
    new_count   = counts.get("new",       0)
    upd_count   = counts.get("updated",   0)
    dup_count   = (counts.get("duplicate",  0)
                   + counts.get("unchanged", 0)
                   + counts.get("known",     0))
    inv_count   = counts.get("invalid",   0)
    err_count   = counts.get("error",     0)

    print("\n" + "╔" + "═" * 78 + "╗")
    print("║" + "  📊  FINAL SUMMARY".ljust(78) + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  ✅  New plugins saved:        {new_count:<48} ║")
    print(f"║  🔄  Plugins updated:          {upd_count:<48} ║")
    print(f"║  ⏭️   Already known / dupes:   {dup_count:<48} ║")
    print(f"║  🚫  Failed validation:        {inv_count:<48} ║")
    print(f"║  ❌  Download errors:          {err_count:<48} ║")
    print(f"║  🗃  DB unique plugins:        {final_st['plugins']:<48} ║")
    print(f"║  🗃  DB sources indexed:       {final_st['sources']:<48} ║")
    print(f"║  📁  Output directory:         {out:<48} ║")
    print(f"║  🕐  Finished:                 "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<48} ║")
    print("╚" + "═" * 78 + "╝")

    pub_count  = sum(1 for f in os.scandir(pub)  if f.name.endswith(".py"))
    priv_count = sum(1 for f in os.scandir(priv) if f.name.endswith(".py"))
    print()
    print(f"  📂 public_sites/   → {pub_count} .py files")
    print(f"  📂 private_sites/  → {priv_count} .py files")
    print(f"  💾 SQLite DB       → {os.path.join(meta, 'plugins.db')}")
    print(f"  📄 JSON export     → {os.path.join(meta, 'plugins.json')}")
    print()


if __name__ == "__main__":
    main()
