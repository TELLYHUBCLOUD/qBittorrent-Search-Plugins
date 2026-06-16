#VERSION: 1.1
#AUTHORS: Christophe Dumez (chris@qbittorrent.org)

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#    * Neither the name of the author nor the names of its contributors may be
#      used to endorse or promote products derived from this software without
#      specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


from novaprinter import prettyPrinter
from helpers import retrieve_url, download_file
import sgmllib
import re

class fenopy(object):
  url = 'http://fenopy.com'
  name = 'fenopy'
  supported_categories = {'all': '', 'movies': 'c_3', 'tv': 'c_78', 'music': 'c_1', 'games': 'c_4', 'anime': 'c_5', 'software': 'c_6', 'books': 'c_7'}

  def __init__(self):
    self.results = []
    self.parser = self.SimpleSGMLParser(self.results, self.url)

  def download_torrent(self, info):
    print download_file(info)

  class SimpleSGMLParser(sgmllib.SGMLParser):
    def __init__(self, results, url, *args):
      sgmllib.SGMLParser.__init__(self)
      self.url = url
      self.dt_counter = None
      self.current_item = None
      self.results = results
      
    def start_a(self, attr):
      params = dict(attr)
      #print params
      if params.has_key('href') and params['href'].startswith("/torrent/"):
        if params['href'].endswith("download.torrent"):
          self.current_item['link'] = self.url+params['href'].strip()
        else:
          self.current_item = {}
          self.current_item['desc_link'] = self.url+params['href'].strip()
          self.dt_counter = 0
    
    def handle_data(self, data):
      if self.dt_counter == 0:
        if not self.current_item.has_key('name'):
          self.current_item['name'] = ''
        self.current_item['name']+= data.strip()
      elif self.dt_counter == 5:
        if not self.current_item.has_key('size'):
          self.current_item['size'] = ''
        self.current_item['size']+= data.strip()
      elif self.dt_counter == 3:
        if not self.current_item.has_key('seeds'):
          self.current_item['seeds'] = ''
        self.current_item['seeds']+= data.strip().replace(',', '')
      elif self.dt_counter == 4:
        if not self.current_item.has_key('leech'):
          self.current_item['leech'] = ''
        self.current_item['leech']+= data.strip().replace(',', '')
      
    def start_td(self,attr):
        if isinstance(self.dt_counter,int):
          self.dt_counter += 1
          if self.dt_counter > 6:
            self.dt_counter = None
            # Display item
            if self.current_item:
              self.current_item['engine_url'] = self.url
              if not self.current_item['seeds'].isdigit():
                self.current_item['seeds'] = 0
              if not self.current_item['leech'].isdigit():
                self.current_item['leech'] = 0
              prettyPrinter(self.current_item)
              self.results.append('a')

  def search(self, what, cat='all'):
    ret = []
    i = 0
    while True and i<11:
      results = []
      parser = self.SimpleSGMLParser(results, self.url)
      dat = retrieve_url(self.url+'/?keyword=%s&torrentsearch=1&select=%s&order=3&sort=0&start=%d'%(what, self.supported_categories[cat], i*30))
      results_re = re.compile('(?s)table id="search_table".*</table>')
      for match in results_re.finditer(dat):
        res_tab = match.group(0)
        parser.feed(res_tab)
        parser.close()
        break
      if len(results) <= 0:
        break
      i += 1
      
