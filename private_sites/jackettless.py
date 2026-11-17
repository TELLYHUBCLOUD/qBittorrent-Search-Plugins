# VERSION: 1.01
# AUTHORS: gusperroni (https://github.com/gusperroni/jackettless)

# LICENSING INFORMATION
# SPDX-License-Identifier: GPL-2.0-or-later
#
# jackettless - serverless Jackett search plugin for qBittorrent
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# This plugin is a Python port of the "Cardigann" definition interpreter of
# Jackett (https://github.com/Jackett/Jackett, GPL-2.0). The indexer definitions
# are written and maintained by the Jackett contributors; they are downloaded at
# runtime from the Jackett repository and are not bundled with this file.

"""jackettless - serverless Jackett for qBittorrent.

A single-file qBittorrent search plugin (nova3 engine) that reads Jackett's
YAML indexer definitions (Cardigann format) and runs them directly, without a
Jackett server.

Configuration lives in ``jackettless.json`` next to this file (created on the
first run). Downloaded definitions, cookies and state are kept in the
``jackettless_data`` folder, also next to this file.

Only the Python standard library is used (qBittorrent plugin rule).
"""

import base64
import gzip
import hashlib
import functools
import html
import html.parser
import http.client
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:  # inside qBittorrent's nova3 folder
    from novaprinter import prettyPrinter  # type: ignore
except Exception:  # pragma: no cover - standalone / tests
    def prettyPrinter(d):  # noqa: N802
        size = d.get('size', -1)
        print('|'.join((d['link'], urllib.parse.quote(d['name']), str(size), str(d['seeds']),
                        str(d['leech']), d['engine_url'], d.get('desc_link', ''),
                        str(d.get('pub_date', -1)))), flush=True)

try:
    import helpers as _qbt_helpers  # type: ignore
    _DEFAULT_UA = _qbt_helpers._headers.get('User-Agent')  # noqa: SLF001
except Exception:  # pragma: no cover
    _qbt_helpers = None
    _DEFAULT_UA = None
if not _DEFAULT_UA:
    _DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0'

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PLUGIN_DIR, 'jackettless.json')
DATA_DIR = os.path.join(PLUGIN_DIR, 'jackettless_data')

DEFAULT_CONFIG = {
    "indexers": {
        "thepiratebay": {},
        "therarbg": {},
        "limetorrents": {},
        "yts": {},
        "nyaasi": {},
        "torrentdownload": {},
        "torrentproject2": {},
    },
    "all_public": False,
    "thread_count": 10,
    "timeout": 20,
    "indexer_timeout": 60,
    "tracker_first": True,
    "definitions_ref": "master",
    "refresh_days": 1,
    "dns_over_https": None,
    "report_errors": "none",
    "dedup": "off",
    "dedup_wait": 8,
    "verbose": False,
}


# ---------------------------------------------------------------------------
# logging (stdout is reserved for results)
# ---------------------------------------------------------------------------

_VERBOSE = bool(os.environ.get('JACKETTLESS_DEBUG'))
_log_lock = threading.Lock()


def log(msg, *args, debug=False):
    if debug and not _VERBOSE:
        return
    if args:
        msg = msg % args
    with _log_lock:
        try:
            sys.stderr.write('[jackettless] %s\n' % msg)
            sys.stderr.flush()
        except Exception:  # pragma: no cover
            pass


def is_blank(s):
    return s is None or (isinstance(s, str) and s.strip() == '')


def as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('true', 'yes', '1', 'on')


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


# ---------------------------------------------------------------------------
# YAML subset parser (enough for Jackett/Cardigann definitions)
# All scalars are returned as strings (like YamlDotNet into string
# properties); null/~/empty become None. Mappings are OMap instances that
# also remember duplicate keys (Cardigann "fields" may repeat a key).
# ---------------------------------------------------------------------------

class YamlError(ValueError):
    pass


class OMap(dict):
    """Ordered dict that also keeps every (key, value) pair, duplicates included."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs = list(super().items())

    def add(self, key, value):
        self.pairs.append((key, value))
        self[key] = value


_Y_ESCAPES = {'0': '\0', 'a': '\a', 'b': '\b', 't': '\t', '\t': '\t', 'n': '\n', 'v': '\v', 'f': '\f',
              'r': '\r', 'e': '\x1b', ' ': ' ', '"': '"', '/': '/', '\\': '\\', 'N': '\x85',
              '_': '\xa0', 'L': ' ', 'P': ' '}


def _y_quoted(s, i):
    """Parse a quoted scalar starting at s[i]. Returns (value, index after closing quote)."""
    q = s[i]
    out = []
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if q == "'":
            if c == "'":
                if i + 1 < n and s[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return ''.join(out), i + 1
            out.append(c)
            i += 1
        else:
            if c == '"':
                return ''.join(out), i + 1
            if c == '\\' and i + 1 < n:
                e = s[i + 1]
                if e == 'x':
                    out.append(chr(int(s[i + 2:i + 4], 16)))
                    i += 4
                elif e == 'u':
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                elif e == 'U':
                    out.append(chr(int(s[i + 2:i + 10], 16)))
                    i += 10
                elif e == '\n':
                    i += 2
                    while i < n and s[i] in ' \t':
                        i += 1
                elif e in _Y_ESCAPES:
                    out.append(_Y_ESCAPES[e])
                    i += 2
                else:
                    raise YamlError('bad escape \\%s' % e)
                continue
            out.append(c)
            i += 1
    raise YamlError('unterminated quoted scalar')


def _y_plain(v):
    v = v.strip()
    if v in ('', '~', 'null', 'Null', 'NULL'):
        return None
    return v


def _y_strip_comment(s):
    """Strip a trailing ' # comment' from a plain scalar."""
    if s.startswith('#'):
        return ''
    k = s.find(' #')
    if k < 0:
        k = s.find('\t#')
    return s if k < 0 else s[:k]


class _YamlParser:
    def __init__(self, text):
        self.lines = []
        for no, raw in enumerate(text.replace('\r\n', '\n').replace('\r', '\n').split('\n'), 1):
            if no == 1 and raw.startswith('﻿'):
                raw = raw[1:]
            stripped = raw.lstrip(' ')
            body = stripped.rstrip()
            if not body or body.startswith('#'):
                continue
            indent = len(raw) - len(stripped)
            if indent == 0 and (body in ('---', '...') or body.startswith('%') or body.startswith('--- ')):
                if body.startswith('--- ') and _y_strip_comment(body[4:]).strip():
                    raise YamlError('inline document content not supported')
                continue
            self.lines.append([indent, body, no])
        self.i = 0

    def parse(self):
        if not self.lines:
            return None
        value = self.block(self.lines[0][0])
        if self.i < len(self.lines):
            raise YamlError('line %d: unexpected content' % self.lines[self.i][2])
        return value

    @staticmethod
    def _is_seq(text):
        return text == '-' or text.startswith('- ') or text.startswith('-\t')

    def block(self, indent):
        text = self.lines[self.i][1]
        if self._is_seq(text):
            return self.seq(indent)
        return self.mapping(indent)

    @staticmethod
    def split_key(text):
        """Return (key, rest) if text is a 'key: value' entry, else None."""
        if text[0] in '"\'':
            try:
                key, j = _y_quoted(text, 0)
            except (YamlError, ValueError):
                return None
            while j < len(text) and text[j] in ' \t':
                j += 1
            if j < len(text) and text[j] == ':' and (j + 1 == len(text) or text[j + 1] in ' \t'):
                return key, text[j + 1:].strip()
            return None
        if text[0] in '[{&*!|>':
            return None
        j = 0
        while True:
            k = text.find(':', j)
            if k < 0:
                return None
            if k + 1 == len(text) or text[k + 1] in ' \t':
                key = text[:k].rstrip()
                if ' #' in key:
                    return None
                return key, text[k + 1:].strip()
            j = k + 1

    def mapping(self, indent):
        out = OMap()
        while self.i < len(self.lines):
            ind, text, no = self.lines[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError('line %d: bad indentation' % no)
            if self._is_seq(text):
                break
            kv = self.split_key(text)
            if kv is None:
                raise YamlError('line %d: expected "key: value"' % no)
            key, rest = kv
            self.i += 1
            if rest == '' or rest.startswith('#'):
                value = None
                if self.i < len(self.lines):
                    nind, ntext, _ = self.lines[self.i]
                    if nind > indent:
                        value = self.block(nind)
                    elif nind == indent and self._is_seq(ntext):
                        value = self.seq(nind)
            else:
                value = self.inline(rest, indent, no)
            out.add(key, value)
        return out

    def seq(self, indent):
        out = []
        while self.i < len(self.lines):
            ind, text, no = self.lines[self.i]
            if ind != indent or not self._is_seq(text):
                if ind > indent:
                    raise YamlError('line %d: bad indentation' % no)
                break
            rest = text[1:].lstrip(' \t')
            if not rest or rest.startswith('#'):
                self.i += 1
                if self.i < len(self.lines) and self.lines[self.i][0] > indent:
                    out.append(self.block(self.lines[self.i][0]))
                else:
                    out.append(None)
                continue
            newind = indent + (len(text) - len(rest))
            if self._is_seq(rest) or self.split_key(rest) is not None:
                self.lines[self.i] = [newind, rest, no]
                out.append(self.block(newind))
            else:
                self.i += 1
                out.append(self.inline(rest, indent, no))
        return out

    def _continuation(self, indent):
        """Collect following lines that are more indented than `indent`."""
        parts = []
        while self.i < len(self.lines) and self.lines[self.i][0] > indent:
            parts.append(self.lines[self.i])
            self.i += 1
        return parts

    def inline(self, rest, indent, no):
        # anchors / tags are ignored (not used by Jackett definitions)
        while rest[:1] in ('&', '!'):
            sp = rest.find(' ')
            if sp < 0:
                return None
            rest = rest[sp + 1:].lstrip()
        c = rest[0]
        if c in '"\'':
            text = rest
            while True:
                try:
                    value, j = _y_quoted(text, 0)
                    break
                except YamlError:
                    if self.i < len(self.lines) and self.lines[self.i][0] > indent:
                        nxt = self.lines[self.i][1]
                        self.i += 1
                        text = (text[:-1] if text.endswith('\\') and c == '"' else text + ' ') + nxt
                    else:
                        raise YamlError('line %d: unterminated string' % no)
            tail = text[j:].strip()
            if tail and not tail.startswith('#'):
                raise YamlError('line %d: unexpected text after string' % no)
            return value
        if c in '[{':
            text = _y_strip_comment_flow(rest)
            while not _y_flow_balanced(text):
                if self.i < len(self.lines) and self.lines[self.i][0] > indent:
                    text += ' ' + _y_strip_comment_flow(self.lines[self.i][1])
                    self.i += 1
                else:
                    raise YamlError('line %d: unterminated flow collection' % no)
            value, j = _y_flow(text, 0)
            if text[j:].strip():
                raise YamlError('line %d: unexpected text after flow collection' % no)
            return value
        if c in '|>':
            header = _y_strip_comment(rest).strip()
            chomp = '-' if '-' in header else ('+' if '+' in header else '')
            lines = self._continuation(indent)
            if not lines:
                return ''
            base = lines[0][0]
            body = [' ' * (ln[0] - base) + ln[1] for ln in lines]
            value = '\n'.join(body) if c == '|' else ' '.join(body)
            if chomp != '-':
                value += '\n'
            return value
        value = _y_strip_comment(rest)
        for ln in self._continuation(indent):
            part = _y_strip_comment(ln[1])
            value += ' ' + part
        return _y_plain(value)


def _y_strip_comment_flow(s):
    # strip a trailing comment outside quotes
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in '"\'':
            k = i - 1
            while k >= 0 and s[k] in ' \t':
                k -= 1
            if k < 0 or s[k] in '[{,:':
                q = ch
        elif ch == '#' and (i == 0 or s[i - 1] in ' \t'):
            return s[:i].rstrip()
    return s


def _y_flow_balanced(s):
    depth = 0
    q = None
    i = 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == '\\' and q == '"':
                i += 2
                continue
            if ch == q:
                if q == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                q = None
        elif ch in '"\'':
            k = i - 1
            while k >= 0 and s[k] in ' \t':
                k -= 1
            if k < 0 or s[k] in '[{,:':  # a quote only opens a scalar at its start
                q = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        i += 1
    return depth <= 0 and q is None


def _y_flow(s, i, key_mode=False):
    n = len(s)
    while i < n and s[i] in ' \t':
        i += 1
    if i >= n:
        return None, i
    c = s[i]
    if c == '[':
        out = []
        i += 1
        while True:
            while i < n and s[i] in ' \t':
                i += 1
            if i < n and s[i] == ']':
                return out, i + 1
            v, i = _y_flow(s, i)
            while i < n and s[i] in ' \t':
                i += 1
            if i < n and s[i] == ':':  # single pair mapping inside a sequence
                v2, i = _y_flow(s, i + 1)
                m = OMap()
                m.add(v, v2)
                v = m
                while i < n and s[i] in ' \t':
                    i += 1
            out.append(v)
            if i < n and s[i] == ',':
                i += 1
                continue
            if i < n and s[i] == ']':
                return out, i + 1
            raise YamlError('bad flow sequence')
    if c == '{':
        out = OMap()
        i += 1
        while True:
            while i < n and s[i] in ' \t':
                i += 1
            if i < n and s[i] == '}':
                return out, i + 1
            k, i = _y_flow(s, i, key_mode=True)
            while i < n and s[i] in ' \t':
                i += 1
            v = None
            if i < n and s[i] == ':':
                v, i = _y_flow(s, i + 1)
                while i < n and s[i] in ' \t':
                    i += 1
            out.add(k, v)
            if i < n and s[i] == ',':
                i += 1
                continue
            if i < n and s[i] == '}':
                return out, i + 1
            raise YamlError('bad flow mapping')
    if c in '"\'':
        return _y_quoted(s, i)
    j = i
    while j < n:
        ch = s[j]
        if ch in ',]}':
            break
        if ch == ':' and (j + 1 >= n or s[j + 1] in ' \t,]}') and key_mode:
            break
        if ch == ':' and (j + 1 >= n or s[j + 1] in ' \t') and not key_mode:
            break
        j += 1
    return _y_plain(s[i:j]), j


def yaml_load(text):
    return _YamlParser(text).parse()


# ---------------------------------------------------------------------------
# .NET regex compatibility
# ---------------------------------------------------------------------------

_UNI_RANGES = {
    'IsCyrillic': 'Ѐ-ӿ',
    'IsCJKUnifiedIdeographs': '一-鿿',
    'IsGreek': 'Ͱ-Ͽ',
    'IsArabic': '؀-ۿ',
    'IsHebrew': '֐-׿',
    'IsBasicLatin': '\u0000-\u007F',
    'L': 'A-Za-zªµºÀ-ÖØ-öø-˿Ͱ-ϿЀ-ԯ'
         '֐-׿؀-ۿ฀-๿぀-ヿ一-鿿가-힯',
    'Lu': 'A-ZÀ-ÖØ-ÞΑ-ΩА-Я',
    'Ll': 'a-zß-öø-ÿα-ωа-я',
    'N': '0-9²³¹¼-¾٠-٩⅐-↏',
    'Nd': '0-9٠-٩',
    'P': '!-#%-*,-/:;?@\\[-\\]_{}¡§«¶·»¿‐-‧‰-⁞'
         '、-〃〈-】！-／',
    'S': '$+<->^`|~¢-¦¨©¬®-±´¸×÷⁄₠-⃏'
         '℀-⅏←-⏿─-➿',
    'Z': '    -     　',
    'M': '̀-ͯ҃-҉֑-ֽً-ٟ',
    'Mn': '̀-ͯ҃-҉֑-ֽً-ٟ',
}

_FLAG_CHARS = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL, 'x': re.VERBOSE}
_re_cache = {}
_re_cache_lock = threading.Lock()


def _translate_net_regex(p):
    out = []
    flags = 0
    i = 0
    n = len(p)
    in_class = False
    while i < n:
        c = p[i]
        if c == '\\' and i + 1 < n:
            e = p[i + 1]
            if e in 'pP' and i + 2 < n and p[i + 2] == '{':
                j = p.find('}', i + 3)
                name = p[i + 3:j]
                rng = _UNI_RANGES.get(name)
                if rng is None:
                    raise re.error('unsupported unicode class %s' % name)
                if in_class:
                    if e == 'P':
                        raise re.error('negated unicode class inside character class')
                    out.append(rng)
                else:
                    out.append(('[^%s]' if e == 'P' else '[%s]') % rng)
                i = j + 1
                continue
            if e == 'k' and i + 2 < n and p[i + 2] in "<'":
                end = p.find('>' if p[i + 2] == '<' else "'", i + 3)
                out.append('(?P=%s)' % p[i + 3:end])
                i = end + 1
                continue
            if e == 'z' and not in_class:
                out.append(r'\Z')
                i += 2
                continue
            out.append(p[i:i + 2])
            i += 2
            # .NET treats '-' after a class shorthand inside [...] as a literal ([\s-_])
            if in_class and e in 'dDsSwW' and i < n and p[i] == '-' and i + 1 < n and p[i + 1] != ']':
                out.append('\\-')
                i += 1
            continue
        if in_class:
            if c == ']':
                in_class = False
            elif c == '[' and i + 1 < n and p[i + 1] == ':':
                out.append('\\[')
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c == '[':
            in_class = True
            out.append(c)
            i += 1
            if i < n and p[i] == '^':
                out.append('^')
                i += 1
            if i < n and p[i] == ']':
                out.append('\\]')
                i += 1
            continue
        if c == '(' and p.startswith('(?', i):
            m = re.match(r"\(\?<([A-Za-z_]\w*)>", p[i:]) or re.match(r"\(\?'([A-Za-z_]\w*)'", p[i:])
            if m:
                out.append('(?P<%s>' % m.group(1))
                i += m.end()
                continue
            m = re.match(r'\(\?([imsx]+)\)', p[i:])
            if m:
                for f in m.group(1):
                    flags |= _FLAG_CHARS[f]
                i += m.end()
                continue
            m = re.match(r'\(\?([imsx]*)-([imsx]+)\)', p[i:])
            if m:
                for f in m.group(1):
                    flags |= _FLAG_CHARS[f]
                i += m.end()
                continue
        out.append(c)
        i += 1
    return ''.join(out), flags


def net_regex(pattern, flags=0):
    key = (pattern, flags)
    rx = _re_cache.get(key)
    if rx is None:
        translated, extra = _translate_net_regex(pattern)
        rx = re.compile(translated, flags | extra)
        with _re_cache_lock:
            _re_cache[key] = rx
    return rx


def _net_expand(m, repl):
    out = []
    i = 0
    n = len(repl)
    ngroups = m.re.groups
    while i < n:
        c = repl[i]
        if c == '$' and i + 1 < n:
            d = repl[i + 1]
            if d == '$':
                out.append('$')
                i += 2
                continue
            if d == '{':
                j = repl.find('}', i + 2)
                if j > 0:
                    name = repl[i + 2:j]
                    try:
                        g = m.group(int(name) if name.isdigit() else name)
                        out.append(g or '')
                        i = j + 1
                        continue
                    except (IndexError, KeyError, re.error):
                        pass
            if d.isdigit():
                j = i + 1
                while j < n and repl[j].isdigit():
                    j += 1
                digits = repl[i + 1:j]
                for k in range(len(digits), 0, -1):
                    num = int(digits[:k])
                    if num <= ngroups:
                        out.append(m.group(num) or '')
                        i = i + 1 + k
                        break
                else:
                    out.append('$')
                    i += 1
                continue
            if d == '&':
                out.append(m.group(0))
                i += 2
                continue
            if d == '`':
                out.append(m.string[:m.start()])
                i += 2
                continue
            if d == "'":
                out.append(m.string[m.end():])
                i += 2
                continue
            if d == '_':
                out.append(m.string)
                i += 2
                continue
        out.append(c)
        i += 1
    return ''.join(out)


def net_sub(pattern, repl, s):
    rx = net_regex(pattern)
    return rx.sub(lambda m: _net_expand(m, repl), s)


# ---------------------------------------------------------------------------
# numbers / sizes (port of Jackett ParseUtil)
# ---------------------------------------------------------------------------

def _num_chars(s):
    return ''.join(c for c in (s or '') if c.isdigit() or c in '.,')


def coerce_double(s):
    v = _num_chars(s)
    v = '0' if not v else v.replace(',', '.')
    if v.count('.') > 1:
        last = v.rfind('.')
        v = v[:last].replace('.', '') + v[last:]
    try:
        return float(v)
    except ValueError:
        return 0.0


def coerce_long(s):
    v = _num_chars(s)
    if not v:
        return 0
    if ',' in v and '.' in v:
        try:
            return int(float(v.replace(',', '')))
        except ValueError:
            return 0
    v = v.replace('.', '').replace(',', '')
    try:
        return int(v) if v else 0
    except ValueError:
        return 0


def get_long_from_string(s):
    if is_blank(s):
        return None
    m = re.search(r'\d+', s)
    return int(m.group(0)) if m else 0


def get_bytes(s):
    s = s or ''
    val = coerce_double(s)
    unit = ''.join(c for c in s if c.isalpha()).replace('i', '').lower()
    if 'kb' in unit:
        val *= 1024
    elif 'mb' in unit:
        val *= 1024 ** 2
    elif 'gb' in unit:
        val *= 1024 ** 3
    elif 'tb' in unit:
        val *= 1024 ** 4
    return int(val)


# ---------------------------------------------------------------------------
# dates (port of Jackett DateTimeUtil, .NET custom formats + Go layouts)
# All datetimes are naive local time.
# ---------------------------------------------------------------------------

_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september',
           'october', 'november', 'december']
_MONTH_ABBR = [m[:3] for m in _MONTHS]
_DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
_DAY_ABBR = [d[:3] for d in _DAYS]
_DAY_ABBR_C = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
_MONTH_ABBR_C = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

_NET_STANDARD_FORMATS = {
    'd': 'MM/dd/yyyy', 'D': 'dddd, dd MMMM yyyy', 'f': 'dddd, dd MMMM yyyy HH:mm',
    'F': 'dddd, dd MMMM yyyy HH:mm:ss', 'g': 'MM/dd/yyyy HH:mm', 'G': 'MM/dd/yyyy HH:mm:ss',
    'm': 'MMMM dd', 'M': 'MMMM dd', 'r': "ddd, dd MMM yyyy HH':'mm':'ss 'GMT'",
    'R': "ddd, dd MMM yyyy HH':'mm':'ss 'GMT'", 's': "yyyy'-'MM'-'dd'T'HH':'mm':'ss",
    't': 'HH:mm', 'T': 'HH:mm:ss', 'u': "yyyy'-'MM'-'dd HH':'mm':'ss'Z'", 'y': 'yyyy MMMM',
    'Y': 'yyyy MMMM', 'o': "yyyy'-'MM'-'dd'T'HH':'mm':'ss.fffffffK", 'O': "yyyy'-'MM'-'dd'T'HH':'mm':'ss.fffffffK",
}

_net_fmt_cache = {}


def _net_format_regex(fmt):
    cached = _net_fmt_cache.get(fmt)
    if cached:
        return cached
    if len(fmt) == 1 and fmt in _NET_STANDARD_FORMATS:
        fmt = _NET_STANDARD_FORMATS[fmt]
    parts = []
    names = []
    i = 0
    n = len(fmt)

    def grp(kind, rx):
        name = '%s%d' % (kind, len(names))
        names.append((name, kind))
        return '(?P<%s>%s)' % (name, rx)

    while i < n:
        c = fmt[i]
        if c in '\'"':
            j = fmt.find(c, i + 1)
            if j < 0:
                j = n
            parts.append(re.escape(fmt[i + 1:j]))
            i = j + 1
            continue
        if c == '\\' and i + 1 < n:
            parts.append(re.escape(fmt[i + 1]))
            i += 2
            continue
        if c == '%':
            i += 1
            continue
        if c in 'dMyhHmsfFtzK':
            j = i
            while j < n and fmt[j] == c:
                j += 1
            cnt = j - i
            i = j
            if c == 'd':
                if cnt <= 2:
                    parts.append(grp('d', r'\d{1,2}'))
                else:
                    parts.append(grp('dname', r'[^\W\d_]+\.?'))
            elif c == 'M':
                if cnt <= 2:
                    parts.append(grp('M', r'\d{1,2}'))
                elif cnt == 3:
                    parts.append(grp('MMM', r'[^\W\d_]{3,4}'))
                else:
                    parts.append(grp('MMMM', r'[^\W\d_]+'))
            elif c == 'y':
                parts.append(grp('yy', r'\d{1,2}') if cnt <= 2 else grp('yyyy', r'\d{4}'))
            elif c in 'hHms':
                parts.append(grp(c, r'\d{1,2}'))
            elif c == 'f':
                parts.append(grp('f', r'\d{%d}' % cnt))
            elif c == 'F':
                parts.append(grp('f', r'\d{0,%d}' % cnt))
            elif c == 't':
                parts.append(grp('t', '[AaPp]' if cnt == 1 else '[AaPp]\\.?[Mm]\\.?'))
            elif c == 'z':
                parts.append(grp('z', r'[+-]\d{1,2}(?::?\d{2})?'))
            elif c == 'K':
                parts.append('(?:' + grp('z', r'Z|[+-]\d{2}:?\d{2}') + ')?')
            continue
        if c.isspace():
            while i < n and fmt[i].isspace():
                i += 1
            parts.append(r'\s+')
            continue
        parts.append(re.escape(c))
        i += 1
    rx = re.compile(''.join(parts) + r'\Z', re.IGNORECASE)
    _net_fmt_cache[fmt] = (rx, names)
    return rx, names


def _month_from_name(name):
    name = name.lower().rstrip('.')
    if name == 'sept':
        return 9
    for idx, m in enumerate(_MONTHS):
        if name == m or name == m[:3]:
            return idx + 1
    raise ValueError('bad month name %r' % name)


def _parse_offset(z):
    if z in (None, '', 'Z', 'z'):
        return timedelta(0) if z else None
    sign = -1 if z[0] == '-' else 1
    z = z[1:].replace(':', '')
    hours = int(z[:2] if len(z) >= 3 else z)
    minutes = int(z[2:]) if len(z) > 2 else 0
    return sign * timedelta(hours=hours, minutes=minutes)


def _to_local(dt, offset):
    if offset is None:
        return dt
    aware = dt.replace(tzinfo=timezone(offset))
    return aware.astimezone().replace(tzinfo=None)


def net_parse_exact(s, fmt, now=None):
    """DateTime.ParseExact(s, fmt, InvariantCulture) look-alike."""
    now = now or datetime.now()
    rx, names = _net_format_regex(fmt)
    m = rx.match(s.strip())
    if not m:
        raise ValueError('"%s" does not match format "%s"' % (s, fmt))
    vals = {}
    for name, kind in names:
        v = m.group(name)
        if v is not None:
            vals[kind] = v
    has_date = any(k in vals for k in ('yy', 'yyyy', 'M', 'MMM', 'MMMM', 'd'))
    if 'yyyy' in vals:
        year = int(vals['yyyy'])
    elif 'yy' in vals:
        yy = int(vals['yy'])
        year = 2000 + yy if yy < 50 else 1900 + yy
    else:
        year = now.year
    if 'M' in vals:
        month = int(vals['M'])
    elif 'MMM' in vals or 'MMMM' in vals:
        month = _month_from_name(vals.get('MMM') or vals.get('MMMM'))
    else:
        month = 1 if has_date else now.month
    if 'd' in vals:
        day = int(vals['d'])
    else:
        day = 1 if has_date else now.day
    if not has_date:
        year, month, day = now.year, now.month, now.day
    hour = int(vals.get('H', vals.get('h', 0)))
    if 't' in vals:
        pm = vals['t'][0].lower() == 'p'
        if 'h' in vals or 'H' not in vals:
            hour = hour % 12 + (12 if pm else 0)
    minute = int(vals.get('m', 0))
    second = int(vals.get('s', 0))
    micro = 0
    if vals.get('f'):
        micro = int((vals['f'] + '000000')[:6])
    dt = datetime(year, month, day, hour, minute, second, micro)
    return _to_local(dt, _parse_offset(vals.get('z')))


_GO_REPLACEMENTS = [
    ('2006', 'yyyy'), ('06', 'yy'), ('January', 'MMMM'), ('Jan', 'MMM'), ('01', 'MM'),
    ('Monday', 'dddd'), ('Mon', 'ddd'), ('02', 'dd'), ('2', 'd'), ('05', 'ss'), ('15', 'HH'),
    ('03', 'hh'), ('3', 'h'), ('04', 'mm'), ('4', 'm'), ('5', 's'), ('1', 'M'),
    ('.0000', 'ffff'), ('.000', 'fff'), ('.00', 'ff'), ('.0', 'f'),
    ('.9999', 'FFFF'), ('.999', 'FFF'), ('.99', 'FF'), ('.9', 'F'),
    ('PM', 'tt'), ('pm', 'tt'), ('Z07:00', "'Z'zzz"), ('Z07', "'Z'zz"), ('Z07:00', "'Z'zzz"),
    ('Z07', "'Z'zz"), ('-07:00', 'zzz'), ('-07', 'zz'),
]


def go_layout_to_net(layout):
    fmt = layout
    for a, b in _GO_REPLACEMENTS:
        fmt = fmt.replace(a, b)
    return fmt


def _minus_year(dt):
    try:
        return dt.replace(year=dt.year - 1)
    except ValueError:  # Feb 29
        return dt.replace(year=dt.year - 1, day=28)


def parse_date_golang(value, layout, now=None):
    now = now or datetime.now()
    value = (value or '').strip()
    low = layout.lower()
    if any(ch in low for ch in 'yhd'):
        try:
            return net_parse_exact(value, layout, now)
        except (ValueError, re.error):
            pass
    fmt = go_layout_to_net(layout)
    dt = net_parse_exact(value, fmt, now)
    if 'yy' not in fmt and dt > now:
        dt = _minus_year(dt)
    return dt


def _local_offset_hours():
    off = time.localtime().tm_gmtoff
    return int(off / 3600)


def fmt_rfc(dt):
    """Format like Jackett's Rfc1123ZPattern: ddd, dd MMM yyyy HH:mm:ss z"""
    off = _local_offset_hours()
    return '%s, %02d %s %04d %02d:%02d:%02d %s%d' % (
        _DAY_ABBR_C[dt.weekday()], dt.day, _MONTH_ABBR_C[dt.month - 1], dt.year, dt.hour, dt.minute,
        dt.second, '-' if off < 0 else '+', abs(off))


