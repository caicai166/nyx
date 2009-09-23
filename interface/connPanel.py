#!/usr/bin/env python
# connPanel.py -- Lists network connections used by tor.
# Released under the GPL v3 (http://www.gnu.org/licenses/gpl.html)

import os
import time
import socket
import curses
from threading import RLock
from TorCtl import TorCtl

import hostnameResolver
import util

# directory servers (IP, port) for tor version 0.2.2.1-alpha-dev
DIR_SERVERS = [("86.59.21.38", "80"),         # tor26
               ("128.31.0.34", "9031"),       # moria1
               ("216.224.124.114", "9030"),   # ides
               ("80.190.246.100", "80"),      # gabelmoo
               ("194.109.206.212", "80"),     # dizum
               ("213.73.91.31", "80"),        # dannenberg
               ("208.83.223.34", "443")]      # urras

# enums for listing types
LIST_IP, LIST_HOSTNAME, LIST_FINGERPRINT, LIST_NICKNAME = range(4)
LIST_LABEL = {LIST_IP: "IP Address", LIST_HOSTNAME: "Hostname", LIST_FINGERPRINT: "Fingerprint", LIST_NICKNAME: "Nickname"}

# attributes for connection types
TYPE_COLORS = {"inbound": "green", "outbound": "blue", "client": "cyan", "directory": "magenta", "control": "red", "localhost": "yellow"}
TYPE_WEIGHTS = {"inbound": 0, "outbound": 1, "client": 2, "directory": 3, "control": 4, "localhost": 5} # defines ordering

# enums for indexes of ConnPanel 'connections' fields
CONN_TYPE, CONN_L_IP, CONN_L_PORT, CONN_F_IP, CONN_F_PORT, CONN_COUNTRY, CONN_TIME = range(7)

# labels associated to 'connectionCount' 
CONN_COUNT_LABELS = ["inbound", "outbound", "client", "directory", "control"]

# enums for sorting types (note: ordering corresponds to SORT_TYPES for easy lookup)
# TODO: add ORD_BANDWIDTH -> (ORD_BANDWIDTH, "Bandwidth", lambda x, y: ???)
ORD_TYPE, ORD_FOREIGN_LISTING, ORD_SRC_LISTING, ORD_DST_LISTING, ORD_COUNTRY, ORD_FOREIGN_PORT, ORD_SRC_PORT, ORD_DST_PORT, ORD_TIME = range(9)
SORT_TYPES = [(ORD_TYPE, "Connection Type",
                lambda x, y: TYPE_WEIGHTS[x[CONN_TYPE]] - TYPE_WEIGHTS[y[CONN_TYPE]]),
              (ORD_FOREIGN_LISTING, "Listing (Foreign)", None),
              (ORD_SRC_LISTING, "Listing (Source)", None),
              (ORD_DST_LISTING, "Listing (Dest.)", None),
              (ORD_COUNTRY, "Country Code",
                lambda x, y: cmp(x[CONN_COUNTRY], y[CONN_COUNTRY])),
              (ORD_FOREIGN_PORT, "Port (Foreign)",
                lambda x, y: int(x[CONN_F_PORT]) - int(y[CONN_F_PORT])),
              (ORD_SRC_PORT, "Port (Source)",
                lambda x, y: int(x[CONN_F_PORT] if x[CONN_TYPE] == "inbound" else x[CONN_L_PORT]) - int(y[CONN_F_PORT] if y[CONN_TYPE] == "inbound" else y[CONN_L_PORT])),
              (ORD_DST_PORT, "Port (Dest.)",
                lambda x, y: int(x[CONN_L_PORT] if x[CONN_TYPE] == "inbound" else x[CONN_F_PORT]) - int(y[CONN_L_PORT] if y[CONN_TYPE] == "inbound" else y[CONN_F_PORT])),
              (ORD_TIME, "Connection Time",
                lambda x, y: cmp(-x[CONN_TIME], -y[CONN_TIME]))]

