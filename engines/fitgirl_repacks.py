# VERSION: 3.5
# AUTHORS: Spidy, afalvarezsite
# Refactored qBittorrent search plugin for FitGirl Repacks.
#
# Architecture:
#   search -> HTTP client -> search parser -> Candidate objects
#          -> download resolver (cache + post fetch) -> deduplicator
#          -> batched tracker scrape -> normalizer -> prettyPrinter
#
# Requirements: Python >= 3.7 (datetime.fromisoformat), standard library only
# plus qBittorrent's novaprinter module.
#
# Environment variables (all optional):
#   FITGIRL_DEBUG=1    log debug messages to stderr
#   FITGIRL_SCRAPE=0   disable tracker scrape (see privacy note below)
#   FITGIRL_STREAMING=0  emit results page by page, in page order, with a
#                        single scrape per page (default: stream as resolved)
#   FITGIRL_CACHE=0    disable the on-disk post cache
#
# PRIVACY NOTE: tracker scrape (UDP/HTTP) is performed directly from the plugin
# process. It does NOT go through the proxy / network interface / VPN binding
# configured in qBittorrent, so trackers can see your real IP together with the
# infohashes being queried. Set FITGIRL_SCRAPE=0 (or ENABLE_SCRAPE = False)
# to disable it; seeds/leech will then be reported as -1.

import base64
import collections
import concurrent.futures
import http.client
import json
import logging
import os
import random
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser

try:
    from helpers import retrieve_url
except ImportError:
    retrieve_url = None

# Imported separately: a failure in one must never silence the other.
try:
    from novaprinter import prettyPrinter
except ImportError:
    def prettyPrinter(result):
        print(result)


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ('', '0', 'false', 'no', 'off')