_RFC_RX = re.compile(r'^(?:[A-Za-z]{3}),\s(\d{2})\s([A-Za-z]{3})\s(\d{4})\s(\d{2}):(\d{2}):(\d{2})\s[+-]\d{1,2}$')


def from_time_ago(s, now=None):
    s = (s or '').lower()
    now = now or datetime.now()
    if 'now' in s:
        return now
    s = s.replace(',', '').replace('ago', '').replace('and', '')
    delta = timedelta(0)
    for m in re.finditer(r'\s*?([\d\.]+)\s*?([^\d\s\.]+)\s*?', s):
        val = coerce_double(m.group(1))
        unit = m.group(2)
        if 'sec' in unit or unit == 's':
            delta += timedelta(seconds=val)
        elif 'min' in unit or unit == 'm':
            delta += timedelta(minutes=val)
        elif 'hour' in unit or 'hr' in unit or unit == 'h':
            delta += timedelta(hours=val)
        elif 'day' in unit or unit == 'd':
            delta += timedelta(days=val)
        elif 'week' in unit or 'wk' in unit or unit == 'w':
            delta += timedelta(days=val * 7)
        elif 'month' in unit or unit == 'mo':
            delta += timedelta(days=val * 30)
        elif 'year' in unit or unit == 'y':
            delta += timedelta(days=val * 365)
        else:
            raise ValueError('TimeAgo parsing failed, unknown unit: ' + unit)
    return now - delta


_MONTH_RX = (r'(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|'
             r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?')
_FZ_TIME = re.compile(r'(?<![\d:])(?P<h>\d{1,2})[:h](?P<m>\d{2})(?::(?P<s>\d{2})(?:[.,]\d+)?)?\s*'
                      r'(?P<ap>[ap]\.?m\.?(?![a-z]))?\s*(?P<tz>(?:Z|UTC|GMT)(?![a-z])|[+-]\d{2}:?\d{2})?', re.I)
_FZ_DATES = [
    ('ymd', re.compile(r'(?<!\d)(?P<y>\d{4})[-/.](?P<a>\d{1,2})[-/.](?P<b>\d{1,2})(?!\d)')),
    ('xyz', re.compile(r'(?<!\d)(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y>\d{4}|\d{2})(?!\d)')),
    ('dMy', re.compile(r'(?<![\w])(?P<a>\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*' + _MONTH_RX +
                       r'(?:[\s\-/.,]*(?P<y>\d{4}|\d{2}(?![\d:])))?', re.I)),
    ('Mdy', re.compile(_MONTH_RX + r'[\s\-/.,]*(?P<a>\d{1,2})(?:st|nd|rd|th)?(?![\d:])'
                       r'(?:[\s\-/.,]+(?P<y>\d{4}|\d{2}(?![\d:])))?', re.I)),
    ('My', re.compile(_MONTH_RX + r'[\s\-/.,]+(?P<y>\d{4})', re.I)),
]


def fuzzy_time(s, uk=False, now=None):
    """Rough equivalent of DateTimeRoutines.TryParseDateOrTime (USA default)."""
    now = now or datetime.now()
    s = s or ''
    date_part = None
    used = None
    for kind, rx in _FZ_DATES:
        m = rx.search(s)
        if not m:
            continue
        gd = m.groupdict()
        try:
            if kind == 'ymd':
                y, mo, d = int(gd['y']), int(gd['a']), int(gd['b'])
            elif kind == 'xyz':
                a, b = int(gd['a']), int(gd['b'])
                y = int(gd['y'])
                if uk:
                    d, mo = a, b
                else:
                    mo, d = a, b
                if mo > 12 >= d:
                    mo, d = d, mo
            elif kind == 'dMy':
                d, mo = int(gd['a']), _month_from_name(gd['mon'])
                y = int(gd['y']) if gd.get('y') else None
            elif kind == 'Mdy':
                d, mo = int(gd['a']), _month_from_name(gd['mon'])
                y = int(gd['y']) if gd.get('y') else None
            else:
                d, mo, y = 1, _month_from_name(gd['mon']), int(gd['y'])
            if y is None:
                y = now.year
            elif y < 100:
                y += 2000 if y < 50 else 1900
            date_part = date(y, mo, d)
            used = m.span()
            break
        except ValueError:
            continue
    rest = s if used is None else s[:used[0]] + ' ' + s[used[1]:]
    tm = _FZ_TIME.search(rest)
    if date_part is None and tm is None:
        raise ValueError('FromFuzzyTime parsing failed')
    if date_part is None:
        date_part = now.date()
    hour = minute = second = 0
    offset = None
    if tm:
        hour, minute = int(tm.group('h')), int(tm.group('m'))
        second = int(tm.group('s') or 0)
        ap = tm.group('ap')
        if ap:
            hour = hour % 12 + (12 if ap[0].lower() == 'p' else 0)
        tz = tm.group('tz')
        if tz:
            offset = timedelta(0) if tz.upper() in ('Z', 'UTC', 'GMT') else _parse_offset(tz)
        if hour > 23 or minute > 59:
            raise ValueError('bad time')
    dt = datetime(date_part.year, date_part.month, date_part.day, hour, minute, min(second, 59))
    return _to_local(dt, offset)


def _time_of_day(s):
    s = (s or '').strip()
    if not s:
        return timedelta(0)
    m = re.search(r'(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*([ap]\.?m\.?)?', s, re.I)
    if not m:
        raise ValueError('bad time %r' % s)
    h = int(m.group(1))
    if m.group(4):
        h = h % 12 + (12 if m.group(4)[0].lower() == 'p' else 0)
    return timedelta(hours=h, minutes=int(m.group(2) or 0), seconds=int(m.group(3) or 0))


_RX_AGO = re.compile(r'(?i)\bago')
_RX_TODAY = re.compile(r'(?i)\btoday(?:[\s,]+(?:at){0,1}\s*|[\s,]*|$)')
_RX_TOMORROW = re.compile(r'(?i)\btomorrow(?:[\s,]+(?:at){0,1}\s*|[\s,]*|$)')
_RX_YESTERDAY = re.compile(r'(?i)\byesterday(?:[\s,]+(?:at){0,1}\s*|[\s,]*|$)')
_RX_DOW = re.compile(r'(?i)\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+at\s+')
_RX_MISSING_YEAR = re.compile(r'^(\d{1,2}-\d{1,2})(\s|$)')
_RX_MISSING_YEAR2 = re.compile(r'^(\d{1,2}\s+\w{3})\s+(\d{1,2}\:\d{1,2}.*)$')


def from_unknown(s, now=None):
    """Port of DateTimeUtil.FromUnknown."""
    now = now or datetime.now()
    s = (s or '').strip()
    m = _RFC_RX.match(s)
    if m:
        return datetime(int(m.group(3)), _month_from_name(m.group(2)), int(m.group(1)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)))
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s))
        except (OverflowError, OSError, ValueError):
            pass
    if 'now' in s.lower():
        return now
    if _RX_AGO.search(s):
        return from_time_ago(s, now)
    today = datetime(now.year, now.month, now.day)
    for rx, days in ((_RX_TODAY, 0), (_RX_YESTERDAY, -1), (_RX_TOMORROW, 1)):
        m = rx.search(s)
        if m:
            return today + _time_of_day(s.replace(m.group(0), '')) + timedelta(days=days)
    m = _RX_DOW.search(s)
    if m:
        dt = today + _time_of_day(s.replace(m.group(0), ''))
        dow = _DAYS.index(m.group(1).lower())
        while dt.weekday() != dow:
            dt -= timedelta(days=1)
        return dt
    m = _RX_MISSING_YEAR.match(s)
    if m:
        s2 = s.replace(m.group(1), '%d-%s' % (now.year, m.group(1)))
        dt = fuzzy_time(s2, now=now)
        return _minus_year(dt) if dt > now else dt
    m = _RX_MISSING_YEAR2.match(s)
    if m:
        dt = fuzzy_time('%s %d %s' % (m.group(1), now.year, m.group(2)), now=now)
        return _minus_year(dt) if dt > now else dt
    return fuzzy_time(s, now=now)


# ---------------------------------------------------------------------------
# HTML DOM (html.parser based, with the HTML5 fix-ups selectors rely on)
# ---------------------------------------------------------------------------

_VOID = frozenset('area base br col embed hr img input keygen link meta param source track wbr '
                  'basefont bgsound frame'.split())
