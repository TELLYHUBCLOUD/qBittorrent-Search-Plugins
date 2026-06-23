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
'Demonoid plug-in for qBittorrent'

import urllib2, re
from urllib import urlencode

class demonoid(object):
	url  = 'http://www.demonoid.me'
	name = 'Demonoid'

	supported_categories = {
		'all'      : '0',
		'movies'   : '1',
		'tv'       : '3',
		'music'    : '2',
		'games'    : '4',
		'anime'    : '9',
		'software' : '5',
	}

	def resetOutput(self):
		self.temp = {
			"link"        : -1,
			"name"        : -1,
			"size"        : -1,
			"seeds"       : -1,
			"leech"       : -1,
			"engine_url"  : "http://www.demonoid.me",
			"description" : -1
		}
		return True

	def search(self, what, cat='all'):
		urldata = {
			'category'    : self.supported_categories[cat],
			'subcategory' : 0,
			'language'    : 0,
			'quality'     : 0,
			'seeded'      : 0,
			'external'    : 2,
			'uid'         : 0,
			'sort'        : '',
		}
		nextpage = "/files/?" + urlencode(urldata) + '&query=' + what
		while nextpage != False:
			try:
				u = urllib2.urlopen("http://www.demonoid.me{0}".format(nextpage))
			except:
				break

			self.resetOutput()

			nextpage = False
			pos      = 0
			flag     = 0
			counter  = 0

			for line in u:
				m = None
				if pos>0:
					if flag==1:
						if counter>0:
							counter -= 1
							continue
						else:
							if pos==1:
								if type(re.search("<IFRAME",line))!=type(None):
									flag = 0
									continue
								m = re.search("<a href=\"(.+)\">(.+)</a>",line)
								self.temp["description"] = "http://www.demonoid.me{0}".format(m.group(1))
								self.temp["link"] = self.temp["description"].replace("details","download")
								self.temp["name"] = m.group(2)
								pos = 2
								counter = 10
							elif pos==2:
								m = re.search(">(.+)</td>",line)
								t = {"GB":1024**3,"MB":1024**2,"KB":1024,"B":1}
								n = re.search("([0-9.]+) (.+)",m.group(1))
								s = int(float(n.group(1))*t[n.group(2)])
								self.temp["size"] = s
								pos = 3
								counter = 2
							elif pos==3:
								m = re.search(">([0-9\.,]+)</",line)
								self.temp["seeds"] = re.sub(",|\.","",m.group(1))
								pos = 4
							elif pos==4:
								m = re.search(">([0-9\.,]+)</",line)
								self.temp["leech"] = re.sub(",|\.","",m.group(1))
								print "{0}|{1}|{2}|{3}|{4}|{5}|{6}".format(self.temp["link"],self.temp["name"],self.temp["size"],self.temp["seeds"],self.temp["leech"],self.temp["engine_url"],self.temp["description"])
								self.resetOutput()
								pos = 1
								flag = 0
					else:
						if type(re.search("<tr align=\"left\" bgcolor=\"#CCCCCC\">",line))!=type(None):
							flag = 1
							counter = 3
						elif type(re.search("<\!-- tstart -->",line))!=type(None):
							flag = 1
				if pos==0:
					m = re.search("<a href=(.+) class=\"menu\">Next &gt;&gt;</a></td>",line)
					if type(m)!=type(None):
						nextpage = m.group(1)
				if type(re.search("<\!-- start torrent list -->",line))!=type(None):
					pos = 1
				if type(re.search("<\!-- end torrent list -->",line))!=type(None):
					pos = 0
					break
			u.close()
