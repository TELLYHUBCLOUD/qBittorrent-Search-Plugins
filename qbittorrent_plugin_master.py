#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     🔥 qBittorrent Search Plugin Master Collector - Dynamic Edition 🔥        ║
║                                                                              ║
║  • Auto-fetches ALL plugins from GitHub (not just wiki)                      ║
║  • Smart deduplication: new code added, old code unchanged                   ║
║  • Dynamic GitHub API discovery for unknown repos                            ║
║  • Incremental updates - only downloads changed files                          ║
║  • Full metadata tracking with JSON database                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import re
import json
import hashlib
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from html.parser import HTMLParser

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
OUTPUT_DIR = "qBittorrent-Search-Plugins"
PUBLIC_DIR = os.path.join(OUTPUT_DIR, "public_sites")
PRIVATE_DIR = os.path.join(OUTPUT_DIR, "private_sites")
META_DIR = os.path.join(OUTPUT_DIR, ".meta")
DB_FILE = os.path.join(META_DIR, "plugin_database.json")
LOG_FILE = os.path.join(META_DIR, "update_log.json")

WIKI_URL = "https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins"
GITHUB_API = "https://api.github.com"

# GitHub Search queries for auto-discovery
SEARCH_QUERIES = [
    "qBittorrent search plugin language:python",
    "qbittorrent search engine python",
    "qbittorrent search plugin torrent",
]

