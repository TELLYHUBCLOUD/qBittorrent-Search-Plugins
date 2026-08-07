#VERSION: 1.00
# AUTHORS: dominc8
#
# LICENSING INFORMATION
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the “Software”), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from html.parser import HTMLParser
from helpers import download_file, retrieve_url
from novaprinter import prettyPrinter
import json
import urllib.parse

class tsukihime(object):
    """
    `url`, `name`, `supported_categories` should be static variables of the engine_name class,
     otherwise qbt won't install the plugin.

    `url`: The URL of the search engine.
    `name`: The name of the search engine, spaces and special characters are allowed here.
    `supported_categories`: What categories are supported by the search engine and their corresponding id,
    possible categories are ('all', 'anime', 'books', 'games', 'movies', 'music', 'pictures', 'software', 'tv').
    """

    url = 'https://tsukihime.org'
    name = 'Tsukihime '
    supported_categories = {
        'all': '0',
        'anime': '7',
    }

    def __init__(self):
        """
        Some initialization
        """
        pass

    def download_torrent(self, info):
        """
        Providing this function is optional.
        It can however be interesting to provide your own torrent download
        implementation in case the search engine in question does not allow
        traditional downloads (for example, cookie-based download).
        """
        print(download_file(info))

    # DO NOT CHANGE the name and parameters of this function
    # This function will be the one called by nova2.py
    def search(self, what, cat='all'):
        """
        Here you can do what you want to get the result from the search engine website.
        Everytime you parse a result line, store it in a dictionary
        and call the prettyPrint(your_dict) function.

        `what` is a string with the search tokens, already escaped (e.g. "Ubuntu+Linux")
        `cat` is the name of a search category in ('all', 'anime', 'books', 'games', 'movies', 'music', 'pictures', 'software', 'tv')
        """
        url = f"https://api.tsukihime.org/v1/search/torrents?q={what}"
        resp = json.loads(retrieve_url(url))

        for result in resp["results"]:

            torrent_info_url = f"https://api.tsukihime.org/v1/torrents/{result["id"]}"
            torrent_info_resp = json.loads(retrieve_url(torrent_info_url))

            current_result = {"engine_url": "https://tsukihime.org"}
            current_result["link"] = f"magnet:?xt=urn:btih:{result["btih"]}&dn={urllib.parse.quote(result["name"], safe='')}"
            current_result["name"] = result["name"]
            current_result["size"] = str(result["totalsize"]) + " B"
            current_result["desc_link"] = f"https://tsukihime.org/view/{result["id"]}"
            current_result["pub_date"] = result["source_date"]

            seeders = 0
            leechers = 0
            for tracker in torrent_info_resp["trackers"]:
                seeders += tracker["seeders"]
                leechers += tracker["leechers"]
                current_result["link"] += f"&tr={urllib.parse.quote(tracker["url"], safe='')}"

            current_result["seeds"] = seeders
            current_result["leech"] = leechers

            prettyPrinter(current_result)

if __name__ == "__main__":
    t = tsukihime()
    t.search("bleach")
