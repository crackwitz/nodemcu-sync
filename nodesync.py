#!/usr/bin/env python2
import os
import sys
import time
import socket
import select
import tempfile
import atexit
import shutil
import json
import datetime

class LuaREPLConn(object):
	replsig = '> '

	def __init__(self, target):
		if isinstance(target, str):
			if ':' in target:
				target = list(target.split(":", 1))
				target[1] = int(target[1])
			else:
				target = (target, 2323) # https://github.com/nodemcu/nodemcu-firmware/blob/master/lua_examples/telnet.lua

		else:
			assert isinstance(target, (list, tuple))

		self.target = target
		self.conn = None
		self.connect()

	def connect(self):
		if self.conn is None:
			self.conn = socket.socket()
			#self.conn.settimeout(5.0)
			self.conn.connect(self.target)

			print "we are", self.conn.getsockname()

			self.expect(">\n")

	def expect(self, string):
		while string:
			block = self.conn.recv(1)

			if not block:
				break

			if not string.startswith(block):
				return False

			string = string[len(block):]

		return len(string) == 0

	def _push_request(self, command):
		if not command.endswith('\n'):
			command += '\r\n'
		#print timestamp(), "_push_request:", repr(command)
		self.conn.send(command)
		#print timestamp(), "_push_request done"

	def _pull_response(self):
		#(rl, wl, xl) = select.select([self.conn], [], [], timeout=0.1)
		#if self.conn not in rl: return None

		#print timestamp(), "_pull_response"
		result = ""
		while True:
			block = self.conn.recv(1)
			if not block: break
			result += block
			#print timestamp(), "_pull_response:", repr(result[:5]), len(result), repr(result[-5:])
			if result.endswith(self.replsig): # test for prompt
				result = result[:-len(self.replsig)]
				break

		#print timestamp(), "_pull_response done"
		return result

	def command(self, command):
		self._push_request(command)
		return self._pull_response()

	def list(self):
		response = self.command("for u,v in pairs(file.list()) do print(u,v) end")

		result = {}
		for line in response.rstrip().split('\n'):
			k,v = line.split('\t', 1)
			result[k] = int(v)

		return result

	def remove(self, fpath):
		self.command('local fname = {}; if file.exists(fname) then file.remove(fname) end'.format(json.dumps(fpath)))

	def download(self, fpath):
		contents = []

		datasock = socket.socket()
		datasock.bind(('', 0))
		#datasock.settimeout(10.0)
		datasock.listen(1)

		connectip = self.conn.getsockname()[0]
		connectport = datasock.getsockname()[1]

		#print timestamp(), "download to:", connectip, connectport

		self.command(
			'function nodesync_sendmore(sock) '+
				'local block = file.read(1024); '+
				'if block == nil then sock:close(); file.close(); return; end; '+
				'sock:send(block); ' +
			'end')
		self.command(
			'file.open({}, "r")'.format(json.dumps(fpath)))
		self.command(
			'nodesync_sock = net.createConnection(net.TCP, 0)')
		self.command(
			'nodesync_sock:connect({port}, "{ip}")'.format(ip=connectip, port=connectport))
		self.command(
			'nodesync_sock:on("sent", nodesync_sendmore)')
		self.command(
			'nodesync_sendmore(nodesync_sock)')

		(dataconn,remoteaddr) = datasock.accept()
		#print timestamp(), "download: connection accepted from", remoteaddr
		contents = ""
		while True:
			block = dataconn.recv(2**16)
			if not block: break
			contents += block
			#print timestamp(), "download: got", len(contents)

		dataconn.close()
		datasock.close()
		#print timestamp(), "download done ({})".format(len(contents))

		return contents

	def upload(self, fpath, contents):
		datasock = socket.socket()
		datasock.bind(('', 0))
		#datasock.settimeout(10.0)
		datasock.listen(1)

		connectip = self.conn.getsockname()[0]
		connectport = datasock.getsockname()[1]

		#print timestamp(), "download to:", connectip, connectport

		self.command(
			'file.open({}, "w")'.format(json.dumps(fpath)))
		self.command(
			'nodesync_sock = net.createConnection(net.TCP, 0)')
		self.command(
			'nodesync_sock:on("receive", function(sock,data) file.write(data) end)')
		self.command(
			'nodesync_sock:on("disconnection", function(sock) sock:close(); file.close(); end)')
		self.command(
			'nodesync_sock:connect({port}, "{ip}")'.format(ip=connectip, port=connectport))

		(dataconn,remoteaddr) = datasock.accept()

		while True:
			sent = dataconn.send(contents)
			contents = contents[sent:]
			if not contents: break

		dataconn.close()
		datasock.close()


last_timestamp = datetime.datetime.now()
def timestamp():
	global last_timestamp
	now = datetime.datetime.now()
	delta = (now - last_timestamp).total_seconds()
	last_timestamp = now

	return "{} ({:+.3f}s)".format(
		now.strftime("%Y-%m-%d %H:%M:%S"),
		delta)

host = sys.argv[1]

conn = LuaREPLConn(host)

tempdir = tempfile.mkdtemp(prefix='nodesync_')
pwd = os.getcwd()
os.chdir(tempdir)

@atexit.register
def exithandler():
	os.chdir(pwd)
	shutil.rmtree(tempdir)


files = {} # path -> mtime

for fpath, fsize in conn.list().iteritems():
	fdir = os.path.join('.', os.path.dirname(fpath))
	if not os.path.exists(fdir):
		print "creating", fdir
		os.path.makedirs(fdir)
	print timestamp(), "fetching {!r} ({} bytes)".format(fpath, fsize)
	contents = conn.download(fpath)
	if not (len(contents) == fsize):
		print "length mismatch, expected {}, got {}".format(fsize, len(contents))

	with open(fpath, 'wb') as fh:
		fh.write(contents)
	
	files[fpath] = os.path.getmtime(fpath)

print "files in:", tempdir
os.system('explorer "{}"'.format(tempdir))

print "monitoring..."
while True:
	# find new files to register
	for _path, _dirs, _files in os.walk(tempdir):
		for f in _files:
			f = os.path.relpath(os.path.join(_path, f), tempdir)
			if f not in files:
				files[f] = 0
				print timestamp(), "adding", f

	# check mtimes for all registered files
	for fpath in sorted(files):
		if not os.path.exists(fpath):
			print timestamp(), "removing", fpath
			del files[fpath]
			conn.remove(fpath)

		else:
			mtime = os.path.getmtime(fpath)
			if mtime != files[fpath]:
				print timestamp(), "uploading {!r}".format(fpath)
				conn.upload(fpath, open(fpath, 'rb').read())
				files[fpath] = mtime
				print timestamp(), "upload done"

	time.sleep(1.0)
