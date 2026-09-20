#!/usr/bin/env python3
"""Loopback-only, in-memory dashboard for the Mac camera customer demo."""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = '127.0.0.1'
PORT = 8765
STALE_SECONDS = 2.5
STABLE_SECONDS = 0.6


class DemoState:
    def __init__(self):
        self.latest = None
        self.received_at = None
        self.stable = None
        self.candidate = None
        self.candidate_at = None
        self.events = deque(maxlen=10)

    def update(self, data, now=None):
        now = time.monotonic() if now is None else now
        count = data['people_count']
        if type(count) is not int or count < 0:
            raise ValueError('people_count must be a nonnegative integer')
        tracked = data.get('tracked_count')
        fps = data.get('fps')
        if tracked is not None and (type(tracked) is not int or tracked < 0):
            raise ValueError('tracked_count must be a nonnegative integer')
        if fps is not None and (type(fps) not in (int, float) or not math.isfinite(fps) or fps < 0):
            raise ValueError('fps must be a nonnegative finite number')
        if data.get('camera_id') != 'classroom-demo':
            raise ValueError('unexpected camera_id')
        if data.get('camera_status') != 'live' or data.get('ai_status') != 'active':
            raise ValueError('unexpected source status')
        if not isinstance(data.get('timestamp'), str):
            raise ValueError('timestamp required')
        if self.received_at is not None and now - self.received_at >= STALE_SECONDS:
            self.stable = None
            self.candidate = None
            self.candidate_at = None
        self.latest = {key: data.get(key) for key in ('camera_id', 'camera_name', 'camera_status', 'ai_status', 'people_count', 'tracked_count', 'fps', 'timestamp')}
        self.received_at = now
        if self.stable is None:
            self.stable = count
        elif count == self.stable:
            self.candidate = None
            self.candidate_at = None
        elif count != self.candidate:
            self.candidate = count
            self.candidate_at = now
        elif now - self.candidate_at >= STABLE_SECONDS:
            change = count - self.stable
            label = ('Person entered' if change == 1 else f'{change} people entered') if change > 0 else ('Person left' if change == -1 else f'{-change} people left')
            self.events.appendleft({'title': label, 'occupancy': count, 'timestamp': data['timestamp']})
            self.stable = count
            self.candidate = None
            self.candidate_at = None

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        live = self.received_at is not None and now - self.received_at < STALE_SECONDS
        return {'live': live, 'telemetry': self.latest if live else None,
                'occupancy': self.stable if live else None, 'events': list(self.events)}


STATE = DemoState()
STATE_LOCK = threading.Lock()
PAGE = Path(__file__).with_name('demo_dashboard.html')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            body = PAGE.read_bytes()
            content_type = 'text/html; charset=utf-8'
        elif self.path == '/state':
            with STATE_LOCK:
                body = json.dumps(STATE.snapshot()).encode()
            content_type = 'application/json'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/telemetry':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 4096:
                raise ValueError('invalid body length')
            data = json.loads(self.rfile.read(length))
            with STATE_LOCK:
                STATE.update(data)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))
            return
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()


def main():
    with ThreadingHTTPServer((HOST, PORT), Handler) as server:
        print(f'VeoTrex live room monitor: http://{HOST}:{PORT}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