_SPECIAL = frozenset('address applet area article aside base basefont bgsound blockquote body br button '
                     'caption center col colgroup dd details dir div dl dt embed fieldset figcaption figure '
                     'footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html iframe img '
                     'input li link listing main marquee menu meta nav noembed noframes noscript object ol '
                     'p param plaintext pre script section select source style summary table tbody td '
                     'template textarea tfoot th thead title tr track ul wbr xmp'.split())
_SCOPE = frozenset('applet caption html table td th marquee object template'.split())
_TABLE_SCOPE = frozenset(('html', 'table', 'template'))
_P_CLOSERS = frozenset('address article aside blockquote center details dialog dir div dl fieldset '
                       'figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol '
                       'p pre section summary table ul li dd dt'.split())
_HEAD_TAGS = frozenset('base link meta noscript script style template title'.split())


class Element:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs if attrs is not None else {}
        self.children = []
        self.parent = parent

    def __repr__(self):
        return '<%s %s>' % (self.tag, ' '.join('%s="%s"' % kv for kv in self.attrs.items()))

    def append(self, node):
        if isinstance(node, Element):
            if node.parent is not None:
                node.detach()
            node.parent = self
        self.children.append(node)

    def detach(self):
        if self.parent is not None:
            try:
                self.parent.children.remove(self)
            except ValueError:
                pass
            self.parent = None

    def get(self, name, default=None):
        return self.attrs.get(name.lower(), default)

    def element_children(self):
        return [c for c in self.children if isinstance(c, Element)]

    def text_content(self):
        out = []
        stack = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                out.append(node)
            else:
                stack.extend(reversed(node.children))
        return ''.join(out)

    def iter_descendants(self):
        stack = list(reversed(self.element_children()))
        while stack:
            el = stack.pop()
            yield el
            stack.extend(reversed(el.element_children()))

    def root(self):
        el = self
        while el.parent is not None and el.parent.tag != '#document':
            el = el.parent
        return el

    def outer_html(self, limit=600):
        parts = []

        def walk(node):
            if isinstance(node, str):
                parts.append(html.escape(node, quote=False))
                return
            attrs = ''.join(' %s="%s"' % (k, html.escape(v)) for k, v in node.attrs.items())
            parts.append('<%s%s>' % (node.tag, attrs))
            for ch in node.children:
                walk(ch)
            if node.tag not in _VOID:
                parts.append('</%s>' % node.tag)

        walk(self)
        s = ''.join(parts)
        return s if len(s) <= limit else s[:limit] + '...'


class _TreeBuilder(html.parser.HTMLParser):
    def __init__(self, xml=False):
        super().__init__(convert_charrefs=True)
        self.xml = xml
        self.doc = Element('#document')
        self.stack = [self.doc]

    @property
    def cur(self):
        return self.stack[-1]

    def _close_until(self, targets, boundaries):
        for idx in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[idx].tag
            if tag in targets:
                del self.stack[idx:]
                return True
            if tag in boundaries:
                return False
        return False

    def _push(self, tag, attrs=None):
        el = Element(tag, attrs or {})
        self.cur.append(el)
        self.stack.append(el)
        return el

    def handle_starttag(self, tag, attrs):
        ad = {}
        for k, v in attrs:
            if k not in ad:
                ad[k] = v if v is not None else ''
        if self.xml:
            self._push(tag, ad)
            return
        if tag in ('td', 'th'):
            self._close_until(('td', 'th'), ('tr', 'table', 'template'))
            if self.cur.tag in ('table', 'tbody', 'thead', 'tfoot'):
                if self.cur.tag == 'table':
                    self._push('tbody')
                self._push('tr')
        elif tag == 'tr':
            self._close_until(('tr',), ('table', 'tbody', 'thead', 'tfoot', 'template'))
            if self.cur.tag == 'table':
                self._push('tbody')
        elif tag in ('tbody', 'thead', 'tfoot'):
            self._close_until(('tbody', 'thead', 'tfoot'), ('table', 'template'))
        elif tag == 'li':
            self._close_until(('li',), ('ul', 'ol', 'menu', 'table', 'td', 'th', 'div', 'template'))
        elif tag in ('dt', 'dd'):
            self._close_until(('dt', 'dd'), ('dl', 'table', 'td', 'th', 'div', 'template'))
        elif tag == 'option':
            self._close_until(('option',), ('select', 'datalist', 'optgroup', 'table'))
        elif tag == 'optgroup':
            self._close_until(('optgroup', 'option'), ('select', 'table'))
        elif tag == 'a':
            self._close_until(('a',), _SCOPE)
        elif tag in ('html', 'body', 'head'):
            existing = next((e for e in self.stack if e.tag == tag), None)
            if existing is not None:
                for k, v in ad.items():
                    existing.attrs.setdefault(k, v)
                return
        if tag in _P_CLOSERS:
            self._close_until(('p',), _SCOPE | {'button'})
        if tag in _VOID:
            self.cur.append(Element(tag, ad))
            return
        self._push(tag, ad)

    def handle_startendtag(self, tag, attrs):
        if self.xml:
            self.handle_starttag(tag, attrs)
            self.stack.pop()
            return
        self.handle_starttag(tag, attrs)  # HTML5 ignores the self-closing flag on non-void elements

    def handle_endtag(self, tag):
        if self.xml:
            self._close_until((tag,), ())
            return
        if tag in _VOID or tag in ('html', 'body', 'head'):
            return
        if tag in ('td', 'th', 'tr', 'tbody', 'thead', 'tfoot', 'table', 'caption'):
            self._close_until((tag,), _TABLE_SCOPE - {tag})
            return
        if tag in _SPECIAL or tag == 'p':
            self._close_until((tag,), _SCOPE - {tag})
            return
        # formatting / phrasing elements: stop at the first special element
        for idx in range(len(self.stack) - 1, 0, -1):
            t = self.stack[idx].tag
            if t == tag:
                del self.stack[idx:]
                return
            if t in _SPECIAL:
                return

    def handle_data(self, data):
        cur = self.cur
        if cur.children and isinstance(cur.children[-1], str):
            cur.children[-1] += data
        else:
            cur.children.append(data)

    def unknown_decl(self, data):
        if data.upper().startswith('CDATA['):
            self.handle_data(data[6:])


def _ensure_html_skeleton(doc):
    """Make sure the tree looks like html > (head, body), as browsers/AngleSharp build it."""
    html_el = next((c for c in doc.children if isinstance(c, Element) and c.tag == 'html'), None)
    if html_el is None:
        html_el = Element('html')
        for node in list(doc.children):
            if isinstance(node, Element):
                node.parent = None
            html_el.children.append(node)
            if isinstance(node, Element):
                node.parent = html_el
        doc.children = [html_el]
        html_el.parent = doc
    else:
        # stray nodes outside <html> go inside it
        for node in list(doc.children):
            if node is html_el:
                continue
            doc.children.remove(node)
            if isinstance(node, Element):
                node.parent = None
                html_el.append(node)
            elif node.strip():
                html_el.children.append(node)
    head = next((c for c in html_el.children if isinstance(c, Element) and c.tag == 'head'), None)
    body = next((c for c in html_el.children if isinstance(c, Element) and c.tag in ('body', 'frameset')), None)
    if body is None:
        body = Element('body')
        rest = []
        for node in html_el.children:
            if node is head:
                continue
            if isinstance(node, Element) and node.tag in _HEAD_TAGS and not body.children and head is None:
                rest.append(node)  # leading head-ish tags stay before body
                continue
            body.children.append(node)
            if isinstance(node, Element):
                node.parent = body
        html_el.children = ([head] if head is not None else []) + rest + [body]
        body.parent = html_el
    if head is None:
        head = Element('head', parent=html_el)
        html_el.children.insert(0, head)
    return doc


def parse_html(text):
    b = _TreeBuilder()
    try:
        b.feed(text or '')
        b.close()
    except Exception as e:  # html.parser is lenient, but never let it kill a search
        log('html parser error: %s', e, debug=True)
    return _ensure_html_skeleton(b.doc)


def parse_xml(text):
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text or '')
    b = _TreeBuilder(xml=True)
    b.feed(text)
    b.close()
    return b.doc


def document_element(doc):
    return next((c for c in doc.children if isinstance(c, Element)), doc)


# ---------------------------------------------------------------------------
# CSS selectors (AngleSharp / jQuery flavour used by Cardigann definitions)
# ---------------------------------------------------------------------------

class SelectorError(ValueError):
    pass


_IDENT_CHARS = re.compile(r'[\w\- -￿]')


class _Compound:
    __slots__ = ('tag', 'tests')

    def __init__(self):
        self.tag = None
        self.tests = []


class _SelParser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def error(self, msg):
        raise SelectorError('%s at %d in selector %r' % (msg, self.i, self.s))

    def peek(self):
        return self.s[self.i] if self.i < self.n else ''

    def skip_ws(self):
        start = self.i
        while self.i < self.n and self.s[self.i] in ' \t\r\n\f':
            self.i += 1
        return self.i > start

    def ident(self):
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == '\\' and self.i + 1 < self.n:
                m = re.match(r'[0-9a-fA-F]{1,6} ?', self.s[self.i + 1:])
                if m:
                    out.append(chr(int(m.group(0).strip(), 16)))
                    self.i += 1 + m.end()
                else:
                    out.append(self.s[self.i + 1])
                    self.i += 2
                continue
            if _IDENT_CHARS.match(c):
                out.append(c)
                self.i += 1
                continue
            break
        if not out:
            self.error('identifier expected')
        return ''.join(out)

    def string(self):
        q = self.s[self.i]
        self.i += 1
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == '\\' and self.i + 1 < self.n:
                out.append(self.s[self.i + 1])
                self.i += 2
                continue
            if c == q:
                self.i += 1
                return ''.join(out)
            out.append(c)
            self.i += 1
        self.error('unterminated string')

    def paren_arg(self):
        """Raw text inside balanced parentheses (the '(' is current char)."""
        assert self.s[self.i] == '('
        self.i += 1
        depth = 1
        start = self.i
        q = None
        while self.i < self.n:
            c = self.s[self.i]
            if q:
                if c == '\\':
                    self.i += 2
                    continue
                if c == q:
                    q = None
            elif c in '"\'':
                q = c
            elif c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    arg = self.s[start:self.i]
                    self.i += 1
                    return arg
            self.i += 1
        self.error('unbalanced parenthesis')

    def parse_list(self, relative=False):
        out = []
        while True:
            self.skip_ws()
            out.append(self.parse_complex(relative))
            self.skip_ws()
            if self.peek() == ',':
                self.i += 1
                continue
            if self.i < self.n:
                self.error('unexpected character %r' % self.peek())
            return out

    def parse_complex(self, relative):
        parts = []
        comb = None
        self.skip_ws()
        if self.peek() in ('>', '+', '~') and self.peek():
            comb = self.peek()
            self.i += 1
            self.skip_ws()
        elif relative:
            comb = ' '
        while True:
            comp = self.parse_compound()
            parts.append((comb, comp))
            had_ws = self.skip_ws()
            c = self.peek()
            if not c or c == ',':
                break
            if c in '>+~':
                comb = c
                self.i += 1
                self.skip_ws()
            elif had_ws:
                comb = ' '
            else:
                self.error('unexpected character %r' % c)
        return parts

    def parse_compound(self):
        comp = _Compound()
        c = self.peek()
        if c == '*':
            self.i += 1
        elif c and (_IDENT_CHARS.match(c) or c == '\\'):
            comp.tag = self.ident().lower()
            if self.peek() == '|':  # namespace prefix: ignore it
                self.i += 1
                comp.tag = '*' if self.peek() == '*' else self.ident().lower()
                if comp.tag == '*':
                    self.i += 1
                    comp.tag = None
        any_part = comp.tag is not None or c == '*'
        while self.i < self.n:
            c = self.s[self.i]
            if c == '#':
                self.i += 1
                v = self.ident()
                comp.tests.append(lambda el, scope, v=v: el.attrs.get('id') == v)
            elif c == '.':
                self.i += 1
                v = self.ident()
                comp.tests.append(lambda el, scope, v=v: v in el.attrs.get('class', '').split())
            elif c == '[':
                comp.tests.append(self.parse_attr())
            elif c == ':':
                comp.tests.append(self.parse_pseudo())
            else:
                break
            any_part = True
        if not any_part:
            self.error('selector expected')
        return comp

    def parse_attr(self):
        self.i += 1
        self.skip_ws()
        name = self.ident().lower()
        if self.peek() == '|' and self.s[self.i + 1:self.i + 2] != '=':
            self.i += 1
            name = self.ident().lower()
        self.skip_ws()
        op = None
        val = None
        ci = False
        c = self.peek()
        if c == ']':
            self.i += 1
            return lambda el, scope: name in el.attrs
        if c in '~|^$*!' and self.s[self.i + 1:self.i + 2] == '=':
            op = c + '='
            self.i += 2
        elif c == '=':
            op = '='
            self.i += 1
        else:
            self.error('bad attribute selector')
        self.skip_ws()
        if self.peek() in '"\'':
            val = self.string()
        else:
            start = self.i
            while self.i < self.n and self.s[self.i] not in ' \t]':
                self.i += 1
            val = self.s[start:self.i]
        self.skip_ws()
        if self.peek() in ('i', 'I', 's', 'S'):
            ci = self.peek() in 'iI'
            self.i += 1
            self.skip_ws()
        if self.peek() != ']':
            self.error('"]" expected')
        self.i += 1
        cval = val.lower() if ci else val

        def test(el, scope):
            a = el.attrs.get(name)
            if a is None:
                return op == '!='
            if ci:
                a = a.lower()
            if op == '=':
                return a == cval
            if op == '!=':
                return a != cval
            if op == '~=':
                return cval in a.split()
            if op == '|=':
                return a == cval or a.startswith(cval + '-')
            if not cval:
                return False
            if op == '^=':
                return a.startswith(cval)
            if op == '$=':
                return a.endswith(cval)
            if op == '*=':
                return cval in a
            return False

        return test

    def parse_pseudo(self):
        self.i += 1
        if self.peek() == ':':
            self.error('pseudo-elements are not supported')
        name = self.ident().lower()
        arg = None
        if self.peek() == '(':
            arg = self.paren_arg()
        if name in ('contains', 'icontains', 'containsown'):
            text = _unquote_arg(arg)
            if name == 'icontains':
                low = text.lower()
                return lambda el, scope: low in _text_of(el).lower()
            return lambda el, scope: text in _text_of(el)
        if name == 'has':
            rel = _parse_selector(arg, relative=True)
            return lambda el, scope: _has(el, rel)
        if name in ('not', 'is', 'matches', 'where'):
            inner = _parse_selector(arg)
            if name == 'not':
                return lambda el, scope: not any(_match_complex(el, c, len(c) - 1, scope) for c in inner)
            return lambda el, scope: any(_match_complex(el, c, len(c) - 1, scope) for c in inner)
        if name in ('nth-child', 'nth-last-child', 'nth-of-type', 'nth-last-of-type', 'eq'):
            if name == 'eq':  # jQuery :eq(n), 0 based among siblings matched so far -> approximate
                a, b = 0, int(arg.strip()) + 1
                name = 'nth-child'
            else:
                a, b = _parse_nth(arg)
            last = 'last' in name
            of_type = 'of-type' in name
            return lambda el, scope: _nth_matches(el, a, b, last, of_type)
        if name == 'first-child':
            return lambda el, scope: _nth_matches(el, 0, 1, False, False)
        if name == 'last-child':
            return lambda el, scope: _nth_matches(el, 0, 1, True, False)
        if name == 'only-child':
            return lambda el, scope: el.parent is not None and len(el.parent.element_children()) == 1
        if name == 'first-of-type':
            return lambda el, scope: _nth_matches(el, 0, 1, False, True)
        if name == 'last-of-type':
            return lambda el, scope: _nth_matches(el, 0, 1, True, True)
        if name == 'only-of-type':
            return lambda el, scope: _nth_matches(el, 0, 1, False, True) and _nth_matches(el, 0, 1, True, True)
        if name == 'empty':
            return lambda el, scope: not any(isinstance(c, Element) or c for c in el.children)
        if name == 'root':
            return lambda el, scope: el.parent is not None and el.parent.tag == '#document'
        if name == 'scope':
            return lambda el, scope: el is scope or (scope is not None and scope.tag == '#document'
                                                     and el.parent is scope)
        if name == 'checked':
            return lambda el, scope: 'checked' in el.attrs or 'selected' in el.attrs
        if name == 'disabled':
            return lambda el, scope: 'disabled' in el.attrs
        if name == 'enabled':
            return lambda el, scope: 'disabled' not in el.attrs
        if name in ('link', 'any-link'):
            return lambda el, scope: el.tag in ('a', 'area', 'link') and 'href' in el.attrs
        if name in ('first', 'last'):  # jQuery positional, approximate as child position
            return lambda el, scope: _nth_matches(el, 0, 1, name == 'last', False)
        self.error('unsupported pseudo-class :%s' % name)


def _unquote_arg(arg):
    arg = (arg or '').strip()
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in '"\'':
        return re.sub(r'\\(.)', r'\1', arg[1:-1])
    return arg


def _parse_nth(arg):
    a = (arg or '').strip().lower().replace(' ', '')
    if ' of ' in (arg or ''):
        raise SelectorError(':nth-child(... of S) is not supported')
    if a == 'odd':
        return 2, 1
    if a == 'even':
        return 2, 0
    m = re.fullmatch(r'([+-]?\d*)n([+-]\d+)?', a)
    if m:
        coef = m.group(1)
        if coef in ('', '+'):
            ca = 1
        elif coef == '-':
            ca = -1
        else:
            ca = int(coef)
        return ca, int(m.group(2) or 0)
    if re.fullmatch(r'[+-]?\d+', a):
        return 0, int(a)
    raise SelectorError('bad nth expression %r' % arg)


# Per-query memo (sibling positions, text content). The tree does not change while one selector runs,
# so this turns :nth-child / :contains over big tables from quadratic into linear work.
_query_memo = threading.local()


def _memo():
    return getattr(_query_memo, 'd', None)