logger = logging.getLogger(__name__)
if _env_flag('FITGIRL_DEBUG', False):
    _debug_handler = logging.StreamHandler(sys.stderr)
    _debug_handler.setFormatter(logging.Formatter('[fitgirl] %(levelname)s %(message)s'))
    logger.addHandler(_debug_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False


DEFAULT_BASE_URL = 'https://fitgirl-repacks.site/'

STRONG_HASH_RE = re.compile(
    r'(?:torrage\.info/torrent\.php\?h=|itorrents\.org/torrent/|'
    r'btcache\.me/torrent/|infohash=|\bbtih:)'
    r'([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])', re.I)
# Bare 40-hex strings (checksums, ids...) are only a last resort.
LOOSE_HASH_RE = re.compile(r'(?<![a-zA-Z0-9])([a-fA-F0-9]{40})(?![a-zA-Z0-9])')
HEX40_RE = re.compile(r'[0-9a-fA-F]{40}')
TORRENT_URL_RE = re.compile(r'\.torrent(?:\?|$)', re.I)

_NUM = r'\d[\d.,]*'
_UNIT = r'([KMGT]?)i?B(?![A-Za-z])'
SIZE_RE = re.compile(r'(' + _NUM + r')\s*(?:/\s*' + _NUM + r'\s*)?' + _UNIT, re.I)
REPACK_SIZE_RE = re.compile(
    r'Repack\s+Size[^\d]*?(' + _NUM + r')\s*(?:/\s*' + _NUM + r'\s*)?' + _UNIT, re.I)
SIZE_MULTIPLIERS = {
    '': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4,
}

BDECODE_MAX_DEPTH = 16


# ----------------------------------------------------------------------
# Small generic helpers
# ----------------------------------------------------------------------

def _remaining(deadline):
    """Seconds left until a monotonic deadline (None = unlimited)."""
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _shutdown(executor):
    """Shut an executor down without waiting for (or running) pending work."""
    try:
        executor.shutdown(wait=False, cancel_futures=True)  # Python >= 3.9
    except TypeError:
        executor.shutdown(wait=False)


def _chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _to_int(value, default=-1):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_text(raw, charset=None):
    try:
        return raw.decode(charset or 'utf-8', errors='replace')
    except LookupError:
        return raw.decode('utf-8', errors='replace')


def _parse_retry_after(value):
    try:
        return max(0.0, min(float(value), 5.0))
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

class Candidate(object):
    """Intermediate representation used between parsing and qBittorrent output.

    size / seeds / leech / pub_date are plain ints (-1 = unknown). They are
    converted to the novaprinter representation only once, in ResultNormalizer.
    """

    __slots__ = (
        'title', 'page_url', 'magnet', 'torrent_url', 'infohash',
        'size', 'pub_date', 'categories', 'seeds', 'leech'
    )

    def __init__(self, title='', page_url='', magnet=None, torrent_url=None,
                 infohash=None, size=-1, pub_date=-1, categories=None,
                 seeds=-1, leech=-1):
        self.title = title or ''
        self.page_url = page_url or ''
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.infohash = infohash
        self.size = _to_int(size)
        self.pub_date = _to_int(pub_date)
        self.categories = tuple(categories or ())
        self.seeds = _to_int(seeds)
        self.leech = _to_int(leech)


# ----------------------------------------------------------------------
# HTML parsing (single parser for search pages and standalone posts)
# ----------------------------------------------------------------------

class _PostState(object):
    """Fields collected for one post (an <article>, or the page itself)."""

    def __init__(self):
        self.title_parts = []
        self.title_link = ''
        self.title_done = False
        self.date_value = ''
        self.categories = []
        self.magnets = []
        self.torrent_urls = []
        self.strong_hashes = []
        self.loose_hashes = []
        self.text_parts = []

    def add_href(self, href):
        href = unescape(href or '').strip()
        if not href:
            return
        if href.lower().startswith('magnet:?'):
            self.magnets.append(href)
        elif TORRENT_URL_RE.search(href):
            self.torrent_urls.append(href)
        self.add_hashes(href)

    def add_hashes(self, value):
        if not value:
            return
        for match in STRONG_HASH_RE.finditer(value):
            self.strong_hashes.append(match.group(1))
        for match in LOOSE_HASH_RE.finditer(value):
            self.loose_hashes.append(match.group(1))

    def has_download_info(self):
        return bool(self.magnets or self.torrent_urls or
                    self.strong_hashes or self.loose_hashes)

    def to_candidate(self, base_url, fallback_title='', fallback_url=''):
        title = clean_text(''.join(self.title_parts)) or clean_text(fallback_title)
        page_url = normalize_url(self.title_link or fallback_url, base_url)
        magnet = self.magnets[0] if self.magnets else None

        # The magnet's own hash wins; then trusted sources; bare hex is last.
        infohash = infohash_from_magnet(magnet) if magnet else None
        if not infohash:
            for value in self.strong_hashes + self.loose_hashes:
                infohash = normalize_infohash(value)
                if infohash:
                    break

        torrent_url = None
        if self.torrent_urls:
            torrent_url = normalize_url(self.torrent_urls[0], page_url or base_url)

        return Candidate(
            title=title,
            page_url=page_url,
            magnet=magnet,
            torrent_url=torrent_url,
            infohash=infohash,
            size=extract_size_from_text(' '.join(self.text_parts)),
            pub_date=parse_pub_date(self.date_value),
            categories=self.categories,
        )


class _PostParser(HTMLParser):
    """Collects every <article> of a page, plus page-level data.

    A single pass accumulates state directly (no HTML re-serialization).
    Only the first entry-title of each post is used. Page-level data (outside
    any <article>) is collected only when ``collect_page`` is True, as a
    fallback for standalone pages without <article>.
    """

    def __init__(self, collect_page=False):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.collect_page = collect_page
        self.articles = []
        self.page = _PostState()
        self.og_title = ''
        self.canonical = ''
        self._cur = None
        self._depth = 0
        self._skip = 0
        self._in_title = False
        self._title_tag = None
        self._title_state = None
        self._cat_open = False

    def _state(self):
        if self._cur is not None:
            return self._cur
        return self.page if self.collect_page else None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)

        if tag in ('script', 'style'):
            self._skip += 1
            return

        if tag == 'article':
            if self._cur is None:
                self._cur = _PostState()
                self._depth = 1
            else:
                self._depth += 1
            return

        rel = (attrs.get('rel') or '').lower().split()
        if tag == 'meta' and (attrs.get('property') or '').lower() == 'og:title':
            if not self.og_title:
                self.og_title = attrs.get('content') or ''
        if tag == 'link' and 'canonical' in rel and not self.canonical:
            self.canonical = attrs.get('href') or ''

        state = self._state()
        if state is None:
            return

        classes = set((attrs.get('class') or '').lower().split())

        if (tag in ('h1', 'h2') and 'entry-title' in classes
                and not state.title_done and not self._in_title):
            self._in_title = True
            self._title_tag = tag
            self._title_state = state
            state.title_parts = []
            return

        if self._in_title and tag == 'a':
            href = attrs.get('href')
            if href and not self._title_state.title_link:
                self._title_state.title_link = href

        if tag == 'time' and 'entry-date' in classes and not state.date_value:
            state.date_value = attrs.get('datetime') or attrs.get('data-datetime') or ''

        if tag == 'a' and 'category' in rel and 'tag' in rel:
            self._cat_open = True

        state.add_href(attrs.get('href'))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('script', 'style'):
            self._skip = max(0, self._skip - 1)
            return

        if self._in_title and tag == self._title_tag:
            self._in_title = False
            self._title_state.title_done = True
        elif tag == 'a' and self._cat_open:
            self._cat_open = False

        if tag == 'article' and self._cur is not None:
            self._depth -= 1
            if self._depth <= 0:
                if self._in_title:
                    self._in_title = False
                    self._title_state.title_done = True
                self._cat_open = False
                self.articles.append(self._cur)
                self._cur = None
                self._depth = 0

    def handle_data(self, data):
        if not data or self._skip:
            return
        state = self._state()
        if state is None:
            return
        if self._in_title:
            self._title_state.title_parts.append(data)
        if self._cat_open:
            value = data.strip()
            if value:
                state.categories.append(value)
        state.text_parts.append(data)
        state.add_hashes(data)

    def finish(self):
        """Flush a truncated trailing <article>."""
        if self._cur is not None:
            self.articles.append(self._cur)
            self._cur = None


class SearchParser(object):
    """Turns HTML into Candidate objects without network access."""

    # Matched against the whole lowercase title...
    IGNORED_EXACT_TITLES = ('faq', 'donate')
    # ...or as a title prefix.
    IGNORED_TITLE_PREFIXES = ('updates digest', 'updates list', 'day of requests')
    IGNORED_CATEGORIES = ('updates digest',)

    PAGE_RE = re.compile(
        r'''class=["'][^"']*page-numbers[^"']*["'][^>]*>\s*(\d+)\s*</a>''', re.I)

    def parse(self, html, base_url, query=''):
        parser = _PostParser(collect_page=False)
        try:
            parser.feed(html or '')
            parser.close()
        except Exception as exc:
            logger.debug('Search HTML parse failed: %s', exc)
        parser.finish()

        results = []
        for state in parser.articles:
            try:
                candidate = state.to_candidate(base_url)
            except Exception as exc:
                logger.debug('Article conversion failed: %s', exc)
                continue
            if self.is_valid(candidate):
                candidate.title = ensure_query_in_title(candidate.title, query)
                results.append(candidate)
        return results

    def parse_document(self, html, url, query=''):
        """Parse a standalone post (e.g. WordPress redirecting an exact search)."""
        parser = _PostParser(collect_page=True)
        try:
            parser.feed(html or '')
            parser.close()
        except Exception as exc:
            logger.debug('Document parse failed: %s', exc)
        parser.finish()

        try:
            fallback_url = parser.canonical or url
            if parser.articles:
                # Only the first article: related posts / sidebars are ignored.
                main = parser.articles[0]
                candidate = main.to_candidate(url, parser.og_title, fallback_url)
                if not main.has_download_info() and parser.page.has_download_info():
                    merge_candidates(
                        candidate, parser.page.to_candidate(url, '', fallback_url))
            else:
                candidate = parser.page.to_candidate(url, parser.og_title, fallback_url)
        except Exception as exc:
            logger.debug('Document conversion failed: %s', exc)
            return None

        if not self.is_valid(candidate):
            return None
        candidate.title = ensure_query_in_title(candidate.title, query)
        return candidate

    def total_pages(self, html):
        pages = self.PAGE_RE.findall(html or '')
        try:
            return max([1] + [int(value) for value in pages])
        except (ValueError, TypeError):
            return 1

    def is_valid(self, candidate):
        if not candidate or not candidate.title:
            return False
        title = candidate.title.lower().strip()
        if title in self.IGNORED_EXACT_TITLES:
            return False
        if title.startswith(self.IGNORED_TITLE_PREFIXES):
            return False
        if any(category.lower() in self.IGNORED_CATEGORIES
               for category in candidate.categories):
            return False
        return bool(candidate.page_url)


