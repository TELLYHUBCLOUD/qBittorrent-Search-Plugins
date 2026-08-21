# VERSION: 2.1
# qBittorrent search engine plugin for snowfl.com
#
# AUTO-GENERATED — DO NOT EDIT engine/snowfl.py BY HAND.
# Source of truth: src/snowfl/core.py + plugin/shell.py
# Regenerate with `make plugin`. Project: https://github.com/ChocoTonic/snowfl
import json
import re
import time
from urllib.parse import quote

try:
    from helpers import retrieve_url
    from novaprinter import prettyPrinter
except ImportError:
    # Lets this file be imported / unit-tested outside of qBittorrent, where the
    # `helpers` and `novaprinter` modules are not available.
    def retrieve_url(url):
        raise RuntimeError("retrieve_url is only available inside qBittorrent")

    def prettyPrinter(dictionary):
        print(dictionary)

# The `# VERSION:` header is stamped at build time (PLUGIN_VERSION env var); the
# release workflow sets a real, monotonic version when publishing to GitHub Releases.

BASE_URL = "https://snowfl.com/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

REGEX_FOR_JS = re.compile(r'((?:b.min.js).*)(?=")')
REGEX_FOR_KEY = re.compile(r'findNextItem.*?"(.*?)"')

SORT_ENUM = {
    "MAX_SEED": "/DH5kKsJw/0/SEED/NONE/",
    "MAX_LEECH": "/DH5kKsJw/0/LEECH/NONE/",
    "SIZE_ASC": "/DH5kKsJw/0/SIZE_ASC/NONE/",
    "SIZE_DSC": "/DH5kKsJw/0/SIZE/NONE/",
    "RECENT": "/DH5kKsJw/0/DATE/NONE/",
    "NONE": "/DH5kKsJw/0/NONE/NONE/",
}


class ApiError(Exception):
    """Raised when the Snowfl API key cannot be located."""

    def __init__(self, message="API error occurred"):
        super().__init__(message)


class FetchError(Exception):
    """Raised for errors while fetching data from Snowfl."""

    def __init__(self, message="Fetch error occurred", status_code=None):
        super().__init__(message)
        self.status_code = status_code

    def __str__(self):
        return "{0}: {1} (Status Code: {2})".format(
            self.__class__.__name__, self.args[0], self.status_code
        )


def get_api_key(fetch):
    """Scrape the per-session API key. ``fetch(url) -> str`` performs the HTTP GET.

    Raises :class:`ApiError` if the homepage JS link or the key cannot be found;
    transport errors are surfaced by ``fetch`` itself.
    """
    home_text = fetch(BASE_URL)
    js_match = REGEX_FOR_JS.search(home_text)
    if not js_match:
        raise ApiError("JS file link not found in homepage")

    js_text = fetch(BASE_URL + js_match.group(0))
    key_match = REGEX_FOR_KEY.search(js_text)
    if not key_match:
        raise ApiError("API key not found in JS file")

    return key_match.group(1)


def build_search_url(api_key, query, sort="NONE", include_nsfw=False):
    """Build the Snowfl search URL for a query."""
    sort_option = SORT_ENUM.get(sort, SORT_ENUM["NONE"])
    flag = 1 if include_nsfw else 0
    return "{0}{1}/{2}{3}{4}".format(BASE_URL, api_key, query, sort_option, flag)


def build_magnet_url(api_key, item):
    """Build the Snowfl magnet-resolution URL for a single result item."""
    encoded_url = quote(item.get("url", ""))
    return "{0}{1}/{2}/{3}".format(
        BASE_URL, api_key, item.get("site", ""), encoded_url
    )


def fill_magnets(api_key, data, fetch):
    """Resolve magnet links for items that lack one, via the magnet endpoint.

    Failures per item are swallowed so one bad result never sinks the search.
    """
    results = []
    for item in data:
        if not item.get("magnet"):
            try:
                resolved = json.loads(fetch(build_magnet_url(api_key, item)))
                item["magnet"] = resolved.get("url", "")
            except Exception:
                pass
        results.append(item)
    return results


def fetch_results(
    query,
    api_key,
    fetch,
    sort="NONE",
    include_nsfw=False,
    force_fetch_magnet=False,
):
    """Run a search and return the raw Snowfl result list.

    When ``force_fetch_magnet`` is true, items missing a magnet are resolved via
    :func:`fill_magnets`.
    """
    if len(query) <= 2:
        raise FetchError("Query should be of length >= 3")

    data = json.loads(fetch(build_search_url(api_key, query, sort, include_nsfw)))
    if force_fetch_magnet and data:
        data = fill_magnets(api_key, data, fetch)
    return data


AGE_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,  # 30 days (approximate)
    "year": 31536000,  # 365 days (approximate)
}
AGE_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?", re.IGNORECASE
)


def age_to_pub_date(age, now):
    """Convert snowfl's relative ``age`` (e.g. ``"2 weeks"``) to an approximate Unix
    timestamp, measured back from ``now``. Returns ``-1`` when unparseable.

    snowfl exposes only a relative age, so this is necessarily approximate (months
    are treated as 30 days, years as 365); it is good enough for the qBittorrent
    "Published On" column and for sorting by recency.
    """
    if not age:
        return -1
    match = AGE_RE.search(age)
    if not match:
        return -1
    quantity = int(match.group(1))
    unit_seconds = AGE_UNIT_SECONDS[match.group(2).lower()]
    return int(now) - (quantity * unit_seconds)


class snowfl(object):
    url = "https://snowfl.com"
    name = "Snowfl"
    supported_categories = {"all": "0"}

    def search(self, what, cat="all"):
        api_key = get_api_key(retrieve_url)
        results = fetch_results(
            what,
            api_key,
            retrieve_url,
            sort="MAX_SEED",
            force_fetch_magnet=True,
        )
        now = time.time()
        for item in results:
            # snowfl is an aggregator; surface the originating site in the name,
            # since engine_url must stay "https://snowfl.com" (qBittorrent matches
            # it to route downloads back to this plugin).
            name = item.get("name", "")
            site = item.get("site", "")
            if site:
                name = "%s [%s]" % (name, site)
            prettyPrinter(
                {
                    "link": item.get("magnet") or item.get("url", ""),
                    "name": name,
                    "size": item.get("size", "-1"),
                    "seeds": item.get("seeder", -1),
                    "leech": item.get("leecher", -1),
                    "engine_url": self.url,
                    "desc_link": item.get("url", ""),
                    "pub_date": age_to_pub_date(item.get("age"), now),
                }
            )

    def download_torrent(self, info):
        # Results already carry a magnet (force_fetch_magnet above), so this is the
        # common path; the page-scrape is a fallback for the rare magnet-less link.
        if info.startswith("magnet:"):
            print(info + " " + info)
            return
        page = retrieve_url(info)
        match = re.search(r'"(magnet:[^"]+)"', page)
        if match:
            print(match.group(1) + " " + info)
        else:
            raise RuntimeError("Could not resolve a magnet link for: " + info)
