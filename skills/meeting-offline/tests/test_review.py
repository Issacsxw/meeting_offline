"""The review page: started in the background and reused, stopped when idle, Obsidian suggestions in one click."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import memory
import minutes_html
import prepare_speakers
import review
import term_review
import workspace as w
from test_obsidian import fake_png

SCRIPT = Path(review.__file__).resolve()


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'pc'})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        transcript = self.base / 'm.json'
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 4000, 'text': '这周背联的上线节奏要定下来。'},
            {'speaker_id': 1, 'start_ms': 4000, 'end_ms': 8000, 'text': '王武那边下周给报价。'}]})
        w.write(self.base / 'state.json', prepare_speakers.prepare(['m=' + str(transcript)]))
        audio = self.base / 'm.m4a'; audio.write_bytes(b'audio')
        self.root = w.initialize(self.base / 'state.json', self.base / 'meetings' / 'm', {'m': audio})

    def tearDown(self):
        review.stop(self.root)
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--workspace', str(self.root), *args],
                              capture_output=True, text=True, timeout=60, env=os.environ)

    def test_the_page_runs_in_the_background_and_is_reused(self):
        first = self.cli('--background')
        self.assertEqual(first.returncode, 0, first.stderr)
        url = first.stdout.strip()
        self.assertRegex(url, r'^http://127\.0\.0\.1:\d+/#token=')
        self.assertEqual(self.cli('--background').stdout.strip(), url)  # the same page, not a second server
        origin, token = url.split('/#token=')
        request = urllib.request.Request(origin + '/api/state', headers={'X-Meeting-Token': token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            self.assertEqual([s['key'] for s in json.loads(response.read())['speakers']], ['m-A', 'm-B'])
        self.assertEqual(oct((self.root / review.INFO).stat().st_mode & 0o777), '0o600')  # holds the token
        self.assertIn('已关闭', self.cli('--stop').stdout)
        self.assertIsNone(review.running(self.root))

    def test_an_idle_page_stops_by_itself(self):
        thread = threading.Thread(target=review.serve, args=(self.root,), kwargs={'idle_minutes': 0.01, 'background': True})
        with contextlib.redirect_stdout(io.StringIO()):
            thread.start(); thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse((self.root / review.INFO).exists())

    def test_obsidian_suggestions_are_confirmed_in_one_click_on_the_page(self):
        term_review.prepare(self.root, [
            {'id': 'obsidian-1', 'source_id': 'm', 'title': '背联', 'scope': 'Obsidian 库「示例」', 'forms': ['背联'],
             'options': [{'text': '贝联', 'basis': 'Obsidian 笔记《贝联》'}], 'suggested': '贝联'},
            {'id': 'obsidian-2', 'source_id': 'm', 'title': '王武', 'scope': 'Obsidian 库「示例」', 'forms': ['王武'],
             'options': [{'text': '王五', 'basis': '人物笔记《王五》'}], 'suggested': '王五'}], 1)
        cards = w.public_state(self.root)['term_candidates']
        self.assertEqual([c['suggested'] for c in cards], ['贝联', '王五'])
        server = review.ReviewServer(self.root); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            body = json.dumps({'revision': w.read(self.root / 'state.json')['revision'],
                               'choices': [{'candidate_id': c['id'], 'decision': 'confirm', 'canonical': c['suggested']} for c in cards]}).encode()
            request = urllib.request.Request(server.origin + '/api/term-batch', data=body, method='POST',
                                             headers={'X-Meeting-Token': server.token, 'Content-Type': 'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()
        self.assertEqual(sorted(c['canonical'] for c in w.read(self.root / 'corrections.json')), ['王五', '贝联'])
        learned = [t for t in memory.read_store(device_context.paths()['memory'])['terms'] if t['canonical'] in ('贝联', '王五')]
        self.assertEqual(len(learned), 2)
        self.assertTrue(all('复核页面' in t['confirmation'] for t in learned))  # the record says where the user confirmed

    def test_the_page_puts_speakers_first_and_says_how_to_finish(self):
        page = (SCRIPT.parents[1] / 'assets' / 'review.html').read_text(encoding='utf-8')
        self.assertLess(page.index('id="role-review"'), page.index('id="terminology"'))
        self.assertIn('确认好了', page)


if __name__ == '__main__':
    unittest.main()
