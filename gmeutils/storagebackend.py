#License GPL v3
#Author Horst Knorr <gpgmailencrypt@gmx.de>
from gmeutils.child 			import _gmechild 
from gmeutils.version			import *
from gmeutils._dbg 				import _dbg
import os.path

__all__ =["get_backend","get_backendlist"]

##############
#_base_storage
##############

class _base_storage(_gmechild):

	def __init__(self,parent,backend):
		_gmechild.__init__(self,parent=parent,filename=__file__)
		self._backend=backend
		self.init()
	
	#####
	#init
	#####
 
	@_dbg
	def init(self):
		pass

	################
	#read_configfile
	################
 
	@_dbg
	def read_configfile(self,cfg):
		raise NotImplementedError

	########
	#usermap
	########
 
	@_dbg
	def usermap(self, user):
		raise NotImplementedError

	##############
	#encryptionmap
	##############
 
	@_dbg
	def encryptionmap(self, user):
		raise NotImplementedError

##############
#_TEXT_BACKEND
##############

class _TEXT_BACKEND(_base_storage):

	#####
	#init
	#####
 
	@_dbg
	def init(self):
		self._addressmap = dict()
		self._encryptionmap = dict()

	################
	#read_configfile
	################
 
	@_dbg
	def read_configfile(self,cfg):

		if cfg.has_section('usermap'):

			for (name, value) in cfg.items('usermap'):
					self._addressmap[name] = value

		if cfg.has_section('encryptionmap'):

			for (name, value) in cfg.items('encryptionmap'):
					self._encryptionmap[name] = value.split(":")

	########
	#usermap
	########
 
	@_dbg
	def usermap(self, user):

		try:
			to_addr=self._addressmap[user]
		except:
			raise KeyError(user)

		self.debug("textbackend usermap %s=>%s"%(user,to_addr))
		return to_addr

	##############
	#encryptionmap
	##############
 
	@_dbg
	def encryptionmap(self, user):

		try:
			self.debug("get_preferred encryptionmap %s"%user)
			encryption=self._encryptionmap[user]
		except:
			raise KeyError(user)

		self.debug("textbackend encryptionmap %s=>%s"%(user,encryption))
		return encryption

#############
#_sql_backend
#############

class _sql_backend(_base_storage):

	#####
	#init
	#####
 
	@_dbg
	def init(self):
		self._DATABASE="gpgmailencrypt"
		self._USERMAPSQL="SELECT to_user FROM usermap WHERE user= ?"
		self._ENCRYPTIONMAPSQL="SELECT encrypt FROM encryptionmap WHERE user= ?"
		self._USER="gpgmailencrypt"
		self._PASSWORD=""
		self._HOST="127.0.0.1"
		self._PORT=4711
		self._db=None
		self._cursor=None
		self.placeholder="?"

	########
	#connect
	########

	def connect(self):
		raise NotImplementedError
	
	################
	#read_configfile
	################
 
	@_dbg
	def read_configfile(self,cfg):


		if cfg.has_section('sql'):

			try:
				self._DATABASE=os.path.expanduser(cfg.get('sql','database'))
			except:
				pass

			try:
				self._USERMAPSQL=cfg.get('sql','usermapsql')
			except:
				pass

			try:
				self._ENCRYPTIONMAPSQL=cfg.get('sql','encryptionmapsql')
			except:
				pass

			try:
				self._USER=cfg.get('sql','user')
			except:
				pass

			try:
				self._PASSWORD=cfg.get('sql','password')
			except:
				pass

			try:
				self._HOST=cfg.get('sql','host')
			except:
				pass

			try:
				self._PORT=cfg.getint('sql','port')
			except:
				pass

		self.connect()

	########
	#usermap
	########
 
	@_dbg
	def usermap(self, user):

		if self._cursor== None:
			raise KeyError(user)
			
		try:
			self._cursor.execute(self._USERMAPSQL.replace("?",
														self.placeholder),
														(user,))
		except:
			self.log_traceback()
			raise
			
		r=self._cursor.fetchone()

		if r==None:
			raise KeyError(user)
		
		self.debug("sqlbackend %s usermap %s=>%s"%(self._backend,user,r[0]))
		return r[0]

	##############
	#encryptionmap
	##############
 
	@_dbg
	def encryptionmap(self, user):

		if self._cursor== None:
			raise KeyError(user)
			
		try:
			self._cursor.execute(self._ENCRYPTIONMAPSQL.replace("?",
														self.placeholder),
														(user,))
		except:
			self.log_traceback()
			raise
			
		r=self._cursor.fetchone()

		if r==None:
			raise KeyError(user)

		self.debug("sqlbackend %s encryptionmap %s=>%s"%(self._backend,
														user,
														r[0]))
		return r[0].split(":")

		
#################
#_SQLITE3_BACKEND
#################

class _SQLITE3_BACKEND(_sql_backend):

	########
	#connect
	########

	def connect(self):
		result=False
		try:
			import sqlite3
		except:
			self.log("SQLITE driver not found","e")
			self.log_traceback()
			return result
			
		if os.path.exists(self._DATABASE):
			self._db=sqlite3.connect(self._DATABASE)
			self._cursor=self._db.cursor()
			result=True
		else:
			self.log("Database '%s' does not exist"%self._DATABASE,"e")
		
		return result

###############
#_MYSQL_BACKEND
###############

class _MYSQL_BACKEND(_sql_backend):


	#####
	#init
	#####
 
	@_dbg
	def init(self):
		_sql_backend.init(self)
		self._PORT=3306
		self.placeholder="%s"
		
	########
	#connect
	########

	def connect(self):
		result=False
		try:
			import mysql.connector as mysql
			from mysql.connector import errorcode

		except:
			self.log("MYSQL (mysql.connector) driver not found","e")
			self.log_traceback()
			return result
			
		try:
			self._db=mysql.connect(	database=self._DATABASE,
									user=self._USER,
									password=self._PASSWORD,
									host=self._HOST,
									port=self._PORT)
			self._cursor=self._db.cursor()
			result=True
		except mysql.Error as err:

			if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
				self.log(	"Could not connect to database, "
							"wrong username and/or password"
							,"e")
			elif err.errno == errorcode.ER_BAD_DB_ERROR:
				self.log("database %s does not exist"%self._DATABASE,"e")

			self.log_traceback()

		return result

################################################################################

################
#get_backendlist
################

def get_backendlist():
	return ["MYSQL","SQLITE3","TEXT"]

############
#get_backend
############

def get_backend(backend,parent):
		backend=backend.upper().strip()

		if backend=="SQLITE3":

			try:
				return _SQLITE3_BACKEND(parent=parent,backend="SQLITE")
			except:
				parent.log("Storage backend %s could not be loaded"%backend,"e")
				
		if backend=="MYSQL":

			try:
				return _MYSQL_BACKEND(parent=parent,backend="MYSQL")
			except:
				parent.log("Storage backend %s could not be loaded"%backend,"e")
				
		else:
			# default backend=="TEXT":
			return _TEXT_BACKEND(parent=parent,backend="TEXT")

