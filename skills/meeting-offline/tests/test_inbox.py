"""The recording inbox: settle, de-duplicate, queue, transcribe locally, notify. Transcription is faked."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import inbox
import transcribe_local


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'mac'})
        self.env.start()
        device_context.initialize()
        self.folder = self.base / '会议录音'; self.folder.mkdir()
        self.calls, self.failures = [], 0
        self.fake = mock.patch.object(transcribe_local, 'transcribe', side_effect=self.transcribe); self.fake.start()
        self.notes, self.t = [], time.time()
        self.notify = mock.patch.object(inbox, 'notify', side_effect=lambda title, msg: self.notes.append(msg)); self.notify.start()

    def tearDown(self):
        self.notify.stop(); self.fake.stop(); self.env.stop(); self.tmp.cleanup()

    def transcribe(self, audio, engine, model=None, hotwords=()):
        self.calls.append(Path(audio).name)
        if self.failures:
            self.failures -= 1
            raise transcribe_local.LocalError('模型加载失败')
        result = self.base / (Path(audio).stem + '.json')
        result.write_text(json.dumps({'model': 'fake', 'segments': [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 2000, 'text': '这周把预算定下来。'},
            {'speaker_id': 1, 'start_ms': 2000, 'end_ms': 4000, 'text': '好，我来算。'}]}, ensure_ascii=False), encoding='utf-8')
        return {'result_path': str(result), 'speaker_separation': 'voice', 'speakers_detected': 2, 'speaker_coverage': 1.0}

    def drop(self, name, data=b'audio-bytes', age=60):
        path = self.folder / name; path.write_bytes(data)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def run_pass(self, step=30):
        self.t += step  # passes are normally minutes apart
        return inbox.run_once(self.folder, clock=lambda: self.t)

    def status_of(self, name):
        return next(i['status'] for i in inbox.status()['items'] if i['name'] == name)

    def test_a_recording_is_read_only_after_it_settles_then_transcribed_once(self):
        self.drop('周会.m4a')
        self.run_pass()
        self.assertEqual((self.status_of('周会.m4a'), self.calls), ('syncing', []))  # first sighting: just remembered
        ready = self.run_pass()['ready']
        self.assertEqual(self.calls, ['周会.m4a'])
        self.assertEqual(len(ready), 1)
        workspace = Path(ready[0]['workspace'])
        state = json.loads((workspace / 'state.json').read_text(encoding='utf-8'))
        self.assertEqual([s['key'] for s in state['speakers']], ['1-A', '1-B'])
        self.assertFalse(any(s['role_confirmed'] for s in state['speakers']))  # who is who is still the user's call
        self.assertIn('周会.m4a 已转写好（2 位发言人）', self.notes[0])
        self.run_pass()
        self.assertEqual(self.calls, ['周会.m4a'])  # never twice
        (workspace / 'publication.json').write_text('{}', encoding='utf-8')
        self.assertEqual(self.status_of('周会.m4a'), 'done')

    def test_a_file_still_being_written_or_synced_waits(self):
        path = self.drop('长会.m4a', b'part')
        self.run_pass()
        path.write_bytes(b'part-and-more'); os.utime(path, (time.time() - 60, time.time() - 60))
        self.run_pass()
        self.assertEqual(self.status_of('长会.m4a'), 'syncing')
        self.drop('拷进来的.m4a', age=3600)  # a copy keeps its old modification time, so time it from arrival
        self.run_pass(step=5); self.run_pass(step=5)
        self.assertEqual(self.status_of('拷进来的.m4a'), 'syncing')
        (self.folder / '.云端.m4a.icloud').write_bytes(b'stub')  # not downloaded from iCloud yet
        self.run_pass()
        item = next(i for i in inbox.status()['items'] if i['name'] == '云端.m4a')
        self.assertEqual((item['status'], item['note']), ('syncing', '等待 iCloud 下载'))

    def test_the_same_recording_under_another_name_is_not_redone(self):
        self.drop('周会.m4a'); self.drop('周会 副本.m4a')
        self.run_pass(); self.run_pass(); self.run_pass()
        self.assertEqual(len(self.calls), 1)  # one transcription for one recording, whichever copy came first
        self.assertEqual(sorted([self.status_of('周会.m4a'), self.status_of('周会 副本.m4a')]), ['duplicate', 'ready'])

    def test_a_new_recording_under_a_name_used_before_is_still_read(self):
        self.drop('新录音.m4a', b'first meeting')
        self.run_pass(); self.run_pass()
        (self.folder / '新录音.m4a').unlink()  # archived after the minutes were written
        self.drop('新录音.m4a', b'second meeting', age=30)
        self.run_pass(); self.run_pass()
        self.assertEqual(self.calls, ['新录音.m4a', '新录音.m4a'])
        self.assertEqual(len(inbox.status()['ready']), 2)  # the first meeting's record is kept
        self.drop('转走了.m4a', b'third', age=60); self.run_pass()
        (self.folder / '转走了.m4a').unlink(); self.run_pass()
        self.assertEqual(self.status_of('转走了.m4a'), 'gone')
        self.drop('转走了.m4a', b'third', age=60); self.run_pass(); self.run_pass()
        self.assertEqual(self.status_of('转走了.m4a'), 'ready')  # put back: picked up again

    def test_a_failed_recording_renamed_is_tried_under_its_new_name(self):
        self.failures = 1
        self.drop('新录音 3.m4a')
        self.run_pass(); self.run_pass()
        self.assertEqual(self.status_of('新录音 3.m4a'), 'failed')
        (self.folder / '新录音 3.m4a').rename(self.folder / '周会.m4a')
        self.run_pass(); self.run_pass()
        self.assertEqual((self.status_of('周会.m4a'), self.status_of('新录音 3.m4a')), ('ready', 'gone'))

    def test_a_half_made_workspace_is_rebuilt_and_problems_are_told_once(self):
        path = self.drop('周会.m4a')
        half = device_context.paths()['meetings'] / ('inbox-' + inbox.digest(path)[:12]); half.mkdir(parents=True)
        (half / 'state.json').write_text('{}', encoding='utf-8')  # stopped right after the first file
        self.run_pass(); self.run_pass()
        self.assertTrue((half / 'exports' / 'latest.json').exists())
        with mock.patch.object(inbox, 'candidates', side_effect=PermissionError):
            for _ in range(3):
                self.assertEqual(self.run_pass()['error'], 'permission')
        self.assertEqual(sum('没有权限' in n for n in self.notes), 1)
        line = inbox.summary(inbox.status())
        self.assertIn('ready=1', line); self.assertNotIn('\n', line)  # the agent log gets one line per pass

    def test_failures_are_retried_then_reported(self):
        self.failures = 5
        self.drop('周会.m4a')
        for _ in range(5):
            self.run_pass()
        self.assertEqual(len(self.calls), inbox.ATTEMPTS)
        self.assertEqual(self.status_of('周会.m4a'), 'failed')
        self.assertIn(f'试了 {inbox.ATTEMPTS} 次', self.notes[-1])

    def test_an_interrupted_run_is_picked_up_again(self):
        self.drop('周会.m4a')
        self.run_pass()
        queue = inbox.load_queue()
        item = next(iter(queue['items'].values()))
        item.update(status='transcribing', sha256='x' * 64, attempts=1)
        inbox.save_queue(queue)
        self.run_pass()
        self.assertEqual(self.status_of('周会.m4a'), 'ready')
        from file_lock import locked_file
        with locked_file(device_context.paths()['data_root'] / 'inbox.lock'):  # a pass already running: skip, don't wait
            self.assertIn('跳过', self.run_pass()['note'])
        inbox.queue_path().write_text('{"items": {', encoding='utf-8')  # a damaged queue does not stop the inbox
        self.assertEqual(inbox.status()['items'], [])

    def test_setup_describes_a_launchd_agent_and_saves_the_choice(self):
        with mock.patch.object(transcribe_local, 'installed', return_value=[]), mock.patch.object(inbox.sys, 'platform', 'linux'):
            plan = inbox.setup(self.folder, 'funasr', dry_run=True)
            self.assertEqual(plan['agent']['WatchPaths'], [str(self.folder)])
            self.assertEqual(plan['agent']['ProgramArguments'][-1], 'run')
            self.assertIn('/opt/homebrew/bin', plan['agent']['EnvironmentVariables']['PATH'])  # ffmpeg outside launchd's bare PATH
            self.assertIsNone(inbox.settings())  # a dry run changes nothing
            self.assertEqual(inbox.setup(self.folder, 'funasr')['mode'], 'manual')
        self.assertEqual(inbox.settings(), {'folder': str(self.folder), 'engine': 'funasr', 'model': None, 'local_only': True})
        with mock.patch.object(inbox.sys, 'platform', 'linux'):
            inbox.remove()
        self.assertIsNone(inbox.settings())
        with mock.patch.object(transcribe_local, 'installed', return_value=['funasr']), self.assertRaisesRegex(ValueError, '还没装好'):
            inbox.setup(self.folder, 'funasr')


if __name__ == '__main__':
    unittest.main()
