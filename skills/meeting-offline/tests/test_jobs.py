import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transcribe_qwen as api
import qwen_job


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.audio = self.root / 'one.m4a'; self.audio.write_bytes(b'synthetic audio fixture')
        self.words = self.root / 'words.json'; self.words.write_text('{"测试产品":2}')
        self.args = argparse.Namespace(audio=str(self.audio), duration_seconds=400,
            region='cn', no_diarization=False, keep_dialect=False, filetrans=False,
            hotwords=str(self.words), job_dir=str(self.root/'job'), dry_run=False,
            network_route='direct', poll_seconds=1, timeout_seconds=3,
            confirm_over_2h=False, confirm_upload=True, retry_upload=False, import_result=None)
        self.task = {'output': {'task_status':'SUCCEEDED', 'results':[{'subtask_status':'SUCCEEDED','transcription_url':'https://result.aliyuncs.com/a'}]}}
        self.transcript = {'transcripts':[{'text':'测试产品', 'sentences':[{'text':'测试产品','begin_time':0,'end_time':1000,'speaker_id':0}]}]}
    def tearDown(self): self.tmp.cleanup()
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


if __name__=='__main__':unittest.main()