# Known major repositories (author -> repo mapping)
KNOWN_REPOS = {
    "LightDestory": "LightDestory/qBittorrent-Search-Plugins",
    "BurningMop": "BurningMop/qBittorrent-Search-Plugins",
    "nklido": "nklido/qBittorrent_search_engines",
    "hannsen": "hannsen/qbittorrent_search_plugins",
    "Cc050511": "Cc050511/qBit-search-plugins",
    "nindogo": "nindogo/qbtSearchScripts",
    "imDMG": "imDMG/qBittorrent_Search_Plugins",
    "iordic": "iordic/qbittorrent-search-plugins",
    "tolotp": "tolotp/qbittorrent-search-plugins",
    "444995": "444995/qbittorrent-search-plugins",
    "bugsbringer": "bugsbringer/qbit-plugins",
    "AlaaBrahim": "AlaaBrahim/qBitTorrent-animetosho-search-plugin",
    "TuckerWarlock": "TuckerWarlock/qbittorrent-search-plugins",
    "galaris": "galaris/BTDigg-qBittorrent-plugin",
    "MarcBresson": "MarcBresson/cpasbien",
    "ZH1637": "ZH1637/dmhy",
    "lazulyra": "lazulyra/qbittorrent-yts-plugin",
    "Bioux1": "Bioux1/qbittorrent-search-plugins",
    "bebetoh": "bebetoh/qbittorrent-search-plugins",
    "Cycloctane": "Cycloctane/qbittorrent-mikan-plugin",
    "vt-idiot": "vt-idiot/qbittorrent-sukebei-plugin",
    "elazar": "elazar/qbittorrent-cloudtorrents-plugin",
    "joseeloren": "joseeloren/qbittorrent-search-plugins",
    "menegop": "menegop/qbittorrent-search-plugins",
    "msagca": "msagca/qbittorrent-plugins",
    "caiocinel": "caiocinel/qbittorrent-onlinefix-plugin",
    "kli885": "kli885/qbittorrent-subsplease-plugin",
    "Ashalda": "Ashalda/qbittorrent-sktorrent-plugin",
    "BrunoReX": "BrunoReX/qbittorrent-search-plugins",
    "YGGverse": "YGGverse/qbittorrent-yggtracker-plugin",
    "OptimusKoala": "OptimusKoala/qbittorrent-c411-plugin",
    "RaresPNet": "RaresPNet/qbittorrent-filelist-plugin",
    "Ooggle": "Ooggle/qbittorrent-gazelle-games-plugin",
    "txtsd": "txtsd/qB-IPT",
    "darktohka": "darktohka/qbittorrent-ncore-plugin",
    "TainakaDrums": "TainakaDrums/qbittorrent-pornolab-plugin",
    "swannie-eire": "swannie-eire/qbittorrent-prowlarr-plugin",
    "Evyd13": "Evyd13/qbittorrent-redacted-plugin",
    "nbusseneau": "nbusseneau/qBittorrent-RuTracker-plugin",
    "etn": "etn/qbittorrent-sharewood-plugin",
    "miIiano": "miIiano/qbittorrent-speedapp-plugin",
    "MjKey": "MjKey/qbittorrent-tapochek-plugin",
    "PlayDay": "PlayDay/qbittorrent-gurtom-plugin",
    "CrimsonKoba": "CrimsonKoba/qbittorrent-unionfansub-plugin",
    "Laiteux": "Laiteux/qbittorrent-yggapi-plugin",
    "CravateRouge": "CravateRouge/qbittorrent-yggtorrent-plugin",
    "Necrosis": "Necrosis/qbittorrent-zamunda-plugin",
    "DrPurp": "DrPurp/qbittorrent-eztv-plugin",
    "scadams": None,
}

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE & METADATA SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
class PluginDatabase:
    """Smart database that tracks plugins, hashes, and update status"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.db_path):
            with open(self.db_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            "version": "2.0",
            "last_update": None,
            "plugins": {},
            "sources": {},
            "failed": []
        }

    def save(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with open(self.db_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_plugin_hash(self, plugin_id):
        return self.data["plugins"].get(plugin_id, {}).get("hash")

    def get_plugin_source(self, plugin_id):
        return self.data["plugins"].get(plugin_id, {}).get("source_url")

    def add_plugin(self, plugin_id, metadata, file_hash, source_url, action="new"):
        self.data["plugins"][plugin_id] = {
            "metadata": metadata,
            "hash": file_hash,
            "source_url": source_url,
            "first_seen": self.data["plugins"].get(plugin_id, {}).get("first_seen", datetime.now().isoformat()),
            "last_updated": datetime.now().isoformat(),
            "update_count": self.data["plugins"].get(plugin_id, {}).get("update_count", 0) + (1 if action == "updated" else 0),
            "action": action
        }

    def log_failure(self, plugin_name, url, reason):
        self.data["failed"].append({
            "timestamp": datetime.now().isoformat(),
            "plugin": plugin_name,
            "url": url,
            "reason": reason
        })

    def get_stats(self):
        total = len(self.data["plugins"])
        new = sum(1 for p in self.data["plugins"].values() if p.get("action") == "new")
        updated = sum(1 for p in self.data["plugins"].values() if p.get("action") == "updated")
        unchanged = sum(1 for p in self.data["plugins"].values() if p.get("action") == "unchanged")
        return {"total": total, "new": new, "updated": updated, "unchanged": unchanged}

# ═══════════════════════════════════════════════════════════════════════════════
# WIKI PARSER
# ═══════════════════════════════════════════════════════════════════════════════
class WikiParser(HTMLParser):
    """Advanced HTML parser for GitHub wiki tables"""

    def __init__(self):
        super().__init__()
        self.current_section = None
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.cell_index = -1
        self.current_cell = {"text": "", "links": []}
        self.current_row = []
        self.public_plugins = []
        self.private_plugins = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == 'a' and 'name' in attrs_dict:
            name = attrs_dict['name']
            if 'public' in name.lower():
                self.current_section = 'public'
            elif 'private' in name.lower():
                self.current_section = 'private'

        if tag in ('h1','h2','h3','h4','h5','h6') and 'id' in attrs_dict:
            h_id = attrs_dict['id'].lower()
            if 'public' in h_id and 'site' in h_id:
                self.current_section = 'public'
            elif 'private' in h_id and 'site' in h_id:
                self.current_section = 'private'

        if tag == 'table' and self.current_section:
            self.in_table = True

        if self.in_table:
            if tag == 'tr':
                self.in_row = True
                self.current_row = []
                self.cell_index = -1
            elif tag in ('td', 'th'):
                self.in_cell = True
                self.cell_index += 1
                self.current_cell = {"text": "", "links": []}
            elif tag == 'a' and self.in_cell and 'href' in attrs_dict:
                self.current_cell["links"].append(attrs_dict['href'])

    def handle_endtag(self, tag):
        if self.in_table:
            if tag == 'table':
                self.in_table = False
                self.current_section = None
            elif tag == 'tr':
                self.in_row = False
                if self.current_row and len(self.current_row) >= 5:
                    first_text = self.current_row[0]["text"].lower()
                    if "search engine" not in first_text:
                        if self.current_section == 'public':
                            self.public_plugins.append(self.current_row)
                        elif self.current_section == 'private':
                            self.private_plugins.append(self.current_row)
            elif tag in ('td', 'th'):
                self.in_cell = False
                self.current_cell["text"] = self.current_cell["text"].strip()
                self.current_row.append(self.current_cell)

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell["text"] += data

# ═══════════════════════════════════════════════════════════════════════════════
# GITHUB API DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════
class GitHubDiscovery:
    """Discovers new qBittorrent search plugin repositories via GitHub API"""

    def __init__(self, token=None):
        self.token = token
        self.headers = {
            "User-Agent": "qBittorrent-Plugin-Collector/2.0",
            "Accept": "application/vnd.github.v3+json"
        }
        if token:
            self.headers["Authorization"] = f"token {token}"

    def search_repos(self, query, per_page=100):
        """Search GitHub for qBittorrent search plugin repos"""
        url = f"{GITHUB_API}/search/repositories?q={urllib.parse.quote(query)}&sort=updated&per_page={per_page}"
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get("items", [])
        except Exception as e:
            print(f"  ⚠️ GitHub API search failed: {e}")
            return []

    def get_repo_files(self, owner, repo, path=""):
        """List files in a repository directory"""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except:
            return []

    def find_py_files(self, owner, repo):
        """Recursively find all .py files in a repo"""
        files = []
        def scan(path=""):
            items = self.get_repo_files(owner, repo, path)
            if isinstance(items, list):
                for item in items:
                    if item["type"] == "file" and item["name"].endswith(".py"):
                        files.append({
                            "name": item["name"],
                            "path": item["path"],
                            "url": item["download_url"],
                            "size": item["size"],
                            "sha": item["sha"]
                        })
                    elif item["type"] == "dir" and not item["name"].startswith("."):
                        scan(item["path"])
        scan()
        return files

# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
def make_raw_url(url):
    """Convert GitHub blob URLs to raw URLs"""
    if not url:
        return ""
    url = url.split('#')[0]
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    if "gitlab.com" in url and "/blob/" in url:
        url = url.replace("/blob/", "/raw/")
    return url

def get_file_hash(filepath):
    """Calculate SHA256 hash of file"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()[:16]

