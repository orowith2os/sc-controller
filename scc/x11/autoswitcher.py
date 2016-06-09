#!/usr/bin/env python2
"""
SC-Controller - Autoswitch Daemon

Observes active window and commands scc-daemon to change profiles as needed.
"""
from __future__ import unicode_literals
from scc.tools import _

from scc.menu_data import MenuGenerator, MenuItem, Separator, MENU_GENERATORS
from scc.lib import xwrappers as X
from scc.tools import find_profile
from scc.paths import get_daemon_socket
from scc.config import Config

import os, sys, re, time, socket, traceback, threading, logging
log = logging.getLogger("AutoSwitcher")

class AutoSwitcher(object):
	INTERVAL = 1
	
	def __init__(self):
		self.dpy = X.open_display(os.environ["DISPLAY"])
		self.lock = threading.Lock()
		self.thread = threading.Thread(target=self.connect_daemon)
		self.config = Config()
		self.socket = None
		self.connected = False
		self.exit_code = None
		self.current_profile = None
		self.conds = AutoSwitcher.parse_conditions()
	
	
	@staticmethod
	def parse_conditions(config):
		""" Parses conditions from config """
		conds = {}
		for c in config['autoswitch']:
			try:
				conds[Condition.parse(c['condition'])] = c['profile']
			except Exception, e:
				# Failure here is not fatal
				log.error("Failed to parse autoswitcher condition '%s'", c)
				log.error(e)
		log.debug("Parsed %s conditions", len(conds))
		return conds
	
	
	def connect_daemon(self, *a):
		try:
			self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			self.socket.connect(get_daemon_socket())
			self.socket.send(b"Register: autoswitch\n")
		except Exception:
			log.error("Failed to connect to scc-daemon")
			os._exit(1)
			return
		buffer = ""
		while self.exit_code is None:
			r = self.socket.recv(1024)
			self.lock.acquire()
			if len(r) == 0:
				self.lock.release()
				log.error("Connection to daemon lost")
				os._exit(2)
				return
			buffer += r
			while "\n" in buffer:
				line, buffer = buffer.split("\n", 1)
				if line.startswith("Version:"):
					version = line.split(":", 1)[-1].strip()
					log.debug("Connected to daemon, version %s", version)
				elif line.startswith("Current profile:"):
					profile = line.split(":", 1)[-1].strip()
					log.debug("Daemon reported profile change: %s", profile)
					self.current_profile = profile
				elif line.startswith("Reconfigured."):
					log.debug("Reloading config...")
					self.config = Config()
					self.parse_conditions()
			
			self.lock.release()
	
	
	def check(self, *a):
		w = X.get_current_window(self.dpy)
		pars = X.get_window_title(self.dpy, w), X.get_window_class(self.dpy, w)
		for c in self.conds:
			if c.matches(*pars):
				profile_name = self.conds[c]
				path = find_profile(profile_name)
				if path:
					self.lock.acquire()
					if path != self.current_profile and not self.current_profile.endswith(".mod"):
						# Switch only if target profile is not active
						# and active profile is not being editted.
						try:
							if self.config['autoswitch_osd']:
								msg = (_("Switched to profile") + " " + profile_name)
								self.socket.send(b"OSD: " + msg.encode('utf-8') + b"\n")
							self.socket.send(b"Profile: " + path.encode('utf-8') + b"\n")
						except:
							self.lock.release()
							log.error("Socket write failed")
							os._exit(2)
							return
					self.lock.release()
				else:
					log.error("Cannot switch to profile '%s', profile file not found", self.conds[c])
	
	
	def sigint(self, *a):
		log.error("break")
		os._exit(0)
	
	
	def run(self):
		self.thread.start()
		log.debug("AutoSwitcher started")
		while self.exit_code is None:
			self.check()
			time.sleep(self.INTERVAL)
		return 1


