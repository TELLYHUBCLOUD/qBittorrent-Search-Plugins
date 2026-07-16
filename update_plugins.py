#!/usr/bin/env python3
"""
qBittorrent Search Plugins Auto-Updater
Designed for GitHub Actions workflow — zero config, just run.
Enhanced edition: expanded repo list, broader discovery queries.
"""

import os
import re
import json
import time
import hashlib
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

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
PUBLIC_DIR  = Path("public_sites")
PRIVATE_DIR = Path("private_sites")
ENGINES_DIR = Path("engines")
ZIP_NAME    = Path("qBittorrent-Search-Plugins-Complete.zip")

GITHUB_API   = "https://api.github.com"
GITHUB_API_DISABLED = False
GITLAB_API   = "https://gitlab.com/api/v4"
CODEBERG_API = "https://codeberg.org/api/v1"
WIKI_URL     = "https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins"

THREADS      = 10
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
    # === NEW REPOS (expanded discovery pass) ===
    # Note: dead/missing repos are skipped automatically (API 404 -> no files).
    "DiegoRBaquero/qBittorrent-Search-Plugins",
    "mandulaj/PirateBay-qBittorrent-plugin",
    "pikdum/qbittorrent-search-plugins",
    "morpheus65535/qbittorrent-search-plugins",
    "naaando/qbittorrent-search-plugins",
    "sebdels/qbittorrent-search-plugins",
    "bluedeck/qbittorrent-search-plugins",
    "Larsluph/qBittorrent-Search-Plugins",
    "zapp-brannigan/qbittorrent-search-plugins",
    "rmartin16/qbittorrent-search-plugins",
    "c0redev/qbittorrent-search-plugins",
    "orpheev/qbittorrent-search-plugins",
    "tornado-cube/qbittorrent-search-plugins",
    "5pr1nter/qBittorrent-Search-Plugins",
    "r3ok/qBittorrent-Search-Plugins",
    "freefq/qbittorrent-search-plugins",
    "TheGoblinKing/qBittorrent-Search-Plugins",
    "tengu-go/qbittorrent-search-plugins",
]

COLLECTION_REPOS = [
    "https://github.com/alessandro-ooo/one-click-qbittorrent-searchplugins",
    "https://github.com/nklido/qBittorrent_search_engines",
    "https://github.com/darktohka/qbittorrent-plugins",
    "https://github.com/LightDestory/qBittorrent-Search-Plugins",
    "https://github.com/BurningMop/qBittorrent-Search-Plugins",
    "https://github.com/hdvinnie/qBittorrent-Search-Plugins",
    "https://github.com/freecoder76/qBittorrent-search-plugins",
    "https://github.com/scadams/qbittorrent-search-plugins",
]

GITHUB_REPO_QUERIES = [
    "qBittorrent search plugin language:python",
    "qbittorrent search engine python",
    "qbittorrent search plugin torrent",
    "qbittorrent-plugin python torrent",
    "qbt search plugin python",
    "qbittorrent searchplugin python",
    "qbittorrent-search-plugin python",
    "qbt search engine python",
    "qbittorrent searchengine python",
]

GITHUB_TOPIC_QUERIES = [
    "qbittorrent-search-plugin", "qbittorrent-plugin",
    "qbittorrent-search", "qbittorrent",
    "search-plugin", "torrent-search",
]

GITHUB_CODE_QUERIES = [
    "class SearchEngine filename:.py qbittorrent",
    "noSearchResult qbittorrent filename:.py",
    "supported_categories qbittorrent filename:.py",
    "def search(self, what, cat='all') qbittorrent",
    "download_torrent qbittorrent filename:.py",
    "prettyUrl qbittorrent filename:.py",
]

# ═══════════════════════════════════════════════════════════════════════════════
# PLUGIN VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════
PLUGIN_SIGS = [
    b"noSearchResult", b"class SearchEngine", b"prettyPrinting",
    b"supported_categories", b"url_dl", b"CORE_SITE",
    b"magnetdl", b"qbittorrentapi", b"def search(self, what,",
]
BAD_SIGS = [b"<!DOCTYPE", b"<html", b"404: Not Found", b"Not Found"]