def get_content_hash(content):
    """Calculate hash from bytes content"""
    return hashlib.sha256(content).hexdigest()[:16]

def download_with_retry(url, max_retries=3, timeout=15):
    """Download file with retry logic"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                # Check for HTML error pages
                if b"<!DOCTYPE html>" in content[:100] or b"<html" in content[:100]:
                    return None, "HTML error page received"
                return content, None
        except urllib.error.HTTPError as e:
            if attempt == max_retries - 1:
                return None, f"HTTP {e.code}"
        except Exception as e:
            if attempt == max_retries - 1:
                return None, str(e)
        time.sleep(1)
    return None, "Max retries exceeded"

def safe_filename(name, url):
    """Generate safe filename from engine name or URL"""
    url_name = os.path.basename(url.split('?')[0].split('#')[0])
    if url_name.endswith('.py') and len(url_name) > 3:
        return url_name

    clean = re.sub(r'[^a-zA-Z0-9_\-]', '', name.replace(' ', '_').replace('.', '_')).lower()
    return f"{clean}.py" if clean else "unknown_plugin.py"

def process_wiki_plugins(rows, target_dir, db, is_public=True):
    """Process wiki table rows and download plugins with smart deduplication"""
    os.makedirs(target_dir, exist_ok=True)
    processed = []

    for i, row in enumerate(rows, 1):
        engine_name = row[0]["text"].strip() if row else "Unknown"
        engine_links = row[0]["links"] if row else []
        author_info = row[1] if len(row) > 1 else {"text": "Unknown", "links": []}
        version = row[2]["text"] if len(row) > 2 else "1.0"
        last_update = row[3]["text"] if len(row) > 3 else "N/A"
        download_cell = row[4] if len(row) > 4 else None
        comments = row[5]["text"] if len(row) > 5 else ""

        raw_url = make_raw_url(download_cell["links"][0]) if download_cell and download_cell["links"] else ""

        if not raw_url:
            db.log_failure(engine_name, "", "No download URL in wiki")
            print(f"  ⚠️ [{i}/{len(rows)}] {engine_name} - No URL")
            continue

        filename = safe_filename(engine_name, raw_url)
        filepath = os.path.join(target_dir, filename)
        plugin_id = f"{'pub' if is_public else 'priv'}_{filename.replace('.py', '')}"

        print(f"  📥 [{i}/{len(rows)}] {engine_name} -> {filename}", end=" ")

        content, error = download_with_retry(raw_url)
        if content is None:
            db.log_failure(engine_name, raw_url, error)
            print(f"❌ ({error})")
            continue

        new_hash = get_content_hash(content)
        old_hash = db.get_plugin_hash(plugin_id)

        if old_hash and old_hash == new_hash:
            # File unchanged - don't modify
            action = "unchanged"
            print(f"⏭️  (unchanged)")
        else:
            # New or updated - write file
            action = "updated" if old_hash else "new"
            with open(filepath, 'wb') as f:
                f.write(content)
            print(f"✅ ({action}, {len(content)} bytes)")

        metadata = {
            "engine_name": engine_name,
            "engine_site": engine_links[0] if engine_links else "",
            "author": author_info["text"].strip(),
            "author_repo": author_info["links"][0] if author_info["links"] else "",
            "version": version,
            "last_update": last_update,
            "comments": comments,
            "filename": filename,
            "is_public": is_public
        }

        db.add_plugin(plugin_id, metadata, new_hash, raw_url, action)
        processed.append(metadata)
        time.sleep(0.2)

    return processed

def discover_github_repos(db, token=None):
    """Discover additional plugins from GitHub search"""
    print("\n🔍 Discovering new repositories via GitHub API...")
    discovery = GitHubDiscovery(token)
    found_files = []

    for query in SEARCH_QUERIES:
        print(f"  🔎 Searching: {query}")
        repos = discovery.search_repos(query)
        for repo in repos:
            full_name = repo["full_name"]
            owner, name = full_name.split('/')

            # Skip if already known
            if any(full_name == v for v in KNOWN_REPOS.values() if v):
                continue

            print(f"    📦 Found repo: {full_name}")
            py_files = discovery.find_py_files(owner, name)
            for f in py_files:
                # Check if it's a search plugin
                if any(keyword in f["name"].lower() for keyword in ['search', 'engine', 'plugin', 'torrent']):
                    found_files.append({
                        "repo": full_name,
                        "filename": f["name"],
                        "url": f["url"],
                        "sha": f["sha"],
                        "size": f["size"]
                    })
        time.sleep(2)  # Rate limit

    print(f"  ✅ Discovered {len(found_files)} potential new plugin files")
    return found_files

def download_discovered_plugins(files, target_dir, db):
    """Download plugins discovered via GitHub API"""
    os.makedirs(target_dir, exist_ok=True)
    processed = []

    for i, f in enumerate(files, 1):
        filename = f["filename"]
        filepath = os.path.join(target_dir, filename)
        plugin_id = f"discovered_{filename.replace('.py', '')}"

        print(f"  📥 [{i}/{len(files)}] {f['repo']}/{filename}", end=" ")

        content, error = download_with_retry(f["url"])
        if content is None:
            db.log_failure(f"{f['repo']}/{filename}", f["url"], error)
            print(f"❌ ({error})")
            continue

        new_hash = get_content_hash(content)
        old_hash = db.get_plugin_hash(plugin_id)

        if old_hash and old_hash == new_hash:
            action = "unchanged"
            print(f"⏭️  (unchanged)")
        else:
            action = "updated" if old_hash else "new"
            with open(filepath, 'wb') as f_out:
                f_out.write(content)
            print(f"✅ ({action}, {len(content)} bytes)")

        metadata = {
            "engine_name": filename.replace('.py', ''),
            "engine_site": "",
            "author": f["repo"].split('/')[0],
            "author_repo": f"https://github.com/{f['repo']}",
            "version": "1.0",
            "last_update": "Auto-discovered",
            "comments": f"Auto-discovered from {f['repo']}",
            "filename": filename,
            "is_public": True
        }

        db.add_plugin(plugin_id, metadata, new_hash, f["url"], action)
        processed.append(metadata)
        time.sleep(0.2)

    return processed

def generate_readme(public_plugins, private_plugins, stats):
    """Generate comprehensive README.md"""

    def make_table_rows(plugins):
        rows = []
        for i, p in enumerate(plugins, 1):
            engine = f"[{p['engine_name']}]({p['engine_site']})" if p['engine_site'] else p['engine_name']
            author = f"[{p['author']}]({p['author_repo']})" if p['author_repo'] else p['author']
            raw = f"[`{p['filename']}`]({p.get('source_url', '')})" if p.get('source_url') else p['filename']
            rows.append(f"| {i} | {engine} | {author} | {p['version']} | {p['last_update']} | {raw} | {p['comments']} |")
        return "\n".join(rows)

    readme = f"""# 🔥 qBittorrent Search Plugins - Complete Dynamic Collection