# provides bi-directional mapping of sorts with their associated labels
def getSortLabel(sortType, withColor = False):
  """
  Provides label associated with a type of sorting. Throws ValueEror if no such
  sort exists. If adding color formatting this wraps with the following mappings:
  Connection Type     red
  Listing *           blue
  Port *              green
  Bandwidth           cyan
  Country Code        yellow
  """
  
  for (type, label, func) in SORT_TYPES:
    if sortType == type:
      color = None
      
      if withColor:
        if label == "Connection Type": color = "red"
        elif label.startswith("Listing"): color = "blue"
        elif label.startswith("Port"): color = "green"
        elif label == "Bandwidth": color = "cyan"
        elif label == "Country Code": color = "yellow"
        elif label == "Connection Time": color = "magenta"
      
      if color: return "<%s>%s</%s>" % (color, label, color)
      else: return label
  
  raise ValueError(sortType)

def getSortType(sortLabel):
  """
  Provides sort type associated with a given label. Throws ValueEror if label
  isn't recognized.
  """
  
  for (type, label, func) in SORT_TYPES:
    if sortLabel == label: return type
  raise ValueError(sortLabel)

class ConnPanel(TorCtl.PostEventListener, util.Panel):
  """
  Lists netstat provided network data of tor.
  """
  
  def __init__(self, lock, conn, torPid, logger):
    TorCtl.PostEventListener.__init__(self)
    util.Panel.__init__(self, lock, -1)
    self.scroll = 0
    self.conn = conn                # tor connection for querrying country codes
    self.logger = logger            # notified in case of problems
    self.pid = torPid               # tor process ID to make sure we've got the right instance
    self.listingType = LIST_IP      # information used in listing entries
    self.allowDNS = True            # permits hostname resolutions if true
    self.showLabel = True           # shows top label if true, hides otherwise
    self.showingDetails = False     # augments display to accomidate details window if true
    self.lastUpdate = -1            # time last stats was retrived
    self.localhostEntry = None      # special connection - tuple with (entry for this node, fingerprint)
    self.sortOrdering = [ORD_TYPE, ORD_FOREIGN_LISTING, ORD_FOREIGN_PORT]
    self.resolver = hostnameResolver.HostnameResolver()
    self.fingerprintLookupCache = {}                              # chache of (ip, port) -> fingerprint
    self.nicknameLookupCache = {}                                 # chache of (ip, port) -> nickname
    self.fingerprintMappings = _getFingerprintMappings(self.conn) # mappings of ip -> [(port, fingerprint, nickname), ...]
    self.nickname = self.conn.get_option("Nickname")[0][1]
    self.providedGeoipWarning = False
    self.orconnStatusCache = []           # cache for 'orconn-status' calls
    self.orconnStatusCacheValid = False   # indicates if cache has been invalidated
    self.clientConnectionCache = None     # listing of nicknames for our client connections
    self.clientConnectionLock = RLock()   # lock for clientConnectionCache
    
    self.isCursorEnabled = True
    self.cursorSelection = None
    self.cursorLoc = 0              # fallback cursor location if selection disappears
    
    # parameters used for pausing
    self.isPaused = False
    self.pauseTime = 0              # time when paused
    self.connectionsBuffer = []     # location where connections are stored while paused
    
    # uses ports to identify type of connections
    self.orPort = self.conn.get_option("ORPort")[0][1]
    self.dirPort = self.conn.get_option("DirPort")[0][1]
    self.controlPort = self.conn.get_option("ControlPort")[0][1]
    
    # netstat results are tuples of the form:
    # (type, local IP, local port, foreign IP, foreign port, country code)
    self.connections = []
    self.connectionsLock = RLock()    # limits modifications of connections
    
    # count of total inbound, outbound, client, directory, and control connections
    self.connectionCount = [0] * 5
    
    self.reset()
  
  # change in client circuits
  def circ_status_event(self, event):
    self.clientConnectionLock.acquire()
    self.clientConnectionCache = None
    self.clientConnectionLock.release()
  
  # when consensus changes update fingerprint mappings
  def new_consensus_event(self, event):
    self.orconnStatusCacheValid = False
    self.fingerprintLookupCache.clear()
    self.nicknameLookupCache.clear()
    self.fingerprintMappings = _getFingerprintMappings(self.conn, event.nslist)
    if self.listingType != LIST_HOSTNAME: self.sortConnections()
  
  def new_desc_event(self, event):
    self.orconnStatusCacheValid = False
    
    for fingerprint in event.idlist:
      # clears entries with this fingerprint from the cache
      if fingerprint in self.fingerprintLookupCache.values():
        invalidEntries = set(k for k, v in self.fingerprintLookupCache.iteritems() if v == fingerprint)
        for k in invalidEntries:
          # nicknameLookupCache keys are a subset of fingerprintLookupCache
          del self.fingerprintLookupCache[k]
          if k in self.nicknameLookupCache.keys(): del self.nicknameLookupCache[k]
      
      # gets consensus data for the new description
      try: nsData = self.conn.get_network_status("id/%s" % fingerprint)
      except (TorCtl.ErrorReply, TorCtl.TorCtlClosed): return
      
      if len(nsData) > 1:
        # multiple records for fingerprint (shouldn't happen)
        self.logger.monitor_event("WARN", "Multiple consensus entries for fingerprint: %s" % fingerprint)
        return
      nsEntry = nsData[0]
      
      # updates fingerprintMappings with new data
      if nsEntry.ip in self.fingerprintMappings.keys():
        # if entry already exists with the same orport, remove it
        orportMatch = None
        for entryPort, entryFingerprint, entryNickname in self.fingerprintMappings[nsEntry.ip]:
          if entryPort == nsEntry.orport:
            orportMatch = (entryPort, entryFingerprint, entryNickname)
            break
        
        if orportMatch: self.fingerprintMappings[nsEntry.ip].remove(orportMatch)
        
        # add new entry
        self.fingerprintMappings[nsEntry.ip].append((nsEntry.orport, nsEntry.idhex, nsEntry.nickname))
      else:
        self.fingerprintMappings[nsEntry.ip] = [(nsEntry.orport, nsEntry.idhex, nsEntry.nickname)]
    if self.listingType != LIST_HOSTNAME: self.sortConnections()
  
  def reset(self):
    """
    Reloads netstat results.
    """
    
    if not self.pid: return
    self.connectionsLock.acquire()
    self.clientConnectionLock.acquire()
    
    # temporary variables for connections and count
    connectionsTmp = []
    connectionCountTmp = [0] * 5
    
    try:
      if self.clientConnectionCache == None and not self.isPaused:
        # client connection cache was invalidated
        self.clientConnectionCache = _getClientConnections(self.conn)
      
      connTimes = {} # mapping of ip/port to connection time
      for entry in (self.connections if not self.isPaused else self.connectionsBuffer):
        connTimes[(entry[CONN_F_IP], entry[CONN_F_PORT])] = entry[CONN_TIME]
      
      # looks at netstat for tor with stderr redirected to /dev/null, options are:
      # n = prevents dns lookups, p = include process (say if it's tor), t = tcp only
      netstatCall = os.popen("netstat -npt 2> /dev/null | grep %s/tor 2> /dev/null" % self.pid)
      
      try:
        results = netstatCall.readlines()
        
        for line in results:
          if not line.startswith("tcp"): continue
          param = line.split()
          local, foreign = param[3], param[4]
          localIP, foreignIP = local[:local.find(":")], foreign[:foreign.find(":")]
          localPort, foreignPort = local[len(localIP) + 1:], foreign[len(foreignIP) + 1:]
          
          if localPort in (self.orPort, self.dirPort):
            type = "inbound"
            connectionCountTmp[0] += 1
          elif localPort == self.controlPort:
            type = "control"
            connectionCountTmp[4] += 1
          else:
            fingerprint = self.getFingerprint(foreignIP, foreignPort)
            nickname = self.getNickname(foreignIP, foreignPort)
            
            isClient = False
            for clientName in self.clientConnectionCache:
              if nickname == clientName or (len(clientName) > 1 and clientName[0] == "$" and fingerprint == clientName[1:]):
                isClient = True
                break
            
            if isClient:
              type = "client"
              connectionCountTmp[2] += 1
            elif (foreignIP, foreignPort) in DIR_SERVERS:
              type = "directory"
              connectionCountTmp[3] += 1
            else:
              type = "outbound"
              connectionCountTmp[1] += 1
          
          try:
            countryCodeQuery = "ip-to-country/%s" % foreign[:foreign.find(":")]
            countryCode = self.conn.get_info(countryCodeQuery)[countryCodeQuery]
          except (socket.error, TorCtl.ErrorReply):
            countryCode = "??"
            if not self.providedGeoipWarning:
              self.logger.monitor_event("WARN", "Tor geoip database is unavailable.")
              self.providedGeoipWarning = True
          
          if (foreignIP, foreignPort) in connTimes: connTime = connTimes[(foreignIP, foreignPort)]
          else: connTime = time.time()
          
          connectionsTmp.append((type, localIP, localPort, foreignIP, foreignPort, countryCode, connTime))
      except IOError:
        # netstat call failed
        self.logger.monitor_event("WARN", "Unable to query netstat for new connections")
        return
      
      # appends localhost connection to allow user to look up their own consensus entry
      selfAddress, selfPort, selfFingerprint = None, None, None
      try:
        selfAddress = self.conn.get_info("address")["address"]
        selfPort = self.conn.get_option("ORPort")[0][1]
        selfFingerprint = self.conn.get_info("fingerprint")["fingerprint"]
      except (TorCtl.ErrorReply, TorCtl.TorCtlClosed, socket.error): pass
      
      if selfAddress and selfPort and selfFingerprint:
        try:
          countryCodeQuery = "ip-to-country/%s" % selfAddress
          selfCountryCode = self.conn.get_info(countryCodeQuery)[countryCodeQuery]
        except (socket.error, TorCtl.ErrorReply):
          selfCountryCode = "??"
        
        if (selfAddress, selfPort) in connTimes: connTime = connTimes[(selfAddress, selfPort)]
        else: connTime = time.time()
        
        self.localhostEntry = (("localhost", selfAddress, selfPort, selfAddress, selfPort, selfCountryCode, connTime), selfFingerprint)
        connectionsTmp.append(self.localhostEntry[0])
      else:
        self.localhostEntry = None
      
      netstatCall.close()
      self.lastUpdate = time.time()
      
      # assigns results
      if self.isPaused:
        self.connectionsBuffer = connectionsTmp
      else:
        self.connections = connectionsTmp
        self.connectionCount = connectionCountTmp
        
        # hostnames are sorted at redraw - otherwise now's a good time
        if self.listingType != LIST_HOSTNAME: self.sortConnections()
    finally:
      self.connectionsLock.release()
      self.clientConnectionLock.release()
  
  def handleKey(self, key):
    # cursor or scroll movement
    if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
      self._resetBounds()
      pageHeight = self.maxY - 1
      if self.showingDetails: pageHeight -= 8
      
      self.connectionsLock.acquire()
      try:
        # determines location parameter to use
        if self.isCursorEnabled:
          try: currentLoc = self.connections.index(self.cursorSelection)
          except ValueError: currentLoc = self.cursorLoc # fall back to nearby entry
        else: currentLoc = self.scroll
        
        # location offset
        if key == curses.KEY_UP: shift = -1
        elif key == curses.KEY_DOWN: shift = 1
        elif key == curses.KEY_PPAGE: shift = -pageHeight + 1 if self.isCursorEnabled else -pageHeight
        elif key == curses.KEY_NPAGE: shift = pageHeight - 1 if self.isCursorEnabled else pageHeight
        newLoc = currentLoc + shift
        
        # restricts to valid bounds
        maxLoc = len(self.connections) - 1 if self.isCursorEnabled else len(self.connections) - pageHeight
        newLoc = max(0, min(newLoc, maxLoc))
        
        # applies to proper parameter
        if self.isCursorEnabled and self.connections:
          self.cursorSelection, self.cursorLoc = self.connections[newLoc], newLoc
        else: self.scroll = newLoc
      finally:
        self.connectionsLock.release()
    elif key == ord('r') or key == ord('R'):
      self.allowDNS = not self.allowDNS
      if not self.allowDNS: self.resolver.setPaused(True)
      elif self.listingType == LIST_HOSTNAME: self.resolver.setPaused(False)
    else: return # skip following redraw
    self.redraw()
  
  def redraw(self):
    if self.win:
      if not self.lock.acquire(False): return
      self.connectionsLock.acquire()
      try:
        # hostnames frequently get updated so frequent sorting needed
        if self.listingType == LIST_HOSTNAME: self.sortConnections()
        
        self.clear()
        if self.showLabel:
          # notes the number of connections for each type if above zero
          countLabel = ""
          for i in range(len(self.connectionCount)):
            if self.connectionCount[i] > 0: countLabel += "%i %s, " % (self.connectionCount[i], CONN_COUNT_LABELS[i])
          if countLabel: countLabel = " (%s)" % countLabel[:-2] # strips ending ", " and encases in parentheses
          self.addstr(0, 0, "Connections%s:" % countLabel, util.LABEL_ATTR)
        
        if self.connections:
          listingHeight = self.maxY - 1
          currentTime = time.time() if not self.isPaused else self.pauseTime
          
          if self.showingDetails:
            listingHeight -= 8
            isScrollBarVisible = len(self.connections) > self.maxY - 9
          else:
            isScrollBarVisible = len(self.connections) > self.maxY - 1
          xOffset = 3 if isScrollBarVisible else 0 # content offset for scroll bar
          
          # ensure cursor location and scroll top are within bounds
          self.cursorLoc = max(min(self.cursorLoc, len(self.connections) - 1), 0)
          self.scroll = max(min(self.scroll, len(self.connections) - listingHeight), 0)
          
          if self.isCursorEnabled:
            # update cursorLoc with selection (or vice versa if selection not found)
            if self.cursorSelection not in self.connections:
              self.cursorSelection = self.connections[self.cursorLoc]
            else: self.cursorLoc = self.connections.index(self.cursorSelection)
            
            # shift scroll if necessary for cursor to be visible
            if self.cursorLoc < self.scroll: self.scroll = self.cursorLoc
            elif self.cursorLoc - listingHeight + 1 > self.scroll: self.scroll = self.cursorLoc - listingHeight + 1
          
          lineNum = (-1 * self.scroll) + 1
          for entry in self.connections:
            if lineNum >= 1:
              type = entry[CONN_TYPE]
              color = TYPE_COLORS[type]
              
              # adjustments to measurements for 'xOffset' are to account for scroll bar
              if self.listingType == LIST_IP:
                # base data requires 73 characters
                src = "%s:%s" % (entry[CONN_L_IP], entry[CONN_L_PORT])
                dst = "%s:%s %s" % (entry[CONN_F_IP], entry[CONN_F_PORT], "" if type == "control" else "(%s)" % entry[CONN_COUNTRY])
                src, dst = "%-21s" % src, "%-26s" % dst
                
                etc = ""
                if self.maxX > 115 + xOffset:
                  # show fingerprint (column width: 42 characters)
                  etc += "%-40s  " % self.getFingerprint(entry[CONN_F_IP], entry[CONN_F_PORT])
                  
                if self.maxX > 127 + xOffset:
                  # show nickname (column width: remainder)
                  nickname = self.getNickname(entry[CONN_F_IP], entry[CONN_F_PORT])
                  nicknameSpace = self.maxX - 118 - xOffset
                  
                  # truncates if too long
                  if len(nickname) > nicknameSpace: nickname = "%s..." % nickname[:nicknameSpace - 3]
                  
                  etc += ("%%-%is  " % nicknameSpace) % nickname
              elif self.listingType == LIST_HOSTNAME:
                # base data requires 80 characters
                src = "localhost:%-5s" % entry[CONN_L_PORT]
                
                # space available for foreign hostname (stretched to claim any free space)
                foreignHostnameSpace = self.maxX - len(self.nickname) - 38
                
                etc = ""
                if self.maxX > 102 + xOffset:
                  # shows ip/locale (column width: 22 characters)
                  foreignHostnameSpace -= 22
                  etc += "%-20s  " % ("%s %s" % (entry[CONN_F_IP], "" if type == "control" else "(%s)" % entry[CONN_COUNTRY]))
                
                if self.maxX > 134 + xOffset:
                  # show fingerprint (column width: 42 characters)
                  foreignHostnameSpace -= 42
                  etc += "%-40s  " % self.getFingerprint(entry[CONN_F_IP], entry[CONN_F_PORT])
                
                if self.maxX > 151 + xOffset:
                  # show nickname (column width: min 17 characters, uses half of the remainder)
                  nickname = self.getNickname(entry[CONN_F_IP], entry[CONN_F_PORT])
                  nicknameSpace = 15 + (self.maxX - 151) / 2
                  foreignHostnameSpace -= (nicknameSpace + 2)
                  
                  if len(nickname) > nicknameSpace: nickname = "%s..." % nickname[:nicknameSpace - 3]
                  etc += ("%%-%is  " % nicknameSpace) % nickname
                
                hostname = self.resolver.resolve(entry[CONN_F_IP])
                
                # truncates long hostnames
                portDigits = len(str(entry[CONN_F_PORT]))
                if hostname and (len(hostname) + portDigits) > foreignHostnameSpace - 1:
                  hostname = hostname[:(foreignHostnameSpace - portDigits - 4)] + "..."
                
                dst = "%s:%s" % (hostname if hostname else entry[CONN_F_IP], entry[CONN_F_PORT])
                dst = ("%%-%is" % foreignHostnameSpace) % dst
              elif self.listingType == LIST_FINGERPRINT:
                # base data requires 75 characters
                src = "localhost"
                if entry[CONN_TYPE] == "control": dst = "localhost"
                else: dst = self.getFingerprint(entry[CONN_F_IP], entry[CONN_F_PORT])
                dst = "%-40s" % dst
                
                etc = ""
                if self.maxX > 92 + xOffset:
                  # show nickname (column width: min 17 characters, uses remainder if extra room's available)
                  nickname = self.getNickname(entry[CONN_F_IP], entry[CONN_F_PORT])
                  nicknameSpace = self.maxX - 78 - xOffset if self.maxX < 126 else self.maxX - 106 - xOffset
                  if len(nickname) > nicknameSpace: nickname = "%s..." % nickname[:nicknameSpace - 3]
                  etc += ("%%-%is  " % nicknameSpace) % nickname
                
                if self.maxX > 125 + xOffset:
                  # shows ip/port/locale (column width: 28 characters)
                  etc += "%-26s  " % ("%s:%s %s" % (entry[CONN_F_IP], entry[CONN_F_PORT], "" if type == "control" else "(%s)" % entry[CONN_COUNTRY]))
              else:
                # base data uses whatever extra room's available (using minimun of 50 characters)
                src = self.nickname
                if entry[CONN_TYPE] == "control": dst = self.nickname
                else: dst = self.getNickname(entry[CONN_F_IP], entry[CONN_F_PORT])
                
                # space available for foreign nickname
                foreignNicknameSpace = self.maxX - len(self.nickname) - 27
                
                etc = ""
                if self.maxX > 92 + xOffset:
                  # show fingerprint (column width: 42 characters)
                  foreignNicknameSpace -= 42
                  etc += "%-40s  " % self.getFingerprint(entry[CONN_F_IP], entry[CONN_F_PORT])
                
                if self.maxX > 120 + xOffset:
                  # shows ip/port/locale (column width: 28 characters)
                  foreignNicknameSpace -= 28
                  etc += "%-26s  " % ("%s:%s %s" % (entry[CONN_F_IP], entry[CONN_F_PORT], "" if type == "control" else "(%s)" % entry[CONN_COUNTRY]))
                
                dst = ("%%-%is" % foreignNicknameSpace) % dst
              if type == "inbound": src, dst = dst, src
              lineEntry = "<%s>%s  -->  %s  %s%5s (<b>%s</b>)%s</%s>" % (color, src, dst, etc, util.getTimeLabel(currentTime - entry[CONN_TIME], 1), type.upper(), " " * (9 - len(type)), color)
              if self.isCursorEnabled and entry == self.cursorSelection:
                lineEntry = "<h>%s</h>" % lineEntry
              
              yOffset = 0 if not self.showingDetails else 8
              self.addfstr(lineNum + yOffset, xOffset, lineEntry)
            lineNum += 1
          
          if isScrollBarVisible:
            topY = 9 if self.showingDetails else 1
            bottomEntry = self.scroll + self.maxY - 9 if self.showingDetails else self.scroll + self.maxY - 1
            util.drawScrollBar(self, topY, self.maxY - 1, self.scroll, bottomEntry, len(self.connections))
        
        self.refresh()
      finally:
        self.lock.release()
        self.connectionsLock.release()
  
  def getFingerprint(self, ipAddr, port):
    """
    Makes an effort to match connection to fingerprint - if there's multiple
    potential matches or the IP address isn't found in the discriptor then
    returns "UNKNOWN".
    """
    
    # checks to see if this matches the localhost entry
    if self.localhostEntry and ipAddr == self.localhostEntry[0][CONN_L_IP] and port == self.localhostEntry[0][CONN_L_PORT]:
      return self.localhostEntry[1]
    
    port = int(port)
    if (ipAddr, port) in self.fingerprintLookupCache:
      return self.fingerprintLookupCache[(ipAddr, port)]
    else:
      match = None
      
      # orconn-status provides a listing of Tor's current connections - used to
      # eliminated ambiguity for outbound connections
      if not self.orconnStatusCacheValid:
        self.orconnStatusCache, isOdd = [], True
        self.orconnStatusCacheValid = True
        try:
          for entry in self.conn.get_info("orconn-status")["orconn-status"].split():
            if isOdd: self.orconnStatusCache.append(entry)
            isOdd = not isOdd
        except (TorCtl.TorCtlClosed, TorCtl.ErrorReply): self.orconnStatusCache = None
      
      if ipAddr in self.fingerprintMappings.keys():
        potentialMatches = self.fingerprintMappings[ipAddr]
        
        if len(potentialMatches) == 1: match = potentialMatches[0][1]
        else:
          # multiple potential matches - look for exact match with port
          for (entryPort, entryFingerprint, entryNickname) in potentialMatches:
            if entryPort == port:
              match = entryFingerprint
              break
        
        if not match:
          # still haven't found it - use trick from Mike's ConsensusTracker,
          # excluding possiblities that have...
          # ... lost their Running flag
          # ... list a bandwidth of 0
          # ... have 'opt hibernating' set
          operativeMatches = list(potentialMatches)
          for entryPort, entryFingerprint, entryNickname in potentialMatches:
            # gets router description to see if 'down' is set
            toRemove = False
            try:
              nsData = self.conn.get_network_status("id/%s" % entryFingerprint)
              if len(nsData) != 1: raise TorCtl.ErrorReply() # ns lookup failed... weird
              else: nsEntry = nsData[0]
              
              descLookupCmd = "desc/id/%s" % entryFingerprint
              descEntry = TorCtl.Router.build_from_desc(self.conn.get_info(descLookupCmd)[descLookupCmd].split("\n"), nsEntry)
              toRemove = descEntry.down
            except TorCtl.ErrorReply: pass # ns or desc lookup fails... also weird
            
            # eliminates connections not reported by orconn-status -
            # this has *very* little impact since few ips have multiple relays
            if self.orconnStatusCache and not toRemove: toRemove = entryNickname not in self.orconnStatusCache
            
            if toRemove: operativeMatches.remove((entryPort, entryFingerprint, entryNickname))
          
          if len(operativeMatches) == 1: match = operativeMatches[0][1]
      
      if not match: match = "UNKNOWN"
      
      self.fingerprintLookupCache[(ipAddr, port)] = match
      return match
  
  def getNickname(self, ipAddr, port):
    """
    Attempts to provide the nickname for an ip/port combination, "UNKNOWN"
    if this can't be determined.
    """
    
    if (ipAddr, port) in self.nicknameLookupCache:
      return self.nicknameLookupCache[(ipAddr, port)]
    else:
      match = self.getFingerprint(ipAddr, port)
      
      try:
        if match != "UNKNOWN": match = self.conn.get_network_status("id/%s" % match)[0].nickname
      except TorCtl.ErrorReply: return "UNKNOWN" # don't cache result
      
      self.nicknameLookupCache[(ipAddr, port)] = match
      return match
  
  def setPaused(self, isPause):
    """
    If true, prevents connection listing from being updated.
    """
    
    if isPause == self.isPaused: return
    
    self.isPaused = isPause
    if isPause:
      self.pauseTime = time.time()
      self.connectionsBuffer = list(self.connections)
    else: self.connections = list(self.connectionsBuffer)
  
  def sortConnections(self):
    """
    Sorts connections according to currently set ordering. This takes into
    account secondary and tertiary sub-keys in case of ties.
    """
    
    # Current implementation is very inefficient, but since connection lists
    # are decently small (count get up to arounk 1k) this shouldn't be a big
    # whoop. Suggestions for improvements are welcome!
    
    sorts = []
    
    # wrapper function for using current listed data (for 'LISTING' sorts)
    if self.listingType == LIST_IP:
      listingWrapper = lambda ip, port: _ipToInt(ip)
    elif self.listingType == LIST_HOSTNAME:
      # alphanumeric hostnames followed by unresolved IP addresses
      listingWrapper = lambda ip, port: self.resolver.resolve(ip).upper() if self.resolver.resolve(ip) else "zzzzz%099i" % _ipToInt(ip)
    elif self.listingType == LIST_FINGERPRINT:
      # alphanumeric fingerprints followed by UNKNOWN entries
      listingWrapper = lambda ip, port: self.getFingerprint(ip, port) if self.getFingerprint(ip, port) != "UNKNOWN" else "zzzzz%099i" % _ipToInt(ip)
    elif self.listingType == LIST_NICKNAME:
      # alphanumeric nicknames followed by Unnamed then UNKNOWN entries
      listingWrapper = lambda ip, port: self.getNickname(ip, port) if self.getNickname(ip, port) not in ("UNKNOWN", "Unnamed") else "zzzzz%i%099i" % (0 if self.getNickname(ip, port) == "Unnamed" else 1, _ipToInt(ip))
    
    for entry in self.sortOrdering:
      if entry == ORD_FOREIGN_LISTING:
        sorts.append(lambda x, y: cmp(listingWrapper(x[CONN_F_IP], x[CONN_F_PORT]), listingWrapper(y[CONN_F_IP], y[CONN_F_PORT])))
      elif entry == ORD_SRC_LISTING:
        sorts.append(lambda x, y: cmp(listingWrapper(x[CONN_F_IP] if x[CONN_TYPE] == "inbound" else x[CONN_L_IP], x[CONN_F_PORT]), listingWrapper(y[CONN_F_IP] if y[CONN_TYPE] == "inbound" else y[CONN_L_IP], y[CONN_F_PORT])))
      elif entry == ORD_DST_LISTING:
        sorts.append(lambda x, y: cmp(listingWrapper(x[CONN_L_IP] if x[CONN_TYPE] == "inbound" else x[CONN_F_IP], x[CONN_F_PORT]), listingWrapper(y[CONN_L_IP] if y[CONN_TYPE] == "inbound" else y[CONN_F_IP], y[CONN_F_PORT])))
      else: sorts.append(SORT_TYPES[entry][2])
    
    self.connectionsLock.acquire()
    try: self.connections.sort(lambda x, y: _multisort(x, y, sorts))
    finally: self.connectionsLock.release()

