# VERSION: 1.00
# AUTHORS: TR4KER plugin contributors
# LICENSING: GPL-3.0-or-later (compatible with qBittorrent search plugins)

"""qBittorrent search plugin for TR4KER (https://tr4ker.net).

Uses the Torznab API:
    https://tr4ker.net/api/torznab?t=search&q=...&cat=...&limit=...&offset=...&apikey=...

Configuration lives in ``tr4ker.json`` next to this file (auto-created on
first run, same approach as the official Jackett plugin). Put your TR4KER
API key in there — qBittorrent cannot see the ``.env`` of this repo.
"""

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Union
from urllib.parse import unquote, urlencode

import helpers
from novaprinter import prettyPrinter

TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

CONFIG_FILE = "tr4ker.json"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DATA: Dict[str, Any] = {
    "api_key": "YOUR_API_KEY_HERE",
    "url": "https://tr4ker.net",
    "timeout": 30,
    "limit": 100,  # TR4KER caps: limits max="100" default="50"
    "max_pages": 3,  # 3 x 100 = 300 results max per search
}

API_PATH = "/api/torznab"


def load_configuration() -> None:
    global CONFIG_DATA
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        # keep defaults for missing keys, override with user values
        for key, value in loaded.items():
            CONFIG_DATA[key] = value
    except ValueError:
        # file exists but is malformed
        CONFIG_DATA["malformed"] = True
    except FileNotFoundError:
        save_configuration()
    except OSError:
        CONFIG_DATA["malformed"] = True

    if any(item not in CONFIG_DATA for item in ("api_key", "url")):
        CONFIG_DATA["malformed"] = True


def save_configuration() -> None:
    data = {k: v for k, v in CONFIG_DATA.items() if k != "malformed"}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=4, sort_keys=True))
    except OSError as exc:
        print(f"tr4ker: cannot write {CONFIG_PATH}: {exc}", file=sys.stderr)


load_configuration()


