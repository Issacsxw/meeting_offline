"""Exercise request_json through real urllib/http.client, with synthetic sockets only."""
import argparse
import contextlib
import errno
import http.client
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import qwen_job
import transcribe_qwen as api


def response_bytes(value, *, status='200 OK', headers=''):
    body = json.dumps(value).encode()
    return (f'HTTP/1.1 {status}\r\n{headers}Content-Length: {len(body)}\r\n'
            'Connection: close\r\n\r\n').encode() + body


class SyntheticSocket:
    def __init__(self, response, *, proxy=False, send_error=None, response_error=None):
        self.responses = ([b'HTTP/1.1 200 Connection established\r\n\r\n'] if proxy else []) + [response]
        self.sent = []
        self.send_error = send_error
        self.response_error = response_error

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        self.sent.append(data)
        if not data.startswith(b'CONNECT ') and self.send_error:
            raise self.send_error

    def makefile(self, *args):
        if len(self.responses) == 1 and self.response_error:
            raise self.response_error
        return io.BytesIO(self.responses.pop(0))

    def close(self):
        pass


class RealRequestTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.audio = self.root / 'synthetic.m4a'
        self.audio.write_bytes(b'synthetic fixture, not a recording')
        self.task = {'output': {'task_status': 'SUCCEEDED', 'results': [
            {'subtask_status': 'SUCCEEDED', 'transcription_url': 'https://result.aliyuncs.com/synthetic'}]}}
        self.raw = {'transcripts': [{'text': '合成测试', 'sentences': [
            {'text': '合成测试', 'begin_time': 0, 'end_time': 900, 'speaker_id': 0}]}]}
        self.short = {'output': {'sentences': [
            {'text': '合成测试', 'begin_time': 0, 'end_time': 900, 'speaker_id': 0}]}}

    def args(self, duration, route, label):
        return argparse.Namespace(audio=str(self.audio), duration_seconds=duration,
            region='cn', no_diarization=False, keep_dialect=False, filetrans=False,
            hotwords=None, job_dir=str(self.root / f'{route}-{duration}-{label}'), dry_run=False,
            network_route=route, poll_seconds=1, timeout_seconds=3,
            confirm_over_2h=False, confirm_upload=True, retry_upload=False, import_result=None)

    @contextlib.contextmanager
    def transport(self, route, connect):
        # Real urllib ProxyHandler and HTTPS CONNECT still run; no real socket or TLS is used.
        patches = [
            patch.object(api, 'NETWORK_ROUTE', route),
            patch('urllib.request.getproxies', return_value={
                'http': 'http://proxy.invalid:8080', 'https': 'http://proxy.invalid:8080'}),
            patch('urllib.request.proxy_bypass', return_value=False),
            patch('socket.create_connection', side_effect=connect),
            patch('ssl.SSLContext.wrap_socket', side_effect=lambda sock, **kwargs: sock),
            patch.object(api, 'get_api_key', return_value='synthetic-not-a-key'),
            patch.object(api, 'get_upload_policy', return_value={}),
            patch.object(api, 'upload_temporary', return_value='oss://synthetic'),
            patch.object(api, 'poll_task', return_value=self.task),
        ]
        with contextlib.ExitStack() as stack:
            yield [stack.enter_context(p) for p in patches]

    def test_proven_connection_failures_retry_same_short_and_long_job(self):
        failures = [
            socket.gaierror(socket.EAI_NONAME, 'synthetic DNS failure'),
            ConnectionRefusedError(errno.ECONNREFUSED, 'synthetic refused connection'),
            OSError(errno.ENETUNREACH, 'synthetic network unreachable'),
            OSError(errno.EHOSTUNREACH, 'synthetic host unreachable'),
            TimeoutError('synthetic pre-request connection timeout'),
            socket.timeout('synthetic socket connection timeout'),
            OSError(errno.ETIMEDOUT, 'synthetic typed connection timeout'),
        ]
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                for index, failure in enumerate(failures):
                    with self.subTest(route=route, duration=duration, failure=type(failure).__name__, errno=failure.errno):
                        args = self.args(duration, route, str(index))
                        sockets = []
                        addresses = []
                        replies = [self.short] if duration == 60 else [{'output': {'task_id': 'synthetic-task'}}, self.raw]

                        def connect(address, *a, **kw):
                            addresses.append(address)
                            if len(addresses) == 1:
                                raise failure
                            sock = SyntheticSocket(response_bytes(replies.pop(0)), proxy=route == 'proxy')
                            sockets.append(sock)
                            return sock

                        with self.transport(route, connect) as mocks:
                            with self.assertRaises(api.TranscriptionError) as caught:
                                qwen_job.execute(args, api)
                            self.assertTrue(caught.exception.not_processed)
                            manifest = json.loads((Path(args.job_dir) / 'job.json').read_text())
                            self.assertEqual(manifest['status'], 'failed')
                            self.assertEqual(qwen_job.execute(args, api)['text'], '合成测试')
                            self.assertEqual(mocks[7].call_count, int(duration == 400))
                        self.assertEqual(addresses[0][0], 'proxy.invalid' if route == 'proxy' else 'dashscope.aliyuncs.com')
                        self.assertEqual(sum(data.startswith(b'POST ') for sock in sockets for data in sock.sent), 1)
                        self.assertEqual(sum(data.startswith(b'CONNECT ') for sock in sockets for data in sock.sent),
                                         len(sockets) if route == 'proxy' else 0)

    def test_sent_disconnect_and_timeout_never_resubmit(self):
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                for phase in ('send_reset', 'send_refused', 'send_no_route', 'send_dns', 'send_timeout',
                              'response_disconnect', 'response_timeout'):
                    with self.subTest(route=route, duration=duration, phase=phase):
                        args = self.args(duration, route, phase)
                        send_error = {'send_reset': ConnectionResetError('synthetic reset after partial send'),
                            'send_refused': ConnectionRefusedError(errno.ECONNREFUSED, 'refused after partial send'),
                            'send_no_route': OSError(errno.ENETUNREACH, 'no route after partial send'),
                            'send_dns': socket.gaierror(socket.EAI_NONAME, 'DNS failure reported after partial send'),
                            'send_timeout': TimeoutError('synthetic timeout after partial send')}.get(phase)
                        response_error = {'response_disconnect': http.client.RemoteDisconnected('synthetic response disconnect'),
                            'response_timeout': TimeoutError('synthetic response timeout')}.get(phase)
                        sock = SyntheticSocket(response_bytes({}), proxy=route == 'proxy',
                                               send_error=send_error, response_error=response_error)

                        with self.transport(route, lambda *a, **kw: sock) as mocks:
                            # Raw response-stage socket errors are also deliberately uncertain.
                            with self.assertRaises((api.TranscriptionError, OSError)) as caught:
                                qwen_job.execute(args, api)
                            self.assertFalse(getattr(caught.exception, 'not_processed', False))
                            self.assertEqual(json.loads((Path(args.job_dir) / 'job.json').read_text())['status'], 'uncertain_submission')
                            with self.assertRaisesRegex(api.TranscriptionError, '结果不确定'):
                                qwen_job.execute(args, api)
                            self.assertEqual(mocks[3].call_count, 1)
                        self.assertEqual(sum(data.startswith(b'POST ') for data in sock.sent), 1)

    def test_https_proxy_connect_and_tls_timeouts_before_origin_request_retry_safely(self):
        for route, phase in (('direct', 'target_tls'), ('proxy', 'proxy_connect'), ('proxy', 'target_tls')):
            for duration in (60, 400):
                with self.subTest(route=route, phase=phase, duration=duration):
                    args = self.args(duration, route, phase)
                    sock = SyntheticSocket(response_bytes({}), proxy=route == 'proxy')
                    sockets = []
                    replies = [self.short] if duration == 60 else [{'output': {'task_id': 'synthetic-task'}}, self.raw]

                    def connect(*a, **kw):
                        if not sockets:
                            sockets.append(sock)
                            return sock
                        connected = SyntheticSocket(response_bytes(replies.pop(0)), proxy=route == 'proxy')
                        sockets.append(connected)
                        return connected

                    with self.transport(route, connect) as mocks:
                        if phase == 'proxy_connect':
                            failure = patch.object(sock, 'makefile', side_effect=TimeoutError('synthetic CONNECT timeout'))
                        else:
                            failure = patch('ssl.SSLContext.wrap_socket', side_effect=TimeoutError('synthetic TLS timeout'))
                        with failure, self.assertRaises(api.TranscriptionError) as caught:
                            qwen_job.execute(args, api)
                        self.assertTrue(caught.exception.not_processed)
                        self.assertEqual(json.loads((Path(args.job_dir) / 'job.json').read_text())['status'], 'failed')
                        self.assertEqual(qwen_job.execute(args, api)['text'], '合成测试')
                        self.assertEqual(mocks[7].call_count, int(duration == 400))
                    self.assertEqual(sum(data.startswith(b'CONNECT ') for data in sock.sent), int(route == 'proxy'))
                    self.assertFalse(any(data.startswith(b'POST ') for data in sock.sent))
                    self.assertEqual(sum(data.startswith(b'POST ') for s in sockets[1:] for data in s.sent), 1)

    def test_redirect_connection_failure_after_post_remains_uncertain(self):
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                with self.subTest(route=route, duration=duration):
                    args = self.args(duration, route, 'redirect')
                    sock = SyntheticSocket(response_bytes({}, status='302 Found',
                        headers='Location: https://other.aliyuncs.com/synthetic\r\n'), proxy=route == 'proxy')
                    calls = []

                    def connect(*a, **kw):
                        calls.append(a)
                        if len(calls) > 1:
                            raise ConnectionRefusedError(errno.ECONNREFUSED, 'synthetic redirect target refused')
                        return sock

                    with self.transport(route, connect):
                        with self.assertRaises(api.TranscriptionError) as caught:
                            qwen_job.execute(args, api)
                        self.assertFalse(caught.exception.not_processed)
                        with self.assertRaisesRegex(api.TranscriptionError, '结果不确定'):
                            qwen_job.execute(args, api)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(sum(data.startswith(b'POST ') for data in sock.sent), 1)

    def test_redirect_http_rejection_after_post_never_resubmits(self):
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                for same_url in (False, True):
                    for status in ('401 Unauthorized', '403 Forbidden', '404 Not Found'):
                        with self.subTest(route=route, duration=duration, same_url=same_url, status=status):
                            args = self.args(duration, route, f'redirect-{same_url}-{status[:3]}')
                            endpoint = ('services/aigc/multimodal-generation/generation' if duration == 60
                                        else 'services/audio/asr/transcription')
                            target = ('https://dashscope.aliyuncs.com/api/v1/' + endpoint if same_url
                                      else 'https://other.aliyuncs.com/processed-result')
                            replies = [response_bytes({}, status='302 Found', headers=f'Location: {target}\r\n'),
                                       response_bytes({'error': 'synthetic redirected page unavailable'}, status=status)]
                            sockets = []

                            def connect(*a, **kw):
                                sock = SyntheticSocket(replies.pop(0), proxy=route == 'proxy')
                                sockets.append(sock)
                                return sock

                            with self.transport(route, connect):
                                with self.assertRaises(api.TranscriptionError) as caught:
                                    qwen_job.execute(args, api)
                                self.assertFalse(caught.exception.not_processed)
                                self.assertEqual(json.loads((Path(args.job_dir) / 'job.json').read_text())['status'], 'uncertain_submission')
                                with self.assertRaisesRegex(api.TranscriptionError, '结果不确定'):
                                    qwen_job.execute(args, api)
                            self.assertEqual(len(sockets), 2)
                            self.assertEqual(sum(data.startswith(b'POST ') for s in sockets for data in s.sent), 1)
                            self.assertEqual(sum(data.startswith(b'GET ') for s in sockets for data in s.sent), 1)

    def test_redirect_connect_timeout_after_post_never_resubmits(self):
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                with self.subTest(route=route, duration=duration):
                    args = self.args(duration, route, 'redirect-timeout')
                    sock = SyntheticSocket(response_bytes({}, status='302 Found',
                        headers='Location: https://other.aliyuncs.com/synthetic\r\n'), proxy=route == 'proxy')
                    calls = []

                    def connect(*a, **kw):
                        calls.append(a)
                        if len(calls) > 1:
                            raise OSError(errno.ETIMEDOUT, 'synthetic redirected connection timeout')
                        return sock

                    with self.transport(route, connect):
                        with self.assertRaises(api.TranscriptionError) as caught:
                            qwen_job.execute(args, api)
                        self.assertFalse(caught.exception.not_processed)
                        self.assertEqual(json.loads((Path(args.job_dir) / 'job.json').read_text())['status'], 'uncertain_submission')
                        with self.assertRaisesRegex(api.TranscriptionError, '结果不确定'):
                            qwen_job.execute(args, api)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(sum(data.startswith(b'POST ') for data in sock.sent), 1)

    def test_initial_http_rejection_remains_retryable(self):
        for route in ('direct', 'proxy'):
            for duration in (60, 400):
                for status in ('401 Unauthorized', '403 Forbidden'):
                    with self.subTest(route=route, duration=duration, status=status):
                        args = self.args(duration, route, f'initial-{status[:3]}')
                        successes = [self.short] if duration == 60 else [{'output': {'task_id': 'synthetic-task'}}, self.raw]
                        replies = [response_bytes({'error': 'synthetic rejected key'}, status=status)] + [
                            response_bytes(r) for r in successes]
                        sockets = []

                        def connect(*a, **kw):
                            sock = SyntheticSocket(replies.pop(0), proxy=route == 'proxy')
                            sockets.append(sock)
                            return sock

                        with self.transport(route, connect) as mocks:
                            with self.assertRaises(api.TranscriptionError) as caught:
                                qwen_job.execute(args, api)
                            self.assertTrue(caught.exception.not_processed)
                            self.assertEqual(json.loads((Path(args.job_dir) / 'job.json').read_text())['status'], 'failed')
                            self.assertEqual(qwen_job.execute(args, api)['text'], '合成测试')
                            self.assertEqual(mocks[7].call_count, int(duration == 400))
                        self.assertEqual(sum(data.startswith(b'POST ') for s in sockets for data in s.sent), 2)

    def test_normal_get_redirect_download_still_works(self):
        for route in ('direct', 'proxy'):
            with self.subTest(route=route):
                replies = [response_bytes({}, status='302 Found',
                    headers='Location: https://result.aliyuncs.com/redirected\r\n'), response_bytes(self.raw)]
                sockets = []

                def connect(*a, **kw):
                    sock = SyntheticSocket(replies.pop(0), proxy=route == 'proxy')
                    sockets.append(sock)
                    return sock

                with self.transport(route, connect):
                    self.assertEqual(api.request_json('https://result.aliyuncs.com/first'), self.raw)
                self.assertEqual(sum(data.startswith(b'GET ') for s in sockets for data in s.sent), 2)
                self.assertFalse(any(data.startswith(b'POST ') for s in sockets for data in s.sent))

    def test_send_cannot_silently_reconnect_after_checked_connection(self):
        original_request = http.client.HTTPConnection.request

        def lose_socket(connection, *args, **kwargs):
            self.assertFalse(connection.auto_open)
            connection.sock = None
            return original_request(connection, *args, **kwargs)

        for route in ('direct', 'proxy'):
            with self.subTest(route=route):
                sock = SyntheticSocket(response_bytes({}), proxy=route == 'proxy')
                with self.transport(route, lambda *a, **k: sock) as mocks, \
                     patch.object(http.client.HTTPConnection, 'request', new=lose_socket):
                    with self.assertRaises(http.client.NotConnected):
                        api.request_json('https://dashscope.aliyuncs.com/synthetic', method='POST', payload={})
                    self.assertEqual(mocks[3].call_count, 1)
                self.assertFalse(any(data.startswith(b'POST ') for data in sock.sent))

    def test_plain_http_uses_same_checked_connection_with_direct_and_proxy_routing(self):
        for route in ('direct', 'proxy'):
            with self.subTest(route=route):
                sock = SyntheticSocket(response_bytes({'synthetic': True}))
                calls = []

                def connect(address, *a, **kw):
                    calls.append(address)
                    if len(calls) == 1:
                        raise ConnectionRefusedError(errno.ECONNREFUSED, 'synthetic HTTP refusal')
                    return sock

                with self.transport(route, connect):
                    with self.assertRaises(api.TranscriptionError) as caught:
                        api.request_json('http://dashscope.aliyuncs.com/synthetic', method='POST', payload={})
                    self.assertTrue(caught.exception.not_processed)
                    self.assertEqual(api.request_json('http://dashscope.aliyuncs.com/synthetic', method='POST', payload={}),
                                     {'synthetic': True})
                self.assertEqual(calls[0][0], 'proxy.invalid' if route == 'proxy' else 'dashscope.aliyuncs.com')
                self.assertEqual(sum(data.startswith(b'POST ') for data in sock.sent), 1)

    def test_disconnect_message_cannot_claim_preconnection_failure(self):
        # A string carried by URLError supplies no proof of where failure occurred.
        for message in ('Name or service not known', 'Connection refused', 'No route to host'):
            with self.subTest(message=message), patch.object(api.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = api.urllib.error.URLError(message)
                with self.assertRaises(api.TranscriptionError) as caught:
                    api.request_json('https://dashscope.aliyuncs.com/synthetic', method='POST', payload={})
                self.assertFalse(caught.exception.not_processed)


if __name__ == '__main__':
    unittest.main()
