# VERSION: 2.3
# AUTHORS: MANCrimSon (https://github.com/MANCrimSon)
# CONTRIBUTORS:
#   swannie-eire (original v1.0 plugin)
#   Diego de las Heras (ngosang@hotmail.es)
#   hannsen (github.com/hannsen)
#   Alexander Georgievskiy <galeksandrp@gmail.com>

import datetime
import gzip
import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure UTF-8 output streams on Windows to prevent UnicodeEncodeError with non-cp1251 titles
if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# qBittorrent nova3 runtime modules
try:
    import helpers
    from novaprinter import prettyPrinter
except ImportError:
    helpers = None  # type: ignore[assignment]
    prettyPrinter = None  # type: ignore[assignment]


###############################################################################
# Proxy Manager: prevents loopback connection failures when qBittorrent uses
# global HTTP/HTTPS/SOCKS proxy, while keeping proxy for external tracker downloads.
###############################################################################
class _ProxyManager:
    HTTP_PROXY_KEY = "http_proxy"
    HTTPS_PROXY_KEY = "https_proxy"

    def __init__(self) -> None:
        self.http_proxy = os.environ.get(self.HTTP_PROXY_KEY, "")
        self.https_proxy = os.environ.get(self.HTTPS_PROXY_KEY, "")

    def enable_proxy(self, enable: bool) -> None:
        if enable:
            if self.http_proxy:
                os.environ[self.HTTP_PROXY_KEY] = self.http_proxy
            if self.https_proxy:
                os.environ[self.HTTPS_PROXY_KEY] = self.https_proxy
        else:
            current_http = os.environ.get(self.HTTP_PROXY_KEY) or os.environ.get(self.HTTP_PROXY_KEY.upper())
            current_https = os.environ.get(self.HTTPS_PROXY_KEY) or os.environ.get(self.HTTPS_PROXY_KEY.upper())
            if current_http:
                self.http_proxy = current_http
            if current_https:
                self.https_proxy = current_https

            os.environ.pop(self.HTTP_PROXY_KEY, None)
            os.environ.pop(self.HTTPS_PROXY_KEY, None)
            os.environ.pop(self.HTTP_PROXY_KEY.upper(), None)
            os.environ.pop(self.HTTPS_PROXY_KEY.upper(), None)

        if helpers is not None:
            try:
                helpers.enable_socks_proxy(enable)
            except AttributeError:
                pass


_proxy_manager = _ProxyManager()
_proxy_manager.enable_proxy(False)


###############################################################################
# Configuration
###############################################################################
_CONFIG_FILE = 'prowlarr.json'
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), _CONFIG_FILE)
_CONFIG_DATA: Dict[str, Any] = {
    'url': 'http://127.0.0.1:9696',  # Prowlarr instance URL
    'api_key': 'YOUR_API_KEY_HERE',  # Prowlarr API key (Settings > General)
    'multithreaded': True,           # True: Search indexers concurrently for progressive instant results
    'thread_count': 10,              # Number of concurrent worker threads (editable: 10 is optimal for low CPU)
    'timeout_seconds': 30,           # HTTP request timeout in seconds (30s is optimal for multi-threaded search)
    'filter_usenet': True,           # True: Ignore Usenet releases to avoid broken .nzb downloads
    'tracker_first': False,          # True: '[Tracker] Title', False: 'Title [Tracker]'
    'show_flags': True,              # True: Show '[Freeleech]' and other flags in release name
    'flags_position': 'end',         # 'end': 'Title [Freeleech] [Tracker]', 'start': '[Freeleech] Title [Tracker]'
}
_DEFAULT_CONFIG_KEYS = tuple(_CONFIG_DATA.keys())
_PRINTER_THREAD_LOCK = Lock()


