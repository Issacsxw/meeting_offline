"""Reconstructed workflow regressions using synthetic files and handler mocks only."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_minutes as a
import archive_meeting
import inbox
import minutes_html
import review
import workspace as w
import test_auto_minutes as fixtures
from test_obsidian import fake_png
from test_minutes_html import SeedParser


class WorkflowTests(unittest.TestCase):
    payload = fixtures.AutoMinutesTests.payload
    envelope = fixtures.AutoMinutesTests.envelope

    def setUp(self):
        fixtures.AutoMinutesTests.setUp(self)
        patch = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png)
        patch.start(); self.addCleanup(patch.stop)

    def seed(self, path):
        parser = SeedParser(); parser.feed(Path(path).read_text(encoding='utf-8'))
        return json.loads(parser.seed)

    def final(self, candidate=None):
        result = a.run(self.root, runner=fixtures.Runner(candidate))
        self.assertIsNotNone(result['finalization'])
        return w.export(self.root)

    def issue(self):
        return a.run(self.root, runner=fixtures.Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))

    def handler(self, path, payload=None):
        request = object.__new__(review.Handler)
        request.server = mock.Mock(root=self.root, origin='http://127.0.0.1:1000', token='fixture-token')
        request.path = path
        raw = json.dumps(payload).encode() if payload is not None else b''
        request.headers = {'Host': '127.0.0.1:1000', 'X-Meeting-Token': 'fixture-token',
                           'Origin': 'http://127.0.0.1:1000', 'Content-Length': str(len(raw))}
        request.rfile = io.BytesIO(raw); request.json = mock.Mock(); request.send_file = mock.Mock()
        return request

    def invalidate_auto(self):
        record = a.load(self.root, 'M'); record['revision'] += 1
        record.update(pipeline_state='awaiting_ai', content_state='draft', finalization=None)
        w.write(a.directory(self.root, 'M') / 'state.json', record)

    def test_auto_final_exports_files_without_confirming_candidate_identity(self):
        before = (self.root / 'analysis.json').read_bytes(); exports = self.final(); files = exports['files']['M']
        for kind in ('html', 'png', 'md', 'minutes', 'structured'): self.assertTrue(Path(files[kind]).is_file())
        seed = self.seed(files['html']); self.assertEqual('', seed['participants'])
        self.assertNotIn('候选甲', json.dumps(seed, ensure_ascii=False))
        self.assertEqual(fixtures.draft()['summary'], seed['summary'])
        structured = w.read(files['structured']); self.assertFalse(structured['roles_confirmed'])
        self.assertEqual('pass', structured['semantic_review'])
        self.assertTrue(all(not s['role_confirmed'] for s in w.read(self.root / 'state.json')['speakers']))
        self.assertEqual(before, (self.root / 'analysis.json').read_bytes())
        self.assertNotIn('候选甲', Path(files['transcript']).read_text())
        self.assertEqual(exports['version'], w.publish(self.root)['export_version'])

    def test_unconfirmed_owner_displays_stable_key_without_candidate_name(self):
        candidate = fixtures.draft(); candidate['actions'] = [{'task': '继续讨论方案', 'evidence': [0],
            'owner': 'M-A', 'owner_basis': 'explicit_assignment', 'deadline': None}]
        files = self.final(candidate)['files']['M']
        self.assertEqual('M-A', self.seed(files['html'])['todos'][0]['owner'])
        for kind in ('md', 'minutes'): self.assertNotIn('候选甲', Path(files[kind]).read_text())

    def test_changed_workspace_hides_old_exports_and_preserves_old_files(self):
        old = self.final(); path = Path(old['files']['M']['html']); before = path.read_bytes()
        state = w.read(self.root / 'state.json'); state['revision'] += 1; w.write(self.root / 'state.json', state)
        self.assertNotIn('html', w.public_state(self.root)['exports']['files']['M'])
        self.assertNotIn('html', w.current_exports(self.root)['files']['M'])
        with self.assertRaises(ValueError): w.publish(self.root, ['M'])
        a.run(self.root, runner=fixtures.Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))
        self.assertNotIn('html', w.export(self.root)['files']['M'])
        self.assertEqual(before, path.read_bytes())

    def test_auto_revision_change_blocks_delivery_even_without_workspace_revision_change(self):
        first = self.final(); revision = w.read(self.root / 'state.json')['revision']; self.invalidate_auto()
        self.assertEqual(revision, w.read(self.root / 'state.json')['revision'])
        self.assertNotIn('html', w.current_exports(self.root)['files']['M'])
        self.assertTrue(Path(first['files']['M']['html']).exists())
        with self.assertRaises(ValueError): w.publish(self.root, ['M'])

    def test_host_cli_final_submission_returns_exports_but_does_not_publish(self):
        prepared = a.prepare(self.root); a.submit(self.root, self.envelope(prepared, fixtures.draft()))
        prepared = a.prepare(self.root); path = self.base / 'review.json'
        path.write_text(json.dumps(self.envelope(prepared, fixtures.review(prepared['request']))))
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['auto_minutes.py', 'submit', '--workspace', str(self.root), '--file', str(path)]), mock.patch('sys.stdout', output):
            a.main()
        result = json.loads(output.getvalue())
        self.assertTrue(Path(result['exports']['files']['M']['html']).is_file())
        self.assertFalse((self.root / 'publication.json').exists())

    def test_api_answer_saves_exact_scope_without_model_and_rejects_cross_origin(self):
        first = self.issue(); payload = self.payload(first, first['issues'][0]); request = self.handler('/api/auto-answer', payload)
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')): request.do_POST()
        self.assertEqual(200, request.json.call_args.args[0]); self.assertTrue(request.json.call_args.args[1]['saved'])
        record = a.load(self.root, 'M'); self.assertEqual(payload['issue_id'], record['answers'][0]['issue_id'])
        self.assertEqual('awaiting_ai', record['pipeline_state']); self.assertIsNone(record['finalization'])
        request.headers['Origin'] = 'http://evil.invalid'; request.json.reset_mock(); request.do_POST()
        self.assertEqual(403, request.json.call_args.args[0]); self.assertEqual(1, len(a.load(self.root, 'M')['answers']))

    def test_inbox_status_exposes_auto_state_without_retranscribing(self):
        self.final(); item = {'status': 'ready', 'workspace': str(self.root), 'first_seen': 'fixture', 'name': 'synthetic.wav'}
        with mock.patch('transcribe_local.transcribe', side_effect=AssertionError('no ASR')):
            status = inbox.status({'items': {'fixture': item}})
        self.assertEqual('completed', status['items'][0]['pipeline_state'])
        self.assertEqual('auto_final', status['items'][0]['content_state']); self.assertEqual('ready', status['items'][0]['status'])

    def test_handler_save_foreground_wait_host_review_cli_exports_without_chat_trigger(self):
        first = self.issue(); payload = self.payload(first, first['issues'][0]); calls = []
        def save_answer(_):
            calls.append(True); request = self.handler('/api/auto-answer', payload); request.do_POST()
            self.assertEqual(200, request.json.call_args.args[0])
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')):
            result = a.wait_for_answer(self.root, 'M', first['revision'], clock=lambda: 0, sleep=save_answer)
        self.assertTrue(result['changed']); self.assertEqual([True], calls)
        prepared = a.prepare(self.root); self.assertEqual('semantic_review', prepared['stage'])
        path = self.base / 'host-review.json'; path.write_text(json.dumps(self.envelope(prepared, fixtures.review(prepared['request']))))
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['auto_minutes.py', 'submit', '--workspace', str(self.root), '--file', str(path)]), mock.patch('sys.stdout', output): a.main()
        final = json.loads(output.getvalue()); self.assertEqual('review_resolved_final', final['content_state'])
        self.assertTrue(Path(final['exports']['files']['M']['html']).is_file())

    def test_foreground_wait_timeout_does_not_fabricate_answer_or_call_model(self):
        first = a.run(self.root)
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')):
            result = a.wait_for_answer(self.root, 'M', first['revision'], timeout=0)
        self.assertFalse(result['changed']); self.assertTrue(result['timed_out'])
        self.assertEqual([], a.load(self.root, 'M')['answers'])

    def test_missing_checkpoint_blocks_old_download_publish_and_archive(self):
        old = self.final(); w.publish(self.root); (a.directory(self.root, 'M') / 'state.json').unlink()
        self.assertNotIn('html', w.current_exports(self.root)['files']['M'])
        with self.assertRaises(ValueError): w.publish(self.root, ['M'])
        with mock.patch.object(archive_meeting, 'collect', side_effect=AssertionError('must reject before moving')):
            with self.assertRaises(ValueError): archive_meeting.collect_workspace(self.root, 'M', archive_root=self.base / 'archive')
        self.assertTrue(Path(old['files']['M']['html']).exists())

    def test_separate_decisions_and_open_questions_are_in_html_and_markdown(self):
        candidate = fixtures.draft(); candidate['decisions'] = [{'text': '当前方案继续讨论。', 'evidence': [0], 'status': 'open', 'proposer': None}]
        candidate['open_questions'] = [{'text': '方案何时再次讨论？', 'evidence': [0]}]
        files = self.final(candidate)['files']['M']; seed = json.dumps(self.seed(files['html']), ensure_ascii=False)
        for text in ('当前方案继续讨论。', '方案何时再次讨论？'):
            self.assertIn(text, seed); self.assertIn(text, Path(files['md']).read_text())

    def test_scoped_corrections_do_not_rewrite_already_checked_auto_text(self):
        state = w.read(self.root / 'state.json'); raw = Path(state['sources'][0]['path']); data = w.read(raw)
        data['segments'][0]['text'] = '预算为100元。'; data['segments'][1]['text'] = '另案预算为300元。'
        w.write(raw, data); state['sources'][0]['transcript_sha256'] = w.file_digest(raw); w.write(self.root / 'state.json', state)
        w.write(self.root / 'corrections.json', [
            {'id': 'c1', 'source_id': 'M', 'heard': '100', 'canonical': '300', 'scope': '甲案', 'segment_indices': [0], 'active': True, 'apply_to_derived': True},
            {'id': 'c2', 'source_id': 'M', 'heard': '300', 'canonical': '500', 'scope': '乙案', 'segment_indices': [1], 'active': True, 'apply_to_derived': True}])
        candidate = fixtures.draft(); candidate.update(topic='预算讨论', summary='预算为300元。')
        candidate['points'][0].update(title='预算', text='预算为300元。')
        files = self.final(candidate)['files']['M']; seed = self.seed(files['html'])
        self.assertEqual('预算为300元。', seed['summary']); self.assertEqual('预算为300元。', seed['points'][0]['text'])
        self.assertEqual(candidate['summary'], w.read(files['structured'])['analysis']['reader']['summary'])

    def test_current_auto_can_be_explicitly_archived_then_move_requires_recheck(self):
        audio = self.base / 'synthetic.wav'; audio.write_bytes(b'synthetic-test-audio-not-a-recording')
        state = w.read(self.root / 'state.json'); state['sources'][0].update(audio_path=str(audio), audio_sha256=w.file_digest(audio))
        w.write(self.root / 'state.json', state); self.final(); w.publish(self.root)
        result = archive_meeting.collect_workspace(self.root, 'M', archive_root=self.base / 'archive', archive_date='2026-10-18')
        self.assertFalse(audio.exists()); self.assertTrue(Path(result['audio']).exists())
        self.assertTrue(Path(result['html']).exists()); self.assertIsNone(a.status(self.root)['finalization'])
        self.assertNotIn('html', w.current_exports(self.root)['files']['M'])

    def test_stale_auto_revision_cannot_rearchive_old_publication(self):
        self.final(); w.publish(self.root); self.invalidate_auto()
        with mock.patch.object(archive_meeting, 'collect', side_effect=AssertionError('must reject before moving')):
            with self.assertRaises(ValueError): archive_meeting.collect_workspace(self.root, 'M', archive_root=self.base / 'archive')

    def test_never_auto_manual_workflow_remains_usable(self):
        state = w.read(self.root / 'state.json')
        for index, speaker in enumerate(state['speakers']):
            speaker.update(role_confirmed=True, role_name='甲' if index == 0 else '乙', display_name='甲' if index == 0 else '乙', identity_group='person-' + str(index))
        w.write(self.root / 'state.json', state)
        candidate = fixtures.draft(); analysis = a.analysis(candidate)
        w.set_analysis(self.root, analysis, state['revision'])
        current = w.current_exports(self.root)
        self.assertTrue(Path(current['files']['M']['html']).is_file())
        self.assertEqual({}, current.get('automatic_minutes', {}))
        self.assertEqual(current['version'], w.publish(self.root)['export_version'])
        self.assertFalse((self.root / 'auto-minutes').exists())

    def test_saved_answer_does_not_fail_due_to_competing_continuation(self):
        first = self.issue(); request = self.handler('/api/auto-answer', self.payload(first, first['issues'][0]))
        with mock.patch.object(a, 'run', side_effect=AssertionError('foreground host owns continuation')): request.do_POST()
        self.assertEqual(200, request.json.call_args.args[0]); self.assertTrue(request.json.call_args.args[1]['saved'])
        self.assertEqual(1, len(a.load(self.root, 'M')['answers']))

    def test_inbox_done_requires_current_publication_and_invalidated_auto_returns_ready(self):
        self.final(); w.publish(self.root)
        item = {'status': 'ready', 'workspace': str(self.root), 'first_seen': 'fixture', 'name': 'synthetic.wav'}
        queue = {'items': {'fixture': item}}
        with mock.patch('transcribe_local.transcribe', side_effect=AssertionError('no ASR')):
            current = inbox.status(queue); self.assertEqual('done', current['items'][0]['status'])
            self.invalidate_auto(); current = inbox.status(queue)
        self.assertEqual('ready', current['items'][0]['status']); self.assertEqual(1, len(current['ready']))
        self.assertEqual(str(self.root), current['ready'][0]['workspace'])

    def test_authenticated_stale_download_routes_return_no_old_file(self):
        self.final(); self.invalidate_auto()
        for route in ('/minutes/M', '/export/M/html', '/export/M/png'):
            request = self.handler(route); request.do_GET()
            self.assertEqual(404, request.json.call_args.args[0]); request.send_file.assert_not_called()


if __name__ == '__main__': unittest.main()