def is_valid_plugin(content):
    if not (400 <= len(content) <= 512000):
        return False
    for bad in BAD_SIGS:
        if bad in content[:300]:
            return False
    for sig in PLUGIN_SIGS:
        if sig in content:
            return True
    return b"def search(" in content and b"class " in content

def is_private_plugin(content):
    low = content.lower()
    signals = [b"passkey", b"api_key", b"apikey", b"auth_token",
               b"bearer", b"username", b"password", b"private",
               b"cookie", b"login", b"authenticate"]
    return sum(1 for s in signals if s in low) >= 2

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════════════════════
def http_get(url, headers=None, timeout=20, retries=4, accept_html=False):
    global GITHUB_API_DISABLED
    if "api.github.com" in url and GITHUB_API_DISABLED:
        return None

    for attempt in range(retries):
        try:
            req_headers = dict(HEADERS)
            if headers:
                req_headers.update(headers)
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                rem = resp.headers.get("X-RateLimit-Remaining", "999")
                if "api.github.com" in url and rem == "0":
                    print("        GitHub rate limit reached (X-RateLimit-Remaining: 0). Disabling further GitHub API calls.")
                    GITHUB_API_DISABLED = True
                    return None
                rst = resp.headers.get("X-RateLimit-Reset")
                if rem == "0" and rst:
                    w = max(0, int(rst) - int(time.time())) + 5
                    print("        Rate-limited - sleeping %ds" % w)
                    time.sleep(w)
                data = resp.read()
                if not accept_html and (
                    data[:15].lstrip().startswith(b"<!DOCTYPE")
                    or data[:6].lstrip().startswith(b"<html")
                ):
                    return None
                return data
        except urllib.error.HTTPError as e:
            if "api.github.com" in url and (e.code == 403 or e.code == 429):
                print("        GitHub rate limit hit (HTTP %d). Disabling further GitHub API calls." % e.code)
                GITHUB_API_DISABLED = True
                return None
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
        h["Authorization"] = "token " + token
        h["Accept"] = "application/vnd.github+json"
    elif token and ("gitlab" in url or "codeberg" in url):
        h["Authorization"] = "Bearer " + token
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
def safe_rmtree(path):
    import stat
    path = Path(path)
    if not path.exists():
        return
    def remove_readonly(func, p, excinfo):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    for i in range(5):
        try:
            shutil.rmtree(path, onerror=remove_readonly)
            if not path.exists():
                return
        except Exception:
            time.sleep(0.1)
    try:
        shutil.rmtree(path, onerror=remove_readonly)
    except Exception as e:
        print("Warning: Could not remove directory %s: %s" % (path, e))

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
    return (clean + ".py") if clean else "unknown.py"

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
                self.row = []
            elif tag in ("td", "th"):
                self.in_cell = True
                self.cell = {"text": "", "links": []}
            elif tag == "a" and self.in_cell and "href" in d:
                self.cell["links"].append(d["href"])

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.in_table = False
        elif tag == "tr":
            if self.row and len(self.row) >= 5:
                if "search engine" not in self.row[0]["text"].lower():
                    if self.section == "public":
                        self.public.append(self.row)
                    elif self.section == "private":
                        self.private.append(self.row)
        elif tag in ("td", "th"):
            self.in_cell = False
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)

    def handle_data(self, data):
        if self.in_cell:
            self.cell["text"] += data

