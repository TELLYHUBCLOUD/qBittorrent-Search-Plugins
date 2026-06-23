#VERSION: 1.00
#AUTHORS: Christophe Dumez (chris@dchris.eu)
from novaprinter import prettyPrinter
import urllib
import re

class torrentule(object):
	url = 'http://www.torrentule.com'
	name = 'Torrentule France'

	def search(self, what):
		i = 1
		while True:
			res = 0
			dat = urllib.urlopen(self.url+'/index.php?q=%s&p=%d'%(what,i)).read().decode('utf8', 'replace')
			print "url is "+self.url+'/index.php?q=%s&p=%d'%(what,i)
			# I know it's not very readable, but the SGML parser feels in pain
			section_re = re.compile('(?s)<a class="search_a_news".*?</li>')
			torrent_re = re.compile('(?s)<a class="search_a_news" href="(?P<link>.*?[^"]+).*?'
			'Titre : (?P<name>.*?)- Comm.*?'
			'Taille : (?P<size>.*?)</p></li>')
			for match in section_re.finditer(dat):
				txt = match.group(0)
				m = torrent_re.search(txt)
				if m:
					torrent_infos = m.groupdict()
					torrent_infos['name'] = re.sub('</?span.*?>', '', torrent_infos['name'])
					torrent_infos['engine_url'] = self.url
                                        torrent_infos['seeds'] = -1
                                        torrent_infos['leech'] = -1
					prettyPrinter(torrent_infos)
					res = res + 1
			if res == 0:
				break
			i = i + 1