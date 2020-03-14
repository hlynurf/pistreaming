#!/usr/bin/env python

import base64
import datetime
import sys
import io
import json
import os
import shutil
import sqlite3

from subprocess import Popen, PIPE
from string import Template
from struct import Struct
from threading import Thread, Timer
from time import sleep, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from wsgiref.simple_server import make_server
import urllib.parse

import picamera
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import (
    WSGIServer,
    WebSocketWSGIHandler,
    WebSocketWSGIRequestHandler,
)
from ws4py.server.wsgiutils import WebSocketWSGIApplication

###########################################
# CONFIGURATION
WIDTH = 640
HEIGHT = 480
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = 8084
COLOR = u'#444'
BGCOLOR = u'#333'
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct('>4sHH')
VFLIP = False
HFLIP = False
TEMPDB_FILE = 'tempfile.db'
###########################################


class StreamingHttpHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_AUTHHEAD(self):
        self.send_response(401)
        self.send_header(
            'WWW-Authenticate', 'Basic realm="Access to horses"')
        self.send_header('Content-type', 'application/json')
        self.end_headers()

    def auth(self):
        key = self.server.get_auth_key()
        if self.headers.get('Authorization') == None:
            self.do_AUTHHEAD()

            response = {
                'success': False,
                'error': 'No auth header received'
            }

            self.wfile.write(bytes(json.dumps(response), 'utf-8'))
        elif self.headers.get('Authorization') == 'Basic ' + str(key):
            return True
        else:
            self.do_AUTHHEAD()

            response = {
                'success': False,
                'error': 'Invalid credentials'
            }

            self.wfile.write(bytes(json.dumps(response), 'utf-8'))
        
        return False

    def do_GET(self):
        if not self.auth():
            return

        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        elif self.path == '/jsmpg.js':
            content_type = 'application/javascript'
            content = self.server.jsmpg_content
        elif self.path == '/temparature':
            content_type = 'application/json'
            content = json.dumps({'temparature': get_temp()})
        elif self.path == '/history':
            query = urllib.parse.urlparse(self.path).query
            if not query:
                interval_raw = '6'
            else:
                query_components = dict(qc.split("=") for qc in query.split("&"))
                interval_raw = query_components.get('interval', None)

            if interval_raw is None or not interval_raw.isdigit():
                self.send_error(400, 'Interval incorrect')
                return
            interval = int(interval_raw)
            rows = get_temp_history(interval)

            content_type = 'text/html; charset=utf-8'
            temparature = get_temp()
            tpl = Template(self.server.history_template)
            content = tpl.safe_substitute(dict(
                data=rows,
                is24=(interval == 24),
                is6=(interval == 6),
                is12=(interval == 12),
                is168=(interval == 168),
                is720=(interval == 72),
            ))
        elif self.path == '/index.html':
            content_type = 'text/html; charset=utf-8'
            temparature = get_temp()
            tpl = Template(self.server.index_template)
            content = tpl.safe_substitute(dict(WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR,
                BGCOLOR=BGCOLOR, TEMPARATURE=temparature))
        else:
            self.send_error(404, 'File not found')
            return
        content = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Last-Modified', self.date_time_string(time()))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(content)


def get_temp():
	temp_sensor = '/sys/bus/w1/devices/28-011452c4fbaa/w1_slave'
	with open(temp_sensor, 'r') as file:
		lines = file.readlines()
		equals_pos = lines[1].find('t=')
		if equals_pos != -1:
			temp_string = lines[1][equals_pos + 2:]
			temp_c = float(temp_string) / 1000.0
			return temp_c
		return 100.0


def ensure_db_exists():
    if os.path.exists(TEMPDB_FILE):
        return
    
    print('Creating DB...')
    conn = sqlite3.connect(TEMPDB_FILE)
    c = conn.cursor()
    c.execute('CREATE TABLE temps (timestamp DATETIME, temp NUMERIC)')
    conn.close()
    print('DB created')


def write_temp_to_db():
    ensure_db_exists()
    conn = sqlite3.connect(TEMPDB_FILE)
    c = conn.cursor()
    temp = get_temp()
    c.execute('INSERT INTO temps VALUES (datetime("now"), ?)', (temp,))
    conn.commit()
    conn.close()
    Timer(30, write_temp_to_db).start()


def get_temp_history(interval: int = 24):
    accepted_intervals = [6, 12, 24, 168, 720]
    if interval not in accepted_intervals:
        return []

    ensure_db_exists()
    conn = sqlite3.connect(TEMPDB_FILE)
    c = conn.cursor()
    date_back = datetime.datetime.now() - datetime.timedelta(hours=interval)
    c.execute(f'SELECT timestamp, temp FROM temps WHERE timestamp >= datetime("now", "-{interval} hours")')
    rows = c.fetchall()
    return rows


class StreamingHttpServer(HTTPServer):
    def __init__(self):
        super(StreamingHttpServer, self).__init__(
                ('', HTTP_PORT), StreamingHttpHandler)
        with io.open('index.html', 'r') as f:
            self.index_template = f.read()
        with io.open('history.html', 'r') as f:
            self.history_template = f.read
        with io.open('jsmpg.js', 'r') as f:
            self.jsmpg_content = f.read()

    def set_auth(self, username, password):
        self.key = base64.b64encode(
            bytes('%s:%s' % (username, password), 'utf-8')).decode('ascii')

    def get_auth_key(self):
        return self.key


class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        self.converter = Popen([
            'ffmpeg',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % camera.resolution,
            '-r', str(float(camera.framerate)),
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', str(float(camera.framerate)),
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read1(32768)
                if buf:
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()


def main():
    print('Initializing camera')
    with picamera.PiCamera() as camera:
        camera.resolution = (WIDTH, HEIGHT)
        camera.framerate = FRAMERATE
        camera.vflip = VFLIP # flips image rightside up, as needed
        camera.hflip = HFLIP # flips image left-right, as needed
        sleep(1) # camera warm-up time
        print('Initializing websockets server on port %d' % WS_PORT)
        WebSocketWSGIHandler.http_version = '1.1'
        websocket_server = make_server(
            '', WS_PORT,
            server_class=WSGIServer,
            handler_class=WebSocketWSGIRequestHandler,
            app=WebSocketWSGIApplication(handler_cls=StreamingWebSocket))
        websocket_server.initialize_websockets_manager()
        websocket_thread = Thread(target=websocket_server.serve_forever)
        print('Initializing HTTP server on port %d' % HTTP_PORT)
        http_server = StreamingHttpServer()
        http_server.set_auth('jon', os.environ['AUTH_PASS'])
        http_thread = Thread(target=http_server.serve_forever)
        print('Initializing broadcast thread')
        output = BroadcastOutput(camera)
        broadcast_thread = BroadcastThread(output.converter, websocket_server)
        print('Starting recording')
        camera.start_recording(output, 'yuv')
        try:
            print('Starting websockets thread')
            websocket_thread.start()
            print('Starting HTTP server thread')
            http_thread.start()
            print('Starting broadcast thread')
            broadcast_thread.start()
            write_temp_to_db()
            while True:
                camera.wait_recording(1)
        except KeyboardInterrupt:
            pass
        finally:
            print('Stopping recording')
            camera.stop_recording()
            print('Waiting for broadcast thread to finish')
            broadcast_thread.join()
            print('Shutting down HTTP server')
            http_server.shutdown()
            print('Shutting down websockets server')
            websocket_server.shutdown()
            print('Waiting for HTTP server thread to finish')
            http_thread.join()
            print('Waiting for websockets thread to finish')
            websocket_thread.join()


if __name__ == '__main__':
    main()
