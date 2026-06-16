#VERSION: 1.00
#AUTHORS: Christophe Dumez (chris@dchris.eu)
from novaprinter import prettyPrinter
import urllib
import re

class sharetv(object):
	url = 'http://sharetv.org'
	name = 'shareTV'

	def search(self, what):
		i = 1
		while True:
			res = 0
			dat = urllib.urlopen(self.url+'/search/%s/pg-%i'%(what,i)).read().decode('utf8', 'replace')
			# I know it's not very readable, but the SGML parser feels in pain
			section_re = re.compile("(?s)href='/torrent.*?<tr>")
			torrent_re = re.compile("(?s)href='/torrent.*?>(?P<name>.*?)</a>.*?"
			"title='(?P<seeds>\d+)\sseeders.*?"
			",\s(?P<leech>\d+)\sdownloaders.*?"
			"href='(?P<link>.*?[^']+)'><img.*?src='/images/download.*?")
			for match in section_re.finditer(dat):
				txt = match.group(0)
				m = torrent_re.search(txt)
				if m:
					torrent_infos = m.groupdict()
					torrent_infos['engine_url'] = self.url
					torrent_infos['link'] = self.url+torrent_infos['link']
					# This is a hack to return -1
					# Size is not provided by shareTV
					torrent_infos['size'] = -1
					prettyPrinter(torrent_infos)
					res = res + 1
			if res == 0:
				break
			i = i + 1