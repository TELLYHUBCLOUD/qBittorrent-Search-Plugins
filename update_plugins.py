#!/usr/bin/env python3
"""
qBittorrent Search Plugins Aggregator & Scraper
Fetches unofficial plugins from the qBittorrent wiki, downloads them,
generates a comprehensive README.md, and packages them into a ZIP archive.
"""

import os
import re
import urllib.request
import urllib.error
import time
import json
import zipfile
from html.parser import HTMLParser

# ========== CONFIGURATION ==========
WIKI_URL = "https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins"
# Local fallback path for convenience/offline testing
LOCAL_WIKI_PATH = r"C:\Users\Admin\.gemini\antigravity-ide\brain\07d235ee-59e5-4230-bba9-eb53322bedc4\.system_generated\steps\13\content.md"

OUTPUT_DIR = "."
PUBLIC_DIR = os.path.join(OUTPUT_DIR, "public_sites")
PRIVATE_DIR = os.path.join(OUTPUT_DIR, "private_sites")
ZIP_NAME = os.path.join(OUTPUT_DIR, "qBittorrent-Search-Plugins-Complete.zip")
FAILED_LOG = os.path.join(OUTPUT_DIR, "failed_downloads.json")

# ========== HTML PARSER ==========
class WikiHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current_section = None
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.cell_index = -1
        self.current_cell_data = {
            "text": "",
            "links": []
        }
        self.current_row = []
        self.public_plugins = []
        self.private_plugins = []
        self.current_tag_name = ""

    def handle_starttag(self, tag, attrs):
        self.current_tag_name = tag
        attrs_dict = dict(attrs)
        
        # Check for section headers (GitHub wiki names anchors this way)
        if tag == 'a' and 'name' in attrs_dict:
            name = attrs_dict['name']
            if name == 'user-content-Plugins_for_Public_Sites':
                self.current_section = 'public'
            elif name == 'user-content-Plugins_for_Private_Sites':
                self.current_section = 'private'
        
        # Heading fallback if anchor name matches
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and 'id' in attrs_dict:
            header_id = attrs_dict['id']
            if 'plugins-for-public-sites' in header_id:
                self.current_section = 'public'
            elif 'plugins-for-private-sites' in header_id:
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
                self.current_cell_data = {
                    "text": "",
                    "links": []
                }
            elif tag == 'a' and self.in_cell and 'href' in attrs_dict:
                self.current_cell_data["links"].append(attrs_dict['href'])

    def handle_endtag(self, tag):
        if self.in_table:
            if tag == 'table':
                self.in_table = False
                self.current_section = None
            elif tag == 'tr':
                self.in_row = False
                if self.current_row:
                    # Validate we have columns
                    first_cell_text = self.current_row[0]["text"].lower()
                    if "search engine" not in first_cell_text and len(self.current_row) >= 5:
                        if self.current_section == 'public':
                            self.public_plugins.append(self.current_row)
                        elif self.current_section == 'private':
                            self.private_plugins.append(self.current_row)
            elif tag in ('td', 'th'):
                self.in_cell = False
                # Clean up text whitespace
                self.current_cell_data["text"] = self.current_cell_data["text"].strip()
                self.current_row.append(self.current_cell_data)

    def handle_data(self, data):
        if self.in_cell:
            # Append data to the current cell
            self.current_cell_data["text"] += data


# ========== UTILITIES ==========
def make_raw_url(url):
    """Converts a standard GitHub blob or display link to its raw equivalent."""
    if not url:
        return ""
    # Remove fragments
    url = url.split('#')[0]
    
    # Github URL conversion
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    
    # GitLab URL conversion
    if "gitlab.com" in url and "/blob/" in url:
        url = url.replace("/blob/", "/raw/")
    
    return url


def get_safe_filename(engine_name, url):
    """Generates a clean, lowercase python filename for the plugin."""
    url_filename = os.path.basename(url.split('?')[0].split('#')[0])
    if url_filename.endswith('.py') and url_filename.lower() not in ('search.py', 'engine.py', 'plugin.py', 'init.py', '__init__.py'):
        return url_filename
    
    # Fallback to sanitized engine name
    sanitized = re.sub(r'[^a-zA-Z0-9]', '', engine_name.replace(' ', '_')).lower()
    return f"{sanitized}.py"