def _with_memo(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if _memo() is not None:  # nested call: reuse the outer query's memo
            return fn(*args, **kwargs)
        _query_memo.d = {}
        try:
            return fn(*args, **kwargs)
        finally:
            _query_memo.d = None
    return wrapper


def _text_of(el):
    d = _memo()
    if d is None:
        return el.text_content()
    key = ('t', id(el))
    if key not in d:
        d[key] = el.text_content()
    return d[key]


def _sibling_index(parent, tag):
    """(count, {id(element): 1-based position}) of the element children of `parent` (optionally one tag)."""
    d = _memo()
    key = ('s', id(parent), tag)
    if d is not None and key in d:
        return d[key]
    sibs = parent.element_children()
    if tag is not None:
        sibs = [s for s in sibs if s.tag == tag]
    value = (len(sibs), {id(s): i for i, s in enumerate(sibs, 1)})
    if d is not None:
        d[key] = value
    return value


def _nth_matches(el, a, b, last, of_type):
    parent = el.parent
    if parent is None:
        return False
    count, index = _sibling_index(parent, el.tag if of_type else None)
    pos = index.get(id(el), 0)
    if not pos:
        return False
    if last:
        pos = count - pos + 1
    if a == 0:
        return pos == b
    k = (pos - b) / a
    return k >= 0 and k == int(k)


def _match_compound(el, comp, scope):
    if comp.tag is not None and el.tag != comp.tag:
        # a type selector without namespace matches the local name (<tv:raw_title> ~ raw_title)
        if ':' not in el.tag or el.tag.rsplit(':', 1)[1] != comp.tag:
            return False
    for t in comp.tests:
        if not t(el, scope):
            return False
    return True


def _is_element(el):
    return el is not None and el.tag != '#document'


def _prev_sibling(el):
    p = el.parent
    if p is None:
        return None
    prev = None
    for c in p.children:
        if c is el:
            return prev
        if isinstance(c, Element):
            prev = c
    return None


def _match_complex(el, parts, idx, scope):
    comb, comp = parts[idx]
    if comp is _SCOPE_ANCHOR:
        if el is not scope:
            return False
    elif el.tag == '#document' or not _match_compound(el, comp, scope):
        return False
    if idx == 0:
        return True
    if comb == ' ':
        anc = el.parent
        while _is_element(anc) or (anc is scope and anc is not None):
            if _match_complex(anc, parts, idx - 1, scope):
                return True
            anc = anc.parent
        return False
    if comb == '>':
        p = el.parent
        return (p is not None and (_is_element(p) or p is scope)) and _match_complex(p, parts, idx - 1, scope)
    if comb == '+':
        p = _prev_sibling(el)
        return p is not None and _match_complex(p, parts, idx - 1, scope)
    if comb == '~':
        p = _prev_sibling(el)
        while p is not None:
            if _match_complex(p, parts, idx - 1, scope):
                return True
            p = _prev_sibling(p)
        return False
    return False


_SCOPE_ANCHOR = _Compound()


def _has(el, rel_list):
    for parts in rel_list:
        comb0 = parts[0][0] or ' '
        anchored = [(None, _SCOPE_ANCHOR)] + [(comb0, parts[0][1])] + parts[1:]
        if comb0 in (' ', '>'):
            candidates = el.iter_descendants()
        else:
            candidates = []
            p = el.parent
            if p is not None:
                seen = False
                for c in p.element_children():
                    if seen:
                        candidates.append(c)
                        candidates.extend(c.iter_descendants())
                    elif c is el:
                        seen = True
        for cand in candidates:
            if _match_complex(cand, anchored, len(anchored) - 1, el):
                return True
    return False


_sel_cache = {}


def _parse_selector(text, relative=False):
    key = (text, relative)
    parsed = _sel_cache.get(key)
    if parsed is None:
        parsed = _SelParser(text or '').parse_list(relative)
        _sel_cache[key] = parsed
    return parsed


def _prepare(node, selector):
    """Jackett's manual ':root' handling: strip it and start from the top element."""
    selector = selector.strip()
    if selector.startswith(':root'):
        selector = selector[5:]
        while node.parent is not None and node.parent.tag != '#document':
            node = node.parent
        if node.parent is not None:
            node = node.parent  # the document: so 'html' itself can match
        if not selector.strip():
            return node, None
    return node, selector


@_with_memo
def _find(node, selector, first):
    node, selector = _prepare(node, selector)
    if selector is None:
        top = document_element(node) if node.tag == '#document' else node
        return top if first else [top]
    rel = selector.lstrip()[:1] in ('>', '+', '~')
    complexes = _parse_selector(selector, relative=rel)
    if rel:
        complexes = [[(None, _SCOPE_ANCHOR)] + [(c[0][0], c[0][1])] + c[1:] for c in complexes]
    out = []
    for el in node.iter_descendants():
        if any(_match_complex(el, c, len(c) - 1, node) for c in complexes):
            if first:
                return el
            out.append(el)
    return None if first else out


def select_all(node, selector):
    return _find(node, selector, False)


def select_one(node, selector):
    return _find(node, selector, True)


@_with_memo
def matches(el, selector):
    if el.tag == '#document':
        return False
    selector = selector.strip()
    if selector.startswith(':root'):
        return False
    try:
        complexes = _parse_selector(selector)
    except SelectorError:
        if selector[:1] in ('>', '+', '~'):
            return False
        raise
    return any(_match_complex(el, c, len(c) - 1, None) for c in complexes)


# ---------------------------------------------------------------------------
# JSON helpers (Newtonsoft SelectToken subset)
# ---------------------------------------------------------------------------

_MISSING = object()


def _json_path_tokens(path):
    path = path.strip()
    if path.startswith('$'):
        path = path[1:]
    tokens = []
    i = 0
    n = len(path)
    while i < n:
        c = path[i]
        if c == '.':
            i += 1
            continue
        if c == '[':
            j = path.find(']', i)
            inner = path[i + 1:j].strip()
            if inner[:1] in '"\'':
                tokens.append(inner[1:-1])
            elif inner == '*':
                tokens.append('*')
            else:
                tokens.append(int(inner))
            i = j + 1
            continue
        j = i
        while j < n and path[j] not in '.[':
            j += 1
        tokens.append(path[i:j])
        i = j
    return tokens


def json_select(obj, path):
    """Return the token at `path` or _MISSING."""
    if path is None:
        return _MISSING
    cur = obj
    for tok in _json_path_tokens(path):
        if tok == '*':
            if isinstance(cur, dict):
                cur = list(cur.values())
            elif not isinstance(cur, list):
                return _MISSING
            continue
        if isinstance(tok, int):
            if isinstance(cur, list) and -len(cur) <= tok < len(cur):
                cur = cur[tok]
            else:
                return _MISSING
        elif isinstance(cur, dict):
            if tok in cur:
                cur = cur[tok]
            else:
                return _MISSING
        else:
            return _MISSING
    return cur


def json_value_str(v):
    """Newtonsoft JToken.Value<string>() look-alike."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 'True' if v else 'False'
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, (int, str)):
        return str(v)
    if isinstance(v, list):
        return ','.join(json_value_str(x) if not isinstance(x, (dict, list)) else json.dumps(x) for x in v)
    return json.dumps(v)


_JSON_SEL_RX = re.compile(r'\:(?P<filter>.+?)\((?P<key>.+?)\)(?=:|\Z)')


def json_field_selector(obj, selector):
    """Port of CardigannIndexer.JsonParseFieldSelector: returns the plain path or None."""
    path = selector.split(':')[0]
    if is_blank(path):
        parsed = obj
    else:
        parsed = json_select(obj, path)
        if parsed is _MISSING or parsed is None:
            return None
    for m in _JSON_SEL_RX.finditer(selector):
        flt, key = m.group('filter'), m.group('key')
        if flt == 'has':
            if _JSON_SEL_RX.search(key):
                if json_field_selector(parsed, key) is None:
                    return None
            elif json_select(parsed, key) in (_MISSING, None):
                return None
        elif flt == 'not':
            if _JSON_SEL_RX.search(key):
                if json_field_selector(parsed, key) is not None:
                    return None
            elif json_select(parsed, key) not in (_MISSING, None):
                return None
        elif flt == 'contains':
            if key not in json.dumps(parsed, ensure_ascii=False):
                return None
    return path


# ---------------------------------------------------------------------------
# Go-template subset: a faithful port of Jackett's regex based implementation
# ---------------------------------------------------------------------------

_RX_RE_REPLACE = re.compile(r'{{\s*re_replace\s+(\..+?)\s+"(.*?)"\s+"(.*?)"\s*}}')
_RX_JOIN = re.compile(r'{{\s*join\s+(\..+?)\s+"(.*?)"\s*}}')
_RX_LOGIC = re.compile(r'\b(and|or|eq|ne)(?:\s+(\(?\.[^\)\s]+\)?|"[^"]+")){2,}')
_RX_LOGIC_PARAM = re.compile(r'\s+(\(?\.[^\)\s]+\)?|"[^"]+")')
_RX_IF_ELSE = re.compile(r'{{\s*if\s*(.+?)\s*}}(.*?){{\s*else\s*}}(.*?){{\s*end\s*}}')
_RX_RANGE = re.compile(r'{{\s*range\s*(((?P<index>\$.+?),)((\s*(?P<element>.+?)\s*(:=)\s*)))?(?P<variable>.+?)\s*}}'
                       r'(?P<prefix>.*?){{\.}}(?P<postfix>.*?){{end}}')
_RX_VAR = re.compile(r'{{\s*(\..+?)\s*}}')


def _tvar(variables, name):
    return variables.get(name)


def _as_str(v):
    if v is None:
        return None
    if isinstance(v, list):
        return None
    return v


def apply_template(template, variables, modifier=None):
    if template is None:
        return None
    if not isinstance(template, str):
        template = str(template)
    if '{{' not in template:
        return template

    def mod(s):
        return modifier(s) if modifier else s

    # re_replace
    for m in list(_RX_RE_REPLACE.finditer(template)):
        whole, var, pattern, repl = m.group(0), m.group(1), m.group(2), m.group(3)
        value = _as_str(_tvar(variables, var)) or ''
        try:
            expanded = net_sub(pattern, repl, value)
        except re.error:
            expanded = value
        template = template.replace(whole, mod(expanded))
    # join
    for m in list(_RX_JOIN.finditer(template)):
        whole, var, delim = m.group(0), m.group(1), m.group(2)
        value = _tvar(variables, var) or []
        if not isinstance(value, list):
            value = [value]
        template = template.replace(whole, mod(delim.join(str(x) for x in value)))
    # logic functions (re-run from the start after each replacement: supports nesting)
    m = _RX_LOGIC.search(template)
    guard = 0
    while m and guard < 1000:
        guard += 1
        start = m.start()
        length = m.end() - m.start()
        fname = m.group(1)
        params = []
        pos = m.end(1)
        while True:
            pm = _RX_LOGIC_PARAM.match(template, pos)
            if not pm or pm.end() > m.end():
                break
            params.append((pm.group(1).strip('()'), pm.start(1)))
            pos = pm.end()
        if fname in ('and', 'or'):
            params = [p for p in params if not p[0].startswith('"')]
            is_and = fname == 'and'
            result = ''
            for p, _ in params:
                result = p
                if is_blank(_as_str(_tvar(variables, p))) == is_and:
                    break
        else:
            want_equal = fname == 'eq'
            if len(params) > 2:
                length = params[2][1] - start
            vals = []
            for p, _ in params[:2]:
                vals.append(p.strip('"') if p.startswith('"') else _as_str(_tvar(variables, p)))
            is_equal = len(set(vals)) == 1
            result = '.True' if is_equal == want_equal else '.False'
        template = template[:start] + result + template[start + length:]
        m = _RX_LOGIC.search(template)
    # if / else / end
    for m in list(_RX_IF_ELSE.finditer(template)):
        whole, cond, on_true, on_false = m.group(0), m.group(1), m.group(2), m.group(3)
        if not cond.startswith('.'):
            raise ValueError("Condition operation '%s' not implemented" % cond)
        value = _tvar(variables, cond)
        if value is None:
            state = False
        elif isinstance(value, list):
            state = len(value) > 0
        else:
            state = not is_blank(str(value))
        template = template.replace(whole, on_true if state else on_false)
    # range
    for m in list(_RX_RANGE.finditer(template)):
        whole = m.group(0)
        index = m.group('index') or ''
        var = m.group('variable')
        prefix, postfix = m.group('prefix'), m.group('postfix')
        values = _tvar(variables, var) or []
        if not isinstance(values, list):
            values = [values]
        index_repl = '{{' + index + '}}'
        expanded = []
        for k, v in enumerate(values):
            expanded.append(prefix.replace(index_repl, str(k)) + mod(str(v)) + postfix.replace(index_repl, str(k)))
        template = template.replace(whole, ''.join(expanded))
    # simple variables
    for m in list(_RX_VAR.finditer(template)):
        whole, var = m.group(0), m.group(1)
        value = _tvar(variables, var)
        if isinstance(value, list):
            value = ','.join(str(x) for x in value)
        if value is not None and not isinstance(value, str):
            value = str(value)
        if modifier is not None and value is not None:
            value = modifier(value)
        template = template.replace(whole, value or '')
    return template


# ---------------------------------------------------------------------------
# URL encoding helpers (.NET WebUtility semantics)
# ---------------------------------------------------------------------------

def _py_encoding(name):
    name = (name or 'utf-8').strip().lower()
    try:
        import codecs
        codecs.lookup(name)
        return name
    except LookupError:
        return 'utf-8'


def net_url_encode(s, encoding='utf-8'):
    """WebUtility.UrlEncode: spaces -> '+', keeps -_.!*() unescaped."""
    if not s:
        return ''
    return urllib.parse.quote_plus(s, safe='-_.!*()', encoding=_py_encoding(encoding), errors='replace')


def net_url_decode(s, encoding='utf-8'):
    if not s:
        return ''
    return urllib.parse.unquote_plus(s, encoding=_py_encoding(encoding), errors='replace')


def query_string(pairs, encoding='utf-8', separator='&'):
    return separator.join('%s=%s' % (k, net_url_encode(v or '', encoding)) for k, v in pairs)


def get_arg_from_query(url, arg):
    if url is None or arg is None:
        return None
    qs = url.split('?', 1)[1]
    qs = qs.split('#', 1)[0]
    values = urllib.parse.parse_qs(qs, keep_blank_values=True).get(arg)
    return values[0] if values else None


def resolve_url(base, path):
    return urllib.parse.urljoin(base, path or '')


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

_INVALID_FILENAME = set('"<>|:*?\\/') | {chr(c) for c in range(32)}


def _filter_args(args):
    if isinstance(args, list):
        return args
    return [args]


def apply_filters(data, filters, variables, encoding='utf-8'):
    if not filters:
        return data
    for flt in filters:
        if not isinstance(flt, dict):
            continue
        name = flt.get('name')
        args = flt.get('args')
        alist = _filter_args(args)
        if data is None:
            data = ''
        if name == 'querystring':
            data = get_arg_from_query(data, alist[0])
        elif name in ('dateparse', 'timeparse'):
            try:
                data = fmt_rfc(parse_date_golang(data, alist[0]))
            except (ValueError, re.error, OverflowError) as e:
                log('dateparse: %s', e, debug=True)
        elif name == 'regexp':
            m = net_regex(alist[0]).search(data)
            data = (m.group(1) or '') if m and m.re.groups >= 1 else ''
        elif name == 're_replace':
            repl = apply_template(alist[1] or '', variables)
            data = net_sub(alist[0], repl, data)
        elif name == 'split':
            sep = alist[0] or ' '
            pos = int(alist[1])
            parts = data.split(sep[0])
            if pos < 0:
                pos += len(parts)
            data = parts[pos]
        elif name == 'replace':
            to = apply_template(alist[1] if len(alist) > 1 else '', variables) or ''
            frm = alist[0] or ''
            data = data.replace(frm, to) if frm else data
        elif name == 'trim':
            cutset = args if not isinstance(args, list) else (args[0] if args else None)
            data = data.strip(cutset[0]) if cutset else data.strip()
        elif name == 'prepend':
            data = (apply_template(alist[0], variables) or '') + data
        elif name == 'append':
            data = data + (apply_template(alist[0], variables) or '')
        elif name == 'tolower':
            data = data.lower()
        elif name == 'toupper':
            data = data.upper()
        elif name == 'base64decode':
            s = data.strip().replace('-', '+').replace('_', '/')
            s += '=' * (-len(s) % 4)
            data = base64.b64decode(s).decode('utf-8', 'replace')
        elif name == 'base64encode':
            data = base64.urlsafe_b64encode(data.encode('utf-8')).decode('ascii').rstrip('=')
        elif name == 'urldecode':
            data = net_url_decode(data, encoding)
        elif name == 'urlencode':
            data = net_url_encode(data, encoding)
        elif name == 'htmldecode':
            data = html.unescape(data)
        elif name == 'htmlencode':
            data = html.escape(data)
        elif name in ('timeago', 'reltime'):
            data = fmt_rfc(from_time_ago(data))
        elif name == 'fuzzytime':
            data = fmt_rfc(from_unknown(data))
        elif name == 'validfilename':
            data = ''.join('_' if ch in _INVALID_FILENAME else ch for ch in data)
        elif name == 'diacritics':
            if alist[0] != 'replace':
                raise ValueError('unsupported diacritics filter argument')
            nfd = unicodedata.normalize('NFD', data)
            data = unicodedata.normalize('NFC', ''.join(ch for ch in nfd if unicodedata.category(ch) != 'Mn'))
        elif name == 'jsonjoinarray':
            obj = json.loads(data)
            tok = json_select(obj, alist[0])
            if tok is _MISSING or tok is None:
                raise ValueError('jsonjoinarray: path not found')
            items = tok if isinstance(tok, list) else (list(tok.values()) if isinstance(tok, dict) else [tok])
            data = (alist[1] if len(alist) > 1 else '').join(json_value_str(x) or '' for x in items)
        elif name == 'validate':
            delims = r'[,\s/)(.;\[\]"|:]+'
            valid = [p for p in re.split(delims, str(alist[0]).lower()) if p]
            present = [p for p in re.split(delims, data.lower()) if p]
            data = ','.join(v for v in dict.fromkeys(valid) if v in present)
        elif name in ('hexdump', 'strdump'):
            log('strdump: %r', data, debug=True)
        else:
            log('unknown filter %r ignored', name, debug=True)
    return data


SUPPORTED_FILTERS = frozenset((
    'querystring', 'dateparse', 'timeparse', 'regexp', 're_replace', 'split', 'replace', 'trim', 'prepend',
    'append', 'tolower', 'toupper', 'base64decode', 'base64encode', 'urldecode', 'urlencode', 'htmldecode',
    'htmlencode', 'timeago', 'reltime', 'fuzzytime', 'validfilename', 'diacritics', 'jsonjoinarray',
    'validate', 'hexdump', 'strdump'))


# ---------------------------------------------------------------------------
# Torznab categories
# ---------------------------------------------------------------------------

TORZNAB_CATS = {
    "Console": 1000, "Console/NDS": 1010, "Console/PSP": 1020, "Console/Wii": 1030, "Console/XBox": 1040,
    "Console/XBox 360": 1050, "Console/Wiiware": 1060, "Console/XBox 360 DLC": 1070, "Console/PS3": 1080,
    "Console/Other": 1090, "Console/3DS": 1110, "Console/PS Vita": 1120, "Console/WiiU": 1130,
    "Console/XBox One": 1140, "Console/PS4": 1180, "Movies": 2000, "Movies/Foreign": 2010,
    "Movies/Other": 2020, "Movies/SD": 2030, "Movies/HD": 2040, "Movies/UHD": 2045, "Movies/BluRay": 2050,
    "Movies/3D": 2060, "Movies/DVD": 2070, "Movies/WEB-DL": 2080, "Audio": 3000, "Audio/MP3": 3010,
    "Audio/Video": 3020, "Audio/Audiobook": 3030, "Audio/Lossless": 3040, "Audio/Other": 3050,
    "Audio/Foreign": 3060, "PC": 4000, "PC/0day": 4010, "PC/ISO": 4020, "PC/Mac": 4030,
    "PC/Mobile-Other": 4040, "PC/Games": 4050, "PC/Mobile-iOS": 4060, "PC/Mobile-Android": 4070, "TV": 5000,
    "TV/WEB-DL": 5010, "TV/Foreign": 5020, "TV/SD": 5030, "TV/HD": 5040, "TV/UHD": 5045, "TV/Other": 5050,
    "TV/Sport": 5060, "TV/Anime": 5070, "TV/Documentary": 5080, "XXX": 6000, "XXX/DVD": 6010,
    "XXX/WMV": 6020, "XXX/XviD": 6030, "XXX/x264": 6040, "XXX/UHD": 6045, "XXX/Pack": 6050,
    "XXX/ImageSet": 6060, "XXX/Other": 6070, "XXX/SD": 6080, "XXX/WEB-DL": 6090, "Books": 7000,
    "Books/Mags": 7010, "Books/EBook": 7020, "Books/Comics": 7030, "Books/Technical": 7040,
    "Books/Other": 7050, "Books/Foreign": 7060, "Other": 8000, "Other/Misc": 8010, "Other/Hashed": 8020,
}


def _cat_range(parent, exclude=()):
    return frozenset(v for v in TORZNAB_CATS.values() if v // 1000 * 1000 == parent and v not in exclude)


# qBittorrent category -> set of Torznab ids (already expanded to sub categories)
QBT_CATEGORIES = {
    'all': None,
    'movies': _cat_range(2000),
    'tv': _cat_range(5000),
    'music': _cat_range(3000, exclude=(3030,)),
    'games': _cat_range(1000) | {4050},
    'anime': frozenset((5070,)),
    'software': _cat_range(4000, exclude=(4050,)),
    'books': _cat_range(7000) | {3030},
    'pictures': frozenset((6060,)) | _cat_range(8000),
}

PUBLIC_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce', 'udp://open.demonii.com:1337/announce',
    'udp://open.stealth.si:80/announce', 'udp://tracker.torrent.eu.org:451/announce',
    'udp://tracker.theoks.net:6969/announce', 'udp://tracker.qu.ax:6969/announce',
]


def public_magnet(infohash, title):
    if is_blank(infohash) or is_blank(title):
        return None
    trs = '&'.join('tr=' + net_url_encode(t) for t in PUBLIC_TRACKERS)
    return 'magnet:?xt=urn:btih:%s&dn=%s&%s' % (infohash, net_url_encode(title), trs)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class IndexerError(Exception):
    pass


class LoginError(IndexerError):
    pass


class CaptchaRequired(LoginError):
    pass


class CloudflareError(IndexerError):
    pass


class NetworkError(IndexerError):
    pass


class DeadlineExceeded(IndexerError):
    pass


class Response:
    __slots__ = ('url', 'status', 'headers', 'content', 'encoding', '_text', '_doc')

    def __init__(self, url, status, headers, content, encoding):
        self.url = url
        self.status = status
        self.headers = headers
        self.content = content
        self.encoding = encoding
        self._text = None
        self._doc = None

    @property
    def text(self):
        if self._text is None:
            self._text = self.content.decode(self.encoding, 'replace')
        return self._text

    @property
    def doc(self):
        """The body parsed as HTML, parsed only once per response."""
        if self._doc is None:
            self._doc = parse_html(self.text)
        return self._doc

    @property
    def content_type(self):
        return (self.headers.get('Content-Type') or '') if self.headers is not None else ''

    @property
    def location(self):
        return self.headers.get('Location') if self.headers is not None else None

    @property
    def is_redirect(self):
        return self.status in (301, 302, 303, 307, 308) and bool(self.location)

    @property
    def redirect_to(self):
        loc = self.location
        if loc and loc.lower().startswith('magnet:'):
            return loc
        return urllib.parse.urljoin(self.url, loc) if loc else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_url(url):
    """Make an IRI usable by urllib (percent-encode spaces / non ASCII, IDNA host, drop fragment)."""
    parts = urllib.parse.urlsplit(url.strip())
    netloc = parts.netloc
    try:
        netloc.encode('ascii')
    except UnicodeEncodeError:
        host, sep, port = netloc.partition(':')
        netloc = host.encode('idna').decode('ascii') + sep + port
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=-._~?[]{}|")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ''))


def parse_cookie_header(s):
    out = {}
    for part in (s or '').split(';'):
        if '=' in part:
            k, _, v = part.partition('=')
            if k.strip():
                out[k.strip()] = v.strip()
    return out


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that, like Jackett, also accepts a self-signed certificate when its SHA-1
    fingerprint is listed in the definition's `certificates` for this host. Normal verification
    is always tried first; the pin is only an extra way to pass, never a way to skip checks."""

    def __init__(self, *args, pins=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pins = pins or {}

    def connect(self):
        try:
            super().connect()
            return
        except ssl.SSLCertVerificationError:
            allowed = self._pins.get((self.host or '').lower())
            if not allowed:
                raise
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        self._context = insecure
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b''
        if hashlib.sha1(der).hexdigest() not in allowed:  # noqa: S324 - fingerprint, as in Jackett
            self.close()
            raise ssl.SSLCertVerificationError('certificate of %s does not match the pinned fingerprint' % self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pins):
        super().__init__(context=ssl.create_default_context())
        self._pins = pins

    def https_open(self, req):
        conn = functools.partial(_PinnedHTTPSConnection, pins=self._pins)
        return self.do_open(conn, req, context=self._context)


def host_matches(host, domains):
    """True when `host` is one of `domains` or a subdomain of one."""
    host = (host or '').lower().rstrip('.')
    return any(host == d or host.endswith('.' + d) for d in domains)


class HttpSession:
    def __init__(self, encoding='utf-8', timeout=20, request_delay=0.0, cookies=None, user_agent=None,
                 force_encoding=False, pinned_certs=None, cookie_domains=None):
        self.encoding = encoding
        self.force_encoding = force_encoding
        self.timeout = timeout
        self.request_delay = request_delay
        self.cookies = dict(cookies or {})
        # cookies set by the user (e.g. cf_clearance): sent on every request, never cleared by (re)logins
        self.pinned_cookies = {}
        # cookies are only sent to (and accepted from) these hosts; None = any host
        self.cookie_domains = {d.lower() for d in cookie_domains} if cookie_domains else None
        self.user_agent = user_agent or _DEFAULT_UA
        self.deadline = None
        self._last = 0.0
        https = _PinnedHTTPSHandler(pinned_certs) if pinned_certs else \
            urllib.request.HTTPSHandler(context=ssl.create_default_context())
        self.opener = urllib.request.build_opener(_NoRedirect(), https)

    def _cookies_allowed(self, url):
        return self.cookie_domains is None or host_matches(urllib.parse.urlsplit(url).hostname,
                                                           self.cookie_domains)

    def _store_cookies(self, headers):
        if headers is None:
            return
        for sc in headers.get_all('Set-Cookie') or []:
            first, _, attrs = sc.partition(';')
            if '=' not in first:
                continue
            name, _, value = first.partition('=')
            name, value = name.strip(), value.strip()
            if not name:
                continue
            expired = value in ('deleted',)
            for a in attrs.split(';'):
                k, _, v = a.strip().partition('=')
                k = k.lower()
                try:
                    if k == 'max-age' and int(v) <= 0:
                        expired = True
                    elif k == 'expires':
                        exp = parsedate_to_datetime(v)
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        if exp < datetime.now(timezone.utc):
                            expired = True
                except (ValueError, TypeError, IndexError):
                    pass
            if expired:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    def _decode_body(self, raw, headers):
        enc = (headers.get('Content-Encoding') or '').lower() if headers is not None else ''
        try:
            if 'gzip' in enc:
                raw = gzip.decompress(raw)
            elif 'deflate' in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except (OSError, zlib.error, EOFError):
            pass
        return raw

    def _encoding_for(self, headers):
        if not self.force_encoding and headers is not None:
            m = re.search(r'charset=["\']?([\w\-]+)', headers.get('Content-Type') or '', re.I)
            if m:
                return _py_encoding(m.group(1))
        return self.encoding

    def request(self, url, method='GET', data=None, headers=None, referer=None, follow=False, max_redirects=5,
                body=None):
        if self.deadline is not None and time.time() > self.deadline:
            raise DeadlineExceeded('indexer time budget exceeded')
        scheme = urllib.parse.urlsplit(url.strip()).scheme.lower()
        if scheme not in ('http', 'https'):
            raise IndexerError('refusing to open a %r URL' % (scheme or 'relative'))
        if self.request_delay:
            delay_left = self._last + self.request_delay - time.time()
            if delay_left > 0:
                if self.deadline is not None and time.time() + delay_left > self.deadline:
                    raise DeadlineExceeded('indexer time budget exceeded (request delay)')
                time.sleep(delay_left)
        self._last = time.time()
        hdrs = {
            'User-Agent': self.user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
        }
        if referer:
            hdrs['Referer'] = referer
        custom_cookie = None
        for k, v in (headers or {}).items():
            if k.lower() == 'cookie':
                custom_cookie = v
            elif v is not None:
                hdrs[k] = v
        same_site = self._cookies_allowed(url)
        if same_site:  # never leak tracker cookies to third-party hosts (redirects, external download links)
            jar = dict(self.pinned_cookies)
            jar.update(self.cookies)
            if custom_cookie:
                jar.update(parse_cookie_header(custom_cookie))
            if jar:
                hdrs['Cookie'] = '; '.join('%s=%s' % kv for kv in jar.items())
        payload = None
        if method.upper() == 'POST':
            if body is not None:
                payload = body.encode(self.encoding, 'replace') if isinstance(body, str) else body
            else:
                payload = query_string(data or [], self.encoding).encode('ascii', 'replace')
                if not any(k.lower() == 'content-type' for k in hdrs):
                    hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
        final_url = safe_url(url)
        req = urllib.request.Request(final_url, data=payload, headers=hdrs, method=method.upper())
        timeout = self.timeout
        if self.deadline is not None:
            timeout = max(1.0, min(timeout, self.deadline - time.time()))
        try:
            resp = self.opener.open(req, timeout=timeout)
            status, rheaders, raw = resp.status, resp.headers, resp.read()
            resp.close()
        except urllib.error.HTTPError as e:
            status, rheaders = e.code, e.headers
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                raw = b''
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, 'reason', e)
            hint = ''
            if isinstance(reason, socket.gaierror) and socket.getaddrinfo is _orig_getaddrinfo:
                hint = ' (DNS lookup failed; if your ISP blocks this site set "dns_over_https" in jackettless.json)'
            raise NetworkError('%s: %s%s' % (urllib.parse.urlsplit(final_url).netloc, reason, hint))
        if same_site:
            self._store_cookies(rheaders)
        raw = self._decode_body(raw, rheaders)
        r = Response(final_url, status, rheaders, raw, self._encoding_for(rheaders))
        log('%s %s -> %s (%d bytes)', method, final_url, status, len(raw), debug=True)
        if follow and r.is_redirect and max_redirects > 0:
            target = r.redirect_to
            if target.lower().startswith('magnet:'):
                return r
            keep_post = status in (307, 308) and method.upper() == 'POST'
            return self.request(target, 'POST' if keep_post else 'GET', data=data if keep_post else None,
                                headers=headers, referer=referer, follow=True, max_redirects=max_redirects - 1,
                                body=body if keep_post else None)
        return r


_orig_getaddrinfo = socket.getaddrinfo
_doh_cache = {}
_doh_lock = threading.Lock()


def _doh_resolve(host, doh_url):
    with _doh_lock:
        if host in _doh_cache:
            return _doh_cache[host]
    ips = []
    for rtype in ('A', 'AAAA'):
        url = '%s%sname=%s&type=%s' % (doh_url, '&' if '?' in doh_url else '?', urllib.parse.quote(host), rtype)
        req = urllib.request.Request(url, headers={'Accept': 'application/dns-json', 'User-Agent': _TOOL_UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode('utf-8'))
            ips.extend(a['data'] for a in data.get('Answer', []) if a.get('type') in (1, 28))
        except Exception as e:  # noqa: BLE001
            log('DNS-over-HTTPS lookup for %s failed: %s', host, e, debug=True)
        if ips:
            break
    with _doh_lock:
        _doh_cache[host] = ips
    return ips


def install_dns_fallback(doh_url):
    """When the system resolver fails (e.g. ISP DNS blocking), resolve through DNS-over-HTTPS.

    TLS still uses the real host name (SNI + certificate check), only the IP lookup changes."""
    if not doh_url:
        socket.getaddrinfo = _orig_getaddrinfo
        return

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        try:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)
        except socket.gaierror:
            if not isinstance(host, str) or not re.search(r'[a-zA-Z]', host) or host == 'localhost':
                raise
            ips = _doh_resolve(host, doh_url)
            if not ips:
                raise
            log('%s resolved through DNS-over-HTTPS', host, debug=True)
            out = []
            for ip in ips:
                try:
                    out.extend(_orig_getaddrinfo(ip, port, family, type, proto, flags | socket.AI_NUMERICHOST))
                except socket.gaierror:
                    pass
            if not out:
                raise
            return out

    socket.getaddrinfo = getaddrinfo


def detect_protection(resp):
    """Raise CloudflareError if the response is an anti-bot challenge page."""
    if resp.status not in (403, 429, 503):
        return
    server = (resp.headers.get('Server') or '').lower() if resp.headers is not None else ''
    mitigated = (resp.headers.get('cf-mitigated') or '').lower() if resp.headers is not None else ''
    head = resp.content[:20000].decode('utf-8', 'replace')
    if mitigated == 'challenge' or (
            'cloudflare' in server and ('challenge-platform' in head or 'Just a moment' in head
                                        or 'cf-chl' in head or 'Attention Required' in head)):
        raise CloudflareError('Cloudflare protection: copy the cf_clearance cookie and the User-Agent from your '
                              'browser into this indexer\'s "cookie" and "user_agent" settings')
    if 'ddos-guard' in server:
        raise CloudflareError('DDoS-Guard protection (not supported)')


# ---------------------------------------------------------------------------
# definitions store (downloads Jackett YAML files on demand and caches them)
# ---------------------------------------------------------------------------

DEFS_API = 'https://api.github.com/repos/Jackett/Jackett/contents/src/Jackett.Common/Definitions?ref=%s'
DEFS_RAW = 'https://raw.githubusercontent.com/Jackett/Jackett/%s/src/Jackett.Common/Definitions/%s.yml'
_TOOL_UA = 'jackettless-qbittorrent-plugin'


def git_blob_sha(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def _atomic_write(path, data, private=False):
    """Write a file atomically; `private` restricts it to the current user (POSIX permissions)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0)
    fd = os.open(tmp, flags, 0o600 if private else 0o666)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


_RX_INDEXER_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def check_indexer_id(ident):
    """Indexer ids become file names: refuse anything that could escape the data folder."""
    if not isinstance(ident, str) or not _RX_INDEXER_ID.match(ident) or '..' in ident:
        raise IndexerError('invalid indexer id %r' % (ident,))
    return ident


class DefinitionStore:
    def __init__(self, data_dir=DATA_DIR, ref='master', refresh_days=1, timeout=20):
        self.data_dir = data_dir
        self.dir = os.path.join(data_dir, 'definitions')
        self.custom_dir = os.path.join(data_dir, 'custom')
        self.index_file = os.path.join(data_dir, 'definitions_index.json')
        self.ref = ref or 'master'
        self.refresh_days = float(refresh_days if refresh_days is not None else 1)
        self.timeout = timeout

    def _fetch(self, url):
        req = urllib.request.Request(url, headers={'User-Agent': _TOOL_UA, 'Accept-Encoding': 'identity'})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read()

    def read_index(self):
        try:
            with open(self.index_file, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    RETRY_AFTER_FAILURE = 3600  # seconds to wait before asking GitHub again after a failed listing

    def is_stale(self):
        idx = self.read_index()
        if time.time() - float(idx.get('failed_at', 0)) < self.RETRY_AFTER_FAILURE:
            return False  # recent failure (rate limit / offline): keep using the cache for a while
        if idx.get('ref') != self.ref:
            return True
        return time.time() - float(idx.get('updated', 0)) > self.refresh_days * 86400

    def remote_listing(self):
        data = json.loads(self._fetch(DEFS_API % urllib.parse.quote(self.ref)).decode('utf-8'))
        return {e['name'][:-4]: e['sha'] for e in data
                if e.get('name', '').endswith('.yml') and _RX_INDEXER_ID.match(e['name'][:-4])}

    def local_path(self, ident):
        check_indexer_id(ident)
        custom = os.path.join(self.custom_dir, ident + '.yml')
        if os.path.exists(custom):
            return custom
        return os.path.join(self.dir, ident + '.yml')

    def _download(self, ident):
        check_indexer_id(ident)
        data = self._fetch(DEFS_RAW % (urllib.parse.quote(self.ref), urllib.parse.quote(ident)))
        _atomic_write(os.path.join(self.dir, ident + '.yml'), data)
        return data

    def sync(self, ids=None, everything=False, workers=8):
        """Refresh the cache from GitHub. Returns the remote listing (or None on failure)."""
        try:
            listing = self.remote_listing()
        except Exception as e:  # noqa: BLE001
            log('could not list Jackett definitions (%s); using cached files', e)
            idx = self.read_index()
            idx['failed_at'] = time.time()
            try:
                _atomic_write(self.index_file, json.dumps(idx).encode('utf-8'))
            except OSError:
                pass
            return None
        wanted = set(listing) if everything else {i for i in (ids or []) if i in listing}
        for i in ids or []:
            if i not in listing and not os.path.exists(os.path.join(self.custom_dir, i + '.yml')):
                log('indexer "%s" does not exist in Jackett (%s)', i, self.ref)
        todo = []
        for ident in wanted:
            path = os.path.join(self.dir, ident + '.yml')
            try:
                with open(path, 'rb') as f:
                    if git_blob_sha(f.read()) == listing[ident]:
                        continue
            except OSError:
                pass
            todo.append(ident)
        if todo:
            log('downloading %d definition(s) from Jackett/%s', len(todo), self.ref, debug=True)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for ident, fut in [(i, ex.submit(self._download, i)) for i in todo]:
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        log('failed to download definition %s: %s', ident, e)
        idx = {'ref': self.ref, 'updated': time.time(), 'files': listing}
        _atomic_write(self.index_file, json.dumps(idx).encode('utf-8'))
        return listing

    def ensure(self, ids, everything=False):
        valid = []
        for i in ids:
            try:
                valid.append(check_indexer_id(i))
            except IndexerError as e:
                log('%s', e)
        ids = valid
        missing = [i for i in ids if not os.path.exists(self.local_path(i))]
        if self.is_stale() or (everything and not self.read_index().get('files')):
            self.sync(ids, everything)
        elif missing:
            for ident in missing:
                try:
                    self._download(ident)
                except Exception as e:  # noqa: BLE001
                    log('failed to download definition %s: %s', ident, e)

    def load(self, ident):
        path = self.local_path(ident)
        if not os.path.exists(path):
            try:
                self._download(ident)
            except Exception as e:  # noqa: BLE001
                raise IndexerError('definition "%s" not available: %s' % (ident, e))
        with open(path, encoding='utf-8-sig') as f:
            return yaml_load(f.read())

    def available_ids(self):
        ids = set()
        for folder in (self.dir, self.custom_dir):
            try:
                ids.update(n[:-4] for n in os.listdir(folder) if n.endswith('.yml'))
            except OSError:
                pass
        return sorted(ids)

    def quick_type(self, ident):
        try:
            with open(self.local_path(ident), encoding='utf-8-sig') as f:
                for line in f:
                    m = re.match(r'type:\s*["\']?([\w-]+)', line)
                    if m:
                        return m.group(1).lower()
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Cardigann indexer
# ---------------------------------------------------------------------------

OPTIONAL_FIELDS = frozenset(('imdb', 'imdbid', 'tmdbid', 'rageid', 'tvdbid', 'tvmazeid', 'traktid', 'doubanid',
                             'poster', 'genre', 'description'))


def _get(m, key, default=None):
    if not isinstance(m, dict):
        return default
    if key in m:
        v = m[key]
        return default if v is None else v
    low = key.lower()
    for k, v in m.items():
        if isinstance(k, str) and k.lower() == low:
            return default if v is None else v
    return default


def _pairs(m):
    if m is None:
        return []
    if isinstance(m, OMap):
        return m.pairs
    if isinstance(m, dict):
        return list(m.items())
    return []


class Release:
    __slots__ = ('title', 'link', 'magnet', 'details', 'size', 'seeders', 'leechers', 'peers', 'date',
                 'category', 'infohash', 'description', 'grabs', 'files', 'dlvf', 'ulvf', 'imdb')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        if self.date:
            d['date'] = self.date.isoformat()
        return d


class CardigannIndexer:
    def __init__(self, definition, settings=None, data_dir=DATA_DIR, timeout=20):
        if not isinstance(definition, dict) or not definition.get('id'):
            raise IndexerError('invalid definition')
        d = definition
        self.d = d
        self.id = str(d['id'])
        self.name = str(_get(d, 'name', self.id))
        self.type = str(_get(d, 'type', 'private')).lower()
        explicit_enc = _get(d, 'encoding')
        self.encoding = _py_encoding(explicit_enc or 'UTF-8')
        self.links = [str(x) for x in as_list(_get(d, 'links'))]
        if not self.links:
            raise IndexerError('definition has no links')
        self.user = dict(settings or {})
        self.data_dir = data_dir
        self.site_link = self._initial_link()
        self.followredirect = as_bool(_get(d, 'followredirect'))
        self.testlinktorrent = as_bool(_get(d, 'testlinktorrent'), True)
        try:
            self.request_delay = float(_get(d, 'requestDelay', 0) or 0)
        except ValueError:
            self.request_delay = 0.0
        self.login = _get(d, 'login')
        if isinstance(self.login, dict) and not _get(self.login, 'method'):
            self.login['method'] = 'form'
        self.search_def = _get(d, 'search') or OMap()
        paths = list(as_list(_get(self.search_def, 'paths')))
        if _get(self.search_def, 'path'):
            p = OMap()
            p.add('path', _get(self.search_def, 'path'))
            paths.append(p)
        self.paths = [p for p in paths if isinstance(p, dict)]
        self.settings_def = _get(d, 'settings')
        if self.settings_def is None:
            self.settings_def = [OMap(name='username', type='text'), OMap(name='password', type='password')]
        self.config = self._init_config()
        self.cat_map = []
        self.default_cats = []
        self._init_caps()
        self.cookie_file = os.path.join(data_dir, 'cookies', self.id + '.json')
        site_hosts = set()
        for link in self.links + [str(x) for x in as_list(_get(d, 'legacylinks'))] + [self.site_link]:
            host = (urllib.parse.urlsplit(link).hostname or '').lower()
            if host:
                site_hosts.add(host[4:] if host.startswith('www.') else host)
        fingerprints = {str(c).strip().lower() for c in as_list(_get(d, 'certificates')) if c}
        self.session = HttpSession(encoding=self.encoding, timeout=timeout, request_delay=self.request_delay,
                                   cookies=self._load_cookies() if self.login else None,
                                   force_encoding=bool(explicit_enc),
                                   pinned_certs={h: fingerprints for h in site_hosts} if fingerprints else None,
                                   cookie_domains=site_hosts)
        login_method = str(self.login.get('method') or 'form').lower() if isinstance(self.login, dict) else None
        if not is_blank(self.user.get('cookie')) and login_method != 'cookie':
            # manual cookies, e.g. Cloudflare's cf_clearance copied from the browser
            self.session.pinned_cookies = parse_cookie_header(str(self.user['cookie']))
        if not is_blank(self.user.get('user_agent')):
            self.session.user_agent = str(self.user['user_agent']).strip()
        self.logged_in = False

    # -- configuration -----------------------------------------------------

    def _state_file(self):
        return os.path.join(self.data_dir, 'state.json')

    _state_lock = threading.Lock()

    def _read_state(self):
        try:
            with open(self._state_file(), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _remember_link(self, link):
        with self._state_lock:
            st = self._read_state()
            st.setdefault('links', {})[self.id] = link
            try:
                _atomic_write(self._state_file(), json.dumps(st, indent=1).encode('utf-8'))
            except OSError:
                pass

    def _initial_link(self):
        link = self.user.get('sitelink')
        if not link:
            remembered = self._read_state().get('links', {}).get(self.id)
            known = {link.rstrip("/") for link in self.links}
            link = remembered if remembered and remembered.rstrip('/') in known else self.links[0]
        return link if link.endswith('/') else link + '/'

    def _init_config(self):
        vals = {}
        for s in as_list(self.settings_def):
            if not isinstance(s, dict) or not s.get('name'):
                continue
            name = s['name']
            typ = s.get('type')
            default = s.get('default')
            has_user = name in self.user and self.user[name] is not None
            uval = self.user.get(name)
            if typ == 'checkbox':
                vals[name] = as_bool(uval) if has_user else (str(default).lower() == 'true')
            elif typ == 'multi-select':
                v = uval if has_user else s.get('defaults')
                vals[name] = [str(x) for x in as_list(v)]
            else:
                vals[name] = str(uval) if has_user else default
        return vals

    def _setting_type(self, name):
        for s in as_list(self.settings_def):
            if isinstance(s, dict) and s.get('name') == name:
                return s.get('type')
        return None

    def base_variables(self):
        today = date.today()
        v = {'.Config.sitelink': self.site_link, '.True': 'True', '.False': None,
             '.Today.Year': str(today.year if today.month > 1 else today.year - 1)}
        for name, val in self.config.items():
            if self._setting_type(name) == 'checkbox':
                v['.Config.' + name] = 'True' if val else None
            else:
                v['.Config.' + name] = val
        return v

    # -- categories --------------------------------------------------------

    def _add_cat(self, tracker, tid, desc):
        tracker = '' if tracker is None else str(tracker)
        self.cat_map.append((tracker, tid, desc))
        if desc:
            if tracker.isdigit():
                custom = int(tracker) + 100000
            else:
                custom = int.from_bytes(hashlib.sha1(tracker.encode('utf-8')).digest()[:2], 'little') + 100000
            self.cat_map.append((tracker, custom, desc))

    def _init_caps(self):
        caps = _get(self.d, 'caps') or {}
        for k, name in _pairs(_get(caps, 'categories')):
            tid = TORZNAB_CATS.get(name)
            if tid is not None:
                self._add_cat(k, tid, None)
        for cm in as_list(_get(caps, 'categorymappings')):
            if not isinstance(cm, dict):
                continue
            catname = cm.get('cat')
            tid = TORZNAB_CATS.get(catname) if catname else None
            if tid is None:
                continue
            self._add_cat(cm.get('id'), tid, cm.get('desc'))
            if as_bool(cm.get('default')):
                self.default_cats.append(str(cm.get('id')))

    def map_query_categories(self, torznab_ids):
        if not torznab_ids:
            return []
        out = []
        for tracker, tid, _ in self.cat_map:
            if tid in torznab_ids and tracker not in out:
                out.append(tracker)
        return out

    def tracker_to_torznab(self, value):
        if is_blank(value):
            return []
        low = value.lower()
        return [tid for tracker, tid, _ in self.cat_map if tracker and tracker.lower() == low]

    def trackerdesc_to_torznab(self, value):
        if is_blank(value):
            return []
        low = value.lower()
        return [tid for _, tid, desc in self.cat_map if desc and desc.lower() == low]

    # -- cookies -----------------------------------------------------------

    def _load_cookies(self):
        try:
            with open(self.cookie_file, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('site') == self.site_link:
                return data.get('cookies') or {}
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    def _save_cookies(self):
        try:
            _atomic_write(self.cookie_file, json.dumps(
                {'site': self.site_link, 'saved': time.time(), 'cookies': self.session.cookies}).encode('utf-8'),
                private=True)
        except OSError as e:
            log('%s: could not save cookies: %s', self.id, e)

    # -- http helpers ------------------------------------------------------

    def _headers(self, block, variables):
        out = {}
        for k, v in _pairs(block):
            first = v[0] if isinstance(v, list) and v else v
            if first is not None:
                out[k] = apply_template(str(first), variables)
        return out

    def _request(self, url, method='GET', data=None, headers=None, referer=None, follow=False, body=None):
        resp = self.session.request(url, method, data=data, headers=headers, referer=referer, follow=follow,
                                    body=body)
        detect_protection(resp)
        if resp.status == 429:
            raise IndexerError('HTTP 429 Too Many Requests')
        return resp

    def resolve(self, path, base=None):
        return resolve_url(base or self.site_link, path)

    def _domain_hint(self, request_url, redirect_url):
        if not redirect_url or not request_url.startswith(self.site_link):
            return None
        r = urllib.parse.urlsplit(redirect_url)
        s = urllib.parse.urlsplit(self.site_link)
        if r.hostname != s.hostname and r.scheme in ('http', 'https'):
            return '%s://%s/' % (r.scheme, r.netloc)
        return None

    # -- login -------------------------------------------------------------

    def _check_errors(self, resp, errors, doc=None):
        if resp.status == 401:
            raise LoginError('401 Unauthorized, check your credentials')
        if not errors:
            return
        doc = doc if doc is not None else resp.doc
        for err in as_list(errors):
            if not isinstance(err, dict) or not err.get('selector'):
                continue
            sel = select_one(doc, err['selector'])
            if sel is not None:
                msg = sel.text_content()
                if err.get('message'):
                    try:
                        msg = self.handle_selector(err['message'], document_element(doc), self.base_variables()) \
                            or msg
                    except Exception:  # noqa: BLE001
                        pass
                raise LoginError('Error: %s' % (msg or '').strip())

    def ensure_login(self, force=False):
        if not isinstance(self.login, dict):
            return
        method = (self.login.get('method') or 'form').lower()
        if method == 'cookie':
            cookie = self.config.get('cookie') or ''
            if is_blank(cookie):
                raise LoginError('set "cookie" for %s in jackettless.json' % self.id)
            self.session.cookies.update(parse_cookie_header(cookie))
            self.logged_in = True
            return
        if self.logged_in and not force:
            return
        if self.session.cookies and not force:
            self.logged_in = True  # trust saved cookies, re-login happens if the site asks for it
            return
        self.do_login()
        self.test_login()
        self._save_cookies()
        self.logged_in = True

    def _require_credentials(self):
        inputs = ' '.join(str(v) for _, v in _pairs(self.login.get('inputs')))
        for name in ('username', 'password', 'apikey', 'passkey', 'pin', 'email'):
            if ('.Config.%s ' % name in inputs + ' ' or '.Config.%s}' % name in inputs) and \
                    is_blank(self.config.get(name)):
                raise LoginError('set "%s" for %s in jackettless.json' % (name, self.id))

    def do_login(self):
        login = self.login
        method = (login.get('method') or 'form').lower()
        variables = self.base_variables()
        headers = self._headers(login.get('headers') or _get(self.search_def, 'headers'), variables)
        self._require_credentials()
        preset = as_list(login.get('cookies'))
        if method == 'post':
            pairs = [(k, apply_template(v, variables) or '') for k, v in _pairs(login.get('inputs'))]
            url = self.resolve(apply_template(login.get('path'), variables))
            self.session.cookies = parse_cookie_header('; '.join(preset))
            resp = self._request(url, 'POST', data=pairs, headers=headers, referer=self.site_link, follow=True)
            self._check_errors(resp, login.get('error'))
        elif method == 'form':
            self._login_form(login, variables, headers, preset)
        elif method == 'get':
            pairs = [(k, apply_template(v, variables) or '') for k, v in _pairs(login.get('inputs'))]
            url = self.resolve(apply_template(login.get('path'), variables) + '?' + query_string(pairs, self.encoding))
            self.session.cookies = {}
            resp = self._request(url, headers=headers, referer=self.site_link)
            self._check_errors(resp, login.get('error'))
        elif method == 'oneurl':
            one = apply_template(_get(login.get('inputs'), 'oneurl'), variables) or ''
            url = self.resolve(apply_template(login.get('path'), variables) + one)
            self.session.cookies = {}
            resp = self._request(url, headers=headers, referer=self.site_link)
            self._check_errors(resp, login.get('error'))
        else:
            raise LoginError('login method %s not implemented' % method)

    def _login_form(self, login, variables, headers, preset):
        login_url = self.resolve(apply_template(login.get('path'), variables))
        self.session.cookies = parse_cookie_header('; '.join(preset))
        landing = self._request(login_url, headers=headers, referer=self.site_link, follow=self.followredirect)
        doc = parse_html(landing.text)
        captcha = login.get('captcha')
        if isinstance(captcha, dict) and captcha.get('selector') and select_one(doc, captcha['selector']) is not None:
            raise CaptchaRequired('%s asks for a captcha on login: use the cookie method instead' % self.id)
        form = select_one(doc, login.get('form') or 'form')
        if form is None:
            raise LoginError('Login failed: no form found on %s using selector %s'
                             % (login_url, login.get('form') or 'form'))
        pairs = OMap()
        for inp in select_all(form, 'input'):
            name = inp.get('name')
            if name is None or 'disabled' in inp.attrs:
                continue
            if (inp.get('type') or '').lower() in ('checkbox', 'radio') and 'checked' not in inp.attrs:
                continue
            pairs[name] = inp.get('value') or ''
        submit = form.get('action')
        if login.get('submitpath'):
            submit = login.get('submitpath')
        use_selectors = as_bool(login.get('selectors'))
        for key, value in _pairs(login.get('inputs')):
            v = apply_template(value, variables) or ''
            if use_selectors:
                el = select_one(doc, key)
                if el is None:
                    raise LoginError('Login failed: no input found using selector %s' % key)
                key = el.get('name')
            pairs[key] = v
        root = document_element(doc)
        for key, block in _pairs(login.get('selectorinputs')):
            optional = as_bool(_get(block, 'optional'))
            v = self.handle_selector(block, root, variables, required=not optional)
            if v is None and optional:
                continue
            pairs[key] = v
        query = []
        for key, block in _pairs(login.get('getselectorinputs')):
            optional = as_bool(_get(block, 'optional'))
            v = self.handle_selector(block, root, variables, required=not optional)
            if v is None and optional:
                continue
            query.append((key, v))
        submit = submit or ''
        if query:
            submit += '?' + query_string(query, self.encoding)
        submit_url = self.resolve(submit, login_url)
        if select_one(doc, 'script[src*="simpleCaptcha"]') is not None:
            cap = self._request(self.resolve('simpleCaptcha.php?numImages=1'), referer=login_url, headers=headers)
            pairs['captchaSelection'] = json.loads(cap.text)['images'][0]['hash']
            pairs['submitme'] = 'X'
        items = [(k, '' if v is None else v) for k, v in pairs.items()]
        if (form.get('enctype') or '').lower() == 'multipart/form-data':
            boundary = '---------------------------%d' % int(time.time() * 1000)
            parts = ['--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s' % (boundary, k, v)
                     for k, v in items]
            parts.append('--%s--' % boundary)
            h = dict(headers)
            h['Content-Type'] = 'multipart/form-data; boundary=' + boundary
            resp = self._request(submit_url, 'POST', headers=h, referer=self.site_link, body='\r\n'.join(parts),
                                 follow=True)
        else:
            resp = self._request(submit_url, 'POST', data=items, headers=headers, referer=login_url, follow=True)
        self._check_errors(resp, login.get('error'))

    def test_login(self):
        login = self.login
        test = login.get('test') if isinstance(login, dict) else None
        if not isinstance(test, dict):
            return False
        variables = self.base_variables()
        headers = self._headers(login.get('headers') or _get(self.search_def, 'headers'), variables)
        url = self.resolve(test.get('path'))
        resp = self._request(url, headers=headers)
        if resp.is_redirect and self._domain_hint(resp.url, resp.redirect_to) is None:
            resp = self._request(resp.redirect_to, headers=headers)
        if resp.is_redirect:
            msg = 'Login failed, got redirected to: %s' % resp.redirect_to
            hint = self._domain_hint(resp.url, resp.redirect_to)
            if hint:
                msg += ' (try "sitelink": "%s")' % hint
            raise LoginError(msg)
        if test.get('selector'):
            if not select_all(resp.doc, test['selector']):
                raise LoginError('Login failed: selector "%s" did not match' % test['selector'])
        return True

    def login_needed(self, resp):
        if resp.is_redirect:
            hint = self._domain_hint(resp.url, resp.redirect_to)
            if hint:
                raise IndexerError('got redirected to another domain, try "sitelink": "%s"' % hint)
            return True
        login = self.login
        if not isinstance(login, dict) or not isinstance(login.get('test'), dict):
            return False
        sel = login['test'].get('selector')
        ctype = resp.content_type
        if sel and (not ctype or 'text/html' in ctype):
            if not select_all(resp.doc, sel):
                return True
        return False

    def relogin(self):
        log('%s: logging in', self.id, debug=True)
        self.session.cookies = {}
        self.do_login()
        self.test_login()
        self._save_cookies()
        self.logged_in = True

    # -- selectors ---------------------------------------------------------

    def handle_selector(self, block, dom, variables=None, required=True):
        if not isinstance(block, dict):
            block = {'text': block}
        variables = variables if variables is not None else self.base_variables()
        filters = block.get('filters')
        if block.get('text') is not None:
            return apply_filters(apply_template(block['text'], variables), filters, variables, self.encoding)
        selection = dom
        if block.get('selector') is not None:
            sel = apply_template(block['selector'], variables)
            selection = dom if matches(dom, sel) else select_one(dom, sel)
            if selection is None:
                if required:
                    raise IndexerError('Selector "%s" didn\'t match' % sel)
                return None
        if block.get('remove'):
            for el in select_all(selection, block['remove']):
                el.detach()
        value = None
        case = block.get('case')
        if case:
            for key, val in _pairs(case):
                if matches(selection, key) or select_one(selection, key) is not None:
                    value = apply_template(val, variables)
                    break
            if value is None:
                if required:
                    raise IndexerError('None of the case selectors matched')
                return None
        elif block.get('attribute') is not None:
            value = selection.get(block['attribute'])
            if value is None:
                if required:
                    raise IndexerError('Attribute "%s" is not set' % block['attribute'])
                return None
        else:
            value = selection.text_content()
        return apply_filters((value or '').strip(), filters, variables, self.encoding)

    def handle_json_selector(self, block, parent, variables, required=True):
        if not isinstance(block, dict):
            block = {'text': block}
        filters = block.get('filters')
        if block.get('text') is not None:
            return apply_filters(apply_template(block['text'], variables), filters, variables, self.encoding)
        value = None
        if block.get('selector') is not None:
            sel = apply_template(block['selector'].lstrip('.'), variables)
            path = json_field_selector(parent, sel)
            selection = json_select(parent, path) if path is not None else _MISSING
            if selection is _MISSING:
                if required:
                    raise IndexerError('Selector "%s" didn\'t match' % sel)
                return None
            value = json_value_str(selection)
        case = block.get('case')
        if case:
            for key, val in _pairs(case):
                if (value is not None and value == key) or key == '*':
                    value = apply_template(val, variables)
                    break
            if value is None:
                if required:
                    raise IndexerError('None of the case selectors matched')
                return None
        return apply_filters((value or '').strip(), filters, variables, self.encoding)

    # -- fields ------------------------------------------------------------

    def parse_field(self, value, name, rel, mods, search_url):
        if name == 'download':
            if not value:
                rel.link = None
                return None
            if value.startswith('magnet:'):
                rel.magnet = value
            else:
                rel.link = self.resolve(value, search_url)
                value = rel.link
        elif name == 'magnet':
            rel.magnet = value
        elif name == 'infohash':
            rel.infohash = value
        elif name == 'details':
            rel.details = self.resolve(value, search_url)
            value = rel.details
        elif name == 'title':
            rel.title = (rel.title or '') + value if 'append' in mods else value
            value = rel.title
        elif name == 'description':
            rel.description = (rel.description or '') + value if 'append' in mods else value
            value = rel.description
        elif name in ('category', 'categorydesc'):
            cats = self.tracker_to_torznab(value) if name == 'category' else self.trackerdesc_to_torznab(value)
            if cats:
                if rel.category is None or 'noappend' in mods:
                    rel.category = list(dict.fromkeys(cats))
                else:
                    rel.category = list(dict.fromkeys(rel.category + cats))
            if rel.category:
                value = ','.join(str(c) for c in rel.category)
        elif name == 'size':
            rel.size = get_bytes(value)
            value = str(rel.size)
        elif name in ('seeders', 'leechers'):
            n = coerce_long(value)
            n = n if n < 5000000 else 0
            if name == 'seeders':
                rel.seeders = n
            else:
                rel.leechers = n
            rel.peers = (rel.peers or 0) + n
            value = str(n)
        elif name == 'date':
            rel.date = from_unknown(value)
            value = fmt_rfc(rel.date)
        elif name == 'files':
            rel.files = coerce_long(value)
            value = str(rel.files)
        elif name == 'grabs':
            rel.grabs = coerce_long(value)
            value = str(rel.grabs)
        elif name == 'downloadvolumefactor':
            rel.dlvf = coerce_double(value)
            value = _num_str(rel.dlvf)
        elif name == 'uploadvolumefactor':
            rel.ulvf = coerce_double(value)
            value = _num_str(rel.ulvf)
        elif name in ('minimumratio',):
            value = _num_str(coerce_double(value))
        elif name in ('minimumseedtime', 'year'):
            value = str(coerce_long(value))
        elif name in ('imdb', 'imdbid', 'tmdbid', 'rageid', 'tvdbid', 'tvmazeid', 'traktid', 'doubanid'):
            n = get_long_from_string(value) or 0
            if name in ('imdb', 'imdbid'):
                rel.imdb = n
            value = str(n)
        elif name == 'poster':
            if not is_blank(value):
                value = self.resolve(value, search_url)
        return value

    def _process_fields(self, rel, fields, variables, search_url, getter):
        for key, block in _pairs(fields):
            parts = key.split('|')
            name, mods = parts[0], parts[1:]
            var_key = '.Result.' + name
            optional = key in OPTIONAL_FIELDS or 'optional' in mods or (
                isinstance(block, dict) and as_bool(block.get('optional')))
            value = None
            try:
                value = getter(block, not optional)
                if optional and is_blank(value):
                    default = apply_template(block.get('default') if isinstance(block, dict) else None, variables)
                    if is_blank(default):
                        variables[var_key] = None
                        continue
                    value = default
                variables[var_key] = self.parse_field(value, name, rel, mods, search_url)
            except Exception as e:  # noqa: BLE001
                if var_key not in variables or optional:
                    variables[var_key] = None
                if optional:
                    continue
                raise IndexerError('field %s: %s (value=%r)' % (key, e, value))

    def _row_filters(self, filters, rel, variables):
        for flt in as_list(filters):
            if not isinstance(flt, dict):
                continue
            if flt.get('name') == 'andmatch':
                limit = int(flt['args']) if flt.get('args') not in (None, '') else -1
                q = variables.get('.Keywords') or ''
                if limit > 0:
                    q = q[:limit]
                words = [w for w in re.split(r'[^\w]+', q) if w and len(w) > 1
                         and w.lower() not in ('and', 'the', 'an')]
                title = (rel.title or '').lower()
                if not all(w.lower() in title for w in words):
                    return True
        return False

    # -- search ------------------------------------------------------------

    def search(self, keywords, qbt_cat='all', emit=None, deadline=None):
        """Run a keyword search. `emit(release)` is called for every result as soon as it is parsed."""
        self.session.deadline = deadline
        candidates = [self.site_link] + [link if link.endswith("/") else link + "/" for link in self.links]
        candidates = list(dict.fromkeys(candidates))
        if self.user.get('sitelink'):
            candidates = candidates[:1]
        first_error = None
        for attempt, link in enumerate(candidates[:3]):
            if link != self.site_link:
                log('%s: trying mirror %s', self.id, link, debug=True)
                self.site_link = link
                self.session.cookies = self._load_cookies() if self.login else {}
                self.logged_in = False
            counter = {'n': 0}

            def counting_emit(rel):
                counter['n'] += 1
                if emit:
                    emit(rel)
            try:
                results = self._search_once(keywords, qbt_cat, counting_emit)
                if attempt:
                    self._remember_link(link)
                return results
            except (NetworkError, CloudflareError) as e:
                if counter['n'] or self.login:
                    raise
                first_error = first_error or e
                if deadline is not None and time.time() > deadline:
                    break
        raise first_error or IndexerError('no usable link')

    def _query_variables(self, keywords, torznab_ids):
        v = self.base_variables()
        for k in ('Series', 'Ep', 'Season', 'Movie', 'Year', 'APIKey', 'TVDBID', 'TVRageID', 'IMDBID',
                  'IMDBIDShort', 'TMDBID', 'TVMazeID', 'TraktID', 'DoubanID', 'Album', 'Artist', 'Label', 'Track',
                  'Genre', 'Author', 'Title', 'Publisher'):
            v['.Query.' + k] = None
        for k in ('IsBookSearch', 'IsDoubanQuery', 'IsGenreQuery', 'IsIdSearch', 'IsImdbQuery', 'IsMovieSearch',
                  'IsMusicSearch', 'IsRssSearch', 'IsTVRageQuery', 'IsTVSearch', 'IsTmdbQuery', 'IsTraktQuery',
                  'IsTvdbQuery', 'IsTvmazeQuery'):
            v['.Query.' + k] = None
        v.update({'.Query.Type': 'search', '.Query.Q': keywords, '.Query.Limit': '0', '.Query.Offset': '0',
                  '.Query.Extended': '0', '.Query.Episode': '', '.Query.IsSearch': 'True',
                  '.Query.Categories': [str(c) for c in sorted(torznab_ids or [])]})
        if is_blank(keywords):
            v['.Query.IsRssSearch'] = 'True'
        return v

    def _search_once(self, keywords, qbt_cat, emit):
        self.ensure_login()
        search = self.search_def
        torznab_ids = QBT_CATEGORIES.get((qbt_cat or 'all').lower())
        variables = self._query_variables(keywords, torznab_ids)
        mapped = self.map_query_categories(torznab_ids) or list(self.default_cats)
        variables['.Query.Keywords'] = keywords.strip() if not is_blank(keywords) else ''
        variables['.Keywords'] = apply_filters(variables['.Query.Keywords'], _get(search, 'keywordsfilters'),
                                               variables, self.encoding)
        enc_mod = lambda s: net_url_encode(s)  # noqa: E731  (WebUtility.UrlEncode is always UTF-8)
        seen = []
        releases = []
        rows_def = _get(search, 'rows') or {}
        fields = _get(search, 'fields')
        relogged = False
        for path in self.paths:
            variables['.Categories'] = mapped
            pcats = [str(c) for c in as_list(_get(path, 'categories'))]
            if pcats:
                inter = [c for c in dict.fromkeys(mapped) if c in pcats]
                has = bool(inter)
                if pcats[0] == '!':
                    has = not has
                if not has:
                    variables['.Categories'] = [c for c in mapped if c not in pcats]
                    continue
                variables['.Categories'] = inter
            url = self.resolve(apply_template(_get(path, 'path', ''), variables, enc_mod).replace('+', '%20'))
            method = 'POST' if str(_get(path, 'method', '')).lower() == 'post' else 'GET'
            pairs = []
            inputs_list = []
            if as_bool(_get(path, 'inheritinputs'), True):
                inputs_list.append(_get(search, 'inputs'))
            inputs_list.append(_get(path, 'inputs'))
            for inputs in inputs_list:
                for k, val in _pairs(inputs):
                    if k == '$raw':
                        raw = apply_template(val, variables, enc_mod) or ''
                        for part in raw.split('&'):
                            kv = part.split('=', 1)
                            if not kv[0]:
                                continue
                            pairs.append((kv[0], kv[1] if len(kv) == 2 else ''))
                    else:
                        iv = apply_template(val, variables)
                        if not is_blank(iv) or as_bool(_get(search, 'allowEmptyInputs')):
                            pairs.append((k, iv or ''))
            if method == 'GET' and pairs:
                url += ('&' if '?' in url else '?') + query_string(pairs, self.encoding)
            if method == 'GET' and url in seen:
                continue
            seen.append(url)
            headers = self._headers(_get(search, 'headers'), variables)
            post_data = pairs if method == 'POST' else None
            follow = as_bool(_get(path, 'followredirect'))
            resp = self._request(url, method, data=post_data, headers=headers, follow=follow)
            if resp.is_redirect and not self.login:
                hint = self._domain_hint(resp.url, resp.redirect_to)
                if hint:
                    raise IndexerError('redirected to another domain, try "sitelink": "%s"' % hint)
                resp = self._request(resp.redirect_to, headers=headers, follow=True)
            response = _get(path, 'response') or {}
            rtype = str(_get(response, 'type', '')).lower()
            if self.login and rtype != 'xml' and not relogged and self.login_needed(resp):
                relogged = True
                self.relogin()
                resp = self._request(url, method, data=post_data, headers=headers, follow=follow)
            if rtype == 'json':
                for rel in self._parse_json(resp, response, rows_def, fields, variables, url):
                    if self._finish(rel, torznab_ids, emit):
                        releases.append(rel)
            else:
                for rel in self._parse_html(resp, rtype == 'xml', rows_def, fields, variables, url):
                    if self._finish(rel, torznab_ids, emit):
                        releases.append(rel)
        return releases

    def _finish(self, rel, torznab_ids, emit):
        """FilterResults + FixResults; returns True when the release is kept."""
        if is_blank(rel.title):
            return False
        if torznab_ids and rel.category and not (set(rel.category) & set(torznab_ids)):
            return False
        now = datetime.now()
        if rel.date and rel.date > now:
            rel.date = now
        if not rel.magnet and not is_blank(rel.infohash) and self.type != 'private':
            rel.magnet = public_magnet(rel.infohash, rel.title)
        if not rel.link and not rel.magnet:
            return False
        if emit:
            emit(rel)
        return True

    def _parse_json(self, resp, response, rows_def, fields, variables, search_url):
        text = resp.text
        if resp.status != 200:
            raise IndexerError('HTTP %d from %s' % (resp.status, resp.url))
        nrm = _get(response, 'noResultsMessage') if 'noResultsMessage' in response else None
        if nrm is not None and ((nrm != '' and nrm in text) or (nrm == '' and text == '')):
            return
        try:
            parsed = json.loads(text)
        except ValueError:
            raise IndexerError('Error parsing JSON response (%d bytes)' % len(text))
        if _get(rows_def, 'count') is not None:
            try:
                cnt = self.handle_json_selector(rows_def['count'], parsed, variables)
                if cnt is not None and re.fullmatch(r'-?\d+', cnt.strip()) and int(cnt) < 1:
                    return
            except Exception:  # noqa: BLE001
                pass
        rows = self._json_rows(parsed, _get(rows_def, 'selector', ''))
        if rows is None:
            if as_bool(_get(rows_def, 'missingAttributeEqualsNoResults')):
                return
            raise IndexerError('Error parsing rows selector: there are 0 rows')
        attribute = _get(rows_def, 'attribute')
        multiple = as_bool(_get(rows_def, 'multiple'))
        for row in rows:
            sel_obj = row
            if attribute:
                sel_obj = json_select(row, attribute)
                if sel_obj is _MISSING or sel_obj is None:
                    if as_bool(_get(rows_def, 'missingAttributeEqualsNoResults')):
                        continue
                    sel_obj = None
            if multiple:
                mul = list(sel_obj.values()) if isinstance(sel_obj, dict) else (sel_obj or [])
            else:
                mul = [sel_obj]
            for mrow in mul:
                if not isinstance(mrow, dict):
                    continue
                rel = Release()

                def getter(block, required, mrow=mrow, row=row):
                    parent = row if isinstance(block, dict) and str(block.get('selector') or '').startswith('..') \
                        else mrow
                    return self.handle_json_selector(block, parent, variables, required)
                try:
                    self._process_fields(rel, fields, variables, search_url, getter)
                except IndexerError as e:
                    log('%s: skipping row: %s', self.id, e, debug=True)
                    continue
                if self._row_filters(_get(rows_def, 'filters'), rel, variables):
                    continue
                yield rel

    def _json_rows(self, parsed, selector):
        selector = apply_template(selector, self.base_variables()) or ''
        path = selector.split(':')[0]
        arr = json_select(parsed, path) if path else parsed
        if not isinstance(arr, list):
            return None
        rest = selector[len(path):]
        out = []
        for item in arr:
            if not isinstance(item, dict):
                return None
            if json_field_selector(item, rest) is not None:
                out.append(item)
        return out

    def _parse_html(self, resp, is_xml, rows_def, fields, variables, search_url):
        text = resp.text
        search = self.search_def
        pre = _get(search, 'preprocessingfilters')
        if is_xml:
            if pre:
                text = apply_filters(text, pre, variables, self.encoding)
            doc = parse_xml(text)
        else:
            doc = resp.doc  # already parsed if login_needed() looked at it
            self._check_errors(resp, _get(search, 'error'), doc)
            if pre:
                text = apply_filters(text, pre, variables, self.encoding)
                doc = parse_html(text)
        rows_sel = apply_template(_get(rows_def, 'selector', ''), variables)
        rows = select_all(doc, rows_sel)
        try:
            after = int(_get(rows_def, 'after', 0) or 0)
        except ValueError:
            after = 0
        if after > 0:
            i = 0
            while i < len(rows):
                cur = rows[i]
                for j in range(after):
                    k = i + j + 1
                    if k < len(rows):
                        for ch in list(rows[k].children):
                            if isinstance(ch, Element):
                                ch.parent = cur
                            cur.children.append(ch)
                        rows[k].children = []
                del rows[i + 1:i + 1 + after]
                i += 1
        dateheaders = _get(rows_def, 'dateheaders')
        for row in rows:
            rel = Release()
            try:
                self._process_fields(rel, fields, variables, search_url,
                                     lambda block, required, row=row: self.handle_selector(block, row, variables,
                                                                                            required))
                if self._row_filters(_get(rows_def, 'filters'), rel, variables):
                    continue
                if rel.date is None and isinstance(dateheaders, dict):
                    value = None
                    prev = _prev_sibling(row) or (_prev_sibling(row.parent) if _is_element(row.parent) else None)
                    while prev is not None:
                        try:
                            value = self.handle_selector(dateheaders, prev, variables)
                            break
                        except Exception:  # noqa: BLE001
                            pass
                        cur = prev
                        prev = _prev_sibling(cur) or (_prev_sibling(cur.parent) if _is_element(cur.parent) else None)
                    if value is None and not as_bool(dateheaders.get('optional')):
                        raise IndexerError('no date header row found')
                    if value is not None:
                        rel.date = from_unknown(value)
            except Exception as e:  # noqa: BLE001
                log('%s: skipping row: %s', self.id, e, debug=True)
                continue
            yield rel

    # -- download ----------------------------------------------------------

    def _match_selector(self, resp, block, variables):
        sel = apply_template(block.get('selector'), variables)
        el = select_one(resp.doc, sel)
        if el is None:
            return None
        if block.get('attribute') is not None:
            val = el.get(block['attribute'])
            if val is None:
                raise IndexerError('Attribute "%s" is not set' % block['attribute'])
        else:
            val = el.text_content()
        return apply_filters(val, block.get('filters'), variables, self.encoding)

    def _get_redirectable(self, url, headers):
        return self._request(url, headers=headers, follow=True)

    def _handle_request(self, block, variables, referer, headers, path=None):
        url = self.resolve(apply_template(path if path is not None else block.get('path'), variables))
        method = 'POST' if str(block.get('method') or '').lower() == 'post' else 'GET'
        pairs = [(k, apply_template(v, variables) or '') for k, v in _pairs(block.get('inputs'))]
        if method == 'GET' and pairs:
            if '?' not in url:
                url += '?'
            url += query_string(pairs, self.encoding, block.get('queryseparator') or '&')
        return self._request(url, method, data=pairs if method == 'POST' else None, headers=headers, referer=referer)

    def download(self, link):
        """Returns ('magnet', uri) or ('torrent', bytes)."""
        self.ensure_login()
        dl = _get(self.d, 'download')
        method = 'GET'
        headers = {}
        if isinstance(dl, dict):
            variables = self.base_variables()
            u = urllib.parse.urlsplit(link)
            variables.update({'.DownloadUri.AbsoluteUri': link, '.DownloadUri.AbsolutePath': u.path,
                              '.DownloadUri.Scheme': u.scheme, '.DownloadUri.Host': u.hostname or '',
                              '.DownloadUri.Port': str(u.port or (443 if u.scheme == 'https' else 80)),
                              '.DownloadUri.PathAndQuery': u.path + ('?' + u.query if u.query else ''),
                              '.DownloadUri.Query': '?' + u.query if u.query else ''})
            for k, vals in urllib.parse.parse_qs(u.query, keep_blank_values=True).items():
                variables['.DownloadUri.Query.' + k] = vals[0]
            headers = self._headers(dl.get('headers') or _get(self.search_def, 'headers'), variables)
            response = None
            before = dl.get('before')
            if isinstance(before, dict):
                path = None
                if isinstance(before.get('pathselector'), dict):
                    response = self._get_redirectable(link, headers)
                    path = self._match_selector(response, before['pathselector'], variables)
                response = self._handle_request(before, variables, link, headers, path)
            if str(dl.get('method') or '').lower() == 'post':
                method = 'POST'
            infohash = dl.get('infohash')
            if isinstance(infohash, dict):
                try:
                    if not as_bool(infohash.get('usebeforeresponse')) or not before or response is None:
                        response = self._get_redirectable(link, headers)
                    h = self._match_selector(response, infohash.get('hash') or {}, variables)
                    t = self._match_selector(response, infohash.get('title') or {}, variables)
                    if not h or not t:
                        raise IndexerError('infohash selectors did not match')
                    return 'magnet', public_magnet(h, t)
                except Exception as e:  # noqa: BLE001
                    log('%s: infohash block failed: %s', self.id, e, debug=True)
            elif dl.get('selectors'):
                found = None
                for sel in as_list(dl['selectors']):
                    if not isinstance(sel, dict):
                        continue
                    try:
                        if not as_bool(sel.get('usebeforeresponse')) or not before or response is None:
                            response = self._get_redirectable(link, headers)
                        href = self._match_selector(response, sel, variables)
                        if not href:
                            continue
                        target = self.resolve(href, link)
                        if target.lower().startswith('magnet:'):
                            return 'magnet', target
                        if self.testlinktorrent:
                            test = self._get_redirectable(target, headers)
                            if test.is_redirect and test.redirect_to.lower().startswith('magnet:'):
                                return 'magnet', test.redirect_to
                            if test.content[:1] != b'd':
                                continue
                            return 'torrent', test.content
                        found = target
                        break
                    except IndexerError as e:
                        log('%s: download selector failed: %s', self.id, e, debug=True)
                if not found:
                    raise IndexerError("download selectors didn't match")
                link = found
        resp = self._request(link, method, headers=headers, follow=True)
        if resp.is_redirect and resp.redirect_to.lower().startswith('magnet:'):
            return 'magnet', resp.redirect_to
        if resp.content[:7] == b'magnet:':
            return 'magnet', resp.content.decode('utf-8', 'replace').strip()
        if resp.content[:1] != b'd':
            raise IndexerError('the server did not return a torrent file (HTTP %d, %s)'
                               % (resp.status, resp.content_type or 'unknown type'))
        return 'torrent', resp.content


def _num_str(x):
    return str(int(x)) if x == int(x) else repr(x)


# ---------------------------------------------------------------------------
# configuration + orchestration
# ---------------------------------------------------------------------------

def load_config(path=CONFIG_FILE):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not os.path.exists(path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
            log('created default configuration at %s', path, debug=True)
        except OSError as e:
            log('could not write %s: %s', path, e)
        return cfg
    try:
        with open(path, encoding='utf-8-sig') as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError('top level must be an object')
    except (OSError, ValueError) as e:
        log('malformed configuration file %s: %s', path, e)
        cfg['_error'] = re.sub(r'\s*\(char \d+\)', '', str(e))
        return cfg
    cfg.update(user)
    if not isinstance(cfg.get('indexers'), dict):
        cfg['indexers'] = {i: {} for i in as_list(cfg.get('indexers'))}
    return cfg


def enabled_indexers(cfg, store):
    ids = [i for i, s in cfg['indexers'].items() if not (isinstance(s, dict) and s.get('enabled') is False)]
    disabled = {i for i, s in cfg['indexers'].items() if isinstance(s, dict) and s.get('enabled') is False}
    if as_bool(cfg.get('all_public')):
        for ident in store.available_ids():
            if ident not in ids and ident not in disabled and store.quick_type(ident) == 'public':
                ids.append(ident)
    return ids


def make_store(cfg, data_dir=DATA_DIR):
    return DefinitionStore(data_dir, cfg.get('definitions_ref', 'master'), cfg.get('refresh_days', 1),
                           cfg.get('timeout', 20))


def build_indexer(ident, cfg, store, data_dir=DATA_DIR):
    definition = store.load(ident)
    settings = cfg['indexers'].get(ident) or {}
    return CardigannIndexer(definition, settings if isinstance(settings, dict) else {}, data_dir,
                            float(cfg.get('timeout', 20)))


PLUGIN_URL = 'https://github.com/gusperroni/jackettless'
DL_MARK = '#jackettless='
ERROR_MARK = '#jackettless-error'


def describe_error(exc):
    """Short, user facing reason for an indexer failure."""
    if isinstance(exc, CloudflareError):
        return 'Cloudflare'
    if isinstance(exc, DeadlineExceeded):
        return 'timeout'
    if isinstance(exc, NetworkError):
        msg = str(exc)
        if 'getaddrinfo' in msg or 'Name or service' in msg or 'DNS lookup failed' in msg:
            return 'DNS blocked/failed'
        if 'timed out' in msg:
            return 'timeout'
        return 'network error'
    if isinstance(exc, LoginError):
        return str(exc) or 'login failed'
    msg = str(exc)
    if 'HTTP' in msg or 'redirected' in msg:
        return msg[:80]
    return type(exc).__name__


HELP_URL = PLUGIN_URL + '#troubleshooting'


def error_result(text, query=''):
    """A message row. qBittorrent hides rows whose name lacks the search words ("Torrent names only"
    filter, on by default), so the query goes into the name, early enough to survive truncation."""
    prefix = '[jackettless] (search: %s) ' % query.strip() if query and query.strip() else '[jackettless] '
    name = re.sub(r'\s+', ' ', prefix + text)[:400]
    return {'link': PLUGIN_URL + ERROR_MARK, 'name': name, 'size': -1, 'seeds': -1, 'leech': -1,
            'engine_url': PLUGIN_URL, 'desc_link': HELP_URL, 'pub_date': -1}


_RX_UNSAFE_OUTPUT = re.compile(r'[\x00-\x20|\x7f]')


def clean_output_url(url):
    """Percent-encode characters that would break qBittorrent's 'a|b|c' result lines.

    A newline coming from a site could otherwise inject a fake result row."""
    return _RX_UNSAFE_OUTPUT.sub(lambda m: '%%%02X' % ord(m.group(0)), url or '')


def release_to_result(idx, rel, tracker_first=True):
    if rel.link and (idx.type != 'public' or not rel.magnet):
        link = rel.link + DL_MARK + idx.id
    else:
        link = rel.magnet or rel.link + DL_MARK + idx.id
    title = re.sub(r'\s+', ' ', rel.title or '').strip()
    name = '[%s] %s' % (idx.name, title) if tracker_first else title
    pub = -1
    if rel.date:
        try:
            pub = int(time.mktime(rel.date.timetuple()))
        except (OverflowError, ValueError):
            pub = -1
    return {
        'link': clean_output_url(link),
        'name': name,
        'size': rel.size if rel.size is not None and rel.size > 0 else -1,
        'seeds': rel.seeders if rel.seeders is not None else -1,
        'leech': rel.leechers if rel.leechers is not None else -1,
        'engine_url': PLUGIN_URL,
        'desc_link': clean_output_url(rel.details or ''),
        'pub_date': pub,
    }


# ---------------------------------------------------------------------------
# merging duplicates by infohash (optional, "dedup": "merge")
# ---------------------------------------------------------------------------

_RX_HEX40 = re.compile(r'^[0-9a-fA-F]{40}$')
_RX_B32 = re.compile(r'^[A-Za-z2-7]{32}$')
_RX_HEX64 = re.compile(r'^[0-9a-fA-F]{64}$')
_RX_URL_HASH = re.compile(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])')


def normalize_infohash(value):
    """Lowercase hex v1 hash, 'v2:<64 hex>' for v2 hashes, or None."""
    if not value:
        return None
    v = value.strip()
    low = v.lower()
    if low.startswith('urn:btih:'):
        v = v[9:]
    elif low.startswith('urn:btmh:'):
        v = v[9:]
        if v.lower().startswith('1220'):  # multihash header: sha2-256, 32 bytes
            v = v[4:]
    if _RX_HEX40.match(v):
        return v.lower()
    if _RX_B32.match(v):
        try:
            return base64.b32decode(v.upper()).hex()
        except (ValueError, TypeError):
            return None
    if _RX_HEX64.match(v):
        return 'v2:' + v.lower()
    return None


def _magnet_hashes(magnet):
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    return [h for h in (normalize_infohash(x) for x in qs.get('xt', [])) if h]


def release_infohash(rel):
    """Best effort infohash of a release, without any extra request."""
    h = normalize_infohash(rel.infohash)
    if h:
        return h
    if rel.magnet:
        hashes = _magnet_hashes(rel.magnet)
        v1 = [x for x in hashes if not x.startswith('v2:')]
        if v1 or hashes:
            return (v1 or hashes)[0]
    for url in (rel.link, rel.details):
        if not url:
            continue
        # only whole path segments ("/<40 hex>/"), not ids hidden in query strings
        for segment in urllib.parse.urlsplit(url).path.split('/'):
            if _RX_URL_HASH.fullmatch(segment):
                return segment.lower()
    return None


def _merge_trackers(magnets):
    """Magnet of the first item, plus the tr= of all of them (no repeats)."""
    base = magnets[0]
    head, _, query = base.partition('?')
    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    seen = {v for k, v in params if k == 'tr'}
    for m in magnets[1:]:
        for k, v in urllib.parse.parse_qsl(urllib.parse.urlsplit(m).query, keep_blank_values=True):
            if k == 'tr' and v not in seen:
                seen.add(v)
                params.append((k, v))
    return head + '?' + '&'.join('%s=%s' % (k, v if k == 'xt' else urllib.parse.quote(v, safe=''))
                                 for k, v in params)


def merged_result(members, tracker_first=True, max_names=2):
    """Build one result row from [(indexer, release), ...] that share an infohash."""
    def seeds(m):
        return m[1].seeders if m[1].seeders is not None else -1

    members = sorted(members, key=seeds, reverse=True)
    best_idx, best = members[0]
    if len(members) == 1:
        return release_to_result(best_idx, best, tracker_first)
    row = release_to_result(best_idx, best, False)
    names = list(dict.fromkeys(idx.name for idx, _ in members))
    label = ', '.join(names[:max_names]) + (' +%d' % (len(names) - max_names) if len(names) > max_names else '')
    row['name'] = '[%s] %s' % (label, row['name']) if tracker_first else '%s [%s]' % (row['name'], label)
    magnets = [r.magnet for _, r in members if r.magnet]
    if magnets:
        row['link'] = clean_output_url(_merge_trackers(magnets))
    row['seeds'] = max(seeds(m) for m in members)
    row['leech'] = max((r.leechers if r.leechers is not None else -1) for _, r in members)
    if row['size'] == -1:
        row['size'] = next((r.size for _, r in members if r.size), -1)
    dates = [r.date for _, r in members if r.date]
    if dates:
        try:
            row['pub_date'] = int(time.mktime(min(dates).timetuple()))
        except (OverflowError, ValueError):
            pass
    return row


class ResultMerger:
    """Holds public results for `dedup_wait` seconds and prints duplicates (same infohash) as one row.

    qBittorrent cannot update a row that was already printed, so after the wait ("live" mode) new
    hashes are printed at once and later copies of an already printed hash are dropped."""

    def __init__(self, printer, tracker_first=True):
        self.printer = printer
        self.tracker_first = tracker_first
        self.lock = threading.Lock()
        self.groups = {}  # hash -> [(indexer, release)]
        self.order = []
        self.printed = set()
        self.live = False
        self.merged = 0
        self.dropped = 0

    def _print(self, row):
        try:
            self.printer(row)
        except Exception as e:  # noqa: BLE001
            log('could not print result: %s', e)

    def add(self, idx, rel):
        h = release_infohash(rel) if idx.type == 'public' else None
        with self.lock:
            if h is None:
                self._print(release_to_result(idx, rel, self.tracker_first))
            elif h in self.printed:
                self.dropped += 1
            elif self.live:
                self.printed.add(h)
                self._print(release_to_result(idx, rel, self.tracker_first))
            else:
                if h not in self.groups:
                    self.groups[h] = []
                    self.order.append(h)
                else:
                    self.merged += 1
                self.groups[h].append((idx, rel))

    def go_live(self):
        with self.lock:
            for h in self.order:
                self.printed.add(h)
                self._print(merged_result(self.groups[h], self.tracker_first))
            self.groups.clear()
            self.order = []
            self.live = True

    finish = go_live


def run_search(what, cat='all', printer=prettyPrinter, cfg=None, data_dir=DATA_DIR, only=None):
    """Search every enabled indexer in parallel, printing results as they arrive.

    Returns a dict {indexer_id: number_of_results or error string}."""
    cfg = cfg or load_config()
    if cfg.get('_error'):
        # always visible, whatever report_errors says: the user's settings are being ignored
        try:
            printer(error_result('jackettless.json is invalid (%s), default settings in use' % cfg['_error'], what))
        except Exception as e:  # noqa: BLE001
            log('could not print error row: %s', e)
    install_dns_fallback(cfg.get('dns_over_https'))
    store = make_store(cfg, data_dir)
    ids = list(only) if only else None
    if ids is None:
        store.ensure([i for i in cfg['indexers']], everything=as_bool(cfg.get('all_public')))
        ids = enabled_indexers(cfg, store)
    else:
        store.ensure(ids)
    tracker_first = as_bool(cfg.get('tracker_first'), True)
    budget = float(cfg.get('indexer_timeout', 60))
    lock = threading.Lock()
    summary = {}
    reasons = {}
    merger = ResultMerger(printer, tracker_first) if str(cfg.get('dedup') or 'off').lower() == 'merge' else None
    try:
        dedup_wait = float(cfg.get('dedup_wait', 8))
    except (TypeError, ValueError):
        dedup_wait = 8.0

    def one(ident):
        try:
            idx = build_indexer(ident, cfg, store, data_dir)
        except Exception as e:  # noqa: BLE001
            return ident, 'error: %s' % e, str(e)[:120]

        def emit(rel):
            if merger is not None:
                merger.add(idx, rel)
                return
            res = release_to_result(idx, rel, tracker_first)
            with lock:
                try:
                    printer(res)
                except Exception as e:  # noqa: BLE001
                    log('%s: could not print result: %s', ident, e)

        try:
            rels = idx.search(what, cat, emit, deadline=time.time() + budget)
            return ident, len(rels), None
        except DeadlineExceeded as e:
            return ident, 'timeout: %s' % e, describe_error(e)
        except Exception as e:  # noqa: BLE001
            return ident, 'error: %s: %s' % (type(e).__name__, e), describe_error(e)

    if not ids:
        log('no indexers enabled, edit %s', CONFIG_FILE)
        return summary
    workers = max(1, min(int(cfg.get('thread_count', 10)), len(ids)))

    def collect(fut):
        ident, outcome, reason = fut.result()
        summary[ident] = outcome
        if reason:
            reasons[ident] = reason
        if isinstance(outcome, str):
            log('%s: %s', ident, outcome, debug=True)
        else:
            log('%s: %d result(s)', ident, outcome, debug=True)

    live_at = time.time() + dedup_wait
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = {ex.submit(one, i) for i in ids}
        if merger is not None:
            # hold public results until every indexer is done or `dedup_wait` seconds have passed
            while pending and time.time() < live_at:
                done, pending = wait(pending, timeout=max(0.0, live_at - time.time()),
                                     return_when=FIRST_COMPLETED)
                for fut in done:
                    collect(fut)
            merger.go_live()
        for fut in as_completed(pending):
            collect(fut)
    if merger is not None:
        merger.finish()
        log('dedup: %d duplicate(s) merged, %d late duplicate(s) dropped', merger.merged, merger.dropped,
            debug=True)
    if reasons:  # one line per search: qBittorrent logs every stderr line as a warning
        log('%d of %d indexers failed: %s', len(reasons), len(ids),
            ', '.join('%s (%s)' % (i, reasons[i]) for i in sorted(reasons)))
    _report_errors(str(cfg.get('report_errors') or 'none').lower(), reasons, len(ids), printer, what)
    return summary


def _report_errors(mode, reasons, total, printer, query=''):
    """Optionally show indexer failures as result rows in qBittorrent ('none', 'summary' or 'all')."""
    if mode not in ('summary', 'all') or not reasons:
        return
    if mode == 'summary':
        detail = ', '.join('%s (%s)' % (i, reasons[i]) for i in sorted(reasons))
        rows = [error_result('%d of %d indexers failed: %s' % (len(reasons), total, detail), query)]
    else:
        rows = [error_result('%s failed: %s' % (i, reasons[i]), query) for i in sorted(reasons)]
    for row in rows:
        try:
            printer(row)
        except Exception as e:  # noqa: BLE001
            log('could not print error row: %s', e)


def run_download(url, cfg=None, data_dir=DATA_DIR):
    """Resolve a result link. Returns the line nova2dl expects: '<file or magnet> <url>'."""
    if url.endswith(ERROR_MARK):
        raise IndexerError('this row is an error report from jackettless, not a torrent')
    base, sep, ident = url.partition(DL_MARK)
    if not sep:
        if base.lower().startswith('magnet:'):
            return base + ' ' + url
        if _qbt_helpers is not None:
            return _qbt_helpers.download_file(url)
        raise IndexerError('not a jackettless link')
    cfg = cfg or load_config()
    install_dns_fallback(cfg.get('dns_over_https'))
    store = make_store(cfg, data_dir)
    idx = build_indexer(ident, cfg, store, data_dir)
    kind, data = idx.download(base)
    if kind == 'magnet':
        return data + ' ' + url
    fd, path = tempfile.mkstemp(suffix='.torrent')
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return path + ' ' + url


# ---------------------------------------------------------------------------
# qBittorrent engine
# ---------------------------------------------------------------------------

class jackettless(object):  # noqa: N801 - class name must match the file name
    url = PLUGIN_URL
    name = 'Jackettless'
    supported_categories = {
        'all': 'all', 'anime': 'anime', 'books': 'books', 'games': 'games', 'movies': 'movies',
        'music': 'music', 'pictures': 'pictures', 'software': 'software', 'tv': 'tv',
    }

    def search(self, query, category='all'):
        """Called by qBittorrent; `query` arrives URL-encoded (e.g. 'ubuntu%2024')."""
        try:
            run_search(urllib.parse.unquote(query), category)
        except Exception as e:  # noqa: BLE001
            log('search failed: %s', e)

    def download_torrent(self, url):
        try:
            print(run_download(url))
        except Exception as e:  # noqa: BLE001
            log('download failed: %s', e)
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# command line (for testing outside qBittorrent)
# ---------------------------------------------------------------------------

def _cli(argv=None):
    import argparse
    global _VERBOSE
    ap = argparse.ArgumentParser(prog='jackettless', description='serverless Jackett search (qBittorrent plugin)')
    ap.add_argument('--config', default=CONFIG_FILE)
    ap.add_argument('--data', default=DATA_DIR)
    ap.add_argument('-v', '--verbose', action='store_true')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('search', help='search enabled indexers (or --indexer ...)')
    s.add_argument('query')
    s.add_argument('--cat', default='all', choices=sorted(jackettless.supported_categories))
    s.add_argument('--indexer', action='append', help='only these indexer ids')
    s.add_argument('--json', action='store_true', help='print results as JSON lines')
    d = sub.add_parser('download', help='resolve a result link')
    d.add_argument('link')
    sy = sub.add_parser('sync', help='download/refresh definitions')
    sy.add_argument('--all', action='store_true', help='download every Jackett definition')
    ls = sub.add_parser('list', help='list cached definitions')
    ls.add_argument('--type', choices=('public', 'semi-private', 'private'))
    lg = sub.add_parser('login', help='log in to a private indexer and save the cookies')
    lg.add_argument('indexer')
    args = ap.parse_args(argv)
    _VERBOSE = _VERBOSE or args.verbose
    cfg = load_config(args.config)
    if cfg.get('_error'):
        return 2
    _VERBOSE = _VERBOSE or as_bool(cfg.get('verbose'))
    store = make_store(cfg, args.data)
    if args.cmd == 'search':
        if args.json:
            def printer(res):
                print(json.dumps(res, ensure_ascii=False), flush=True)
        else:
            printer = prettyPrinter
        summary = run_search(args.query, args.cat, printer, cfg, args.data, args.indexer)
        for ident, outcome in sorted(summary.items()):
            log('summary %-25s %s', ident, outcome)
        return 0
    if args.cmd == 'download':
        print(run_download(args.link, cfg, args.data))
        return 0
    if args.cmd == 'sync':
        listing = store.sync(list(cfg['indexers']), everything=args.all)
        log('%s definitions available upstream, %d cached locally', len(listing or {}), len(store.available_ids()))
        return 0 if listing is not None else 1
    if args.cmd == 'list':
        for ident in store.available_ids():
            t = store.quick_type(ident)
            if not args.type or t == args.type:
                print('%-30s %s' % (ident, t))
        return 0
    if args.cmd == 'login':
        store.ensure([args.indexer])
        idx = build_indexer(args.indexer, cfg, store, args.data)
        idx.relogin()
        log('%s: login OK, cookies saved', args.indexer)
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(_cli())
