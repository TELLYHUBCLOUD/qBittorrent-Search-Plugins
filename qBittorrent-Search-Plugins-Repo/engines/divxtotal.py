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
'DivxTotal plug-in for qBittorrent'

import re
import urllib2

class divxtotal(object):
	url  = 'http://divxtotal.com'
	name = 'DivxTotal'

	supported_categories = {
		'all'      : '',
		'movies'   : 'peliculas',
		'tv'       : 'series',
		'music'    : 'musica',
		'software' : 'programas',
	}

	out = [-1]*7
	(LINK,NAME,SIZE,SEEDS,LEECH,ENGINE_URL,DESCRIPTION) = range(0,7)

	MULTIPLIERS = {
		'B'  : 1,
		'KB' : 1024,
		'MB' : 1024**2,
		'GB' : 1024**3,
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
		nextpage = 'buscar.php?busqueda=' + what
		while nextpage != False:
			u = urllib2.urlopen('http://divxtotal.com/' + nextpage)
			nextpage = False

			content = ''
			for line in u.readlines():
				content += line.strip()
			u.close()

			for item in re.split('<li class="section_item2?">', content)[1:-1]:

				# Name, link and description
				match = re.search('<p class="seccontnom">(.+)<p class="seccontgen"', item)
				n = re.search('<a href="(.+)/torrent/([0-9]+)/(.+)"(.+)>(.+)</a>', match.group(1))
				self.out[self.DESCRIPTION] = 'http://divxtotal.com/' + re.search('<a href="(.+)/"(.+)>(.+)</a>', match.group(1)).group(1) + '/'
				self.out[self.LINK] = 'http://divxtotal.com/download.php?id={0}'.format(
					n.group(2)
				)
				self.out[self.NAME] = n.group(5)

				# Torrent size
				match = re.search('<p class="seccontfetam">(.+)</p></li>', item)
				if re.search('^[0-9\-]+$', match.group(1))==None:
					matchSize = re.search('([0-9\.]+) (.+)', match.group(1))
					size = int(
						float(matchSize.group(1)) * self.MULTIPLIERS[matchSize.group(2)]
					)
					self.out[self.SIZE] = size

				# Category
				match = re.search('<p class="seccontgen">(.+)</p><p class="seccontfetam', item)
				matchCategory = re.search("<a href=\"(.+)/\"(.+)</a>", match.group(1)).group(1)
				if cat=='all' or matchCategory==self.supported_categories[cat]:
					print_this = True

				if print_this:
					self.put()
					print_this = False

			match = re.search("<a href='([A-z0-9\-\&\=\+\?\.]+)'>Siguiente >>", re.split('<div class="pagination">',content)[-1])
			if match!=None:
				nextpage = match.group(1)