# ═══════════════════════════════════════════════════════════════════════════════
# STORE
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

        if fp.exists():
            stem = fp.stem
            c = 1
            while fp.exists():
                fp = target / (stem + "_" + str(c) + ".py")
                c += 1
            filename = fp.name

        fp.write_bytes(content)
        self.counts["new"] += 1

        ep = ENGINES_DIR / filename
        if not ep.exists():
            ep.write_bytes(content)

        self.results.append({
            "engine_name": engine_name,
            "engine_site": engine_site,
            "author_name": author,
            "repo_link": repo,
            "version": version,
            "last_update": " ".join(last_update.split()),
            "filename": filename,
            "download_url": url,
            "comments": " ".join(comments.split()),
            "file_size": len(content),
            "is_private": priv,
            "forge": forge,
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
    global GITHUB_API_DISABLED
    if GITHUB_API_DISABLED:
        return None
    url = GITHUB_API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return api_get(url, GITHUB_TOKEN)

def gh_search_repos(query, max_pages=2):
    found = []
    for page in range(1, max_pages + 1):
        data = gh_api("/search/repositories", {
            "q": query, "sort": "updated", "per_page": 100, "page": page
        })
        if not data or not data.get("items"):
            break
        for item in data["items"]:
            fn = item["full_name"]
            if fn not in KNOWN_REPOS:
                KNOWN_REPOS.append(fn)
                found.append(fn)
        if len(data["items"]) < 100:
            break
        time.sleep(2)
    return found

def gh_search_topics(topic):
    data = gh_api("/search/repositories", {
        "q": "topic:" + topic, "sort": "updated", "per_page": 100
    })
    if not data:
        return []
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
        if not data or not data.get("items"):
            break
        for item in data["items"]:
            repo = item.get("repository", {}).get("full_name", "")
            path = item.get("path", "")
            html_url = item.get("html_url", "")
            if html_url and path.endswith(".py") and path not in SKIP_FILES:
                items.append({"repo": repo, "path": path, "url": html_url})
        if len(data["items"]) < 100:
            break
        time.sleep(3)
    return items

def gh_list_files(full_name):
    files = []
    try:
        owner, repo = full_name.split("/", 1)
    except ValueError:
        return files
    for sub in ["", "engines", "plugins", "search_engines", "src"]:
        data = gh_api("/repos/" + owner + "/" + repo + "/contents/" + sub)
        if not data or not isinstance(data, list):
            continue
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
        url = (GITLAB_API + "/projects/"
               + urllib.parse.quote(pid, safe="")
               + "/repository/tree?path=" + sub + "&per_page=100")
        data = api_get(url, GITLAB_TOKEN)
        if not data or not isinstance(data, list):
            continue
        for item in data:
            if (item.get("type") == "blob"
                    and item["name"].endswith(".py")
                    and item["name"] not in SKIP_FILES):
                raw = (GITLAB_API + "/projects/"
                       + urllib.parse.quote(pid, safe="")
                       + "/repository/files/"
                       + urllib.parse.quote(item["path"], safe="")
                       + "/raw?ref=main")
                files.append({"repo": pid, "path": item["path"],
                              "url": raw, "name": item["name"]})
        time.sleep(0.5)
    return files

# ═══════════════════════════════════════════════════════════════════════════════
# CODEBERG
# ═══════════════════════════════════════════════════════════════════════════════
def cb_list_files(full_name):
    files = []
    try:
        owner, repo = full_name.split("/", 1)
    except ValueError:
        return files
    for sub in ["", "engines", "plugins"]:
        url = CODEBERG_API + "/repos/" + owner + "/" + repo + "/contents/" + sub
        data = api_get(url)
        if not data or not isinstance(data, list):
            continue
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
# BULK DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════
def bulk_download(files, store, forge="github"):
    if not files:
        return 0
    new = 0
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futs = {}
        for fi in files:
            f = pool.submit(dl, fi["url"], store,
                            filename=fi.get("name", ""),
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
# PHASE 1 - WIKI
# ═══════════════════════════════════════════════════════════════════════════════
def phase_wiki(store):
    print("[1/4] Wiki...")
    html = fetch_text(WIKI_URL, timeout=20)
    if not html:
        print("      Wiki fetch FAILED")
        return
    wp = WikiParser()
    wp.feed(html)
    print("      Rows: %d public, %d private" % (len(wp.public), len(wp.private)))

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
                store.log_fail("", "Wiki: %s - no URL" % ename)
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
    print("      Downloaded: %d" % ok)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 - GITHUB
# ═══════════════════════════════════════════════════════════════════════════════
def phase_github(store):
    print("[2/4] GitHub API...")
    for q in GITHUB_REPO_QUERIES:
        found = gh_search_repos(q, max_pages=2)
        if found:
            print("      Repo '%s...' -> +%d" % (q[:35], len(found)))
    for topic in GITHUB_TOPIC_QUERIES:
        found = gh_search_topics(topic)
        if found:
            print("      Topic '%s' -> +%d" % (topic, len(found)))

    code_files = []
    if GITHUB_TOKEN:
        for q in GITHUB_CODE_QUERIES:
            code_files.extend(gh_search_code(q, max_pages=1))

    all_files = []
    total_repos = len(KNOWN_REPOS)
    print("      Scanning %d repos..." % total_repos)
    for i, repo in enumerate(KNOWN_REPOS):
        all_files.extend(gh_list_files(repo))
        if (i + 1) % 25 == 0 or (i + 1) == total_repos:
            print("        %d/%d - %d files found" % (i + 1, total_repos, len(all_files)))
        time.sleep(0.25)

    new1 = bulk_download(all_files, store, "github")
    print("      GitHub repos: %d new" % new1)

    if code_files:
        new2 = bulk_download(code_files, store, "github-code")
        print("      GitHub code: %d new" % new2)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 - OTHER SOURCES
# ═══════════════════════════════════════════════════════════════════════════════
def phase_other(store):
    print("[3/4] GitLab / Codeberg / Collections...")

    for q in ["qbittorrent search plugin", "qbittorrent plugin"]:
        url = GITLAB_API + "/projects?search=" + urllib.parse.quote(q) + "&per_page=50"
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
                            print("      GitLab %s: +%d" % (pid, n))
        time.sleep(1)

    for q in ["qbittorrent search plugin", "qbt plugin"]:
        data = api_get(
            CODEBERG_API + "/repos/search?q=" + urllib.parse.quote(q) + "&limit=50"
        )
        if data and isinstance(data, dict):
            for r in data.get("data", []):
                fn = r.get("full_name", "")
                if fn:
                    files = cb_list_files(fn)
                    if files:
                        n = bulk_download(files, store, "codeberg")
                        if n:
                            print("      Codeberg %s: +%d" % (fn, n))
        time.sleep(1)

    tasks = []
    for repo_url in COLLECTION_REPOS:
        html = fetch_text(repo_url, timeout=10)
        if not html:
            continue
        for match in re.findall(r'href="([^"]+\.py)"', html):
            full = urllib.parse.urljoin(repo_url, match)
            full = to_raw(full)
            name = os.path.basename(full)
            if name not in SKIP_FILES:
                tasks.append((full, name, repo_url))

    if tasks:
        print("      Collection links: %d" % len(tasks))
        new = 0
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futs = {}
            for u, n, r in tasks:
                f = pool.submit(dl, u, store, filename=n, repo=r, forge="collection")
                futs[f] = u
            for f in as_completed(futs):
                try:
                    if f.result() == "new":
                        new += 1
                except Exception:
                    pass
        print("      Collections: +%d" % new)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 - OUTPUT (NO f-string, pure .format() and concatenation)
# ═══════════════════════════════════════════════════════════════════════════════
def build_table_rows(items, start=1):
    lines = []
    for i, it in enumerate(items, start):
        en = it["engine_name"]
        es = it["engine_site"]
        an = it["author_name"]
        rl = it["repo_link"]
        if es:
            en = "[" + en + "](" + es + ")"
        if rl:
            an = "[" + an + "](" + rl + ")"
        dl = "[`" + it["filename"] + "`](" + it["download_url"] + ")"
        forge = "`" + str(it.get("forge", "wiki")) + "`"
        comm = it["comments"]
        line = "| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, en, an, it["version"], it["last_update"], dl, forge, comm
        )
        lines.append(line)
    return "\n".join(lines)

def phase_output(store):
    print("[4/4] Generating outputs...")
    c = store.counts
    print("      %d new | %d dup | %d invalid | %d skip | %d fail" % (
        c["new"], c["dup"], c["invalid"], c["skip"], c["fail"]))

    pub_count = len(list(PUBLIC_DIR.glob("*.py")))
    priv_count = len(list(PRIVATE_DIR.glob("*.py")))
    eng_count = len(list(ENGINES_DIR.glob("*.py")))

    pub_r = [r for r in store.results if not r["is_private"]]
    priv_r = [r for r in store.results if r["is_private"]]

    repo_env = os.environ.get("GITHUB_REPOSITORY", "user/qBittorrent-Search-Plugins")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    pub_table = build_table_rows(pub_r, 1)
    priv_table = build_table_rows(priv_r, len(pub_r) + 1)

    # Build README with .format() - NO f-string
    readme_template = (
        "# qBittorrent Search Plugins - Complete Collection\n\n"
        "[![Auto-Update](https://github.com/{repo}/actions/workflows/auto-update.yml/badge.svg)]"
        "(https://github.com/{repo}/actions/workflows/auto-update.yml)\n\n"
        "Aggregates **all** unofficial qBittorrent search engine plugins from the\n"
        "[Official Wiki](https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins),\n"
        "GitHub, GitLab, Codeberg, and community collections.\n\n"
        "> **DISCLAIMER**: Unofficial plugins. All credits to original authors. Use at your own risk.\n\n"
        "## Stats\n\n"
        "| Metric | Count |\n"
        "|--------|-------|\n"
        "| **Total plugins** | {total} |\n"
        "| Public sites | {pub} |\n"
        "| Private sites | {priv} |\n\n"
        "## Download\n\n"
        "- **[qBittorrent-Search-Plugins-Complete.zip]"
        "(https://raw.githubusercontent.com/{repo}/main/qBittorrent-Search-Plugins-Complete.zip)**\n"
        "- `public_sites/` - Public torrent site plugins\n"
        "- `private_sites/` - Private tracker plugins (may need credentials)\n"
        "- `engines/` - All plugins combined\n\n"
        "## Install\n\n"
        "1. Open **qBittorrent** -> **Search** tab\n"
        "2. Click **Search plugins...** -> **Install a new one**\n"
        "3. Select **Local file** and pick `.py` files\n\n"
        "## Public Sites ({pub})\n\n"
        "| # | Search Engine | Author | Version | Updated | Download | Source | Comments |\n"
        "|---|---------------|--------|---------|---------|----------|--------|----------|\n"
        "{pub_table}\n\n"
        "## Private Sites ({priv})\n\n"
        "| # | Search Engine | Author | Version | Updated | Download | Source | Comments |\n"
        "|---|---------------|--------|---------|---------|----------|--------|----------|\n"
        "{priv_table}\n\n"
        "## Testing\n\n"
        "```\n"
        "python3 nova2.py <plugin_name> all \"ubuntu\"\n"
        "```\n\n"
        "## License\n\n"
        "All plugins are property of their respective authors. This aggregator is provided as-is.\n\n"
        "---\n"
        "*Last updated: {now}*\n"
    )

    readme = readme_template.format(
        repo=repo_env,
        total=eng_count,
        pub=pub_count,
        priv=priv_count,
        pub_table=pub_table,
        priv_table=priv_table,
        now=now_str,
    )

    Path("README.md").write_text(readme, encoding="utf-8")

    # plugins.json
    meta = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "stats": {"total": eng_count, "public": pub_count, "private": priv_count},
        "plugins": store.results,
        "failures": store.failures,
    }
    Path("plugins.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ZIP
    if ZIP_NAME.exists():
        ZIP_NAME.unlink()
    with zipfile.ZipFile(str(ZIP_NAME), "w", zipfile.ZIP_DEFLATED) as zf:
        for d in [PUBLIC_DIR, PRIVATE_DIR, ENGINES_DIR]:
            for f in sorted(d.glob("*.py")):
                zf.write(str(f), str(f.relative_to(Path("."))))
    zs = ZIP_NAME.stat().st_size
    print("      ZIP: %.1f KB" % (zs / 1024))

    return eng_count, pub_count, priv_count, zs

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()

    for d in [PUBLIC_DIR, PRIVATE_DIR, ENGINES_DIR]:
        safe_rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    store = Store()

    print("=" * 60)
    print("  qBittorrent Plugin Auto-Updater (Enhanced)")
    print("  Token: %s" % ("YES" if GITHUB_TOKEN else "NO"))
    print("  Threads: %d" % THREADS)
    print("  Known repos: %d" % len(KNOWN_REPOS))
    print("=" * 60)

    phase_wiki(store)
    phase_github(store)
    phase_other(store)
    total, pub, priv, zs = phase_output(store)

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print("  DONE in %ds" % int(elapsed))
    print("  %d plugins | %d public | %d private" % (total, pub, priv))
    print("  ZIP: %.1f KB" % (zs / 1024))
    print("=" * 60)

if __name__ == "__main__":
    main()
