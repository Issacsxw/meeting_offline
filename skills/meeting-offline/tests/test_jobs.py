import argparse
import io
import struct
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transcribe_qwen as api
import qwen_job


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.audio = self.root / 'one.wav'
        self.write_wav(1)
        self.words = self.root / 'words.json'; self.words.write_text('{"测试产品":2}')
        self.args = argparse.Namespace(audio=str(self.audio), duration_seconds=400,
            region='cn', no_diarization=False, keep_dialect=False, filetrans=False,
            hotwords=str(self.words), job_dir=str(self.root/'job'), dry_run=False,
            network_route='direct', poll_seconds=1, timeout_seconds=3,
            confirm_over_2h=False, confirm_upload=True, retry_upload=False, import_result=None)
        self.task = {'output': {'task_status':'SUCCEEDED', 'results':[{'subtask_status':'SUCCEEDED','transcription_url':'https://result.aliyuncs.com/a'}]}}
        self.transcript = {'transcripts':[{'text':'测试产品', 'sentences':[{'text':'测试产品','begin_time':0,'end_time':1000,'speaker_id':0}]}]}
    def tearDown(self): self.tmp.cleanup()
    def write_wav(self, channels):
        with wave.open(str(self.audio), 'wb') as output:
            output.setparams((channels, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\0\0' * channels * 8000)

    def save_manifest(self, **extra):
        _, config, sha, job_id, directory = qwen_job.describe(self.args, api)
        directory.mkdir(exist_ok=True)
        manifest = {'device_id': qwen_job.device_context.device_id(), 'status': 'planned',
                    'audio_sha256': sha, 'config': config, 'job_id': job_id, **extra}
        qwen_job.save_json(directory / 'job.json', manifest)
        return directory
    def test_resume_and_cache_never_duplicate_upload(self):
        def request(url, **kwargs):
            if kwargs.get('method')=='POST':
                self.assertEqual(kwargs['payload']['parameters']['vocabulary'],{'测试产品':2})
                return {'output':{'task_id':'synthetic-task'}}
            return self.transcript
        with patch.object(api,'get_api_key',return_value='synthetic-not-real'), patch.object(api,'get_upload_policy',return_value={}), patch.object(api,'upload_temporary',return_value='oss://synthetic') as upload, patch.object(api,'request_json',side_effect=request) as requests, patch.object(api,'poll_task',side_effect=[api.TranscriptionError('timeout'),self.task]):
            with self.assertRaises(api.TranscriptionError): qwen_job.execute(self.args,api)
            manifest=json.loads((self.root/'job/job.json').read_text());self.assertEqual(manifest['task_id'],'synthetic-task')
            self.args.confirm_upload=False
            result=qwen_job.execute(self.args,api);self.assertEqual(result['text'],'测试产品')
            self.assertEqual(upload.call_count,1)
            self.assertEqual(sum(c.kwargs.get('method')=='POST' for c in requests.call_args_list),1)
        with patch.object(api,'get_api_key',side_effect=AssertionError('cache must not read key')),patch.object(api,'request_json',side_effect=AssertionError('cache must not use network')):
            self.assertEqual(qwen_job.execute(self.args,api)['text'],'测试产品')
        self.assertNotIn('synthetic-not-real',(self.root/'job/job.json').read_text())
    def test_ambiguous_submission_is_not_retried(self):
        with patch.object(api,'get_api_key',return_value='synthetic'),patch.object(api,'get_upload_policy',return_value={}),patch.object(api,'upload_temporary',return_value='oss://fixture'),patch.object(api,'request_json',side_effect=api.TranscriptionError('reset')) as request:
            with self.assertRaises(api.TranscriptionError):qwen_job.execute(self.args,api)
            with self.assertRaisesRegex(api.TranscriptionError,'结果不确定'):qwen_job.execute(self.args,api)
            self.assertEqual(request.call_count,1)
    def test_rejected_submission_stays_retryable(self):
        # A bad key (401) or a refused connection means nothing ran; the user can fix it and retry.
        self.args.hotwords=None; self.args.duration_seconds=60
        failures=[api.TranscriptionError('HTTP 401', not_processed=True), {'output':{'sentences':[{'text':'好','begin_time':0,'end_time':900,'speaker_id':0}]}}]
        def short(*a, **k):
            item=failures.pop(0)
            if isinstance(item, Exception): raise item
            return api.normalize_short(item, 60)
        with patch.object(api,'get_api_key',return_value='synthetic'),patch.object(api,'transcribe_short',side_effect=short) as call:
            with self.assertRaisesRegex(api.TranscriptionError,'401'):qwen_job.execute(self.args,api)
            self.assertEqual(json.loads((self.root/'job/job.json').read_text())['status'],'failed')
            self.assertEqual(qwen_job.execute(self.args,api)['text'],'好')
            self.assertEqual(call.call_count,2)
    def test_missing_key_explains_both_ways_forward(self):
        with patch.dict('os.environ',{'DASHSCOPE_API_KEY':''}),patch.object(api.sys,'platform','linux'):
            with self.assertRaisesRegex(api.TranscriptionError,'transcription-options.md.*transcribe_local.py') as caught:api.get_api_key()
        self.assertTrue(caught.exception.not_processed)
    def test_dry_run_no_directory_key_or_network(self):
        self.args.dry_run=True
        with patch.object(api,'get_api_key',side_effect=AssertionError),patch.object(api,'request_json',side_effect=AssertionError):
            plan=qwen_job.execute(self.args,api)
        self.assertEqual(plan['vocabulary'],{'测试产品':2});self.assertFalse((self.root/'job').exists())
    def test_no_upload_without_recording_authorization(self):
        self.args.confirm_upload=False
        with patch.object(api,'get_api_key',side_effect=AssertionError):
            with self.assertRaisesRegex(api.TranscriptionError,'尚未上传'):qwen_job.execute(self.args,api)
    def test_invalid_and_short_hotword_inputs_fail_before_network(self):
        self.words.write_text('{"测试产品":true}')
        with self.assertRaises(api.TranscriptionError):qwen_job.describe(self.args,api)
        self.words.write_text('{"测试产品":2}');self.args.duration_seconds=30
        with self.assertRaisesRegex(api.TranscriptionError,'filetrans'):qwen_job.describe(self.args,api)
        self.args.filetrans=True;self.assertEqual(qwen_job.describe(self.args,api)[1]['model'],api.LONG_MODEL)
    def test_network_routing_is_scoped(self):
        api.NETWORK_ROUTE='direct'
        self.assertTrue(api.direct_aliyun('https://bucket.oss-cn-hangzhou.aliyuncs.com/file'))
        self.assertFalse(api.direct_aliyun('https://aliyuncs.com.other.example/file'))
        self.assertFalse(api.direct_aliyun('https://example.com/file'))
        api.NETWORK_ROUTE='proxy';self.assertFalse(api.direct_aliyun('https://dashscope.aliyuncs.com/api'))
        api.NETWORK_ROUTE='direct'
    def test_import_does_not_claim_hotword_effect(self):
        self.args.import_result=str(self.root/'result.json')
        with self.assertRaisesRegex(api.TranscriptionError,'热词'):qwen_job.execute(self.args,api)

    def complete_filetrans(self):
        def request(url, **kwargs):
            if kwargs.get('method') == 'POST':
                self.assertEqual(kwargs['payload']['parameters']['channel_id'], [0])
                return {'output': {'task_id': 'synthetic-task'}}
            return self.transcript
        with patch.object(api, 'get_api_key', return_value='synthetic'), \
             patch.object(api, 'get_upload_policy', return_value={}), \
             patch.object(api, 'upload_temporary', return_value='oss://synthetic') as upload, \
             patch.object(api, 'request_json', side_effect=request) as calls, \
             patch.object(api, 'poll_task', return_value=self.task):
            result = qwen_job.execute(self.args, api)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(sum(c.kwargs.get('method') == 'POST' for c in calls.call_args_list), 1)
        return result

    def test_confirmed_stereo_with_different_channel_content_can_continue(self):
        with wave.open(str(self.audio), 'wb') as output:
            output.setparams((2, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<hh', i, -i) for i in range(8000)))
        self.args.confirm_channel_0 = True
        with patch.object(sys, 'stderr', new_callable=io.StringIO) as warning:
            self.assertEqual(self.complete_filetrans()['text'], '测试产品')
        manifest = json.loads((self.root / 'job/job.json').read_text())
        self.assertEqual(manifest['audio_channels'], [2])
        self.assertTrue(manifest['channel_0_confirmed'])
        self.assertIn('其他声道', warning.getvalue())
        self.assertIn('可能遗漏', warning.getvalue())

    def test_missing_optional_ffprobe_can_continue_after_confirmation(self):
        self.audio = self.root / 'recording.m4a'
        self.audio.write_bytes(b'synthetic m4a fixture; never uploaded')
        self.args.audio = str(self.audio)
        self.args.confirm_channel_0 = True
        with patch.object(qwen_job.audio_duration.shutil, 'which', return_value=None):
            self.assertEqual(self.complete_filetrans()['text'], '测试产品')
        manifest = json.loads((self.root / 'job/job.json').read_text())
        self.assertIsNone(manifest['audio_channels'])
        self.assertTrue(manifest['channel_0_confirmed'])

    def test_stereo_dry_run_returns_risk_and_confirmation_plan(self):
        self.write_wav(2)
        self.args.dry_run = True
        with patch.object(api, 'get_api_key', side_effect=AssertionError('no key')), \
             patch.object(api, 'request_json', side_effect=AssertionError('no network')):
            plan = qwen_job.execute(self.args, api)
        self.assertEqual(plan['audio_channels'], [2])
        self.assertTrue(plan['channel_0_confirmation_required'])
        self.assertFalse(plan['channel_0_confirmed'])
        self.assertIn('可能遗漏', plan['channel_warning'])
        self.assertFalse((self.root / 'job').exists())

    def test_channel_confirmation_is_not_upload_authorization(self):
        self.write_wav(2)
        self.args.confirm_channel_0 = True
        self.args.confirm_upload = False
        with patch.object(api, 'get_api_key', side_effect=AssertionError('no key')):
            with self.assertRaisesRegex(api.TranscriptionError, '尚未上传'):
                qwen_job.execute(self.args, api)
        self.assertFalse((self.root / 'job/job.json').exists())

    def test_multiple_tracks_can_continue_only_after_explicit_confirmation(self):
        self.args.confirm_channel_0 = True
        with patch.object(qwen_job.audio_duration, 'audio_channels', return_value=[1, 1]):
            self.assertEqual(self.complete_filetrans()['text'], '测试产品')
        self.assertEqual(json.loads((self.root / 'job/job.json').read_text())['audio_channels'], [1, 1])

    def test_saved_channel_consent_applies_to_same_job_retry_without_reupload(self):
        self.write_wav(2)
        self.args.confirm_channel_0 = True
        with patch.object(api, 'get_api_key', return_value='synthetic'), \
             patch.object(api, 'get_upload_policy', return_value={}), \
             patch.object(api, 'upload_temporary', return_value='oss://synthetic'), \
             patch.object(api, 'request_json', side_effect=api.TranscriptionError('HTTP 401', not_processed=True)):
            with self.assertRaisesRegex(api.TranscriptionError, '401'):
                qwen_job.execute(self.args, api)
        self.args.confirm_channel_0 = False
        self.args.confirm_upload = False
        def request(url, **kwargs):
            return {'output': {'task_id': 'synthetic-task'}} if kwargs.get('method') == 'POST' else self.transcript
        with patch.object(api, 'get_api_key', return_value='synthetic'), \
             patch.object(api, 'upload_temporary', side_effect=AssertionError('no reupload')), \
             patch.object(api, 'request_json', side_effect=request), \
             patch.object(api, 'poll_task', return_value=self.task):
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '测试产品')
        self.assertTrue(json.loads((self.root / 'job/job.json').read_text())['channel_0_confirmed'])

    def test_cli_accepts_separate_channel_zero_risk_confirmation(self):
        with patch.object(sys, 'argv', ['transcribe_qwen.py', '--audio', str(self.audio), '--confirm-channel-0']):
            self.assertTrue(api.parse_args().confirm_channel_0)

    def test_filetrans_rejects_stereo_before_key_upload_or_submission(self):
        self.write_wav(2)
        for duration, filetrans, no_diarization in [(400, False, False), (400, False, True), (30, True, False)]:
            with self.subTest(duration=duration, no_diarization=no_diarization):
                self.args.duration_seconds = duration
                self.args.filetrans = filetrans
                self.args.no_diarization = no_diarization
                with patch.object(api, 'get_api_key', side_effect=AssertionError('no key')), \
                     patch.object(api, 'request_json', side_effect=AssertionError('no network')), \
                     patch.object(api, 'upload_temporary', side_effect=AssertionError('no upload')):
                    with self.assertRaisesRegex(api.TranscriptionError, 'confirm-channel-0'):
                        qwen_job.execute(self.args, api)
                self.assertFalse((self.root / 'job/job.json').exists())

    def test_unknown_layout_dry_run_returns_risk_without_creating_a_job(self):
        self.args.dry_run = True
        with patch.object(qwen_job.audio_duration, 'audio_channels', side_effect=ValueError('无法确认声道')):
            plan = qwen_job.execute(self.args, api)
        self.assertIsNone(plan['audio_channels'])
        self.assertTrue(plan['channel_0_confirmation_required'])
        self.assertIn('confirm-channel-0', plan['channel_warning'])
        self.assertFalse((self.root / 'job').exists())

    def test_unknown_channel_layout_fails_before_reading_key(self):
        with patch.object(qwen_job.audio_duration, 'audio_channels', side_effect=ValueError('无法确认声道')), \
             patch.object(api, 'get_api_key', side_effect=AssertionError('no key')):
            with self.assertRaisesRegex(api.TranscriptionError, '声道'):
                qwen_job.execute(self.args, api)

    def test_multiple_mono_streams_are_also_rejected(self):
        with patch.object(qwen_job.audio_duration, 'audio_channels', return_value=[1, 1]), \
             patch.object(api, 'get_api_key', side_effect=AssertionError('no key')):
            with self.assertRaisesRegex(api.TranscriptionError, '其他声道或音轨'):
                qwen_job.execute(self.args, api)

    def test_mono_preflight_preserves_existing_config_and_job_identity(self):
        self.args.job_dir = None
        _, config, _, _, directory = qwen_job.describe(self.args, api)
        self.args.dry_run = True
        plan = qwen_job.execute(self.args, api)
        self.assertEqual(plan['job_dir'], str(directory))
        self.assertEqual({key: plan[key] for key in config}, config)

    def test_existing_task_resumes_even_for_stereo_without_submitting_again(self):
        self.write_wav(2)
        self.save_manifest(status='submitted', task_id='existing-task')
        with patch.object(qwen_job.audio_duration, 'audio_channels', side_effect=AssertionError('resume needs no probe')), \
             patch.object(api, 'get_api_key', return_value='synthetic'), \
             patch.object(api, 'poll_task', return_value=self.task), \
             patch.object(api, 'request_json', return_value=self.transcript) as request, \
             patch.object(api, 'upload_temporary', side_effect=AssertionError('no upload')):
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '测试产品')
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('method', request.call_args.kwargs)
        with patch.object(qwen_job.audio_duration, 'audio_channels', side_effect=AssertionError('cache needs no probe')), \
             patch.object(api, 'get_api_key', side_effect=AssertionError('cache needs no key')):
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '测试产品')

    def test_uploaded_stereo_without_task_id_is_not_submitted(self):
        self.write_wav(2)
        directory = self.save_manifest(status='uploaded', oss_url='oss://synthetic', uploaded_at=qwen_job.now())
        before = (directory / 'job.json').read_bytes()
        with patch.object(api, 'get_api_key', side_effect=AssertionError('no key')):
            with self.assertRaisesRegex(api.TranscriptionError, 'confirm-channel-0'):
                qwen_job.execute(self.args, api)
        self.assertEqual((directory / 'job.json').read_bytes(), before)

    def test_imported_stereo_result_remains_offline(self):
        self.write_wav(2)
        self.args.hotwords = None
        imported = self.root / 'imported.json'
        imported.write_text(json.dumps(api.normalize_long(self.transcript, self.task, 400)))
        self.args.import_result = str(imported)
        with patch.object(qwen_job.audio_duration, 'audio_channels', side_effect=AssertionError('import needs no probe')), \
             patch.object(api, 'get_api_key', side_effect=AssertionError('import needs no key')):
            self.assertEqual(qwen_job.execute(self.args, api)['text'], '测试产品')


if __name__=='__main__':unittest.main()