# ----------------------------------------------------------------------
# Text / URL / hash helpers
# ----------------------------------------------------------------------

def clean_text(value):
    value = unescape(value or '')
    value = value.replace('|', '-')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def normalize_url(value, base_url=DEFAULT_BASE_URL):
    if not value:
        return ''
    return urllib.parse.urljoin(base_url, unescape(str(value)).strip())


def normalize_infohash(value):
    """Return a lowercase 40-hex infohash (base32 is converted), or None."""
    if not value:
        return None
    value = unescape(str(value)).strip()
    match = re.search(r'(?<![a-zA-Z0-9])([a-fA-F0-9]{40})(?![a-zA-Z0-9])', value)
    if match:
        return match.group(1).lower()
    match = re.search(r'(?<![a-zA-Z0-9])([a-zA-Z2-7]{32})(?![a-zA-Z0-9])', value)
    if match:
        try:
            return base64.b32decode(match.group(1).upper()).hex().lower()
        except Exception:
            return None
    return None


def infohash_from_magnet(magnet):
    if not magnet:
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
        for value in query.get('xt') or []:
            match = re.search(r'urn:btih:([a-zA-Z0-9]+)', value, re.I)
            if match:
                return normalize_infohash(match.group(1))
    except (ValueError, TypeError):
        pass
    return None


def ensure_query_in_title(title, query):
    title = clean_text(title)
    query = clean_text(query)
    if not title or not query:
        return title

    def compact(value):
        return re.sub(r'[^a-zA-Z0-9]+', '', value).lower()

    if compact(query) and compact(query) in compact(title) and query.lower() not in title.lower():
        return '%s [%s]' % (title, query)
    return title


def build_magnet(infohash, title):
    if not infohash:
        return None
    return 'magnet:?xt=urn:btih:%s&dn=%s' % (
        infohash.lower(),
        urllib.parse.quote(title or '', safe='')
    )


def normalize_magnet(magnet):
    if not magnet:
        return ''
    try:
        parsed = urllib.parse.urlsplit(magnet)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = sorted(query, key=lambda item: (item[0].lower(), item[1]))
        return urllib.parse.urlunsplit((
            parsed.scheme.lower(), parsed.netloc, parsed.path,
            urllib.parse.urlencode(query, doseq=True), parsed.fragment
        ))
    except (ValueError, TypeError):
        return magnet.strip()


def enrich_magnet(magnet, trackers):
    """Append missing &tr= parameters, leaving the original magnet untouched."""
    if not magnet or not magnet.lower().startswith('magnet:'):
        return magnet
    try:
        base, sep, fragment = magnet.partition('#')
        existing = set(
            value.strip()
            for key, value in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(base).query, keep_blank_values=True)
            if key.lower() == 'tr'
        )
        extra = ''
        for tracker in trackers:
            if tracker and tracker not in existing:
                existing.add(tracker)
                extra += '&tr=' + urllib.parse.quote(tracker, safe='')
        return base + extra + sep + fragment
    except (ValueError, TypeError) as exc:
        logger.debug('Magnet enrichment failed: %s', exc)
        return magnet


PUB_DATE_FORMATS = (
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%d %H:%M:%S%z',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d',
)


def parse_pub_date(value):
    if not value:
        return -1
    text = str(value).strip()
    candidates = [text, text.replace('Z', '+00:00')]
    for item in candidates:
        try:
            dt = datetime.fromisoformat(item)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    for fmt in PUB_DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError, OverflowError):
            continue
    return -1


# ----------------------------------------------------------------------
# Sizes (always ints internally; -1 = unknown)
# ----------------------------------------------------------------------

def _parse_number(text):
    """Parse '8.2', '8,2', '1,234.5', '1.234,5' or '1.234.567' as a Decimal."""
    text = text.strip().rstrip('.,')
    if ',' in text and '.' in text:
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        if re.fullmatch(r'\d{1,3}(?:,\d{3})+', text):
            text = text.replace(',', '')
        else:
            text = text.replace(',', '.')
    elif text.count('.') > 1:
        text = text.replace('.', '')
    return Decimal(text)


def _match_to_bytes(match):
    try:
        number = _parse_number(match.group(1))
    except (InvalidOperation, ValueError):
        return -1
    return int(number * SIZE_MULTIPLIERS[match.group(2).upper()])


