#!/usr/bin/env python3
"""Loopback-only review UI. Streams existing local audio; no cloud requests.

  review.py --workspace W --background --open   start (or reuse) the page for W, open it, return at once
  review.py --workspace W --stop                stop that page
  review.py --workspace W                       run in this terminal until Ctrl-C
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import workspace
import term_review

INFO = '.review-server.json'  # in the workspace: how to reach its background page
IDLE_MINUTES = 120


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, root, port=0):
        self.root = Path(root).expanduser().resolve()
        workspace.assert_workspace(self.root, workspace.read(self.root / 'state.json'))
        self.token = secrets.token_urlsafe(24)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.url = self.origin + '/#token=' + self.token
        self.last_seen = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    server_version = 'MeetingReview'
    def log_message(self, *args):
        pass  # Never log tokens, full paths, quotes, or transcripts.

    def common(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')

    def json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') == urllib.parse.urlparse(self.server.origin).netloc

    def authorized(self):
        parsed = urllib.parse.urlparse(self.path)
        token = self.headers.get('X-Meeting-Token') or urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
        return self.valid_host() and hmac.compare_digest(token, self.server.token)

    def do_GET(self):
        self.server.last_seen = time.monotonic()
        path = urllib.parse.urlparse(self.path).path
        if not self.valid_host():
            return self.json(403, {'error': 'Invalid local host.'})
        if path == '/':
            body = (Path(__file__).resolve().parent.parent / 'assets/review.html').read_bytes()
            self.send_response(200); self.common()
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        if not self.authorized():
            return self.json(403, {'error': '需要从本机工具链接打开。'})
        try:
            if path == '/api/term-preview':
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self.json(200, workspace.term_preview(self.server.root, params.get('source', [''])[0], params.get('heard', [''])[0]))
            if path == '/api/state':
                return self.json(200, workspace.public_state(self.server.root))
            if path.startswith('/minutes/'):
                sid = path.removeprefix('/minutes/')
                latest = workspace.read(self.server.root / 'exports/latest.json')
                target = latest.get('files', {}).get(sid, {}).get('html')
                if not target:
                    return self.json(404, {'error': '该会议尚未形成可编辑纪要。'})
                data = Path(target).read_bytes()
                self.send_response(200); self.common()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src 'none'; connect-src 'none'; frame-ancestors 'none'; base-uri 'none'")
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path.startswith('/audio/'):
                sid = path.removeprefix('/audio/')
                state = workspace.read(self.server.root / 'state.json')
                source = next((s for s in state['sources'] if s['source_id'] == sid), None)
                if not source or not source.get('audio_path'):
                    return self.json(404, {'error': 'No local audio for this recording.'})
                return self.send_file(Path(source['audio_path']), audio=True)
            if path.startswith('/export/'):
                parts = path.split('/')
                if len(parts) != 4:
                    return self.json(404, {'error': 'Unknown export.'})
                latest = workspace.read(self.server.root / 'exports/latest.json')
                target = latest.get('files', {}).get(parts[2], {}).get(parts[3])
                if not target:
                    return self.json(404, {'error': 'Unknown export.'})
                return self.send_file(Path(target), download=True)
            return self.json(404, {'error': 'Not found.'})
        except (ValueError, OSError, KeyError):
            self.json(400, {'error': '本地资料暂不可用，请检查工作区。'})

    def do_POST(self):
        self.server.last_seen = time.monotonic()
        origin = self.headers.get('Origin')
        if not self.authorized() or (origin and origin != self.server.origin):
            return self.json(403, {'error': 'Invalid local request.'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 65536:
                return self.json(413, {'error': 'Request too large or empty.'})
            payload = json.loads(self.rfile.read(length))
            path = urllib.parse.urlparse(self.path).path
            if path == '/api/roles':
                result = workspace.save_roles(self.server.root, payload['mappings'], payload['revision'])
            elif path == '/api/term':
                result = workspace.add_term(self.server.root, payload['term'], payload['revision'])
            elif path == '/api/term-choice':
                result = term_review.save(self.server.root, payload['choice'], payload['revision'])
            elif path == '/api/term-batch':  # “全部按库里的写法改”
                result = term_review.confirm_batch(self.server.root, payload['choices'], payload['revision'], via='page')
            else:
                return self.json(404, {'error': 'Not found.'})
            return self.json(200, {'saved': True, 'exports': result})
        except (ValueError, KeyError) as exc:
            self.json(400, {'error': str(exc)})
        except Exception:
            self.json(500, {'error': '保存未完成，请重新打开工作区检查当前状态。'})

    def send_file(self, path, audio=False, download=False):
        size = path.stat().st_size; start = 0; end = size-1; status = 200
        header = self.headers.get('Range')
        if audio and header:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
            if not match or not any(match.groups()):
                self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
            a, b = match.groups()
            if a:
                start = int(a); end = min(int(b), size-1) if b else size-1
            else:
                start = max(0, size-int(b)); end = size-1
            if start > end or start >= size:
                self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return
            status = 206
        mime = 'audio/mp4' if audio and path.suffix.lower() == '.m4a' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self.send_response(status); self.common(); self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(end-start+1))
        if audio: self.send_header('Accept-Ranges', 'bytes')
        if status == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if download: self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(path.name))
        self.end_headers()
        with path.open('rb') as handle:
            handle.seek(start); remaining = end-start+1
            try:
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def info_path(root):
    return Path(root).expanduser().resolve() / INFO


def running(root):
    """The link of this workspace's background page while it still answers, else None."""
    try:
        info = json.loads(info_path(root).read_text(encoding='utf-8'))
        if not re.fullmatch(r'http://127\.0\.0\.1:\d+', info['origin']):  # the token never goes anywhere else
            return None
        request = urllib.request.Request(info['origin'] + '/api/state', headers={'X-Meeting-Token': info['token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return info['url'] if response.status == 200 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def serve(root, port=0, idle_minutes=None, background=False, open_browser=False):
    server = ReviewServer(root, port)
    path = info_path(server.root)
    if background:
        record = {'pid': os.getpid(), 'origin': server.origin, 'token': server.token, 'url': server.url,
                  'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        temp = path.with_name(path.name + '.tmp')
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token only for this user
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(record, handle)
        os.replace(temp, path)
    if idle_minutes:
        def watch():  # nobody has used the page for a while: stop, so no server is left behind
            while time.monotonic() - server.last_seen < idle_minutes * 60:
                time.sleep(min(30, idle_minutes * 60))
            server.shutdown()
        threading.Thread(target=watch, daemon=True).start()
    if not background:
        print(server.url, flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, [server.url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if background and json.loads(path.read_text(encoding='utf-8')).get('pid') == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass


def start_background(root, idle_minutes=IDLE_MINUTES):
    """Start the page in its own process and return its link at once; an open page is reused, not duplicated."""
    root = Path(root).expanduser().resolve()
    workspace.assert_workspace(root, workspace.read(root / 'state.json'))
    url = running(root)
    if url:
        return url, True
    info_path(root).unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), '--workspace', str(root), '--serve',
               '--idle-minutes', str(idle_minutes)]
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True  # keeps running after the AI tool's command returns
    with (root / '.review-server.log').open('ab') as log:
        child = subprocess.Popen(command, stderr=log, **options)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        url = running(root)
        if url:
            return url, False
        if child.poll() is not None:
            break
        time.sleep(0.2)
    raise RuntimeError('复核页面没能启动，详情见工作区里的 .review-server.log。')


def stop(root):
    # only a page that still answers with its token is ours to stop; a dead one's pid may belong to anything now
    alive = running(root)
    try:
        if alive:
            os.kill(int(json.loads(info_path(root).read_text(encoding='utf-8'))['pid']), signal.SIGTERM)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    info_path(root).unlink(missing_ok=True)
    return bool(alive)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workspace', required=True); parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--background', action='store_true', help='在后台运行并立即返回链接；已经开着就复用')
    parser.add_argument('--open', action='store_true', help='同时在浏览器里打开')
    parser.add_argument('--stop', action='store_true', help='关掉这个工作区的后台页面')
    parser.add_argument('--idle-minutes', type=float, default=None, help=f'无人使用多久后自动关闭（后台默认 {IDLE_MINUTES}）')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)  # the background process itself
    args = parser.parse_args()
    try:
        if args.stop:
            print('已关闭复核页面。' if stop(args.workspace) else '这个工作区没有开着的复核页面。')
        elif args.serve:
            serve(args.workspace, args.port, args.idle_minutes, background=True)
        elif args.background:
            url, reused = start_background(args.workspace, args.idle_minutes or IDLE_MINUTES)
            print(url, flush=True)
            if args.open:
                webbrowser.open(url)
        else:
            serve(args.workspace, args.port, args.idle_minutes, open_browser=args.open)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
