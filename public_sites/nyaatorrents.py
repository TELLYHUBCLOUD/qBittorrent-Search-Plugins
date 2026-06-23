#VERSION: 1.1
#AUTHORS: Shiro Hazuki (hazuki.shiro@gmail.com)
#CONTRIBUTORS: Joe (boxofmailforme@gmail.com)
#
#                    GNU GENERAL PUBLIC LICENSE
#                       Version 3, 29 June 2007
#
#                   <http://www.gnu.org/licenses/>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#


from __future__ import print_function

try:
    from urllib import urlencode
except:
    from urllib.parse import urlencode

import re
import string
from helpers import retrieve_url


class nyaatorrents(object):
    url  = 'http://www.nyaa.eu'
    name = 'NyaaTorrents'
    supported_categories = {
        'all'       : ['0_0'],
        'music'     : ['3_0','33_0'], # Lossy Audio and Lossless Audio
        'anime'     : ['1_0'],
        'software'  : ['6_0'],
        'pictures'  : ['4_0'],
        'books'     : ['2_0'],
    }
    
    def search(self, what, cat='all'):
        for current_cat in self.supported_categories[cat]:
            for current_page in range(2, self.Page(what, 1, current_cat).load().find_torrents(True).last_page+1):
                self.Page(what, current_page, current_cat).load().find_torrents(True)

    class Torrent(object):
        _link        = -1
        _name        = -1
        _size        = -1
        _seeds       = -1
        _leech       = -1
        _description = -1
        
        def __str__(self):
            return '|'.join([str(x) for x in [
                self._link,
                self._name,
                self._size,
                self._seeds,
                self._leech,
                nyaatorrents.url,
                self._description,
            ]])

        def link(self, link):
            self._link = link
            return self

        def description(self, description):
            self._description = description
            return self

        def seeds(self, i):
            self._seeds = int(i)
            return self

        def leech(self, i):
            self._leech = int(i)
            return self

        def name(self, name):
            if re.search('&#[0-9]{1,3};', name) != None:
                for i in range(256):
                    name = name.replace('&#{0};'.format(i),chr(i))
            self._name = name.replace('|','-')
            return self

        def size(self, torrent_size):
            if type(torrent_size) == type(1):
                self._size = torrent_size
            else:
                match = re.search("([0-9\.]+) ?([kmgt]?i?b)", torrent_size.lower())
                if match != None:
                    n = float(match.group(1))
                    s = match.group(2)
                    if s == 'b':
                        self._size = int(n)
                    elif re.match('ki?b', s) != None:
                        self._size = int( n * 1024 )
                    elif re.match('mi?b', s) != None:
                        self._size = int( n * 1024**2 )
                    elif re.match('gi?b', s) != None:
                        self._size = int( n * 1024**3 )
                    elif re.match('ti?b', s) != None:
                        self._size = int( n * 1024**4 )
            return self

    class Page(object):
        html = ''
        torrents = []
        last_page = 1

        def __init__(self, query="", page=1, category="all"):
            self.url = 'http://www.nyaa.eu/?' + urlencode((
                ('page'   , 'search'),
                ('cats'   , category),
                ('filter' , '0'),
                ('offset' , page),
            )) + '&term=' + query

        def load(self):
            self.html = retrieve_url(self.url)
            return self

        def find_torrents(self, echo=True):
            html = self.html
            try:
                html = string.split(html, 'tr class="pages"')[1]
            except:
                return self.parse_description(echo)
            pages, torrents = string.split(html, '</table')[0:2]
            torrents = string.split(torrents, '<tr')[2:]
            match = re.search('offset=([0-9]+)">(&gt;&gt;|>>)</a>', pages)
            if match != None:
                self.last_page = int(match.group(1))
            for x in torrents:
                match = {
                    'name' : re.search('<td class="tlistname"><a href="[^"]+">(.+)</a></td><td class="tlistdownload">',x),
                    'description' : re.search('<td class="tlistname"><a href="([^"]+)">',x),
                    'seeds' : re.search('<td class="tlistsn">([0-9\.]+)',x),
                    'leech' : re.search('<td class="tlistln">([0-9\.]+)',x),
                    'size' : re.search('<td class="tlistsize">([0-9\.]+ *[a-zA-Z]+)</td>',x),
                }
                if match['seeds'] == None:
                    match['seeds'] = -1
                if match['leech'] == None:
                    match['leech'] = -1
                if None in match.values():
                    continue
                self.torrents.append( nyaatorrents.Torrent() )
                for key in match:
                    value = match[key]
                    self.torrents[-1].__class__.__dict__[key](self.torrents[-1], value if value == -1 else value.group(1))
                self.torrents[-1].link(self.torrents[-1]._description.replace('torrentinfo','download'))
                if echo:
                    print(self.torrents[-1])
            return self

        def parse_description(self, echo=True):
            self.torrents.append(
                nyaatorrents.Torrent()
                .name(re.search('class="tinfotorrentname">([^<]+)</td>', self.html).group(1))
                .seeds(re.search('class="tinfosn">([^<]+)<', self.html).group(1))
                .leech(re.search('class="tinfoln">([^<]+)<', self.html).group(1))
                .link(re.search('<a href="([^"]+)"[^>]*><img[^>]*download[^>]*></a>',self.html).group(1).replace("&amp;","&"))
                .size(re.search('<td class="vtop">([0-9\.]+ (b|ki?b|mi?b|gi?b|ti?b))</td>',self.html.lower()).group(1))
            )
            self.torrents[-1].description(self.torrents[-1]._link.replace("page=download","page=torrentinfo"))
            if echo:
                print(self.torrents[-1])
            return self