def size_to_bytes(value):
    """Convert '5.2 GB', '8.2/10.6 GB', '123' or an int into bytes (-1 if unknown)."""
    if value is None:
        return -1
    if isinstance(value, int):
        return value if value >= 0 else -1
    text = str(value).strip()
    if not text or text == '-1':
        return -1
    if re.fullmatch(r'\d+', text):
        return int(text)
    match = SIZE_RE.search(text)
    return _match_to_bytes(match) if match else -1


def extract_size_from_text(text):
    if not text:
        return -1
    match = REPACK_SIZE_RE.search(text)
    return _match_to_bytes(match) if match else -1


def same_site(url, base_url):
    try:
        return (urllib.parse.urlsplit(url).netloc.lower() ==
                urllib.parse.urlsplit(base_url).netloc.lower())
    except (ValueError, AttributeError):
        return False


def announce_to_scrape_path(path):
    """BEP 48: replace the last path segment 'announce*' by 'scrape*'."""
    head, sep, last = path.rpartition('/')
    if last.startswith('announce'):
        return head + sep + 'scrape' + last[len('announce'):]
    return None


def bdecode(data, index=0, depth=0):
    """Minimal bencode decoder for tracker scrape responses (depth-limited)."""
    if depth > BDECODE_MAX_DEPTH:
        raise ValueError('Bencode nesting too deep')
    if index >= len(data):
        raise ValueError('Unexpected end of bencode')
    token = data[index:index + 1]
    if token == b'i':
        end = data.index(b'e', index + 1)
        return int(data[index + 1:end]), end + 1
    if token == b'l':
        values = []
        index += 1
        while data[index:index + 1] != b'e':
            value, index = bdecode(data, index, depth + 1)
            values.append(value)
        return values, index + 1
    if token == b'd':
        result = {}
        index += 1
        while data[index:index + 1] != b'e':
            key, index = bdecode(data, index, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError('Bencode dict key is not a string')
            value, index = bdecode(data, index, depth + 1)
            result[key] = value
        return result, index + 1
    if token.isdigit():
        colon = data.index(b':', index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        if end > len(data):
            raise ValueError('Bencode string exceeds data')
        return data[start:end], end
    raise ValueError('Invalid bencode token')


def merge_candidates(target, source):
    """Fill missing fields of ``target`` from ``source`` (never overwrites)."""
    for field in ('title', 'page_url', 'magnet', 'torrent_url', 'infohash'):
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if target.size < 0 <= source.size:
        target.size = source.size
    if target.pub_date == -1 and source.pub_date != -1:
        target.pub_date = source.pub_date
    if not target.categories and source.categories:
        target.categories = source.categories
    if target.seeds < 0 <= source.seeds:
        target.seeds = source.seeds
    if target.leech < 0 <= source.leech:
        target.leech = source.leech
    return target


# ----------------------------------------------------------------------
# HTTP boundary
# ----------------------------------------------------------------------

class HttpClient(object):
    """URL validation, timeouts, gzip, size cap, backoff and global deadline."""

    def __init__(self, headers=None, timeout=8, retries=1,
                 max_bytes=4 * 1024 * 1024, retry_delay=0.5):
        merged = {'Accept-Encoding': 'gzip'}
        merged.update(headers or {})
        self.headers = merged
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.max_bytes = int(max_bytes)
        self.retry_delay = retry_delay

    @staticmethod
    def _sleep(seconds, deadline):
        remaining = _remaining(deadline)
        if remaining is not None:
            seconds = min(seconds, remaining - 0.1)
        if seconds > 0:
            time.sleep(seconds)

    def _decompress(self, raw, encoding):
        if raw and encoding and 'gzip' in encoding.lower():
            try:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                return decoder.decompress(raw, self.max_bytes)
            except zlib.error as exc:
                logger.debug('gzip decode failed: %s', exc)
                return b''
        return raw

    def _request(self, url, timeout=None, deadline=None, retries=None):
        """Return (raw, final_url, charset, network_failure)."""
        url = str(url or '').strip()
        try:
            if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
                return b'', url, None, False
        except ValueError:
            return b'', url, None, False

        attempts = (self.retries if retries is None else max(0, int(retries))) + 1
        base_timeout = self.timeout if timeout is None else timeout
        network_failure = False

        for attempt in range(attempts):
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0.1:
                break
            current = base_timeout if remaining is None else min(base_timeout, remaining)
            delay = self.retry_delay * (2 ** attempt)
            try:
                request = urllib.request.Request(url, headers=self.headers, method='GET')
                with urllib.request.urlopen(request, timeout=current) as response:
                    raw = response.read(self.max_bytes)
                    encoding = response.headers.get('Content-Encoding')
                    charset = response.headers.get_content_charset()
                    final_url = response.geturl()
                return self._decompress(raw, encoding), final_url, charset, False
            except urllib.error.HTTPError as exc:
                logger.debug('HTTP %s for %s, attempt %d', exc.code, url, attempt + 1)
                if exc.code in (408, 429) or exc.code >= 500:
                    if exc.code != 429:
                        network_failure = True
                    retry_after = _parse_retry_after(
                        exc.headers.get('Retry-After') if exc.headers else None)
                    if retry_after is not None:
                        delay = retry_after
                else:
                    # 3xx/4xx: definitive, no retry and no fallback request.
                    return b'', url, None, False
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                logger.debug('HTTP/network error for %s, attempt %d: %s', url, attempt + 1, exc)
                network_failure = True
            except ValueError as exc:
                logger.debug('Invalid request for %s: %s', url, exc)
                return b'', url, None, False

            if attempt + 1 < attempts:
                self._sleep(min(delay, 5.0), deadline)

        return b'', url, None, network_failure

    def get_bytes(self, url, timeout=None, deadline=None, retries=None):
        raw, final_url, _, _ = self._request(url, timeout, deadline, retries)
        return raw, final_url

    def get(self, url, timeout=None, deadline=None):
        raw, final_url, charset, network_failure = self._request(url, timeout, deadline)
        if raw:
            return _decode_text(raw, charset), final_url

        # qBittorrent's helper is only worth trying after a network/5xx failure.
        if network_failure and retrieve_url is not None:
            remaining = _remaining(deadline)
            if remaining is None or remaining > 1:
                try:
                    data = retrieve_url(url)
                    if data:
                        if isinstance(data, bytes):
                            data = _decode_text(data)
                        return data, url
                except Exception as exc:
                    logger.debug('retrieve_url fallback failed for %s: %s', url, exc)
        return '', final_url


# ----------------------------------------------------------------------
# On-disk cache of resolved posts
# ----------------------------------------------------------------------

class PageCache(object):
    """Small JSON cache keyed by post URL (magnet/hash/size/date only).

    Live swarm stats are never cached. Failures are silent: the plugin works
    exactly the same without a writable cache location.
    """

    def __init__(self, path=None, ttl=3 * 24 * 3600, max_entries=500, enabled=True):
        self.path = path
        self.ttl = ttl
        self.max_entries = max_entries
        self.enabled = bool(enabled and path)
        self._lock = threading.Lock()
        self._data = None
        self._dirty = False

    def _ensure_loaded(self):
        if self._data is not None:
            return
        self._data = {}
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._data = data
        except (OSError, ValueError):
            pass

    def _fresh(self, entry):
        try:
            return isinstance(entry, dict) and \
                time.time() - float(entry.get('t', 0)) <= self.ttl
        except (TypeError, ValueError):
            return False

    def get(self, key):
        if not self.enabled or not key:
            return None
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(key)
        if not self._fresh(entry):
            return None
        magnet = entry.get('magnet')
        if not (isinstance(magnet, str) and magnet.lower().startswith('magnet:')):
            magnet = None
        infohash = entry.get('infohash')
        if not (isinstance(infohash, str) and HEX40_RE.fullmatch(infohash)):
            infohash = None
        if not (magnet or infohash):
            return None
        torrent_url = entry.get('torrent_url')
        return Candidate(
            page_url=key, magnet=magnet, infohash=infohash,
            torrent_url=torrent_url if isinstance(torrent_url, str) else None,
            size=entry.get('size', -1), pub_date=entry.get('pub_date', -1),
        )

    def put(self, key, candidate):
        if not self.enabled or not key or not candidate:
            return
        if not (candidate.magnet or candidate.infohash):
            return
        entry = {
            't': time.time(),
            'magnet': candidate.magnet,
            'infohash': candidate.infohash,
            'torrent_url': candidate.torrent_url,
            'size': candidate.size,
            'pub_date': candidate.pub_date,
        }
        with self._lock:
            self._ensure_loaded()
            self._data[key] = entry
            self._dirty = True

    def save(self):
        if not self.enabled:
            return
        with self._lock:
            if not self._dirty or self._data is None:
                return
            items = [(k, v) for k, v in self._data.items() if self._fresh(v)]
            items.sort(key=lambda kv: kv[1].get('t', 0), reverse=True)
            data = dict(items[:self.max_entries])
            tmp_path = '%s.%d.tmp' % (self.path, os.getpid())
            try:
                with open(tmp_path, 'w', encoding='utf-8') as handle:
                    json.dump(data, handle)
                os.replace(tmp_path, self.path)
                self._data = data
                self._dirty = False
            except (OSError, TypeError, ValueError) as exc:
                logger.debug('Cache save failed: %s', exc)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


# ----------------------------------------------------------------------
# Tracker scrape (batched: one request per tracker for all infohashes)
# ----------------------------------------------------------------------

class TrackerStatsResolver(object):
    """Best-effort BitTorrent tracker scrape.

    FitGirl pages do not expose live swarm counts, so each tracker is asked once
    (BEP 15 for UDP, BEP 48 for HTTP) for all infohashes of a result page.
    Failure is non-fatal: qBittorrent receives -1 when nothing trustworthy is
    obtained. DNS resolution cannot be interrupted by socket timeouts, so each
    tracker runs in its own thread and the overall wait is bounded instead.
    """

    UDP_PROTOCOL_ID = 0x41727101980
    UDP_CONNECT_ACTION = 0
    UDP_SCRAPE_ACTION = 2
    UDP_ERROR_ACTION = 3
    UDP_MAX_HASHES = 74
    HTTP_MAX_HASHES = 40
    SEEDS_ENOUGH = 10

    def __init__(self, http_client, trackers, timeout=1.2, max_trackers=4,
                 max_workers=8, enabled=True):
        self.http = http_client
        self.trackers = tuple(trackers or ())
        self.timeout = max(0.8, float(timeout))
        self.max_trackers = max(1, int(max_trackers))
        self.max_workers = max(1, int(max_workers))
        self.enabled = bool(enabled)

    def resolve_many(self, candidates, deadline=None):
        if not self.enabled or not candidates:
            return candidates

        by_hash = {}
        for candidate in candidates:
            infohash = normalize_infohash(
                candidate.infohash or infohash_from_magnet(candidate.magnet))
            if infohash and HEX40_RE.fullmatch(infohash):
                candidate.infohash = infohash
                by_hash.setdefault(infohash, []).append(candidate)
        if not by_hash:
            return candidates

        trackers = self._select_trackers(candidates)
        if not trackers:
            return candidates

        wait = self.timeout * 2 + 0.5
        remaining = _remaining(deadline)
        if remaining is not None:
            wait = min(wait, remaining)
        if wait <= 0:
            return candidates

        infohashes = list(by_hash)
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(trackers)))
        futures = [executor.submit(self._scrape, tracker, infohashes)
                   for tracker in trackers]
        best = {}
        try:
            for future in concurrent.futures.as_completed(futures, timeout=wait):
                try:
                    stats = future.result()
                except Exception as exc:
                    logger.debug('Tracker scrape failed: %s', exc)
                    continue
                for infohash, (seeds, leech) in stats.items():
                    current = best.get(infohash)
                    if current is None or (seeds, leech) > current:
                        best[infohash] = (seeds, leech)
                if all(best.get(h, (-1, -1))[0] >= self.SEEDS_ENOUGH for h in infohashes):
                    break
        except concurrent.futures.TimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()
            _shutdown(executor)

        for infohash, (seeds, leech) in best.items():
            for candidate in by_hash.get(infohash, ()):
                candidate.seeds = seeds
                candidate.leech = leech
        return candidates

    def _select_trackers(self, candidates):
        counter = collections.Counter()
        for candidate in candidates:
            if not candidate.magnet:
                continue
            try:
                query = urllib.parse.urlsplit(candidate.magnet).query
                for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
                    if key.lower() == 'tr' and value:
                        counter[unescape(value).strip()] += 1
            except (ValueError, TypeError):
                continue

        ordered = [tracker for tracker, _ in counter.most_common()]
        ordered.extend(self.trackers)

        seen = set()
        result = []
        for tracker in ordered:
            tracker = unescape(str(tracker)).strip()
            key = tracker.lower()
            if not tracker or key in seen:
                continue
            try:
                scheme = urllib.parse.urlsplit(tracker).scheme.lower()
            except ValueError:
                continue
            if scheme not in ('http', 'https', 'udp'):
                continue
            seen.add(key)
            result.append(tracker)
            if len(result) >= self.max_trackers:
                break
        return result

    def _scrape(self, tracker, infohashes):
        try:
            scheme = urllib.parse.urlsplit(tracker).scheme.lower()
            if scheme in ('http', 'https'):
                return self._scrape_http(tracker, infohashes)
            if scheme == 'udp':
                return self._scrape_udp(tracker, infohashes)
        except Exception as exc:
            logger.debug('Scrape error for %s: %s', tracker, exc)
        return {}

    @staticmethod
    def _first_int(entry, keys):
        for key in keys:
            value = entry.get(key)
            if isinstance(value, int):
                return value
        return -1

    def _scrape_http(self, tracker, infohashes):
        parsed = urllib.parse.urlsplit(tracker)
        path = announce_to_scrape_path(parsed.path or '/announce')
        if path is None:
            return {}  # tracker URL has no announce segment: scrape unsupported

        extra = [(k, v) for k, v in
                 urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() != 'info_hash']
        base_parts = [urllib.parse.quote(k, safe='') + '=' + urllib.parse.quote(v, safe='')
                      for k, v in extra]

        stats = {}
        for chunk in _chunks(infohashes, self.HTTP_MAX_HASHES):
            parts = list(base_parts)
            for infohash in chunk:
                parts.append('info_hash=' + urllib.parse.quote_from_bytes(
                    bytes.fromhex(infohash), safe=''))
            scrape_url = urllib.parse.urlunsplit((
                parsed.scheme, parsed.netloc, path, '&'.join(parts), ''))

            raw, _ = self.http.get_bytes(scrape_url, timeout=self.timeout, retries=0)
            if not raw or b'5:files' not in raw:
                continue
            try:
                decoded, _ = bdecode(raw, 0)
            except (ValueError, TypeError, IndexError):
                continue
            files = decoded.get(b'files', {}) if isinstance(decoded, dict) else {}
            if not isinstance(files, dict):
                continue
            for infohash in chunk:
                entry = files.get(bytes.fromhex(infohash))
                if not isinstance(entry, dict):
                    continue
                # BEP 48 keys: complete (seeds) and incomplete (leechers)
                seeds = self._first_int(entry, (b'complete', b'seeds', b'seeders'))
                leech = self._first_int(entry, (b'incomplete', b'peers', b'leechers'))
                if seeds >= 0 and leech >= 0:
                    stats[infohash] = (seeds, leech)
        return stats

    def _scrape_udp(self, tracker, infohashes):
        try:
            parsed = urllib.parse.urlsplit(tracker)
            host = parsed.hostname
            port = parsed.port or 6969
        except ValueError:
            return {}
        if not host:
            return {}

        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
        except OSError:
            return {}
        if not infos:
            return {}
        infos.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
        family, socktype, proto, _, address = infos[0]

        stats = {}
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(self.timeout)
        try:
            transaction = random.getrandbits(32)
            sock.sendto(struct.pack('!QII', self.UDP_PROTOCOL_ID,
                                    self.UDP_CONNECT_ACTION, transaction), address)
            data, _ = sock.recvfrom(2048)
            if len(data) < 16:
                return stats
            action, response_transaction = struct.unpack('!II', data[:8])
            if action != self.UDP_CONNECT_ACTION or response_transaction != transaction:
                return stats  # includes action 3 (tracker error)
            connection_id = data[8:16]

            # The connection id stays valid ~60 s: reuse it for every chunk.
            for chunk in _chunks(infohashes, self.UDP_MAX_HASHES):
                scrape_transaction = random.getrandbits(32)
                request = (connection_id +
                           struct.pack('!II', self.UDP_SCRAPE_ACTION, scrape_transaction) +
                           b''.join(bytes.fromhex(h) for h in chunk))
                sock.sendto(request, address)
                data, _ = sock.recvfrom(2048)
                if len(data) < 8:
                    break
                action, response_transaction = struct.unpack('!II', data[:8])
                if action == self.UDP_ERROR_ACTION or \
                        response_transaction != scrape_transaction or \
                        action != self.UDP_SCRAPE_ACTION:
                    break
                count = min((len(data) - 8) // 12, len(chunk))
                for position in range(count):
                    seeders, _completed, leechers = struct.unpack_from(
                        '!III', data, 8 + 12 * position)
                    stats[chunk[position]] = (int(seeders), int(leechers))
        except (OSError, ValueError, struct.error):
            pass
        finally:
            sock.close()
        return stats


# ----------------------------------------------------------------------
# Resolver / normalizer / deduplicator
# ----------------------------------------------------------------------

class DownloadResolver(object):
    """Resolves a Candidate into a usable download link (no swarm stats)."""

    def __init__(self, http_client, trackers, parser=None, cache=None, post_timeout=6):
        self.http = http_client
        self.trackers = tuple(trackers or ())
        self.parser = parser or SearchParser()
        self.cache = cache
        self.post_timeout = post_timeout

    def _finalize(self, candidate):
        """Return the candidate with a magnet link, or None if it has no hash/magnet."""
        if candidate.magnet:
            candidate.infohash = infohash_from_magnet(candidate.magnet) or candidate.infohash
            candidate.magnet = enrich_magnet(candidate.magnet, self.trackers)
            return candidate
        if candidate.infohash:
            candidate.magnet = enrich_magnet(
                build_magnet(candidate.infohash, candidate.title), self.trackers)
            return candidate
        return None

    def resolve(self, candidate, query='', deadline=None):
        if not candidate:
            return None

        ready = self._finalize(candidate)
        if ready:
            return ready

        if candidate.page_url:
            cached = self.cache.get(candidate.page_url) if self.cache else None
            if cached:
                merge_candidates(candidate, cached)
                ready = self._finalize(candidate)
                if ready:
                    return ready

            html, final_url = self.http.get(
                candidate.page_url, timeout=self.post_timeout, deadline=deadline)
            if html:
                parsed = self.parser.parse_document(html, final_url, query or candidate.title)
                if parsed:
                    if self.cache:
                        self.cache.put(candidate.page_url, parsed)
                    merge_candidates(candidate, parsed)
                    ready = self._finalize(candidate)
                    if ready:
                        return ready

        return candidate if candidate.torrent_url else None


class ResultNormalizer(object):
    """Converts Candidate objects into the qBittorrent novaprinter schema."""

    def __init__(self, engine_url=DEFAULT_BASE_URL):
        self.engine_url = engine_url

    def normalize(self, candidate):
        if not candidate:
            return None
        link = candidate.magnet or candidate.torrent_url
        if not link:
            return None
        return {
            'link': link,
            'name': candidate.title,
            'size': str(candidate.size if candidate.size >= 0 else -1),
            'seeds': str(candidate.seeds if candidate.seeds >= 0 else -1),
            'leech': str(candidate.leech if candidate.leech >= 0 else -1),
            'engine_url': self.engine_url,
            'desc_link': candidate.page_url,
            'pub_date': candidate.pub_date,
        }


class Deduplicator(object):
    """Deduplicates by the strongest stable identity available."""

    def __init__(self):
        self.seen = set()

    @staticmethod
    def key(candidate):
        infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
        if infohash:
            return 'hash:' + infohash.lower()
        if candidate.magnet:
            return 'magnet:' + normalize_magnet(candidate.magnet)
        if candidate.torrent_url:
            return 'torrent:' + normalize_url(candidate.torrent_url)
        return 'page:' + normalize_url(candidate.page_url)

    def add(self, candidate):
        key = self.key(candidate)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


# ----------------------------------------------------------------------
# qBittorrent plugin entry point
# ----------------------------------------------------------------------

class fitgirl_repacks(object):
    url = DEFAULT_BASE_URL
    name = 'FitGirl Repacks'
    supported_categories = {'all': '', 'games': ''}

    # Tried in order on the first search page; the first one that answers wins.
    MIRRORS = (url,)

    # Verify these from time to time: public trackers come and go.
    DEFAULT_TRACKERS = (
        'udp://tracker.opentrackr.org:1337/announce',
        'udp://open.stealth.si:80/announce',
        'udp://tracker.torrent.eu.org:451/announce',
        'udp://exodus.desync.com:6969/announce',
        'udp://tracker.dler.org:6969/announce',
    )

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    MAX_RESULTS = 30
    MAX_PAGES = 3
    MAX_WORKERS = 8
    SEARCH_TIMEOUT = 8
    POST_TIMEOUT = 6
    MAX_RETRIES = 1
    SEARCH_DEADLINE = 40            # hard cap (seconds) for a whole search
    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    ENABLE_SCRAPE = _env_flag('FITGIRL_SCRAPE', True)
    # True : results are printed as soon as each one is resolved. With scrape
    #        enabled they are grouped in small batches (STREAM_BATCH_SIZE
    #        results, or STREAM_BATCH_WAIT seconds after the first pending one)
    #        so that each tracker is still asked once per batch.
    # False: every page is resolved first, scraped once and printed in the
    #        order of the search page (fewest requests, highest latency).
    STREAMING = _env_flag('FITGIRL_STREAMING', True)
    STREAM_BATCH_SIZE = 6
    STREAM_BATCH_WAIT = 1.5
    ENABLE_CACHE = _env_flag('FITGIRL_CACHE', True)
    CACHE_TTL = 3 * 24 * 3600       # FitGirl updates change infohashes: keep it short
    CACHE_PATH = os.path.join(tempfile.gettempdir(), 'qbt_fitgirl_repacks_cache.json')

    def __init__(self):
        self.http = HttpClient(
            headers=self.headers,
            timeout=self.SEARCH_TIMEOUT,
            retries=self.MAX_RETRIES,
            max_bytes=self.MAX_RESPONSE_BYTES,
        )
        self.parser = SearchParser()
        self.cache = PageCache(self.CACHE_PATH, self.CACHE_TTL, enabled=self.ENABLE_CACHE)
        self.resolver = DownloadResolver(
            self.http, self.DEFAULT_TRACKERS, self.parser, self.cache,
            post_timeout=self.POST_TIMEOUT)
        self.stats = TrackerStatsResolver(
            self.http, self.DEFAULT_TRACKERS, enabled=self.ENABLE_SCRAPE)
        self.normalizer = ResultNormalizer(self.url)

    # ------------------------------------------------------------------
    # Public plugin API
    # ------------------------------------------------------------------

    def search(self, what, cat='all'):
        query = urllib.parse.unquote(what or '').strip()
        if not query or cat not in self.supported_categories:
            return

        deadline = time.monotonic() + self.SEARCH_DEADLINE
        try:
            self._search(query, deadline)
        finally:
            self.cache.save()

    def _search(self, query, deadline):
        deduplicator = Deduplicator()
        emitted = 0
        base = self.url

        for page in range(1, self.MAX_PAGES + 1):
            remaining = _remaining(deadline)
            if emitted >= self.MAX_RESULTS or remaining <= 0.5:
                break

            html, final_url = '', ''
            for mirror in (self.MIRRORS if page == 1 else (base,)):
                html, final_url = self.http.get(
                    self.build_search_url(query, page, mirror),
                    timeout=self.SEARCH_TIMEOUT, deadline=deadline)
                if html:
                    base = mirror
                    break
            if not html:
                logger.debug('No response for search page %d of %r', page, query)
                break

            # Exact WordPress matches can redirect directly to a post.
            if not self.is_search_result_page(final_url):
                candidate = self.parser.parse_document(html, final_url, query)
                candidates = [candidate] if candidate else []
                total_pages = 1
            else:
                candidates = self.parser.parse(html, final_url, query)
                total_pages = min(self.parser.total_pages(html), self.MAX_PAGES)

            if not candidates:
                break

            budget = self.MAX_RESULTS - emitted
            candidates = candidates[:budget]
            if self.STREAMING:
                emitted += self._process_streaming(
                    candidates, query, deadline, deduplicator, budget)
            else:
                emitted += self._process_ordered(
                    candidates, query, deadline, deduplicator, budget)

            if page >= total_pages or not same_site(final_url, base):
                break

    def build_search_url(self, query, page=1, base=None):
        base = base or self.url
        encoded = urllib.parse.quote(query)
        if page <= 1:
            return base + '?s=' + encoded
        return base + 'page/%d/?s=%s' % (page, encoded)

    def _emit(self, candidate):
        result = self.normalizer.normalize(candidate)
        if not result:
            return False
        prettyPrinter(result)
        return True

    def _process_ordered(self, candidates, query, deadline, deduplicator, budget):
        """Resolve the whole page, scrape once, print in search-page order."""
        fresh = [candidate
                 for candidate in self.resolve_candidates(candidates, query, deadline)
                 if deduplicator.add(candidate)]
        self.stats.resolve_many(fresh, deadline)

        emitted = 0
        for candidate in fresh:
            if emitted >= budget:
                break
            if self._emit(candidate):
                emitted += 1
        return emitted

    def _process_streaming(self, candidates, query, deadline, deduplicator, budget):
        """Print results as they are resolved.

        With scrape enabled, resolved candidates wait in a small batch that is
        flushed when it reaches STREAM_BATCH_SIZE, when STREAM_BATCH_WAIT
        seconds have passed since its first member, or when nothing else is
        pending. Workers keep resolving while a batch is being scraped.
        """
        use_scrape = self.stats.enabled
        emitted = 0
        batch = []
        batch_started = 0.0

        def flush():
            nonlocal emitted, batch
            if not batch:
                return
            items, batch = batch, []
            if use_scrape:
                self.stats.resolve_many(items, deadline)
            for item in items:
                if emitted >= budget:
                    break
                if self._emit(item):
                    emitted += 1

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.MAX_WORKERS, len(candidates)))
        try:
            pending = set(executor.submit(self.resolver.resolve, candidate, query, deadline)
                          for candidate in candidates)
            while pending and emitted < budget:
                timeout = _remaining(deadline)
                if timeout is not None and timeout <= 0:
                    break
                if batch:
                    due = max(0.0, batch_started + self.STREAM_BATCH_WAIT - time.monotonic())
                    timeout = due if timeout is None else min(timeout, due)

                done, pending = concurrent.futures.wait(
                    pending, timeout=timeout,
                    return_when=concurrent.futures.FIRST_COMPLETED)

                for future in done:
                    try:
                        candidate = future.result()
                    except Exception as exc:
                        logger.debug('Candidate resolution failed: %s', exc)
                        continue
                    if candidate and deduplicator.add(candidate):
                        if not batch:
                            batch_started = time.monotonic()
                        batch.append(candidate)

                if batch and (not use_scrape
                              or len(batch) >= self.STREAM_BATCH_SIZE
                              or time.monotonic() - batch_started >= self.STREAM_BATCH_WAIT):
                    flush()
            flush()
        finally:
            _shutdown(executor)
        return emitted

    def resolve_candidates(self, candidates, query='', deadline=None):
        """Resolve in parallel; results keep the order of the search page."""
        if not candidates:
            return []

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.MAX_WORKERS, len(candidates)))
        try:
            futures = [executor.submit(self.resolver.resolve, candidate, query, deadline)
                       for candidate in candidates]
            wait_time = _remaining(deadline)
            done, _ = concurrent.futures.wait(
                futures, timeout=None if wait_time is None else max(0.0, wait_time))
        finally:
            _shutdown(executor)

        results = []
        for future in futures:
            if future not in done or future.cancelled():
                continue
            try:
                candidate = future.result()
            except Exception as exc:
                logger.debug('Candidate resolution failed: %s', exc)
                continue
            if candidate:
                results.append(candidate)
        return results

    def is_search_result_page(self, final_url):
        # A redirected post normally has no '?s=' search parameter.
        parsed = urllib.parse.urlsplit(final_url or '')
        return 's' in urllib.parse.parse_qs(parsed.query)