# recursively checks primary, secondary, and tertiary sorting parameter in ties
def _multisort(conn1, conn2, sorts):
  comp = sorts[0](conn1, conn2)
  if comp or len(sorts) == 1: return comp
  else: return _multisort(conn1, conn2, sorts[1:])

# provides comparison int for sorting IP addresses
def _ipToInt(ipAddr):
  total = 0
  for comp in ipAddr.split("."):
    total *= 255
    total += int(comp)
  return total

# uses consensus data to map IP addresses to port / fingerprint combinations
def _getFingerprintMappings(conn, nsList = None):
  ipToFingerprint = {}
  
  if not nsList:
    try: nsList = conn.get_network_status()
    except (TorCtl.TorCtlClosed, TorCtl.ErrorReply): nsList = []
    except TypeError: nsList = [] # TODO: temporary workaround for a TorCtl bug, remove when fixed
  
  for entry in nsList:
    if entry.ip in ipToFingerprint.keys(): ipToFingerprint[entry.ip].append((entry.orport, entry.idhex, entry.nickname))
    else: ipToFingerprint[entry.ip] = [(entry.orport, entry.idhex, entry.nickname)]
  
  return ipToFingerprint

# provides client relays we're currently attached to (first hops in circuits)
# this consists of the nicknames and ${fingerprint} if unnamed
def _getClientConnections(conn):
  clients = []
  
  try:
    for line in conn.get_info("circuit-status")["circuit-status"].split("\n"):
      components = line.split()
      if len(components) > 3: clients += [components[2].split(",")[0]]
  except (TorCtl.ErrorReply, TorCtl.TorCtlClosed, socket.error): pass
  
  return clients

