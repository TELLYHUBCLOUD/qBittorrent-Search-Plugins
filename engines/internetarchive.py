# VERSION: 1.1
# AUTHORS: OyunErbabi

import json
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from novaprinter import prettyPrinter

class internetarchive(object):
    name = "Internet Archive"
    url = "https://archive.org"
    # qBittorrent category -> archive.org mediatype
    supported_categories = {
        'all': None,
        'movies': 'movies',
        'tv': 'movies',
        'music': 'audio',
        'books': 'texts',
        'software': 'software',
        'games': 'software',
        'pictures': 'image',
    }

    rows_per_page = 100
    max_results = 200

    def retrieve_url(self, url):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
            'Accept': 'application/json'
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode('utf-8')

    def build_terms(self, what):
        # Keep quoted phrases, strip Lucene special characters from other words
        terms = []
        for phrase, word in re.findall(r'"([^"]+)"|(\S+)', what):
            if phrase:
                phrase = re.sub(r'["\\]', ' ', phrase).strip()
                if phrase:
                    terms.append(f'"{phrase}"')
            else:
                word = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', ' ', word).strip()
                terms.extend(f'"{w}"' if w in ('AND', 'OR', 'NOT') else w for w in word.split())
        return " AND ".join(terms)

    def fetch_page(self, query, page):
        params = [
            ('q', query),
            ('fl[]', 'identifier'),
            ('fl[]', 'title'),
            ('fl[]', 'item_size'),
            ('fl[]', 'publicdate'),
            ('sort[]', 'downloads desc'),
            ('rows', self.rows_per_page),
            ('page', page),
            ('output', 'json'),
        ]
        search_url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
        data = json.loads(self.retrieve_url(search_url))
        response = data.get('response', {})
        return response.get('docs', []), response.get('numFound', 0)

    def parse_date(self, value):
        try:
            return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").timestamp())
        except (TypeError, ValueError):
            return -1

    def search(self, what, cat='all'):
        # qBittorrent passes the query URL-encoded
        terms = self.build_terms(urllib.parse.unquote(what))
        if not terms:
            return

        filters = ' AND format:"Archive BitTorrent"'
        mediatype = self.supported_categories.get(cat)
        if mediatype:
            filters += f' AND mediatype:{mediatype}'

        # Search titles first for relevance, fall back to full text if nothing matches
        for query in (f'title:({terms}){filters}', f'({terms}){filters}'):
            try:
                result_count = self.search_query(query)
            except Exception as e:
                # stdout is reserved for result lines
                print(f"Internet Archive search error: {e}", file=sys.stderr)
                return
            if result_count:
                return

    def search_query(self, query):
        page = 1
        result_count = 0

        while result_count < self.max_results:
            docs, total_results = self.fetch_page(query, page)
            if not docs:
                break

            for doc in docs:
                identifier = doc.get('identifier')
                if not identifier:
                    continue

                title = doc.get('title') or identifier
                if isinstance(title, list):
                    title = title[0] if title else identifier

                prettyPrinter({
                    'name': str(title),
                    'link': f"https://archive.org/download/{identifier}/{identifier}_archive.torrent",
                    'size': int(doc.get('item_size') or 0),
                    'seeds': -1,
                    'leech': -1,
                    'engine_url': self.url,
                    'desc_link': f"https://archive.org/details/{identifier}",
                    'pub_date': self.parse_date(doc.get('publicdate')),
                })
                result_count += 1
                if result_count >= self.max_results:
                    break

            if page * self.rows_per_page >= total_results:
                break
            page += 1

        return result_count