class Condition(object):
	"""
	Represents AutoSwitcher condition loaded from configuration file.
	
	Currently, there are 4 ways to match window:
	By exact title, by part of title, by regexp aplied on title and by matching
	window class.
	It's possible to combine all three types of title matching with window class
	matching.
	"""
	
	def __init__(self, exact_title=None, title=None, regexp=None, wm_class=None):
		"""
		At least one parameter has to be specified; regexp has to be
		compiled regular expression.
		"""
		self.exact_title = exact_title
		self.title = title
		self.regexp = regexp
		if type(self.regexp) in (str, unicode):
			self.regexp = re.compile(self.regexp)
		self.wm_class = wm_class
		self.empty = not ( title or title or regexp or wm_class )
	
	
	def __str__(self):
		return "<Condition title=%s, exact_title=%s, regexp=%s, wm_class=%s>" % (
			self.title, self.exact_title, self.regexp, self.wm_class)
	
	
	def describe(self):
		"""
		Returns string that describes condition in human-readable form.
		Used in GUI.
		"""
		rv = []
		if self.title:
			rv += [ _("title contains '%s'") % (self.title,) ]
		if self.exact_title:
			rv += [ _("title is '%s'") % (self.exact_title,) ]
		if self.regexp:
			rv += [ _("title matches '%s'") % (self.regexp.pattern,) ]
		if self.wm_class:
			rv += [ _("class is '%s'") % (self.wm_class,) ]
		if rv:
			return _("and ").join(rv)
		return _("matches nothing")
	
	
	@staticmethod
	def parse(data):
		if 'regexp' in data:
			data = dict(data)
			data['regexp'] = re.compile(data['regexp'])
		return Condition(**data)
	
	def encode(self):
		"""
		Returns Condition in dict that can be stored in json configuration
		"""
		rv = {}
		if self.title:
			rv['title'] = self.title
		if self.exact_title:
			rv['exact_title'] = self.exact_title
		if self.regexp:
			rv['regexp'] = self.regexp.pattern
		if self.wm_class:
			rv['wm_class'] = self.wm_class
		return rv
	
	
	def matches(self, window_title, wm_class):
		"""
		Returns True if condition matches provided window properties.
		
		wm_class is what xwrappers.get_window_class returns, tuple of two strings.
		"""
		if self.empty:
			# Empty condition matches nothing
			return False
		
		if self.wm_class:
			if self.wm_class != wm_class[0] and self.wm_class != wm_class[1]:
				# Window class matching is enabled and window doesn't match
				return False
			
		if self.exact_title and self.exact_title != window_title:
			# Matching exact title is enabled, but title doesn't match
			return False
		
		if self.title and self.title not in window_title:
			# Matching part of title is enabled, but doesn't match
			return False
		
		if self.regexp and not self.regexp.match(window_title):
			# Matching by regexp is enabled, but regexp doesn't match
			return False
		
		return True


class AutoswitchOptsMenuGenerator(MenuGenerator):
	""" Generates entire Autoswich Options submenu """
	GENERATOR_NAME = "autoswitch"
	
	def callback(self, menu, daemon, menuitem):
		print "callback", menuitem
	
	
	def generate(self, menuhandler):
		rv = []
		win = X.get_current_window(menuhandler.xdisplay)
		if not win:
			# Bail out if active window cannot be determined
			rv.append(self.mk_item(None, _("No active window")))
			rv.append(self.mk_item("as::close", _("Close")))
			return rv
		
		title = X.get_window_title(menuhandler.xdisplay, win)
		wm_class = X.get_window_class(menuhandler.xdisplay, win)
		assigned_prof = None
		conds = AutoSwitcher.parse_conditions(Config())
		for c in conds:
			if c.matches(title, wm_class):
				assigned_prof = conds[c]
				break
		assigned_prof = None
		if win:
			display_title = title or _("No Title")
			rv.append(self.mk_item(None, _("Current Window: %s") % (title[0:25],)))
			if assigned_prof:
				rv.append(self.mk_item(None, _("Assigned Profile: %s") % (assigned_prof,)))
			else:
				rv.append(self.mk_item(None, _("No Profile Assigned")))
			rv.append(Separator())
			rv.append(Separator())
			rv.append(Separator())
			if assigned_prof:
				rv.append(self.mk_item("as::unassign", _("Unassign Profile")))
			rv.append(self.mk_item("as::assign", _("Assign Current Profile")))
		return rv
	
	
	def mk_item(self, id, title):
		""" Creates menu item and assigns callback """
		menuitem = MenuItem(id, title)
		menuitem.callback = self.callback
		return menuitem

MENU_GENERATORS[AutoswitchOptsMenuGenerator.GENERATOR_NAME] = AutoswitchOptsMenuGenerator
