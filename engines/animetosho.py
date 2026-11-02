# VERSION: 1.21
# AUTHORS: ALAA_BRAHIM, phlexis96
# LICENSING INFORMATION

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

import json
import urllib.parse
from helpers import retrieve_url, download_file
from novaprinter import prettyPrinter


class animetosho(object):
    url = "https://animetosho.xyz"
    name = "Anime Tosho"
    supported_categories = {
        "all": "all",
        "anime": "anime",
    }

    def __init__(self):
        pass

    def download_torrent(self, info):
        print(download_file(info))

    def search(self, what, cat='all'):
        query = urllib.parse.quote_plus(urllib.parse.unquote_plus(what))
        url = f"https://feed.animetosho.xyz/json?q={query}"
        response = retrieve_url(url)
        if not response:
            return

        try:
            results = json.loads(response)
        except Exception:
            return

        if not isinstance(results, list):
            return

        for result in results:
            link = result.get("magnet_uri") or result.get("torrent_url")
            if not link:
                continue

            seeds = result.get("seeders")
            seeds = -1 if seeds is None else seeds

            leech = result.get("leechers")
            leech = -1 if leech is None else leech

            size = result.get("total_size")
            size_str = f"{size} B" if size is not None else "-1"

            pub_date = result.get("timestamp")
            pub_date = int(pub_date) if pub_date is not None else -1

            current_result = {
                "engine_url": self.url,
                "link": link,
                "name": result.get("title") or result.get("torrent_name") or "Unknown",
                "size": size_str,
                "seeds": seeds,
                "leech": leech,
                "desc_link": result.get("link", ""),
                "pub_date": pub_date,
            }

            prettyPrinter(current_result)


if __name__ == "__main__":
    a = animetosho()
    a.search("zom judas")
