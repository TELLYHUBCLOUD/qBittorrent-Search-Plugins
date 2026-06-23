import os, re, json, hashlib, shutil, zipfile, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import urllib.request
import urllib.error
from html.parser import HTMLParser

# ==================== CONFIG ====================
OUTPUT_DIR = Path("qBittorrent-Search-Plugins-Repo")
PUBLIC_DIR = OUTPUT_DIR / "public_sites"
PRIVATE_DIR = OUTPUT_DIR / "private_sites"
ENGINES_DIR = OUTPUT_DIR / "engines"
ZIP_NAME = OUTPUT_DIR / "qBittorrent-Search-Plugins-Complete.zip"
FAILED_LOG = OUTPUT_DIR / "failed_downloads.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

SKIP_FILES = {
    "__init__.py", "helpers.py", "helpers_106.py", "helpers_122.py",
    "novaprinter.py", "novaprinter_107.py", "novaprinter_125.py",
    "nova2.py", "nova2dl.py", "sgmllib3.py", "socks.py",
    "validate_plugin.py", "test_bitsearch.py", "test_bitsearch_real.py",
    "final_test.py", "main.py", "ripper.py", "sqliteplugin.py",
    "telegram_torrent_bot.py",
}

# Clean slate
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
        print(f"Warning: Could not remove directory {path}: {e}")

