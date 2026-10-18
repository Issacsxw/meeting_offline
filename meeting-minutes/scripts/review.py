#!/usr/bin/env python3
"""Loopback-only review UI. Streams existing local audio; no cloud requests."""
from __future__ import annotations
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import urllib.parse
import workspace
import term_review


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, root, port=0):
        self.root = Path(root).expanduser().resolve()
        workspace.assert_workspace(self.root, workspace.read(self.root / 'state.json'))
        self.token = secrets.token_urlsafe(24)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.url = self.origin + '/#token=' + self.token


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True); parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args(); server = ReviewServer(args.workspace, args.port)
    print(server.url, flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__':
    main()