def _safe_int(value: Any, default: int, min_val: int = 1) -> int:
    """Safely convert config value to int without crashing at module import time."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
        return max(min_val, parsed)
    except (ValueError, TypeError):
        return default


def _load_configuration() -> None:
    global _CONFIG_DATA
    config_needs_save = False
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)
            if isinstance(loaded_data, dict):
                if any(k not in loaded_data for k in _DEFAULT_CONFIG_KEYS):
                    config_needs_save = True
                _CONFIG_DATA.update(loaded_data)
            else:
                _CONFIG_DATA['malformed'] = True
    except ValueError:
        _CONFIG_DATA['malformed'] = True
    except FileNotFoundError:
        config_needs_save = True
    except Exception:
        _CONFIG_DATA['malformed'] = True

    # Validate essential configuration keys and types
    if not isinstance(_CONFIG_DATA.get('url'), str) or not _CONFIG_DATA['url'].strip():
        _CONFIG_DATA['malformed'] = True
    if not isinstance(_CONFIG_DATA.get('api_key'), str):
        _CONFIG_DATA['malformed'] = True

    # Validate numeric configuration values
    for int_key in ('timeout_seconds', 'thread_count'):
        val = _CONFIG_DATA.get(int_key)
        if isinstance(val, bool):
            _CONFIG_DATA['malformed'] = True
        else:
            try:
                int(val)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                _CONFIG_DATA['malformed'] = True

    # Write initial or upgraded config to disk (never overwrite a malformed user config)
    if config_needs_save and not _CONFIG_DATA.get('malformed'):
        _save_configuration()


def _save_configuration() -> None:
    try:
        to_save = {k: v for k, v in _CONFIG_DATA.items() if k != 'malformed'}
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(json.dumps(to_save, indent=4))
    except Exception:
        pass


_load_configuration()


def _get_browser_user_agent() -> str:
    base_date = datetime.date(2024, 4, 16)
    base_version = 125
    now_date = datetime.datetime.now(datetime.timezone.utc).date()
    now_version = base_version + ((now_date - base_date).days // 30)
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{now_version}.0) Gecko/20100101 Firefox/{now_version}.0"


def _parse_iso_date(date_str: Optional[str]) -> int:
    """Convert ISO 8601 date string to POSIX timestamp."""
    if not date_str:
        return -1
    try:
        # Standard ISO 8601 normalization (handle 'Z' suffix)
        normalized = date_str.strip().replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(normalized)
        return int(dt.timestamp())
    except Exception:
        return -1


###############################################################################
# Prowlarr Search Engine
###############################################################################
class prowlarr:
    name = 'Prowlarr'
    _raw_url = _CONFIG_DATA.get('url', 'http://127.0.0.1:9696')
    url = _raw_url.strip().rstrip('/') if isinstance(_raw_url, str) and _raw_url.strip() else 'http://127.0.0.1:9696'
    _raw_api_key = _CONFIG_DATA.get('api_key', 'YOUR_API_KEY_HERE')
    api_key = _raw_api_key if isinstance(_raw_api_key, str) else ''
    tracker_first = bool(_CONFIG_DATA.get('tracker_first', False))
    show_flags = bool(_CONFIG_DATA.get('show_flags', True))
    flags_position = str(_CONFIG_DATA.get('flags_position', 'end')).lower()
    filter_usenet = bool(_CONFIG_DATA.get('filter_usenet', True))
    timeout = _safe_int(_CONFIG_DATA.get('timeout_seconds', 30), default=30, min_val=1)
    multithreaded = bool(_CONFIG_DATA.get('multithreaded', True))
    thread_count = _safe_int(_CONFIG_DATA.get('thread_count', 10), default=10, min_val=1)
    last_error: Optional[str] = None

    # Torznab / Newznab categories supported by Prowlarr
    supported_categories = {
        'all': None,
        'anime': ['5070'],
        'books': ['8000'],
        'games': ['1000', '4000'],
        'movies': ['2000'],
        'music': ['3000'],
        'pictures': None,
        'software': ['4000'],
        'tv': ['5000'],
    }

    def _is_prowlarr_url(self, target_url: str) -> bool:
        """Check whether target_url belongs to the configured Prowlarr instance."""
        if not self.url or not target_url:
            return False
        base = self.url.rstrip('/')
        return target_url == base or target_url.startswith(base + '/') or target_url.startswith(base + '?')

    def _download_to_temp_or_magnet(self, download_url: str, send_api_key: bool) -> Optional[str]:
        """Download torrent to a temp file or resolve magnet redirect in a single HTTP request."""
        try:
            req = urllib.request.Request(download_url)
            req.add_header('User-Agent', _get_browser_user_agent())
            if send_api_key and self.api_key:
                req.add_header('X-Api-Key', self.api_key)

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            with opener.open(req, timeout=self.timeout) as resp:
                data = resp.read()
                if resp.headers.get('Content-Encoding', '').lower() == 'gzip':
                    data = gzip.decompress(data)

            if data.startswith(b'magnet:?'):
                return data.decode('utf-8', errors='replace').strip()

            fd, tmp_path = tempfile.mkstemp(suffix='.torrent')
            try:
                with os.fdopen(fd, 'wb') as tmp_file:
                    tmp_file.write(data)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return None
            return tmp_path
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                redirect_url = e.headers.get('Location')
                if redirect_url and redirect_url.startswith('magnet:?'):
                    return redirect_url
            return None
        except Exception:
            return None

    def download_torrent(self, download_url: str) -> None:
        """Handle torrent or magnet download request from qBittorrent."""
        if download_url.startswith('magnet:?'):
            print(f"{download_url} {download_url}")
            return

        is_local_prowlarr = self._is_prowlarr_url(download_url)
        _proxy_manager.enable_proxy(not is_local_prowlarr)

        try:
            result = self._download_to_temp_or_magnet(download_url, send_api_key=is_local_prowlarr)
            if result is not None:
                print(f"{result} {download_url}")
            elif helpers is not None:
                print(helpers.download_file(download_url))
            else:
                print(download_url)
        finally:
            _proxy_manager.enable_proxy(False)

    def format_release(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse raw release item from Prowlarr into standardized dictionary for prettyPrinter."""
        if not isinstance(item, dict):
            return None

        # Skip Usenet releases if configured
        protocol = str(item.get('protocol', '')).lower()
        if self.filter_usenet and (protocol == 'usenet' or protocol == '2'):
            return None

        raw_title = item.get('title')
        if not raw_title:
            return None

        # Sanitize title: replace newlines, carriage returns, tabs and collapse multiple spaces
        cleaned_title = re.sub(r'[\r\n\t]+', ' ', str(raw_title))
        cleaned_title = re.sub(r'\s{2,}', ' ', cleaned_title).strip()
        if not cleaned_title:
            return None

        indexer = item.get('indexer', 'Prowlarr')

        # Parse indexer flags (e.g. Freeleech, HalfFreeleech)
        flag_badge = ""
        if self.show_flags:
            indexer_flags = item.get('indexerFlags') or []
            clean_flags = []
            for f in indexer_flags:
                f_name = str(f).replace('G_', '')
                clean_flags.append(f_name)
            if clean_flags:
                flag_badge = f"[{'/'.join(clean_flags)}]"

        # Format item name according to user preference
        if self.flags_position == 'start' and flag_badge:
            if self.tracker_first:
                formatted_name = f"{flag_badge} [{indexer}] {cleaned_title}"
            else:
                formatted_name = f"{flag_badge} {cleaned_title} [{indexer}]"
        else:
            # Default: flag before tracker, with tracker at the very end
            flag_mid = f" {flag_badge}" if flag_badge else ""
            if self.tracker_first:
                formatted_name = f"[{indexer}] {cleaned_title}{flag_mid}"
            else:
                formatted_name = f"{cleaned_title}{flag_mid} [{indexer}]"

        # Determine download link: prefer magnetUrl, fallback to downloadUrl or infoHash magnet
        magnet_url = item.get('magnetUrl')
        download_url = item.get('downloadUrl')
        info_hash = item.get('infoHash')
        chosen_link = magnet_url or download_url
        if not chosen_link and isinstance(info_hash, str) and info_hash.strip():
            chosen_link = f"magnet:?xt=urn:btih:{info_hash.strip()}&dn={urllib.parse.quote(cleaned_title)}"

        if not chosen_link:
            return None

        # Size in bytes
        raw_size = item.get('size', -1)
        try:
            size_val = int(raw_size) if raw_size is not None else -1
        except (ValueError, TypeError):
            size_val = -1

        # Seeders and leechers
        raw_seeds = item.get('seeders')
        seeds = int(raw_seeds) if raw_seeds is not None and isinstance(raw_seeds, (int, str)) and str(raw_seeds).lstrip('-').isdigit() else -1

        raw_leech = item.get('leechers')
        leech = int(raw_leech) if raw_leech is not None and isinstance(raw_leech, (int, str)) and str(raw_leech).lstrip('-').isdigit() else -1

        # Description link / info
        desc_link = item.get('infoUrl') or item.get('commentUrl') or item.get('guid') or ''

        # Publication date as POSIX timestamp
        pub_date = _parse_iso_date(item.get('publishDate'))

        return {
            'link': chosen_link,
            'name': formatted_name,
            'size': f"{size_val} B" if size_val >= 0 else -1,
            'seeds': seeds,
            'leech': leech,
            'engine_url': self.url,
            'desc_link': desc_link,
            'pub_date': pub_date,
        }

    def get_indexers(self, what: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch list of enabled torrent indexers from Prowlarr."""
        indexers_url = f"{self.url}/api/v1/indexer"
        _proxy_manager.enable_proxy(False)
        response_text = self.get_response(indexers_url)
        if response_text is None:
            err = self.last_error or f"Failed to connect to Prowlarr at {self.url}"
            self.handle_error(err, what)
            return None

        try:
            indexers = json.loads(response_text)
        except json.JSONDecodeError:
            if "<html" in response_text.lower():
                self.handle_error("Received HTML instead of JSON (check reverse proxy, URL Base, or Cloudflare)", what)
            else:
                self.handle_error("Invalid JSON response received when fetching indexers", what)
            return None

        if not isinstance(indexers, list):
            if isinstance(indexers, dict) and 'message' in indexers:
                self.handle_error(f"Prowlarr API error: {indexers['message']}", what)
            else:
                self.handle_error("Unexpected response schema for indexer list from Prowlarr", what)
            return None

        active_indexers: List[Dict[str, Any]] = []
        for idx in indexers:
            if not isinstance(idx, dict):
                continue
            if not idx.get('enable', False):
                continue
            protocol = str(idx.get('protocol', '')).lower()
            if self.filter_usenet and (protocol == 'usenet' or protocol == '2'):
                continue
            active_indexers.append(idx)

        return active_indexers

    def search_single_indexer(self, what: str, categories: Optional[List[str]], indexer_id: Union[int, str]) -> Tuple[int, Optional[str]]:
        """Search a single indexer and emit results progressively to qBittorrent."""
        params_list: List[Tuple[str, str]] = [
            ('query', what),
            ('type', 'search'),
            ('indexerIds', str(indexer_id)),
        ]
        if categories:
            for cat_id in categories:
                params_list.append(('categories', cat_id))

        query_string = urllib.parse.urlencode(params_list)
        search_endpoint = f"{self.url}/api/v1/search?{query_string}"

        response_text, err = self._fetch_response(search_endpoint)
        if response_text is None:
            return 0, err or f"Failed to query indexer {indexer_id}"

        try:
            results = json.loads(response_text)
        except json.JSONDecodeError:
            return 0, "Invalid JSON response received from Prowlarr"

        if not isinstance(results, list):
            if isinstance(results, dict) and 'message' in results:
                return 0, f"Prowlarr API error: {results['message']}"
            return 0, "Unexpected response schema from Prowlarr"

        count = 0
        for item in results:
            res = self.format_release(item)
            if res:
                self.pretty_printer_thread_safe(res)
                count += 1
        return count, None

    def search(self, what: str, cat: str = 'all') -> None:
        what = urllib.parse.unquote(what).strip()
        cat_key = cat.lower()
        categories = self.supported_categories.get(cat_key)

        # Check configuration sanity
        if _CONFIG_DATA.get('malformed'):
            self.handle_error("Configuration file is malformed", what)
            return

        if self.api_key == "YOUR_API_KEY_HERE" or not self.api_key:
            self.handle_error("API key is not configured in prowlarr.json", what)
            return

        # Ensure local Prowlarr requests bypass global proxy once before spawning worker threads
        _proxy_manager.enable_proxy(False)

        # Multi-threaded concurrent search across indexers for progressive instant results
        if self.multithreaded and self.thread_count > 1:
            active_indexers = self.get_indexers(what)
            if active_indexers is None:
                return  # error already reported by get_indexers
            if not active_indexers:
                return  # no enabled torrent indexers configured in Prowlarr

            indexer_ids = [idx['id'] for idx in active_indexers if 'id' in idx]
            if not indexer_ids:
                return

            workers = max(1, min(len(indexer_ids), self.thread_count))
            succeeded_indexers = 0
            last_worker_error: Optional[str] = None
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(self.search_single_indexer, what, categories, idx_id)
                    for idx_id in indexer_ids
                ]
                for future in as_completed(futures):
                    try:
                        _, worker_err = future.result()
                        if worker_err is None:
                            succeeded_indexers += 1
                        else:
                            last_worker_error = worker_err
                    except Exception as exc:
                        last_worker_error = str(exc)

            if succeeded_indexers == 0 and last_worker_error:
                self.handle_error(last_worker_error, what)
            return

        # Fallback to single monolithic search query (indexerIds=-2)
        params_list: List[Tuple[str, str]] = [
            ('query', what),
            ('type', 'search'),
            ('indexerIds', '-2'),
        ]
        if categories:
            for cat_id in categories:
                params_list.append(('categories', cat_id))

        query_string = urllib.parse.urlencode(params_list)
        search_endpoint = f"{self.url}/api/v1/search?{query_string}"

        response_text = self.get_response(search_endpoint)

        if response_text is None:
            err = self.last_error or f"Failed to connect to Prowlarr at {self.url}"
            self.handle_error(err, what)
            return

        try:
            results = json.loads(response_text)
        except json.JSONDecodeError:
            if "<html" in response_text.lower():
                self.handle_error("Received HTML instead of JSON (check reverse proxy, URL Base, or Cloudflare)", what)
            else:
                self.handle_error("Invalid JSON response received from Prowlarr", what)
            return

        if not isinstance(results, list):
            # Check for Prowlarr error messages (e.g. {"message": "Unauthorized"})
            if isinstance(results, dict) and 'message' in results:
                self.handle_error(f"Prowlarr API error: {results['message']}", what)
            else:
                self.handle_error("Unexpected response schema from Prowlarr", what)
            return

        for item in results:
            res = self.format_release(item)
            if res:
                self.pretty_printer_thread_safe(res)

    def _fetch_response(self, query_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Thread-safe HTTP GET helper returning (response_text, error_message)."""
        try:
            req = urllib.request.Request(query_url)
            req.add_header('User-Agent', _get_browser_user_agent())
            if self._is_prowlarr_url(query_url) and self.api_key:
                req.add_header('X-Api-Key', self.api_key)
            req.add_header('Accept', 'application/json, text/plain, */*')

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            with opener.open(req, timeout=self.timeout) as resp:
                charset = resp.headers.get_content_charset() or 'utf-8'
                return resp.read().decode(charset, errors='replace'), None
        except urllib.error.HTTPError as e:
            # Handle 302/301 redirects to magnet links
            if e.code in (301, 302, 303, 307, 308):
                redirect_url = e.headers.get('Location')
                if redirect_url and redirect_url.startswith('magnet:?'):
                    return redirect_url, None

            if e.code == 401:
                return None, "Invalid API key (HTTP 401 Unauthorized)"
            elif e.code == 403:
                return None, "Access forbidden (HTTP 403 Forbidden)"
            elif e.code == 404:
                return None, "Endpoint not found (HTTP 404 - check URL or URL Base)"
            elif e.code == 500:
                return None, "Prowlarr server error (HTTP 500 Internal Server Error)"
            elif e.code == 502:
                return None, "Bad Gateway (HTTP 502 - reverse proxy cannot reach Prowlarr)"
            elif e.code == 503:
                return None, "Service Unavailable (HTTP 503 - Prowlarr is busy or restarting)"
            elif e.code == 504:
                return None, "Gateway Timeout (HTTP 504 - reverse proxy timed out waiting for Prowlarr)"
            else:
                return None, f"HTTP Error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
                return None, f"Request timed out after {self.timeout}s (Prowlarr indexers responded too slowly)"
            elif isinstance(reason, ConnectionRefusedError) or "connection refused" in str(reason).lower() or "10061" in str(reason):
                return None, f"Connection refused (is Prowlarr running at {self.url}?)"
            elif isinstance(reason, socket.gaierror) or "getaddrinfo failed" in str(reason).lower():
                return None, f"Cannot resolve host in {self.url} (check address/domain)"
            elif "certificate" in str(reason).lower() or "ssl" in str(reason).lower():
                return None, f"SSL/TLS certificate error connecting to {self.url}: {reason}"
            else:
                return None, f"Network connection failed: {reason}"
        except (socket.timeout, TimeoutError):
            return None, f"Request timed out after {self.timeout}s (Prowlarr indexers responded too slowly)"
        except Exception as e:
            return None, f"Connection error: {e}"

    def get_response(self, query_url: str) -> Optional[str]:
        """Send HTTP GET request to Prowlarr API with proper headers and cookie handling."""
        response_text, err = self._fetch_response(query_url)
        self.last_error = err
        return response_text

    def handle_error(self, error_msg: str, what: str) -> None:
        """Report errors directly in the qBittorrent search results list."""
        self.pretty_printer_thread_safe({
            'link': self.url,
            'name': f"Prowlarr: {error_msg}! Config: '{_CONFIG_PATH}' Search: '{what}'",
            'size': -1,
            'seeds': -1,
            'leech': -1,
            'engine_url': self.url,
            'desc_link': 'https://wiki.servarr.com/prowlarr',
            'pub_date': -1,
        })

    def pretty_printer_thread_safe(self, dictionary: Dict[str, Any]) -> None:
        """Output escaped search result securely under mutex lock."""
        escaped_dict = self.escape_pipe(dictionary)
        with _PRINTER_THREAD_LOCK:
            if prettyPrinter is not None:
                prettyPrinter(escaped_dict)
            else:
                # Standalone fallback output
                name = escaped_dict.get('name')
                link = escaped_dict.get('link')
                size = escaped_dict.get('size')
                seeds = escaped_dict.get('seeds')
                leech = escaped_dict.get('leech')
                pub_date = escaped_dict.get('pub_date')
                print(f"[{seeds}/{leech}] {name} ({size}) [pubDate:{pub_date}] -> {link}")

    def escape_pipe(self, dictionary: Dict[str, Any]) -> Dict[str, Any]:
        """Escape pipe delimiter '|' from values to protect nova3 stream protocol."""
        sanitized = {}
        for key, value in dictionary.items():
            if isinstance(value, str):
                sanitized[key] = value.replace('|', '%7C')
            else:
                sanitized[key] = value
        return sanitized


if __name__ == "__main__":
    test_search = sys.argv[1] if len(sys.argv) > 1 else "ubuntu"
    test_cat = sys.argv[2] if len(sys.argv) > 2 else "all"
    prowlarr_engine = prowlarr()
    prowlarr_engine.search(test_search, test_cat)

