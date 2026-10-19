import copy
import hashlib
import importlib.machinery
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import struct
import types
import wave
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context
import voiceprint as v


class VoiceprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': self.tmp.name,
            'MEETING_OFFLINE_DEVICE_ID': 'voice-test', 'MEETING_OFFLINE_SANDBOX': '0'})
        self.env.start()
        self.root = Path(self.tmp.name)/'meeting'
        self.audio = Path(self.tmp.name)/'synthetic.wav'
        self.audio.write_bytes(b'synthetic PCM fixture')
        self.state = {'device_id': device_context.device_id(), 'sources': [
            {'source_id': 'm', 'audio_path': str(self.audio), 'audio_sha256': hashlib.sha256(self.audio.read_bytes()).hexdigest()}],
            'speakers': [self.speaker('m-A', 0, 12000)]}

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    @staticmethod
    def speaker(key, start, end):
        return {'key': key, 'source_id': 'm', 'role_confirmed': False,
                'utterances': [{'start_ms': start, 'end_ms': end}]}

    def seed(self):
        v.enable(True)
        memory = device_context.paths()['memory']
        v.atomic_write(memory, {'schema_version': 1, 'device_id': device_context.device_id(),
            'roles': [{'id': 'person-a', 'name': '甲', 'confirmation': '用户明确确认', 'confirmed_by_user': True, 'active': True}],
            'terms': [], 'history': []})
        v.atomic_write(v.location(), {'schema_version': 1, 'device_id': device_context.device_id(),
            'model': v.MODEL_ID, 'profiles': {'person-a': {'vector': [1., 0.], 'count': 1,
            'vector_sum': [1., 0.], 'sources': []}}})

    def test_default_off_no_files_or_optional_dependency(self):
        self.assertFalse(v.status()['enabled'])
        self.assertFalse(v.match_workspace(self.root, self.state)['enabled'])
        self.assertFalse(v.location().exists())
        self.assertFalse(device_context.paths()['preferences'].exists())

    def test_consent_before_install(self):
        with patch('voiceprint.subprocess.run') as run:
            with self.assertRaises(ValueError): v.enable(False, True)
            run.assert_not_called()
        self.assertFalse(device_context.paths()['preferences'].exists())
        v.enable(True)
        self.assertTrue(v.status()['enabled'])
        self.assertTrue(v.status()['consented_at'])

    def test_sandbox_disabled_even_with_previous_consent(self):
        v.enable(True)
        with patch.dict(os.environ, {'MEETING_OFFLINE_SANDBOX': '1'}):
            self.assertFalse(v.status()['enabled'])
            with self.assertRaises(ValueError): v.enable(True, True)
            self.assertFalse(v.match_workspace(self.root, self.state)['enabled'])

    def test_threshold_margin_and_one_profile(self):
        profiles = {'a': {'vector': [1., 0.]}, 'b': {'vector': [0., 1.]}}
        self.assertEqual({}, v.choose_matches({'m-A': [.7, .7]}, profiles))
        self.assertEqual({}, v.choose_matches({'m-A': [.79, -math.sqrt(1-.79**2)]}, profiles))
        matches = v.choose_matches({'m-A': [1., 0.], 'm-B': [.99, .01]}, profiles)
        self.assertEqual({}, matches)
        self.assertEqual({}, v.choose_matches({'m-A': [1., 0.]}, profiles, {'a'}))
        close = {'a': {'vector': [1., 0.]}, 'b': {'vector': [.99, .01]}}
        self.assertEqual({}, v.choose_matches({'m-A': [1., 0.]}, close))

    def test_numeric_scale_and_margin_boundary(self):
        self.assertAlmostEqual(1 / math.sqrt(2), v.normalize([1e308, 1e308])[0])
        self.assertEqual([1., 0.], v.normalize([1e-308, 0.]))
        with patch('voiceprint.cosine', side_effect=[.9, .8]):
            self.assertIn('m-A', v.choose_matches({'m-A': [1, 0]},
                          {'a': {'vector': [1, 0]}, 'b': {'vector': [0, 1]}}))

    def test_sources_match_independently_and_reservations_are_local(self):
        self.seed()
        second = self.speaker('n-A', 0, 12000); second['source_id'] = 'n'
        self.state['speakers'].append(second)
        self.state['sources'].append(dict(self.state['sources'][0], source_id='n'))
        with patch('voiceprint.extract_vector', return_value=[1., 0.]):
            self.assertEqual({'m-A', 'n-A'}, set(v.match_workspace(self.root, self.state)['matches']))
            self.state['speakers'][0].update(role_confirmed=True, identity_group='person-a')
            self.assertEqual({'n-A'}, set(v.match_workspace(self.root, self.state)['matches']))

    def test_corrupt_library_falls_back_without_replacing_file(self):
        self.seed()
        for raw in ('{broken', '[]', '{}'):
            v.location().write_text(raw)
            with self.assertRaises(ValueError): v.read_library()
            result = v.match_workspace(self.root, self.state)
            self.assertEqual('voice_library_unavailable', result['reason'])
            self.assertEqual({}, result['matches'])
            self.assertIn('m-A', result['skipped'])
            self.assertEqual(raw, v.location().read_text())

    def test_initialize_survives_corrupt_voice_library(self):
        import workspace
        self.seed(); v.location().write_text('{broken')
        transcript = Path(self.tmp.name) / 'transcript.json'
        transcript.write_text(json.dumps({'segments': [{'speaker': 'A', 'text': '合成测试',
                                                        'start_ms': 0, 'end_ms': 12000}]}))
        state = copy.deepcopy(self.state)
        state['sources'][0]['path'] = str(transcript)
        initial = Path(self.tmp.name) / 'input.json'; initial.write_text(json.dumps(state))
        with patch.object(workspace, 'snapshot'), patch.object(workspace, 'export'):
            workspace.initialize(initial, self.root, {'m': self.audio})
        saved = json.loads((self.root / 'state.json').read_text())
        self.assertTrue(saved['voiceprint_match']['enabled'])
        self.assertEqual('voice_library_unavailable', saved['voiceprint_match']['reason'])
        self.assertTrue(saved['voiceprint_match']['error'])
        self.assertTrue((self.root / 'manifest.json').is_file())
        self.assertEqual('{broken', v.location().read_text())

    def test_no_confirmed_profiles_does_not_read_voice_library(self):
        v.enable(True)
        with patch('voiceprint.read_library', side_effect=AssertionError('must not read')):
            self.assertEqual({}, v.match_workspace(self.root, self.state)['matches'])

    def test_preference_writer_shares_lock_and_merges_legacy(self):
        import obsidian
        from contextlib import nullcontext
        path = device_context.paths()['preferences']
        v.atomic_write(path, {'device_id': device_context.device_id(),
            'meeting_minutes': {'theme': 'old', 'layout': 'cards'},
            'meeting_offline': {'theme': 'new'}})
        with patch('file_lock.locked_file', return_value=nullcontext()) as lock:
            v.enable(True)
            obsidian.write_preference({'obsidian': {'declined': True}})
        self.assertEqual([((path.with_suffix('.lock'),), {}),
                          ((path.with_suffix('.lock'),), {})], lock.call_args_list)
        settings = device_context.minutes_preferences()
        self.assertEqual('new', settings['theme']); self.assertEqual('cards', settings['layout'])
        self.assertTrue(settings['voiceprint']['enabled']); self.assertTrue(settings['obsidian']['declined'])

    def test_concurrent_preferences_preserve_both_updates(self):
        import obsidian
        v.enable(True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(v.set_preference, {'enabled': True, 'consented_at': 'test'}),
                       pool.submit(obsidian.write_preference, {'obsidian': {'declined': True}})]
            for future in futures: future.result()
        settings = device_context.minutes_preferences()
        self.assertTrue(settings['voiceprint']['enabled'])
        self.assertTrue(settings['obsidian']['declined'])

    def test_batch_hash_decode_and_engine_are_cached(self):
        v.enable(True)
        v.model_path().parent.mkdir(parents=True, exist_ok=True)
        v.model_path().write_bytes(b'synthetic model')
        with wave.open(str(self.audio), 'wb') as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
            handle.writeframes(b'\x01\x00' * (24 * 16000))
        self.state['sources'][0]['audio_sha256'] = hashlib.sha256(self.audio.read_bytes()).hexdigest()
        self.state['speakers'].append(self.speaker('m-B', 12000, 24000))
        class Stream:
            def accept_waveform(self, rate, samples): pass
            def input_finished(self): pass
        class Extractor:
            def __init__(self, config): pass
            def create_stream(self): return Stream()
            def is_ready(self, stream): return True
            def compute(self, stream): return [1., 0.]
        from unittest.mock import Mock
        constructor = Mock(side_effect=Extractor)
        stub = types.SimpleNamespace(__spec__=importlib.machinery.ModuleSpec('sherpa_onnx', loader=None),
            SpeakerEmbeddingExtractorConfig=lambda **kwargs: kwargs, SpeakerEmbeddingExtractor=constructor)
        with patch.dict(sys.modules, {'sherpa_onnx': stub}), \
             patch('transcribe_local.to_wav', return_value=self.audio) as decode, \
             patch('voiceprint.hashlib.sha256', wraps=hashlib.sha256) as digest:
            vectors, skipped = v.speaker_vectors(self.root, self.state)
        self.assertEqual({'m-A', 'm-B'}, set(vectors)); self.assertEqual({}, skipped)
        constructor.assert_called_once(); decode.assert_called_once()
        # Device ID hashing also uses hashlib: audio hashing alone has no initial argument.
        self.assertEqual(1, sum(not call.args and not call.kwargs for call in digest.call_args_list))

    def test_codec_error_explains_ffmpeg_or_pcm(self):
        with v.ExtractionSession() as session, patch('voiceprint.shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Install ffmpeg'):
                session.decode(Path(self.tmp.name) / 'sample.m4a')
        v.enable(True)
        with v.ExtractionSession() as session, patch.object(session, 'engine', return_value=object()):
            with self.assertRaisesRegex(ValueError, 'PCM WAV'):
                v.extract_vector(self.audio, [(0., 12.)], session)

    def test_unassigned_excluded_in_all_paths(self):
        speaker = self.speaker('m-UNASSIGNED', 0, 20000)
        self.assertEqual([], v.reliable_segments(speaker, [speaker]))
        self.assertEqual({}, v.choose_matches({'m-UNASSIGNED': [1., 0.]}, {'a': {'vector': [1., 0.]}}))
        self.seed(); speaker.update(role_confirmed=True, identity_group='person-a')
        self.state['speakers'] = [speaker]
        with patch('voiceprint.extract_vector') as extract:
            self.assertEqual([], v.enroll_confirmed(self.root, self.state, [speaker['key']])['enrolled'])
            extract.assert_not_called()

    def test_short_segments_overlap_and_duplicate_duration(self):
        speaker = self.speaker('m-A', 0, 9000)
        self.assertEqual([], v.reliable_segments(speaker, [speaker]))
        speaker['utterances'] *= 3  # duplicated transcript cannot manufacture sufficient audio
        self.assertEqual([], v.reliable_segments(speaker, [speaker]))
        speaker = self.speaker('m-A', 0, 20000)
        other = self.speaker('m-B', 1000, 11000)
        self.assertEqual([], v.reliable_segments(speaker, [speaker, other]))
        other = self.speaker('m-UNASSIGNED', 5000, 9000)
        self.assertEqual([(0., 5.), (9., 20.)], v.reliable_segments(speaker, [speaker, other]))

    def test_reliable_segments_multiple_sources_boundaries(self):
        speaker = self.speaker('m-A', 0, 14000)
        # Concurrent timestamps in an independent recording are not overlap.
        independent = self.speaker('n-A', 0, 14000)
        independent['source_id'] = 'n'
        self.assertEqual([(0., 14.)], v.reliable_segments(speaker, [speaker, independent]))
        # Subtract same-recording speech: exactly 2 + 8 = 10 seconds qualify.
        other = self.speaker('m-B', 2000, 6000)
        self.assertEqual([(0., 2.), (6., 14.)],
                         v.reliable_segments(speaker, [speaker, other, independent]))
        # A sub-2-second remainder is discarded; total then falls below 10.
        other['utterances'][0]['start_ms'] = 1999
        self.assertEqual([], v.reliable_segments(speaker, [speaker, other]))
        speaker['utterances'] = [{'start_ms': -1, 'end_ms': 20000},
                                 {'start_ms': 0, 'end_ms': float('inf')},
                                 {'start_ms': 12000, 'end_ms': 1000}]
        self.assertEqual([], v.reliable_segments(speaker, [speaker]))

    def test_stereo_44100_original_wav_uses_correct_local_timecuts(self):
        v.enable(True)
        v.model_path().parent.mkdir(parents=True, exist_ok=True)
        v.model_path().write_bytes(b'synthetic model stub')
        audio = Path(self.tmp.name) / 'stereo.wav'
        with wave.open(str(audio), 'wb') as handle:
            handle.setnchannels(2); handle.setsampwidth(2); handle.setframerate(44100)
            # Excluded first/last seconds differ so incorrect cuts are observable.
            handle.writeframes(struct.pack('<hh', -20000, -10000) * 44100)
            handle.writeframes(struct.pack('<hh', 10000, 20000) * (10 * 44100))
            handle.writeframes(struct.pack('<hh', -20000, -10000) * 44100)
        seen = []
        class Stream:
            def accept_waveform(self, rate, samples): seen.append((rate, samples))
            def input_finished(self): pass
        class Extractor:
            def __init__(self, config): pass
            def create_stream(self): return Stream()
            def is_ready(self, stream): return True
            def compute(self, stream): return [1., 0.]
        stub = types.SimpleNamespace(__spec__=importlib.machinery.ModuleSpec('sherpa_onnx', loader=None), SpeakerEmbeddingExtractorConfig=lambda **kwargs: kwargs,
                                     SpeakerEmbeddingExtractor=Extractor)
        with patch.dict(sys.modules, {'sherpa_onnx': stub}):
            self.assertEqual([1., 0.], v.extract_vector(audio, [(1., 11.)]))
        self.assertEqual(16000, seen[0][0])
        self.assertEqual(160000, len(seen[0][1]))
        self.assertTrue(all(abs(sample - 15000 / 32768) < 1e-8 for sample in seen[0][1]))

    def test_replaced_original_audio_is_not_enrolled_or_matched(self):
        self.seed()
        self.audio.write_bytes(b'replaced recording')
        with patch('voiceprint.extract_vector') as extract:
            result = v.match_workspace(self.root, self.state)
            self.assertEqual({}, result['matches'])
            self.assertIn('changed', result['skipped']['m-A'])
            self.state['speakers'][0].update(role_confirmed=True, identity_group='person-a')
            self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
            extract.assert_not_called()

    def test_confirmation_string_without_user_marker_is_not_trusted(self):
        self.seed()
        path = device_context.paths()['memory']
        stored = json.loads(path.read_text())
        for marker in (None, False, 'true', 1):
            stored['roles'][0]['confirmed_by_user'] = marker
            v.atomic_write(path, stored)
            before = v.location().read_bytes()
            with patch('voiceprint.extract_vector') as extract:
                self.assertEqual({}, v.confirmed_profiles(self.state))
                self.assertEqual({}, v.match_workspace(self.root, self.state)['matches'])
                self.state['speakers'][0].update(role_confirmed=True, identity_group='person-a')
                self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
                extract.assert_not_called()
            self.assertEqual(before, v.location().read_bytes())
            self.state['speakers'][0]['role_confirmed'] = False

    def test_matching_never_writes_libraries_or_confirms(self):
        self.seed()
        before = v.location().read_bytes(); mem = device_context.paths()['memory'].read_bytes()
        with patch('voiceprint.extract_vector', return_value=[1., 0.]):
            result = v.match_workspace(self.root, self.state)
        self.assertIn('m-A', result['matches'])
        speaker = self.state['speakers'][0]
        self.assertFalse(speaker['role_confirmed'])
        self.assertIsNone(speaker['identity_group'])
        self.assertEqual('voice_match', speaker['role_source'])
        self.assertEqual('甲', speaker['display_name'])
        self.assertEqual(before, v.location().read_bytes())
        self.assertEqual(mem, device_context.paths()['memory'].read_bytes())
        with patch('voiceprint.extract_vector') as extract:
            self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
            extract.assert_not_called()

    def test_only_user_confirmed_profiles_enroll_and_repeat_is_idempotent(self):
        self.seed()
        speaker = self.state['speakers'][0]
        speaker.update(role_confirmed=True, identity_group='person-a')
        with patch('voiceprint.extract_vector', return_value=[1., 0.]):
            self.assertEqual(['m-A'], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
            before = v.location().read_bytes()
            self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
        self.assertEqual(before, v.location().read_bytes())
        entry = v.read_library()['profiles']['person-a']
        self.assertEqual(2, entry['count'])
        self.assertEqual('meeting', entry['sources'][0]['meeting'])
        self.assertIn('confirmed_at', entry['sources'][0])

    def test_user_correction_removes_previous_voice_assignment(self):
        self.seed()
        memory_path = device_context.paths()['memory']
        stored = json.loads(memory_path.read_text())
        stored['roles'].append({'id': 'person-b', 'name': '乙', 'confirmation': '用户确认', 'confirmed_by_user': True})
        v.atomic_write(memory_path, stored)
        # Start with no profiles, to test enrollment and correction of one real sample.
        v.forget('person-a')
        speaker = self.state['speakers'][0]
        speaker.update(role_confirmed=True, identity_group='person-a')
        with patch('voiceprint.extract_vector', return_value=[1., 0.]):
            v.enroll_confirmed(self.root, self.state, ['m-A'])
            speaker['identity_group'] = 'person-b'
            v.enroll_confirmed(self.root, self.state, ['m-A'])
        profiles = v.read_library()['profiles']
        self.assertNotIn('person-a', profiles)
        self.assertEqual(1, profiles['person-b']['count'])

    def test_atomic_write_failure_keeps_previous_library(self):
        self.seed(); before = v.location().read_bytes()
        with patch('voiceprint.os.replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError): v.atomic_write(v.location(), {'new': True})
        self.assertEqual(before, v.location().read_bytes())
        self.assertEqual([], list(v.location().parent.glob('.voiceprint-*')))

    def test_cli_match_records_zero_match_report(self):
        import workspace
        from contextlib import nullcontext
        v.enable(True)
        state = copy.deepcopy(self.state)
        with patch.object(workspace, 'locked', return_value=nullcontext()), \
             patch.object(workspace, 'read', return_value=state), \
             patch.object(workspace, 'assert_workspace'), \
             patch.object(workspace, 'commit') as commit:
            result = v.match_workspace(self.root)
        self.assertEqual('no_match', result['reason'])
        self.assertEqual(result, state['voiceprint_match'])
        commit.assert_called_once()

    def test_unknown_profile_not_enrolled(self):
        v.enable(True)
        self.state['speakers'][0].update(role_confirmed=True, identity_group='does-not-exist')
        with patch('voiceprint.extract_vector') as extract:
            self.assertEqual([], v.enroll_confirmed(self.root, self.state, ['m-A'])['enrolled'])
            extract.assert_not_called()
        self.assertFalse(v.location().exists())

    def test_forget_and_off_delete_voiceprints_only(self):
        self.seed(); mem = device_context.paths()['memory'].read_bytes()
        self.assertTrue(v.forget('person-a')['removed'])
        self.assertEqual({}, v.read_library()['profiles'])
        v.off()
        self.assertFalse(v.location().exists()); self.assertFalse(v.status()['enabled'])
        self.assertEqual(mem, device_context.paths()['memory'].read_bytes())

    def test_nonfinite_zero_and_dimension_vectors_rejected(self):
        for bad in ([0., 0.], [float('nan')], [float('inf')], [], [True]):
            with self.assertRaises(ValueError): v.normalize(bad)
        with self.assertRaises(ValueError): v.cosine([1., 0.], [1.])

    def test_match_does_not_replace_confirmed_identity(self):
        self.seed(); self.state['speakers'][0].update(role_confirmed=True, identity_group='person-a', display_name='用户名字')
        with patch('voiceprint.extract_vector') as extract:
            self.assertEqual({}, v.match_workspace(self.root, self.state)['matches']); extract.assert_not_called()
        self.assertEqual('用户名字', self.state['speakers'][0]['display_name'])


if __name__ == '__main__': unittest.main()
