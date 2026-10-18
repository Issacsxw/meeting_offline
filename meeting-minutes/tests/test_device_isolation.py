import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context as d
import memory
import workspace as w
import review
import archive_meeting as a
import qwen_job
import transcribe_qwen as api
import audio_duration
from file_lock import locked_file


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.patch = patch.dict(os.environ, {'MEETING_MINUTES_DATA_HOME': str(self.base / 'shared-data'),
                                             'MEETING_MINUTES_DEVICE_ID': 'computer-a'})
        self.patch.start()

    def tearDown(self):
        self.patch.stop(); self.tmp.cleanup()

    def test_shared_folder_starts_empty_on_second_device_and_keeps_first(self):
        first = d.initialize(); original_id = first['device_id']
        data = memory.read_store(first['memory']); data['terms'].append({'canonical': '只属于甲设备'})
        d.atomic_json(first['memory'], data)
        d.atomic_json(first['preferences'], {'schema_version': 1, 'device_id': original_id, 'self_display_name': '甲设备称呼'})
        with patch.dict(os.environ, {'MEETING_MINUTES_DEVICE_ID': 'computer-b'}):
            second = d.initialize()
            self.assertNotEqual(second['device_id'], original_id)
            self.assertNotEqual(second['archive'], first['archive'])
            self.assertEqual(memory.read_store(second['memory'])['terms'], [])
            self.assertEqual(list(second['meetings'].iterdir()), [])
            self.assertEqual(list(second['jobs'].iterdir()), [])
            self.assertNotIn('self_display_name', json.loads(second['preferences'].read_text()))
            with self.assertRaises(ValueError):memory.read_store(first['memory'])
        self.assertEqual(d.initialize()['device_id'], original_id)
        self.assertEqual(memory.read_store(first['memory'])['terms'][0]['canonical'], '只属于甲设备')
        self.assertNotIn('computer-a', first['profile'].read_text())

    def test_foreign_workspace_and_archive_rejected_before_changes(self):
        current = d.initialize()
        transcript = self.base / 'raw.json'
        w.write(transcript, {'segments': [{'speaker_id': 0, 'text': '测试发言', 'start_ms': 0, 'end_ms': 1000}]})
        prepared = self.base / 'prepared.json'
        w.write(prepared, {'sources': [{'source_id': '1', 'path': str(transcript)}], 'speakers': [],
                          'memory_store': str(current['memory'])})
        root = self.base / 'meeting'; w.initialize(prepared, root, {})
        before = (root / 'state.json').read_bytes()
        audio = self.base / 'one.m4a'; audio.write_bytes(b'first recording')
        archived = a.collect(audio, root=self.base / 'archive')
        second = self.base / 'second.m4a'; second.write_bytes(b'second recording')
        with patch.dict(os.environ, {'MEETING_MINUTES_DEVICE_ID': 'computer-b'}):
            with self.assertRaises(ValueError):w.public_state(root)
            with self.assertRaises(ValueError):w.export(root)
            with self.assertRaises(ValueError):review.ReviewServer(root)
            with self.assertRaises(ValueError):a.collect(second, root=self.base / 'archive')
            self.assertTrue(second.exists())
        self.assertEqual((root / 'state.json').read_bytes(), before)
        self.assertTrue(Path(archived['audio']).is_file())

    def test_foreign_job_rejected_without_key_or_network(self):
        audio = self.base / 'one.m4a'; audio.write_bytes(b'audio')
        job = self.base / 'job'; job.mkdir()
        d.atomic_json(job / 'job.json', {'device_id': d.device_id(), 'status': 'completed'})
        args = argparse.Namespace(audio=str(audio), duration_seconds=400, region='cn', no_diarization=False,
            keep_dialect=False, filetrans=False, hotwords=None, job_dir=str(job), dry_run=False,
            network_route='direct', poll_seconds=1, timeout_seconds=3, confirm_over_2h=False,
            confirm_upload=True, retry_upload=False, import_result=None)
        with patch.dict(os.environ, {'MEETING_MINUTES_DEVICE_ID': 'computer-b'}), \
             patch.object(api, 'get_api_key', side_effect=AssertionError('must not read key')), \
             patch.object(api, 'request_json', side_effect=AssertionError('must not call network')):
            with self.assertRaises(ValueError):qwen_job.execute(args, api)

    def test_unbound_old_memory_not_automatically_adopted(self):
        path = self.base / 'old-memory.json'
        d.atomic_json(path, {'schema_version': 1, 'roles': [], 'terms': [{'canonical': '旧词'}], 'history': []})
        with self.assertRaises(ValueError):memory.read_store(path)

    def test_local_binding_applies_only_on_original_device(self):
        initial = d.paths(); base = initial['base']
        initial['profile_dir'].mkdir(parents=True)
        d.atomic_json(initial['profile'], {'device_id': initial['device_id'], 'data_root': str(base),
                                         'legacy_root': str(base), 'archive_root': str(self.base / 'old-archive')})
        d.atomic_json(base / 'memory.json', {'schema_version': 1, 'roles': [], 'terms': [{'canonical': '本机旧词'}], 'history': []})
        self.assertEqual(memory.read_store(d.paths()['memory'])['terms'][0]['canonical'], '本机旧词')
        with patch.dict(os.environ, {'MEETING_MINUTES_DEVICE_ID': 'computer-b'}):
            other = d.initialize()
            self.assertFalse(other['legacy'])
            self.assertEqual(memory.read_store(other['memory'])['terms'], [])

    def test_m4a_duration_and_nonblocking_lock(self):
        payload = b'\0' * 12 + struct.pack('>II', 1000, 12345) + b'\0' * 60
        mvhd = struct.pack('>I4s', len(payload) + 8, b'mvhd') + payload
        path = self.base / 'minimal.m4a'; path.write_bytes(struct.pack('>I4s', len(mvhd) + 8, b'moov') + mvhd)
        self.assertAlmostEqual(audio_duration.mp4_duration(path), 12.345)
        with locked_file(self.base / '.lock'):
            with self.assertRaises(BlockingIOError):
                with locked_file(self.base / '.lock', blocking=False):pass


if __name__ == '__main__':unittest.main()