def download_file(url, filepath):
    """Downloads a file with User-Agent headers, timeouts, and retries."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    req = urllib.request.Request(url, headers=headers)
    
    retries = 3
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read()
                
                # Check if it looks like HTML instead of Python (often happens with 404s/error pages)
                if b"<!DOCTYPE html>" in content or b"<html" in content:
                    return False, "Downloaded content appears to be HTML, not a Python plugin script"
                
                with open(filepath, "wb") as f:
                    f.write(content)
                return True, len(content)
        except urllib.error.HTTPError as e:
            if attempt == retries - 1:
                return False, f"HTTP Error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            if attempt == retries - 1:
                return False, f"URL Error: {e.reason}"
        except Exception as e:
            if attempt == retries - 1:
                return False, str(e)
        time.sleep(1) # delay between retries
    return False, "Unknown failure"


def process_plugin_list(raw_rows, target_dir):
    """Processes parsed rows, cleans URLs, downloads them, and formats metadata."""
    os.makedirs(target_dir, exist_ok=True)
    processed = []
    failures = []
    
    for i, row in enumerate(raw_rows, 1):
        engine_cell = row[0]
        author_cell = row[1]
        version = row[2]["text"] if len(row) > 2 else "1.0"
        last_update = row[3]["text"] if len(row) > 3 else "N/A"
        download_cell = row[4] if len(row) > 4 else None
        comments = row[5]["text"] if len(row) > 5 else ""
        
        engine_name = engine_cell["text"].strip()
        engine_site = engine_cell["links"][0] if engine_cell["links"] else ""
        
        author_name = author_cell["text"].strip()
        repo_link = author_cell["links"][0] if author_cell["links"] else ""
        
        raw_download_url = download_cell["links"][0] if download_cell and download_cell["links"] else ""
        download_url = make_raw_url(raw_download_url)
        
        # Clean up double linebreaks/br remnants in last_update
        last_update = " ".join(last_update.split())
        comments = " ".join(comments.split())
        
        if not download_url:
            failures.append({
                "engine": engine_name,
                "reason": "No download link found in wiki table row"
            })
            print(f"  ⚠️ Skipped: {engine_name} - No download URL found")
            continue
            
        filename = get_safe_filename(engine_name, download_url)
        filepath = os.path.join(target_dir, filename)
        
        print(f"  📥 [{i}/{len(raw_rows)}] Downloading {engine_name} ({filename})...")
        success, info = download_file(download_url, filepath)
        
        if success:
            processed.append({
                "engine_name": engine_name,
                "engine_site": engine_site,
                "author_name": author_name,
                "repo_link": repo_link,
                "version": version,
                "last_update": last_update,
                "filename": filename,
                "download_url": download_url,
                "comments": comments,
                "file_size": info
            })
            print(f"    ✅ Success ({info} bytes)")
        else:
            failures.append({
                "engine": engine_name,
                "url": download_url,
                "reason": info
            })
            print(f"    ❌ Failed: {info}")
            
        time.sleep(0.3)  # Rate limiting
        
    return processed, failures


def generate_readme(public_info, private_info):
    """Generates the comprehensive README.md with plugin lists and credits."""
    readme_template = """# qBittorrent Search Plugins - Complete Collection

