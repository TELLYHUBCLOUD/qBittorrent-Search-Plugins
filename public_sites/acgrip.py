# -*- coding: utf-8 -*-
# VERSION: 2.1
# AUTHORS: nishizhen (Original author: Yun <chenzm39@gmail.com>)
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.


import re
import ssl

try:
    import urllib.request
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

try:
    from HTMLParser import HTMLParser
except ModuleNotFoundError:
    from html.parser import HTMLParser

# import qBT modules
try:
    from novaprinter import prettyPrinter
    from helpers import retrieve_url as _retrieve_url
except ModuleNotFoundError:
    pass


USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

def acgrip_retrieve_url(url):
    """Retrieve URL content with proper headers and SSL settings."""
    if HAS_URLLIB:
        # Try multiple SSL approaches for maximum compatibility
        ssl_attempts = []

        # Approach 1: Standard with cert verification disabled
        try:
            ctx1 = ssl.create_default_context()
            ctx1.check_hostname = False
            ctx1.verify_mode = ssl.CERT_NONE
            ssl_attempts.append(ctx1)
        except Exception:
            pass

        # Approach 2: TLSv1.2 only (most compatible)
        try:
            ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            ctx2.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx2.maximum_version = ssl.TLSVersion.TLSv1_3
            ssl_attempts.append(ctx2)
        except Exception:
            pass

        for ctx in ssl_attempts:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        'User-Agent': USER_AGENT,
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5',
                        'Connection': 'keep-alive',
                    }
                )
                with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                    return response.read().decode('utf-8')
            except Exception:
                continue

    # Fallback to qBittorrent's retrieve_url if available
    try:
        return _retrieve_url(url)
    except Exception:
        pass

    raise ConnectionError(f"Failed to retrieve URL: {url}")


class acgrip(object):
    """Class used by qBittorrent to search for torrents."""

    url = 'https://acg.rip'
    name = 'acg.rip'

    ###########################################################################

    # defines which search categories are supported by this search engine
    # and their corresponding id. Possible categories are:
    # 'all', 'movies', 'tv', 'music', 'games', 'anime', 'software', 'pictures',
    # 'books'
    supported_categories = {'all': '0_0'}

    class acgripParser(HTMLParser):
        """Parses acg.rip search page for results."""

        def __init__(self, res, url):
            """Construct a acgrip html parser.

            Parameters:
            :param list res: a list to store the results in
            :param str url: the base url of the search engine
            """
            try:
                super().__init__()
            except TypeError:
                HTMLParser.__init__(self)

            self.engine_url = url
            self.results = res

            # State tracking
            self.in_table = False
            self.in_row = False
            self.current_row = None        # Dict to hold current row data
            self.cell_index = -1           # Current cell index (0=pub, 1=title, 2=dl, 3=size)
            self.row_pub_date = None       # Unix timestamp from <time datetime="..."> for current row

        def handle_starttag(self, tag, attrs):
            """Handle opening tags."""
            params = dict(attrs)

            if tag == 'table':
                self.in_table = True

            elif tag == 'tr' and self.in_table:
                self.in_row = True
                self.cell_index = -1
                self.current_row = None
                self.row_pub_date = None

            elif tag == 'td' and self.in_row:
                self.cell_index += 1

            elif tag == 'time' and self.in_row:
                # Extract Unix timestamp from datetime attribute on any <time> tag within a row
                # e.g., <time datetime="1764558056">8 个月</time>
                for attr_name, attr_value in attrs:
                    if attr_name == 'datetime':
                        try:
                            self.row_pub_date = int(attr_value)
                        except ValueError:
                            pass
                        break

            elif tag == 'a' and self.in_row:
                href = params.get('href', '')

                # Cell 1 (index 1): Title link (desc_link) or team link
                if self.cell_index == 1 and href.startswith('/t/'):
                    self.current_row = {
                        'engine_url': self.engine_url,
                        'desc_link': self.engine_url + href
                    }

                # Cell 2 (index 2): Download link (.torrent)
                elif self.cell_index == 2 and href.endswith('.torrent'):
                    if self.current_row:
                        self.current_row['link'] = self.engine_url + href

        def handle_data(self, data):
            """Capture text data from relevant cells."""
            data_stripped = data.strip()
            if not data_stripped or not self.in_row:
                return

            # Cell 1 (index 1): Title
            if self.cell_index == 1:
                if self.current_row and 'name' not in self.current_row:
                    self.current_row['name'] = data_stripped
                elif self.current_row and 'name' in self.current_row:
                    # Append if there's more text (e.g., after a team link)
                    self.current_row['name'] += ' ' + data_stripped

            # Cell 3 (index 3): Size
            elif self.cell_index == 3:
                if self.current_row:
                    self.current_row['size'] = data_stripped

        def handle_endtag(self, tag):
            """Handle closing tags."""
            if tag == 'tr' and self.in_row:
                self.in_row = False
                # If we have a complete row with required fields, add it
                if self.current_row and 'name' in self.current_row and 'link' in self.current_row:
                    # Ensure seeds and leech are set (website doesn't provide this info)
                    if 'seeds' not in self.current_row:
                        self.current_row['seeds'] = -1
                    if 'leech' not in self.current_row:
                        self.current_row['leech'] = -1
                    # Clean up size string if needed
                    if 'size' in self.current_row:
                        self.current_row['size'] = self._parse_size(self.current_row['size'])
                    else:
                        self.current_row['size'] = '-1'
                    # Set pub_date from <time datetime="..."> or fallback to current timestamp
                    if self.row_pub_date is not None:
                        self.current_row['pub_date'] = self.row_pub_date
                    else:
                        import time as _time
                        self.current_row['pub_date'] = int(_time.time())
                    # Clean up name
                    if 'name' in self.current_row:
                        self.current_row['name'] = self.current_row['name'].strip()
                    self.results.append(self.current_row)
                self.current_row = None

            elif tag == 'table':
                self.in_table = False

        def _parse_size(self, size_str):
            """Parse size string to bytes."""
            import re
            size_str = size_str.strip()
            match = re.match(r'([\d.]+)\s*(KB|MB|GB|TB)', size_str, re.IGNORECASE)
            if not match:
                return '-1'

            value = float(match.group(1))
            unit = match.group(2).upper()

            units = {
                'KB': 1024,
                'MB': 1024 ** 2,
                'GB': 1024 ** 3,
                'TB': 1024 ** 4
            }

            return str(int(value * units.get(unit, 1)))

    # DO NOT CHANGE the name and parameters of this function
    # This function will be the one called by nova2.py

    def search(self, what, cat='all'):
        """
        Retrieve and parse engine search results by category and query.

        Only fetches the first page of results (up to 30 items).
        This design decision avoids SSL/TLS compatibility issues with paginated
        requests in qBittorrent nox environments, improves search speed,
        and reduces unnecessary server load.

        Parameters:
        :param what: a string with the search tokens, already escaped
                     (e.g. "Ubuntu+Linux")
        :param cat:  the name of a search category, see supported_categories.
        """
        url = self.url
        hits = []
        parser = self.acgripParser(hits, self.url)

        try:
            # Only fetch first page
            req_url = f'{url}/?term={what}'
            res = acgrip_retrieve_url(req_url)
            parser.feed(res)

            for each in hits:
                prettyPrinter(each)

        except Exception:
            pass

        parser.close()