safe_rmtree(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
for d in [PUBLIC_DIR, PRIVATE_DIR, ENGINES_DIR]:
    d.mkdir(exist_ok=True)

# ==================== UTILS ====================
def fetch_url(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        return None

def fetch_text(url, timeout=15):
    data = fetch_url(url, timeout)
    return data.decode("utf-8", errors="ignore") if data else None

def make_raw_url(url):
    if not url:
        return ""
    url = url.split("#")[0]
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    if "gitlab.com" in url and "/blob/" in url:
        url = url.replace("/blob/", "/raw/")
    return url

def get_safe_filename(name, url):
    url_fn = os.path.basename(url.split("?")[0].split("#")[0])
    if url_fn.endswith(".py") and url_fn.lower() not in ("search.py", "engine.py", "plugin.py", "init.py", "__init__.py"):
        return url_fn
    sanitized = re.sub(r"[^a-zA-Z0-9]", "", name.replace(" ", "_")).lower()
    return f"{sanitized}.py" if sanitized else "unknown.py"

def download_file(url, filepath):
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
                if b"<!DOCTYPE html>" in content[:200] or b"<html" in content[:200]:
                    return False, "HTML"
                Path(filepath).parent.mkdir(parents=True, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(content)
                return True, len(content)
        except Exception as e:
            if attempt == 1:
                return False, str(e)[:50]
            time.sleep(0.5)
    return False, "fail"

def dedup_remove_framework(directory):
    seen = {}
    for f in directory.glob("*.py"):
        if f.name in SKIP_FILES:
            f.unlink()
            continue
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        if h in seen:
            f.unlink()
        else:
            seen[h] = f
    return seen

# ==================== WIKI PARSER ====================
class WikiParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = None
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.cell_data = {"text": "", "links": []}
        self.current_row = []
        self.public = []
        self.private = []
        
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "name" in attrs:
            n = attrs["name"].lower()
            if "public" in n:
                self.section = "public"
            elif "private" in n:
                self.section = "private"
        if tag in ("h1", "h2", "h3", "h4") and "id" in attrs:
            hid = attrs["id"].lower()
            if "public" in hid:
                self.section = "public"
            elif "private" in hid:
                self.section = "private"
        if tag == "table" and self.section:
            self.in_table = True
        if self.in_table:
            if tag == "tr":
                self.in_row = True
                self.current_row = []
            elif tag in ("td", "th"):
                self.in_cell = True
                self.cell_data = {"text": "", "links": []}
            elif tag == "a" and self.in_cell and "href" in attrs:
                self.cell_data["links"].append(attrs["href"])
                
    def handle_endtag(self, tag):
        if self.in_table:
            if tag == "table":
                self.in_table = False
                self.section = None
            elif tag == "tr":
                self.in_row = False
                if self.current_row and len(self.current_row) >= 5:
                    if self.section == "public":
                        self.public.append(self.current_row)
                    elif self.section == "private":
                        self.private.append(self.current_row)
            elif tag in ("td", "th"):
                self.in_cell = False
                self.cell_data["text"] = self.cell_data["text"].strip()
                self.current_row.append(self.cell_data)
                
    def handle_data(self, data):
        if self.in_cell:
            self.cell_data["text"] += data

# ==================== PROCESS WIKI ====================
def process_rows(rows, target_dir, is_private=False):
    processed = []
    failures = []
    for i, row in enumerate(rows, 1):
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
        download_url = make_raw_url(raw_url)
        
        if not download_url:
            failures.append({"engine": engine_name, "reason": "No URL"})
            continue
        
        filename = get_safe_filename(engine_name, download_url)
        filepath = target_dir / filename
        
        success, info = download_file(download_url, str(filepath))
        if success:
            processed.append({
                "engine_name": engine_name, "engine_site": engine_site,
                "author_name": author_name, "repo_link": repo_link,
                "version": version, "last_update": " ".join(last_update.split()),
                "filename": filename, "download_url": download_url,
                "comments": " ".join(comments.split()), "file_size": info,
                "is_private": is_private
            })
        else:
            failures.append({"engine": engine_name, "url": download_url, "reason": info})
    return processed, failures

# ==================== GITHUB API ====================
def scrape_github_repos():
    print("[+] GitHub API search for extra repos...")
    repos = set()
    for q in ["qBittorrent+search+plugin+language:python", "qbittorrent+plugin+language:python"]:
        api = f"https://api.github.com/search/repositories?q={q}&sort=updated&per_page=100"
        data = fetch_text(api, timeout=15)
        if data:
            try:
                j = json.loads(data)
                for item in j.get("items", []):
                    repos.add(item["html_url"])
            except:
                pass
    print(f"    {len(repos)} repos found")
    added = 0
    for repo in repos:
        parts = repo.replace("https://github.com/", "").strip("/").split("/")
        if len(parts) < 2:
            continue
        owner, repo_name = parts[0], parts[1]
        for sub in ["", "engines", "plugins"]:
            api = f"https://api.github.com/repos/{owner}/{repo_name}/contents/{sub}"
            data = fetch_text(api, timeout=8)
            if not data:
                continue
            try:
                j = json.loads(data)
                if not isinstance(j, list):
                    continue
                for item in j:
                    if item.get("type") == "file" and item["name"].endswith(".py"):
                        dl = item.get("download_url")
                        if dl and item["name"] not in SKIP_FILES:
                            success, info = download_file(dl, str(ENGINES_DIR / item["name"]))
                            if success:
                                added += 1
            except:
                pass
    print(f"    Added {added} from GitHub repos")

# ==================== COLLECTIONS ====================
def scrape_collections():
    print("[+] Scraping known collection repos...")
    urls = [
        "https://github.com/alessandro-ooo/one-click-qbittorrent-searchplugins",
        "https://github.com/nklido/qBittorrent_search_engines",
        "https://github.com/darktohka/qbittorrent-plugins",
        "https://github.com/HazukiShiro/qBittorrent-Search-Plugins",
    ]
    added = 0
    for url in urls:
        html = fetch_text(url, timeout=10)
        if not html:
            continue
        for match in re.findall(r'href="([^"]+\.py)"', html):
            full = urljoin("https://github.com", match) if match.startswith("/") else match
            if "/blob/" in full:
                full = full.replace("/blob/", "/raw/")
            name = os.path.basename(full)
            if name not in SKIP_FILES:
                success, info = download_file(full, str(ENGINES_DIR / name))
                if success:
                    added += 1
    print(f"    Added {added} from collections")

# ==================== MAIN ====================
print("=" * 60)
print("qBittorrent Search Plugins Aggregator (Full Edition)")
print("=" * 60)

# 1. Fetch & parse wiki
print("[+] Fetching official wiki...")
wiki_html = fetch_text("https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins", timeout=15)
if not wiki_html:
    print("    FAILED")
    wiki_html = ""

parser = WikiParser()
parser.feed(wiki_html)
print(f"    Public rows: {len(parser.public)} | Private rows: {len(parser.private)}")

# 2. Download wiki plugins
print("[+] Downloading public site plugins...")
public_ok, public_fail = process_rows(parser.public, PUBLIC_DIR, is_private=False)
print(f"    OK: {len(public_ok)} | Fail: {len(public_fail)}")

print("[+] Downloading private site plugins...")
private_ok, private_fail = process_rows(parser.private, PRIVATE_DIR, is_private=True)
print(f"    OK: {len(private_ok)} | Fail: {len(private_fail)}")

# 3. Scrape GitHub & collections
scrape_github_repos()
scrape_collections()

# 4. Deduplicate each folder
print("[+] Deduplicating...")
dedup_remove_framework(PUBLIC_DIR)
dedup_remove_framework(PRIVATE_DIR)
dedup_remove_framework(ENGINES_DIR)

# 5. Merge engines/ into public/private based on classification
print("[+] Classifying engine extras...")
for f in ENGINES_DIR.glob("*.py"):
    content = f.read_text(encoding="utf-8", errors="ignore")
    low = content.lower()
    is_private = any(x in low for x in ["passkey", "api_key", "apikey", "auth_token", "bearer", "username", "password", "private"])
    if is_private:
        dest = PRIVATE_DIR / f.name
        if not dest.exists():
            shutil.copy2(f, dest)
    else:
        dest = PUBLIC_DIR / f.name
        if not dest.exists():
            shutil.copy2(f, dest)

# Also copy all public/private to engines/
for f in list(PUBLIC_DIR.glob("*.py")) + list(PRIVATE_DIR.glob("*.py")):
    dest = ENGINES_DIR / f.name
    if not dest.exists():
        shutil.copy2(f, dest)

# 6. Final counts
pub_count = len(list(PUBLIC_DIR.glob("*.py")))
priv_count = len(list(PRIVATE_DIR.glob("*.py")))
engine_count = len(list(ENGINES_DIR.glob("*.py")))
print(f"    Final: {engine_count} total | {pub_count} public | {priv_count} private")

# 7. Build README with table format
def fmt_table_rows(info_list, start_idx=1):
    rows = []
    for idx, item in enumerate(info_list, start_idx):
        engine = f"[{item['engine_name']}]({item['engine_site']})" if item['engine_site'] else item['engine_name']
        author = f"[{item['author_name']}]({item['repo_link']})" if item['repo_link'] else item['author_name']
        dl = f"[`{item['filename']}`]({item['download_url']})"
        rows.append(f"| {idx} | {engine} | {author} | {item['version']} | {item['last_update']} | {dl} | {item['comments']} |")
    return "\n".join(rows)

repo_env = os.environ.get("GITHUB_REPOSITORY", "HazukiShiro/qBittorrent-Search-Plugins")
readme = f"""# qBittorrent-Search-Plugins - Complete Collection

[![Auto-Update](https://github.com/{repo_env}/actions/workflows/auto-update.yml/badge.svg)](https://github.com/{repo_env}/actions/workflows/auto-update.yml)

This repository aggregates all unofficial qBittorrent search engine plugins from the [Official Unofficial Wiki](https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins) and GitHub community repositories.

> **DISCLAIMER**: This is a collection of **unofficial** plugins. I am NOT the author. All credits go to original creators. Use at your own risk.

## Stats

| Metric | Count |
|--------|-------|
| Total plugins | {engine_count} |
| Public sites | {pub_count} |
| Private sites | {priv_count} |

## Download

- **[qBittorrent-Search-Plugins-Complete.zip](https://raw.githubusercontent.com/{repo_env}/main/qBittorrent-Search-Plugins-Complete.zip)**
- `public_sites/` — Public torrent site plugins
- `private_sites/` — Private tracker plugins (may require credentials)
- `engines/` — All plugins combined

## Install

1. Open **qBittorrent** → **Search** tab.
2. Click **Search plugins...** → **Install a new one**.
3. Select **Local file** and pick `.py` files from the folder.

## Public Sites ({pub_count})

| # | Search Engine | Author | Version | Updated | Download | Comments |
|---|---------------|--------|---------|---------|----------|----------|
{fmt_table_rows(public_ok)}

## Private Sites ({priv_count})

| # | Search Engine | Author | Version | Updated | Download | Comments |
|---|---------------|--------|---------|---------|----------|----------|
{fmt_table_rows(private_ok, len(public_ok)+1)}

## Testing

```bash
# Using official qBittorrent search-plugins test framework
python3 nova2.py <plugin_name> all "ubuntu"
```

## License

All plugins are property of their respective authors. This aggregator is provided as-is for educational convenience.
"""
(OUTPUT_DIR / "README.md").write_text(readme, encoding="utf-8")
(OUTPUT_DIR / "requirements.txt").write_text("requests\nbeautifulsoup4\n", encoding="utf-8")

# JSON metadata
all_meta = public_ok + private_ok
meta = {
    "total": engine_count,
    "public": pub_count,
    "private": priv_count,
    "plugins": all_meta,
    "failures": {"public": public_fail, "private": private_fail}
}
(OUTPUT_DIR / "plugins.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
(OUTPUT_DIR / "failed_downloads.json").write_text(json.dumps(meta["failures"], indent=2), encoding="utf-8")

# 8. ZIP
print("[+] Building ZIP...")
with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(OUTPUT_DIR):
        for f in files:
            fp = Path(root) / f
            if fp == ZIP_NAME:
                continue
            zf.write(fp, str(fp.relative_to(OUTPUT_DIR)))

zip_size = ZIP_NAME.stat().st_size
print(f"    ZIP: {zip_size/1024:.1f} KB")

print("\n" + "=" * 60)
print(f"COMPLETE! {engine_count} plugins | {pub_count} public | {priv_count} private")
print(f"ZIP: {ZIP_NAME} ({zip_size/1024:.1f} KB)")
print("=" * 60)