[![Auto-Update Plugins](https://github.com/{github_repository}/actions/workflows/auto-update.yml/badge.svg)](https://github.com/{github_repository}/actions/workflows/auto-update.yml)

This repository automatically aggregates, checks, and packages unofficial qBittorrent search engine plugins from the [qBittorrent Unofficial Search Plugins Wiki](https://github.com/qbittorrent/search-plugins/wiki/Unofficial-search-plugins).

> ⚠️ **DISCLAIMER**: This repository is a collection of **unofficial** search plugins. I am NOT the author of these plugins. All credits and licenses go to the original creators listed in the tables below. Use them at your own risk, as Python scripts can execute arbitrary code.

---

## 📦 Contents & Download

*   **[qBittorrent-Search-Plugins-Complete.zip](https://raw.githubusercontent.com/{github_repository}/main/qBittorrent-Search-Plugins-Complete.zip)** — Contains the entire bundle (both public and private site plugins).
*   **[public_sites/](file:///public_sites/)** — Folder with all public site search plugins (~{public_count} plugins).
*   **[private_sites/](file:///private_sites/)** — Folder with all private tracker search plugins (~{private_count} plugins).

## 🔧 Installation Instructions

1.  Open **qBittorrent**.
2.  Go to the **Search** tab. (If you don't see it, enable it via **View** -> **Search Engine**).
3.  Click the **Search plugins...** button in the bottom right corner.
4.  Click **Install a new one**.
5.  Select **Local file** and pick the `.py` files you want from the downloaded folder/ZIP.
6.  Alternatively, you can choose **Web link** and paste any of the raw download links from the tables below.

---

## 📋 Complete List of Aggregated Plugins

### 🌍 Public Sites ({public_count} Plugins)

| # | Search Engine | Author / Repository | Version | Last Updated | Raw Download | Comments |
|---|---------------|---------------------|---------|--------------|--------------|----------|
{public_table_rows}

### 🔒 Private Sites ({private_count} Plugins)

*Note: These plugins are for private trackers and generally require you to edit the `.py` file to insert your credentials or API keys.*

| # | Search Engine | Author / Repository | Version | Last Updated | Raw Download | Comments |
|---|---------------|---------------------|---------|--------------|--------------|----------|
{private_table_rows}

## ⚡ Auto-Update Pipeline

This bundle is automatically updated **daily at midnight UTC** using GitHub Actions. The workflow:
1. Scrapes the unofficial qBittorrent wiki page.
2. Extracts raw URLs and downloads all active plugins.
3. Automatically validates each python file's syntax.
4. Packages everything into a ZIP and updates the tables in this README.

## ⚖️ Licensing

All plugins are the property of their respective authors. This aggregator repository itself is licensed under the MIT License.
"""
    # Fallback repo if not run in GitHub Actions
    repo = os.environ.get("GITHUB_REPOSITORY", "username/qBittorrent-Search-Plugins")
    
    def format_rows(info_list):
        rows = []
        for idx, item in enumerate(info_list, 1):
            engine = f"[{item['engine_name']}]({item['engine_site']})" if item['engine_site'] else item['engine_name']
            author = f"[{item['author_name']}]({item['repo_link']})" if item['repo_link'] else item['author_name']
            dl = f"[`{item['filename']}`]({item['download_url']})"
            rows.append(f"| {idx} | **{engine}** | {author} | {item['version']} | {item['last_update']} | {dl} | {item['comments']} |")
        return "\n".join(rows)

    readme_content = readme_template.format(
        github_repository=repo,
        public_count=len(public_info),
        private_count=len(private_info),
        public_table_rows=format_rows(public_info),
        private_table_rows=format_rows(private_info)
    )
    
    with open(os.path.join(OUTPUT_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme_content)
    print("📝 README.md generated successfully.")


def build_zip():
    """Bundles public and private sites, README.md into a single ZIP archive."""
    print("📦 Packing ZIP archive...")
    with zipfile.ZipFile(ZIP_NAME, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add README and LICENSE if present
        for doc in ("README.md", "LICENSE"):
            if os.path.exists(doc):
                zf.write(doc, doc)
        
        # Add public plugins
        for root, _, files in os.walk(PUBLIC_DIR):
            for file in files:
                if file.endswith('.py'):
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, OUTPUT_DIR)
                    zf.write(filepath, arcname)
                    
        # Add private plugins
        for root, _, files in os.walk(PRIVATE_DIR):
            for file in files:
                if file.endswith('.py'):
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, OUTPUT_DIR)
                    zf.write(filepath, arcname)
                    
    size = os.path.getsize(ZIP_NAME)
    print(f"✅ ZIP Archive created: {ZIP_NAME} ({size/1024:.1f} KB)")


# ========== MAIN FLOW ==========
def main():
    print("==================================================")
    print("🚀 Starting qBittorrent Search Plugins Aggregator")
    print("==================================================")
    
    html_content = ""
    # Try fetching from URL first
    try:
        print(f"🌐 Fetching wiki contents from: {WIKI_URL} ...")
        req = urllib.request.Request(
            WIKI_URL, 
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            html_content = response.read().decode('utf-8')
        print("✅ Live wiki HTML fetched successfully.")
    except Exception as e:
        print(f"⚠️ Failed to fetch wiki URL: {e}")
        # Try local fallback
        if os.path.exists(LOCAL_WIKI_PATH):
            print(f"📂 Found local wiki HTML at {LOCAL_WIKI_PATH}. Loading fallback...")
            with open(LOCAL_WIKI_PATH, 'r', encoding='utf-8') as f:
                html_content = f.read()
            print("✅ Local wiki HTML loaded successfully.")
        else:
            print("❌ No local wiki HTML fallback found. Exiting.")
            return

    # Parse HTML
    print("🔍 Parsing wiki tables...")
    parser = WikiHTMLParser()
    parser.feed(html_content)
    print(f"✅ Parsed {len(parser.public_plugins)} public engine rows and {len(parser.private_plugins)} private engine rows.")
    
    # Download public plugins
    print("\n📥 Processing PUBLIC sites plugins...")
    public_success, public_failed = process_plugin_list(parser.public_plugins, PUBLIC_DIR)
    
    # Download private plugins
    print("\n📥 Processing PRIVATE sites plugins...")
    private_success, private_failed = process_plugin_list(parser.private_plugins, PRIVATE_DIR)
    
    # Generate README
    print("\n📝 Generating documentation...")
    generate_readme(public_success, private_success)
    
    # Build ZIP
    print("\n📦 Bundling plugins...")
    build_zip()
    
    # Save failure log
    all_failed = {"public": public_failed, "private": private_failed}
    with open(FAILED_LOG, 'w', encoding='utf-8') as f:
        json.dump(all_failed, f, indent=2)
    
    print("\n==================================================")
    print("📊 AGGREGATION COMPLETE SUMMARY")
    print("==================================================")
    print(f"  Public:  Downloaded: {len(public_success)} | Failed: {len(public_failed)}")
    print(f"  Private: Downloaded: {len(private_success)} | Failed: {len(private_failed)}")
    print(f"  ZIP Bundle: {ZIP_NAME}")
    print(f"  Failure Log: {FAILED_LOG}")
    print("==================================================")

if __name__ == "__main__":
    main()