class tr4ker:
    """qBittorrent engine class. Filename (tr4ker) must match class name."""

    url = CONFIG_DATA["url"].rstrip("/") if CONFIG_DATA["url"].endswith("/") else CONFIG_DATA["url"]
    name = "TR4KER"
    api_key = CONFIG_DATA["api_key"]

    # Mapped from TR4KER caps (t=caps). Sub-ids included so filtering works.
    # qBittorrent calls search() with one of these keys.
    supported_categories: Dict[str, Optional[List[str]]] = {
        "all": None,
        "anime": ["5070"],
        "books": ["7000", "7010", "7020", "7030", "3030"],  # + Audio/Audiobook
        "games": ["1000", "1010", "1020", "1030"],
        "movies": ["2000", "2010", "2040"],
        "music": ["3000", "3010", "3040"],
        "software": ["4000", "4030", "4070"],
        "tv": ["5000", "5040", "5060"],
    }

    def download_torrent(self, url: str) -> None:
        if url.startswith("magnet:?"):
            print(f"{url} {url}")
            return
        try:
            print(helpers.download_file(url))
        except Exception as exc:  # noqa: BLE001 - must never crash nova2dl
            print(f"tr4ker: download failed: {exc}", file=sys.stderr)

    # DO NOT CHANGE the name and parameters of this function (called by nova2.py)
    def search(self, query: str, category: str = "all") -> None:
        what = unquote(query)
        cat_key = category.lower()
        cats = self.supported_categories.get(cat_key, self.supported_categories["all"])

        if "malformed" in CONFIG_DATA:
            self.handle_error("malformed configuration file", what)
            return
        if not self.api_key or self.api_key == "YOUR_API_KEY_HERE":
            self.handle_error("api key error", what)
            return

        limit = int(CONFIG_DATA.get("limit", 100) or 100)
        limit = max(1, min(limit, 100))
        max_pages = int(CONFIG_DATA.get("max_pages", 3) or 1)
        max_pages = max(1, min(max_pages, 10))

        results: List[Dict[str, Any]] = []
        for page in range(max_pages):
            offset = page * limit
            response = self.fetch_page(what, cats, limit, offset)
            if response is None:
                if page == 0:
                    self.handle_error("connection error", what)
                return
            items = self.parse_response(response)
            if not items:
                break
            results.extend(items)
            if len(items) < limit:
                break  # last page

        # Most seeds first, as recommended by qBittorrent docs.
        results.sort(key=lambda r: r.get("_seeds_int", -1), reverse=True)
        for res in results:
            res.pop("_seeds_int", None)
            self.pretty_print(res)

    # -- internals -----------------------------------------------------

    def build_url(self, what: str, cats: Optional[List[str]], limit: int, offset: int) -> str:
        params: List[tuple] = [
            ("t", "search"),
            ("q", what),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("apikey", self.api_key),
        ]
        if cats:
            params.insert(2, ("cat", ",".join(cats)))
        base = self.url.rstrip("/")
        return f"{base}{API_PATH}?{urlencode(params)}"

    def fetch_page(
        self, what: str, cats: Optional[List[str]], limit: int, offset: int
    ) -> Optional[str]:
        url = self.build_url(what, cats, limit, offset)
        return self.get_response(url)

    def get_response(self, url: str) -> Optional[str]:
        # helpers.retrieve_url does not handle redirects/cookies well enough
        # for API downloads, so use our own opener (same as jackett.py).
        # Never log the URL: it contains the API key.
        try:
            timeout = int(CONFIG_DATA.get("timeout", 30) or 30)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(request, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - report via stderr, keep stdout clean
            print(f"tr4ker: request failed: {exc}", file=sys.stderr)
            return None

    def parse_response(self, xml_text: str) -> List[Dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            print(f"tr4ker: invalid XML response: {exc}", file=sys.stderr)
            return []

        channel = root.find("channel")
        if channel is None:
            # error response from the API (e.g. bad key) has no channel
            err = (root.text or "").strip() if root.text else ""
            if err:
                print(f"tr4ker: API error: {err[:200]}", file=sys.stderr)
            return []

        out: List[Dict[str, Any]] = []
        for item in channel.findall("item"):
            parsed = self.parse_item(item)
            if parsed is not None:
                out.append(parsed)
        return out

    def parse_item(self, item: ET.Element) -> Optional[Dict[str, Any]]:
        title_el = item.find("title")
        if title_el is None or not title_el.text:
            return None
        title = title_el.text.strip()
        if not title:
            return None

        link_el = item.find("link")
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not link:
            # TR4KER provides no magneturl attr; link is the .torrent download
            # URL (with apikey). Without it the result is useless.
            return None

        attrs = self.torznab_attrs(item)

        size = self.to_int(attrs.get("size"), -1)
        if size < 0:
            size = self.enclosure_length(item)

        seeds = self.to_int(attrs.get("seeders"), -1)
        # TR4KER exposes leechers AND peers (peers = seeds+leechers).
        # qBittorrent wants leechers only.
        leech = self.to_int(attrs.get("leechers"), -1)
        if leech < 0 and attrs.get("peers") is not None and seeds >= 0:
            peers = self.to_int(attrs.get("peers"), -1)
            leech = peers - seeds if peers >= 0 else -1

        comments_el = item.find("comments")
        guid_el = item.find("guid")
        desc_link = ""
        if comments_el is not None and comments_el.text:
            desc_link = comments_el.text.strip()
        elif guid_el is not None and guid_el.text:
            desc_link = guid_el.text.strip()

        res: Dict[str, Any] = {
            "link": link,
            "name": title,
            "size": size,
            "seeds": seeds,
            "leech": leech,
            "engine_url": self.url,
            "desc_link": desc_link,
            "pub_date": self.parse_pubdate(item),
            "_seeds_int": seeds,
        }
        return res

    def torznab_attrs(self, item: ET.Element) -> Dict[str, str]:
        attrs: Dict[str, str] = {}
        for attr in item.findall(f"./{{{TORZNAB_NS}}}attr"):
            name = attr.attrib.get("name")
            value = attr.attrib.get("value")
            if name is not None and value is not None:
                attrs[name] = value
        return attrs

    @staticmethod
    def enclosure_length(item: ET.Element) -> int:
        enc = item.find("enclosure")
        if enc is not None and enc.attrib.get("length"):
            return tr4ker.to_int(enc.attrib.get("length"), -1)
        return -1

    @staticmethod
    def to_int(value: Union[str, None], default: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError, AttributeError):
            return default

    @staticmethod
    def parse_pubdate(item: ET.Element) -> int:
        pub = item.find("pubDate")
        if pub is None or not pub.text:
            return -1
        text = pub.text.strip()
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                return int(datetime.strptime(text, fmt).timestamp())
            except ValueError:
                continue
        return -1

    def handle_error(self, error_msg: str, what: str) -> None:
        self.pretty_print(
            {
                "link": self.url,
                "name": (
                    f"TR4KER: {error_msg}! Edit '{CONFIG_PATH}' "
                    f"and set your API key (see tr4ker.json.example). Search: '{what}'"
                ),
                "size": -1,
                "seeds": -1,
                "leech": -1,
                "engine_url": self.url,
                "desc_link": "https://tr4ker.net/wiki/prowlarr-radarr-sonarr",
                "pub_date": -1,
            }
        )

    def pretty_print(self, dictionary: Dict[str, Any]) -> None:
        escaped = {k: (v.replace("|", "%7C") if isinstance(v, str) else v) for k, v in dictionary.items()}
        prettyPrinter(escaped)
