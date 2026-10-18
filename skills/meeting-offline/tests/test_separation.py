"""Speech the separation could not attribute never becomes somebody's words or somebody's task."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import memory
import minutes_html
import prepare_speakers
import workspace as w
from test_obsidian import fake_png


class SeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'pc'})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        transcript = self.base / 'm.json'
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 3000, 'text': '预算这周要定下来。'},
            {'speaker_id': None, 'start_ms': 3000, 'end_ms': 6000, 'text': '那我来做测算，周五前给。'},
            {'speaker_id': None, 'start_ms': 6000, 'end_ms': 8000, 'text': '好的，我跟进供应商。'}]})
        state = prepare_speakers.prepare(['m=' + str(transcript)])
        self.assertEqual(state['sources'][0]['separation'], {'status': 'partial', 'coverage': .29, 'speakers': 1})
        w.write(self.base / 'state.json', state)
        audio = self.base / 'm.m4a'; audio.write_bytes(b'audio')
        self.root = w.initialize(self.base / 'state.json', self.base / 'meetings' / 'm', {'m': audio})

    def tearDown(self):
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def revision(self):
        return w.read(self.root / 'state.json')['revision']

    def test_unassigned_speech_cannot_be_claimed_as_one_person_without_the_user_saying_so(self):
        with self.assertRaisesRegex(ValueError, '说话人未分离'):
            w.save_roles(self.root, [{'key': 'm-UNASSIGNED', 'name': '王五', 'entity_type': 'person', 'scope': '测试'}], self.revision())
        w.save_roles(self.root, [{'key': 'm-A', 'name': '林悦', 'entity_type': 'person', 'scope': '测试'},
                                 {'key': 'm-UNASSIGNED', 'keep_unknown': True}], self.revision())
        speaker = next(s for s in w.read(self.root / 'state.json')['speakers'] if s['key'] == 'm-UNASSIGNED')
        self.assertTrue(speaker['accepted_unknown'])
        # a one-person recording whose separation failed: allowed once the user confirms it, from the chat or the page
        self.assertTrue(next(s for s in w.public_state(self.root)['speakers'] if s['key'] == 'm-UNASSIGNED')['diarization_unassigned'])
        mapping = self.base / 'roles.json'
        w.write(mapping, [{'key': 'm-UNASSIGNED', 'name': '王五', 'entity_type': 'person', 'scope': '测试', 'confirm_single_speaker': True}])
        run = subprocess.run([sys.executable, str(Path(w.__file__)), 'roles', '--workspace', str(self.root), '--file', str(mapping),
                              '--revision', str(self.revision())], capture_output=True, text=True, env=os.environ)
        self.assertEqual(run.returncode, 0, run.stderr)
        roles = memory.read_store(device_context.paths()['memory'])['roles']
        self.assertIn('用户在对话中明确确认', next(r for r in roles if r['name'] == '王五')['confirmation'])

    def test_unassigned_speech_is_never_an_owner_or_a_participant(self):
        w.save_roles(self.root, [{'key': 'm-A', 'name': '林悦', 'entity_type': 'person', 'scope': '测试'},
                                 {'key': 'm-UNASSIGNED', 'keep_unknown': True}], self.revision())
        claim = {'task': '做测算', 'owner': 'm-UNASSIGNED', 'owner_basis': 'self_commitment', 'evidence': [1]}
        with self.assertRaisesRegex(ValueError, '说话人未分离'):
            w.set_analysis(self.root, {'sources': {'m': {'actions': [claim]}}}, self.revision())
        w.set_analysis(self.root, {'sources': {'m': {'actions': [{**claim, 'owner': None, 'owner_basis': None}], 'reader': {
            'topic': '预算会', 'points': [{'title': '预算', 'text': '本周定预算。', 'evidence': [0]}], 'todos': []}}}}, self.revision())
        files = w.read(self.root / 'exports' / 'latest.json')['files']['m']
        minutes = Path(files['md']).read_text(encoding='utf-8')
        self.assertIn('参会人：林悦', minutes)
        self.assertNotIn('等2人', minutes)  # unattributed speech is not counted as another person
        self.assertIn('**说话人未分离**', Path(files['transcript_md']).read_text(encoding='utf-8'))

    def test_revoking_an_attributed_unassigned_identity_is_rejected_before_any_write(self):
        w.save_roles(self.root, [{'key': 'm-A', 'name': '林悦', 'entity_type': 'person', 'scope': '测试'},
                                 {'key': 'm-UNASSIGNED', 'name': '王五', 'entity_type': 'person',
                                  'scope': '测试', 'confirm_single_speaker': True}], self.revision())
        action = {'task': '做测算', 'owner': 'm-UNASSIGNED', 'owner_basis': 'self_commitment', 'evidence': [1]}
        w.set_analysis(self.root, {'sources': {'m': {'actions': [action]}}}, self.revision())
        revision = self.revision()
        paths = [self.root / name for name in ('state.json', 'manifest.json', 'analysis.json',
                                               'corrections.json', 'exports/latest.json')]
        paths.append(device_context.paths()['memory'])
        before = {path: path.read_bytes() for path in paths}
        snapshots = sorted((self.root / 'revisions').iterdir())
        with self.assertRaises(ValueError) as caught:
            w.save_roles(self.root, [{'key': 'm-A', 'name': '还没批准的新人物', 'entity_type': 'person', 'scope': '测试'},
                                     {'key': 'm-UNASSIGNED', 'keep_unknown': True}], revision)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(sorted((self.root / 'revisions').iterdir()), snapshots)
        self.assertIn('尚未保存', str(caught.exception))
        # Clearing the attribution first makes revocation valid, without silently assigning the task elsewhere.
        w.set_analysis(self.root, {'sources': {'m': {'actions': [{**action, 'owner': None, 'owner_basis': None}]}}}, revision)
        w.save_roles(self.root, [{'key': 'm-UNASSIGNED', 'keep_unknown': True}], self.revision())
        speaker = next(s for s in w.read(self.root / 'state.json')['speakers'] if s['key'] == 'm-UNASSIGNED')
        self.assertTrue(speaker['accepted_unknown'])
        self.assertIsNone(speaker['identity_group'])
        self.assertEqual(w.read(self.root / 'exports/latest.json')['revision'], self.revision())


if __name__ == '__main__':
    unittest.main()
