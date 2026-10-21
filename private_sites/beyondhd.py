#VERSION: 1.0
# AUTHORS: swannie-eire (github.com/swannie-eire)
# DESCRIPTION: qBittorrent search plugin for Beyond-HD using their official JSON API
# Requires: api_key and rss_key from BHD profile -> My Security

import json
import os
import sys
import requests
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from nova3.engines.novaprinter import prettyPrinter
except Exception:
    from novaprinter import prettyPrinter

###############################################################################
CONFIG_FILE = 'bhd.json'
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DATA = {
    'api_key': 'YOUR_API_KEY_HERE',      # 32-char API key from BHD My Security
    'rss_key': 'YOUR_RSS_KEY_HERE',      # 32-char RSS key from BHD My Security
    'url': 'https://beyond-hd.me',
    'sort_by_seeders': True,             # sort results by seeders descending
    'min_seeders': 0,                    # filter out results below this seed count
}


def load_configuration():
    global CONFIG_PATH, CONFIG_DATA
    try:
        with open(CONFIG_PATH) as f:
            CONFIG_DATA = json.load(f)
    except ValueError:
        CONFIG_DATA['malformed'] = True
    except Exception:
        with open(CONFIG_PATH, 'w') as f:
            f.write(json.dumps(CONFIG_DATA, indent=4, sort_keys=True))

    required = ['api_key', 'rss_key', 'url']
    if any(item not in CONFIG_DATA for item in required):
        CONFIG_DATA['malformed'] = True


load_configuration()
###############################################################################


class beyondhd(object):
    name = 'Beyond-HD'
    url = CONFIG_DATA['url'] if CONFIG_DATA['url'][-1] != '/' else CONFIG_DATA['url'][:-1]
    api_key = CONFIG_DATA['api_key']
    rss_key = CONFIG_DATA['rss_key']
    supported_categories = {
        'all': None,
        'movies': [1],
        'tv': [2],
    }

    def fix_mojibake(self, text):
        """Attempt to repair mojibake by re-encoding latin-1 -> utf-8."""
        if not isinstance(text, str):
            return text
        try:
            return text.encode('latin-1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

    def handle_error(self, error_msg, what):
        prettyPrinter({
            'seeds': -1,
            'size': -1,
            'leech': -1,
            'engine_url': self.url,
            'link': 'Beyond-HD',
            'desc_link': 'https://beyond-hd.me/forums/topic/torrents-api-overview-beyondhd.3721',
            'name': "BeyondHD: %s! Right-click this row and select 'Open description page' for help. Config: '%s' Search: '%s'" % (error_msg, CONFIG_PATH, what)
        })

    def search(self, what, cat='all'):
        what = unquote(what)

        if 'malformed' in CONFIG_DATA:
            self.handle_error('malformed configuration file', what)
            return

        if self.api_key == 'YOUR_API_KEY_HERE' or self.rss_key == 'YOUR_RSS_KEY_HERE':
            self.handle_error('api key error', what)
            return

        # Build the search payload
        post_data = {
            'action': 'search',
            'rsskey': self.rss_key,
            'search': '',
        }

        # Determine search type from input
        if what.startswith('tt'):
            # IMDB ID (strip 'tt' prefix for BHD API)
            post_data['imdb_id'] = what.lstrip('tt')
        elif what.startswith('imdb:'):
            post_data['imdb_id'] = what.split('imdb:', 1)[1].lstrip('tt')
        elif 'imdb.com/title/' in what:
            imdb_id = what.split('imdb.com/title/', 1)[1].split('/')[0]
            post_data['imdb_id'] = imdb_id.lstrip('tt')
        elif what.startswith('tmdb:'):
            tmdb_id = what.split('tmdb:', 1)[1]
            # BHD requires tv/ or movie/ prefix for TMDB; default to movie
            post_data['tmdb_id'] = 'movie/' + tmdb_id
        elif what.startswith('tvdb:'):
            # BHD API doesn't support TVDB directly; fall back to text search
            post_data['search'] = what.split('tvdb:', 1)[1]
        elif 'themoviedb.org' in what:
            tmdb_id = what.split('themoviedb.org', 1)[1].split('/')[2].split('-')[0]
            media_type = what.split('themoviedb.org', 1)[1].split('/')[1]
            prefix = 'tv/' if media_type == 'tv' else 'movie/'
            post_data['tmdb_id'] = prefix + tmdb_id
        else:
            post_data['search'] = what

        # Apply category filter if specified
        if cat != 'all' and cat in self.supported_categories:
            post_data['categories'] = self.supported_categories[cat]

        # BHD API endpoint
        api_url = self.url + '/api/torrents/' + self.api_key

        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

        all_results = []
        page = 1
        while True:
            post_data['page'] = str(page)
            try:
                resp = requests.post(api_url, json=post_data, headers=headers, timeout=30)
                resp.raise_for_status()
                resp.encoding = 'utf-8'
                data = resp.json()
            except Exception as e:
                self.handle_error('API request failed: ' + str(e), what)
                return

            if data.get('status_code') == 0:
                self.handle_error('Indexer Error: ' + str(data.get('status_message', 'Unknown')), what)
                return

            # BHD returns lowercase 'results'
            results = data.get('results') or []
            all_results.extend(results)

            total_pages = data.get('total_pages', 1)
            if page >= total_pages or not results:
                break
            page += 1

        # Filter by minimum seeders if configured
        min_seed = CONFIG_DATA.get('min_seeders', 0)
        if min_seed > 0:
            all_results = [r for r in all_results 
                          if r.get('Seeders') is not None and r['Seeders'] >= min_seed]

        # Sort by seeders descending
        if CONFIG_DATA.get('sort_by_seeders', True):
            all_results = sorted(all_results, 
                                key=lambda x: x['Seeders'] if x.get('Seeders') is not None else 0, 
                                reverse=True)

        for result in all_results:
            download_url = result.get('download_url')
            info_url = result.get('url')
            if not download_url:
                print("BHD: skipping result (no download_url - check rss_key): %s" % result.get('name'), file=sys.stderr)
                continue
            if not info_url:
                info_url = self.url

            res = {}
            title = result.get('name', 'Unknown')
            title = self.fix_mojibake(title)

            features = []
            if result.get('dv'):
                features.append('DV')
            if result.get('hdr10'):
                features.append('HDR10')
            if result.get('hdr10+'):
                features.append('HDR10+')
            if result.get('hlg'):
                features.append('HLG')
            if len(features) > 1:
                title += ' (' + ' / '.join(features) + ')'

            res['name'] = title
            res['link'] = download_url
            res['size'] = result.get('size', -1)
            res['seeds'] = result.get('seeders', -1)
            res['leech'] = result.get('leechers', -1)
            res['desc_link'] = info_url
            res['engine_url'] = self.url

            prettyPrinter(res)


if __name__ == '__main__':
    bhd_se = bhd()
    # qBittorrent's nova2.py calls: python3 beyondhd.py <category> <search_term>
    # e.g. python3 beyondhd.py all "star trek"
    if len(sys.argv) > 2:
        category = sys.argv[1]
        search_term = ' '.join(sys.argv[2:])
        bhd_se.search(search_term, category)
    elif len(sys.argv) > 1:
        bhd_se.search(sys.argv[1])
    else:
        # Default test search when no args given
        bhd_se.search('ubuntu')