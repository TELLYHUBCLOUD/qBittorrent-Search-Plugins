#VERSION: 1.2
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
import StringIO, gzip, urllib2, tempfile, os
import urllib
import sgmllib
import re
import cookielib

class sumotorrent(object):
  url = 'http://torrents.sumotorrent.com'
  name = 'SumoTorrent'
  supported_categories = {'all': '', 'movies': '4', 'tv': '9', 'music': '0', 'games': '2', 'anime': '8', 'software': '1'}

  def __init__(self):
    self.results = []
    self.parser = self.SimpleSGMLParser(self.results, self.url)

  def download_torrent(self, url):
    file, path = tempfile.mkstemp()
    file = os.fdopen(file, "w")
    # Reconstruct referer URL
    referer = url.replace('/torrent_download/', '/download/').replace('+', '%20')
    user_agent = 'Mozilla/5.0 (X11; Linux i686; rv:6.0.2) Gecko/20100101 Firefox/6.0.2'
    # Fetch cookies from referer page
    cj = cookielib.LWPCookieJar()
    opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
    urllib2.install_opener(opener)

    pre_req = urllib2.Request(referer)
    pre_req.add_header('User-Agent', user_agent)
    urllib2.urlopen(pre_req)
    # Actually download torrent
    req = urllib2.Request(url)
    req.add_header('referer', referer)
    req.add_header('User-Agent', user_agent)
    response = urllib2.urlopen(req)
    dat = response.read()
    # Check if data is gzip encoded
    response_info = response.info()
    content_encoding = response_info.get('Content-Encoding')
    if content_encoding is not None and 'gzip' in content_encoding:
        # Data is gzip encoded, decode it
        compressedstream = StringIO.StringIO(dat)
        gzipper = gzip.GzipFile(fileobj=compressedstream)
        extracted_data = gzipper.read()
        dat = extracted_data
    # Write it to a file
    file.write(dat)
    file.close()
    # return file path
    print path+" "+url

  class SimpleSGMLParser(sgmllib.SGMLParser):
    def __init__(self, results, url, *args):
      sgmllib.SGMLParser.__init__(self)
      self.url = url
      self.td_counter = None
      self.current_item = None
      self.results = results
      
    def start_a(self, attr):
      params = dict(attr)
      #print params
      if params.has_key('href') and 'en/details/' in params['href'] and (self.td_counter is None or self.td_counter > 5):
        self.current_item = {}
        self.td_counter = 0
        self.current_item['desc_link'] = params['href']
      elif params.has_key('href') and params['href'].startswith("http://torrents.sumotorrent.com/download/"):
        parts = params['href'].strip().split('/')
        self.current_item['link'] = 'http://torrents.sumotorrent.com/torrent_download/'+parts[-3]+'/'+parts[-2]+'/'+urllib.quote(parts[-1]).replace('%20', '+')
    
    def handle_data(self, data):
      if self.td_counter == 0:
        if not self.current_item.has_key('name'):
          self.current_item['name'] = ''
        self.current_item['name']+= data.strip()
      elif self.td_counter == 3:
        if not self.current_item.has_key('size'):
          self.current_item['size'] = ''
        self.current_item['size']+= data.strip()
      elif self.td_counter == 4:
        if not self.current_item.has_key('seeds'):
          self.current_item['seeds'] = ''
        self.current_item['seeds']+= data.strip()
      elif self.td_counter == 5:
        if not self.current_item.has_key('leech'):
          self.current_item['leech'] = ''
        self.current_item['leech']+= data.strip()
      
    def start_td(self,attr):
        if isinstance(self.td_counter,int):
          self.td_counter += 1
          if self.td_counter > 6:
            self.td_counter = None
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
      dat = retrieve_url(self.url+'/searchResult.php?search=%s&lngMainCat=%s&order=seeders&by=down&start=%d'%(what, self.supported_categories[cat], i))
      parser.feed(dat)
      parser.close()
      if len(results) <= 0:
        break
      i += 1
      
