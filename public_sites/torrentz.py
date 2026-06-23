#VERSION: 1.2
#AUTHORS: Shiro Hazuki (hazuki.shiro@gmail.com), cdgg (cdgg.cdgg@gmail.com)
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
'TorrentZ plug-in for qBittorrent'

import re
import string
import urllib2

class torrentz(object):
	url  = 'http://torrentz.eu'
	name = 'Torrentz'

	supported_categories = {'all': ''}

	out = [-1]*7
	(LINK,NAME,SIZE,SEEDS,LEECH,ENGINE_URL,DESCRIPTION) = range(0,7)

	MULTIPLIERS = {
		'b'  : 1,
		'B'  : 1,
		'Kb' : 1024,
		'Mb' : 1024**2,
		'Gb' : 1024**3,
	}

	def reset(self):
		self.out = [-1]*7
		self.out[self.ENGINE_URL] = self.url

	def put(self):
		print '|'.join([str(x) for x in self.out])
		self.reset()

	def __init__(self):
		self.reset()

	def search(self, what, cat='all'):
		nextpage = 'search?f=' + what
		while nextpage != False:
			u = urllib2.urlopen('http://torrentz.eu/' + nextpage)

			content = ''
			for line in u:
				content += line.strip('\n')
			u.close()

			m = re.search('<div class="results">(.+)</div><div class="help">', content)
			content = m.group(1)

			l = string.split(content, "<dl>")[1:]

			temp = string.split(l[-1], "</dl>")
			l[-1] = temp[0]
			p = temp[1]

			# Searches for another page
			nextpage = False
			m = re.search("<a href=\"/([a-zA-Z0-9\%\&\;\+\?\=]+)\">Next \&raquo\;</a>", p)
			if m!=None:
				nextpage = m.group(1).replace("&amp;", "&")

			# Name, link and description link
			pattern1 = '<dt.*><a href="/([a-zA-Z0-9]+)">(.+)</a>'

			# Seed, leech and size
			pattern2 = '<span class="s">(.+)</span> <span class="u">(.+)</span> <span class="d">(.+)</span></dd>'

			# Valid links
			link = [
				'http://www.torrenthound.com/torrent/{0}',
				'http://h33t.com/download.php?id={0}',
				'http://torrage.com/torrent/{0}.torrent',
				'http://torcache.com/torrent/{0}.torrent',
				'http://zoink.it/torrent/{0}.torrent',
			]

			m = None

			for line in l:
				m = re.match(pattern1, line)
				self.out[self.DESCRIPTION] = '{0}/{1}'.format(self.out[self.ENGINE_URL], m.group(1))

				# Test whether the link is availabe or not
				available = False
				for current_link in link:
					self.out[self.LINK] = current_link.format(m.group(1))
					try:
						urllib2.urlopen(self.out[self.LINK])
						available = True
						break
					except:
						pass

				if not available:
					continue

				self.out[self.NAME] = re.sub('<[bB]>|</[bB]>', '', m.group(2))

				m = re.search(pattern2, line)

				self.out[self.SEEDS] = m.group(2).replace(',', '')
				self.out[self.LEECH] = m.group(3).replace(',', '')

				# Torrent size
				n = re.search("([0-9,.]+) (.+)", m.group(1))
				try:
					self.out[self.SIZE] = int(float(n.group(1)) * self.MULTIPLIERS[n.group(2)])
					self.put()
				except:
					self.reset()