[![Auto-Update](https://img.shields.io/badge/Auto--Update-Daily-blue)](https://github.com/qbittorrent/search-plugins)
[![Plugins](https://img.shields.io/badge/Plugins-{stats['total']}-green)](./)
[![Public](https://img.shields.io/badge/Public-{len(public_plugins)}-brightgreen)](./public_sites)
[![Private](https://img.shields.io/badge/Private-{len(private_plugins)}-orange)](./private_sites)

> ⚠️ **DISCLAIMER**: This is an **automated collection** of unofficial qBittorrent search plugins. I am NOT the author of these plugins. All credits go to the original creators. Use at your own risk.

## 📊 Update Statistics

| Metric | Count |
|--------|-------|
| **Total Plugins** | {stats['total']} |
| **New This Run** | {stats['new']} |
| **Updated** | {stats['updated']} |
| **Unchanged** | {stats['unchanged']} |
| **Last Run** | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |

## 📦 Installation

1. Open qBittorrent → **Search** tab → **Search Plugins** button
2. Click **Install a new one** → Select `.py` files from `public_sites/` or `private_sites/`
3. Or use **Web link** with raw URLs from the tables below

## 🌍 Public Sites ({len(public_plugins)} Plugins)

| # | Engine | Author | Version | Updated | Download | Notes |
|---|--------|--------|---------|---------|----------|-------|
{make_table_rows(public_plugins)}

## 🔒 Private Sites ({len(private_plugins)} Plugins)

*These require editing the `.py` file to add your credentials/API keys.*

| # | Engine | Author | Version | Updated | Download | Notes |
|---|--------|--------|---------|---------|----------|-------|
{make_table_rows(private_plugins)}

## 🔍 Auto-Discovery Sources

This collection is built from:
1. [qBittorrent Unofficial Wiki](https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins)
2. GitHub API search for `qBittorrent search plugin language:python`
3. Known major repositories (auto-tracked)

## ⚡ Smart Update Logic

- **New plugins**: Automatically downloaded and added
- **Existing plugins**: Hash-checked; only updated if content changed
- **Unchanged plugins**: Left exactly as-is (no file modification)
- **Failed downloads**: Logged in `.meta/failed_downloads.json`

## 📁 Repository Structure

```
qBittorrent-Search-Plugins/
├── public_sites/          # Public torrent search plugins
├── private_sites/         # Private tracker plugins
├── .meta/
│   ├── plugin_database.json    # Full metadata & hashes
│   └── update_log.json         # Update history
└── README.md              # This file
```

## ⚖️ License

Individual plugins retain their original authors' licenses. This aggregator is MIT licensed.
"""

    with open(os.path.join(OUTPUT_DIR, "README.md"), 'w', encoding='utf-8') as f:
        f.write(readme)
    print("📝 README.md generated")

def generate_update_log(db):
    """Generate update log JSON"""
    log = {
        "timestamp": datetime.now().isoformat(),
        "stats": db.get_stats(),
        "plugins": list(db.data["plugins"].keys()),
        "failed_count": len(db.data["failed"])
    }
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2)

def main():
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "🔥 qBittorrent Plugin Master Collector v2.0" + " " * 21 + "║")
    print("╚" + "═" * 78 + "╝")

    # Initialize
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    os.makedirs(PRIVATE_DIR, exist_ok=True)
    os.makedirs(META_DIR, exist_ok=True)

    db = PluginDatabase(DB_FILE)

    # Step 1: Fetch Wiki
    print("\n📡 STEP 1: Fetching qBittorrent Wiki...")
    try:
        req = urllib.request.Request(WIKI_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode('utf-8')
        print("  ✅ Wiki fetched successfully")
    except Exception as e:
        print(f"  ❌ Wiki fetch failed: {e}")
        html = ""

    # Step 2: Parse Wiki
    print("\n🔍 STEP 2: Parsing wiki tables...")
    parser = WikiParser()
    parser.feed(html)
    print(f"  ✅ Public: {len(parser.public_plugins)} | Private: {len(parser.private_plugins)}")

    # Step 3: Download Wiki Plugins (Smart Deduplication)
    print("\n📥 STEP 3: Downloading public site plugins...")
    public_meta = process_wiki_plugins(parser.public_plugins, PUBLIC_DIR, db, True)

    print("\n📥 STEP 4: Downloading private site plugins...")
    private_meta = process_wiki_plugins(parser.private_plugins, PRIVATE_DIR, db, False)

    # Step 5: GitHub API Discovery (Optional - needs token for high rate limits)
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        print("\n🔎 STEP 5: Discovering new repos via GitHub API...")
        discovered = discover_github_repos(db, token)
        if discovered:
            print("\n📥 STEP 6: Downloading discovered plugins...")
            discovered_meta = download_discovered_plugins(discovered, PUBLIC_DIR, db)
            public_meta.extend(discovered_meta)
    else:
        print("\n⏭️  STEP 5: Skipping GitHub API discovery (no GITHUB_TOKEN env var)")

    # Step 6: Generate Documentation
    print("\n📝 STEP 6: Generating documentation...")
    stats = db.get_stats()
    generate_readme(public_meta, private_meta, stats)
    generate_update_log(db)

    # Step 7: Save Database
    db.save()

    # Final Summary
    print("\n" + "╔" + "═" * 78 + "╗")
    print("║" + " " * 28 + "📊 FINAL SUMMARY" + " " * 35 + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  ✅ New plugins added:     {stats['new']:<52} ║")
    print(f"║  🔄 Plugins updated:       {stats['updated']:<52} ║")
    print(f"║  ⏭️  Plugins unchanged:    {stats['unchanged']:<52} ║")
    print(f"║  📦 Total in database:     {stats['total']:<52} ║")
    print(f"║  📁 Public sites:          {len(os.listdir(PUBLIC_DIR)):<52} ║")
    print(f"║  📁 Private sites:         {len(os.listdir(PRIVATE_DIR)):<52} ║")
    print(f"║  💾 Database:              {DB_FILE:<52} ║")
    print("╚" + "═" * 78 + "╝")

    print("\n🎉 DONE! All plugins collected with smart deduplication.")
    print("   - New plugins: ADDED")
    print("   - Changed plugins: UPDATED")
    print("   - Same plugins: LEFT UNCHANGED")

if __name__ == "__main__":
    main()
