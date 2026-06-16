# VERSION: 1.1
# AUTHORS: etn406 <etn406@gmail.com>

import json
from datetime import datetime
from helpers import retrieve_url
from novaprinter import prettyPrinter

class sharewood(object):
    """
    `url`, `name`, `supported_categories` should be static variables of the engine_name class,
     otherwise qbt won't install the plugin.

    `url`: The URL of the search engine.
    `name`: The name of the search engine, spaces and special characters are allowed here.
    `supported_categories`: What categories are supported by the search engine and their corresponding id,
    possible categories are ('all', 'anime', 'books', 'games', 'movies', 'music', 'pictures', 'software', 'tv').
    """

    url = 'https://www.sharewood.tv/'
    name = 'Sharewood'
    passkey='<passkey>'
    supported_categories = {
        'all': True,
        'books': True,
        'games': True,
        'movies': True,
        'music': True,
        'software': True,
        'tv': True
    }

    sw_categories = {
        'books': '4',
        'games': '5',
        'music': '2',
        'software': '3'
    }

    sw_subcategories = {
        'movies': '9',
        'tv': '10'
    }
    
    limit_results_count=100

    def search(self, what, cat="all"):
        category_param = ""

        if cat != "all" and cat in self.sw_categories:
            category_param = f"&category={self.sw_categories[cat]}"

        elif cat != "all" and cat in self.sw_subcategories:
            category_param = f"&subcategory={self.sw_subcategories[cat]}"

        search_url = f"{self.url}/api/{self.passkey}/search?name={what}{category_param}&limit={self.limit_results_count}"

        response = retrieve_url(search_url)
        results = json.loads(response)

        if results:
            for torrent in results:
                result = {
                    "link": torrent["download_url"],
                    "name": torrent["name"],
                    "size": torrent["size"],
                    "seeds": torrent["seeders"],
                    "leech": torrent["leechers"],
                    "engine_url": self.url,
                    "desc_link": f"{self.url}/torrents/{torrent['slug']}.{torrent['id']}",
                    "pub_date": int(datetime.strptime(torrent["created_at"], "%Y-%m-%d %H:%M:%S").timestamp())
                }

                prettyPrinter(result)