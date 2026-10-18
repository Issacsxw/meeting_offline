"""Local engines are optional; these tests stand in for them with fakes that follow their documented APIs."""
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transcribe_local as local
import prepare_speakers


def module(name, **attrs):
    fake = types.ModuleType(name)
    fake.__spec__ = importlib.machinery.ModuleSpec(name, None)
    fake.__dict__.update(attrs)
    return fake


class FakeAutoModel:
    calls = []

    def __init__(self, **kwargs):
        FakeAutoModel.calls.append(('init', kwargs))

    def generate(self, input, **kwargs):
        FakeAutoModel.calls.append(('generate', input, kwargs))
        return [{'key': 'a', 'text': '...', 'sentence_info': [
            {'text': '今天主要对一下上线节奏。', 'start': 0, 'end': 2400, 'spk': 0},
            {'text': '打样还在等。', 'start': 2600, 'end': 3900, 'spk': 1},
            {'text': ' ', 'start': 3900, 'end': 4000, 'spk': 1},
            {'text': '那毛利十六号前算完。', 'start': 4200, 'end': 6000, 'spk': 0}]}]


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'pc'})
        self.env.start()
        self.audio = base / '周会.wav'; self.audio.write_bytes(b'RIFF0000WAVE')
        FakeAutoModel.calls = []

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def fake_funasr(self):
        torch = module('torch', cuda=types.SimpleNamespace(is_available=lambda: False))
        return mock.patch.dict(sys.modules, {'funasr': module('funasr', AutoModel=FakeAutoModel), 'torch': torch})

    def test_check_reports_missing_engines_with_install_command(self):
        with mock.patch.object(local.importlib.util, 'find_spec', return_value=None):
            report = local.check()
        self.assertFalse(report['engines']['funasr']['ready'])
        self.assertIn(sys.executable, report['engines']['whisper']['install'])
        self.assertIn('faster-whisper sherpa-onnx', report['engines']['whisper']['install'])
        with mock.patch.object(local.importlib.util, 'find_spec', return_value=None), \
                self.assertRaisesRegex(local.LocalError, '尚未安装.*pip install'):
            local.transcribe(self.audio, 'funasr')

    def test_funasr_result_matches_qwen_shape_and_is_cached(self):
        with self.fake_funasr():
            result = local.transcribe(self.audio, 'funasr', speakers=2)
            again = local.transcribe(self.audio, 'funasr', speakers=2)
        self.assertEqual(result['segments'], [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 2400, 'text': '今天主要对一下上线节奏。'},
            {'speaker_id': 1, 'start_ms': 2600, 'end_ms': 3900, 'text': '打样还在等。'},
            {'speaker_id': 0, 'start_ms': 4200, 'end_ms': 6000, 'text': '那毛利十六号前算完。'}])
        self.assertEqual((result['route'], result['model'], result['speaker_separation']), ('local', 'funasr:paraformer-zh+cam++', 'voice'))
        init = FakeAutoModel.calls[0][1]
        self.assertEqual((init['vad_model'], init['punc_model'], init['spk_model'], init['device']), ('fsmn-vad', 'ct-punc', 'cam++', 'cpu'))
        self.assertEqual(FakeAutoModel.calls[1][2]['preset_spk_num'], 2)
        self.assertEqual(len([c for c in FakeAutoModel.calls if c[0] == 'generate']), 1)
        self.assertTrue(again['reused'])
        state = prepare_speakers.prepare(['1=' + result['result_path']])
        self.assertEqual([s['key'] for s in state['speakers']], ['1-A', '1-B'])

    def test_hotwords_reach_funasr_and_change_the_cache_key(self):
        words = Path(self.tmp.name) / 'hotwords.json'
        words.write_text(json.dumps({'有效量房': 1, '贝联': 2, '净草签': 2, 'Model Studio': 1}, ensure_ascii=False), encoding='utf-8')
        loaded = local.load_hotwords(words)
        # Heavier words first, then file order (engines cut long lists); terms with spaces are skipped.
        self.assertEqual(loaded, ['贝联', '净草签', '有效量房'])
        with self.fake_funasr():
            plain = local.transcribe(self.audio, 'funasr')
            biased = local.transcribe(self.audio, 'funasr', hotwords=loaded)
        self.assertFalse(biased['reused'])
        self.assertNotEqual(plain['result_path'], biased['result_path'])
        self.assertEqual(biased['hotwords_used'], ['贝联', '净草签', '有效量房'])
        self.assertEqual(FakeAutoModel.calls[-1][2]['hotword'], '贝联 净草签 有效量房')
        self.assertNotIn('hotword', FakeAutoModel.calls[1][2])
        # Without hotwords the cache name is the one earlier versions used, so their results are still found.
        self.assertEqual(local.cache_path('a' * 64, 'funasr', 'paraformer-zh').name, 'a' * 24 + '-funasr-paraformer-zh-auto.json')
        with self.assertRaisesRegex(local.LocalError, '热词'):
            words.write_text('["贝联"]', encoding='utf-8'); local.load_hotwords(words)

    def test_speaker_count_is_part_of_the_cache_key(self):
        with self.fake_funasr():
            local.transcribe(self.audio, 'funasr')
            self.assertFalse(local.transcribe(self.audio, 'funasr', speakers=3)['reused'])

    def test_assign_speakers_by_overlap_and_merges_runs(self):
        pieces = [(0.0, 0.5, '今天'), (0.5, 1.0, '开会。'), (1.2, 1.6, '我'), (1.6, 2.0, '同意'), (2.0, 2.4, '。'),
                  (3.0, 3.2, '对'), (5.0, 5.2, '好')]
        turns = [(0.0, 1.1, 0), (1.1, 2.5, 1)]
        self.assertEqual(local.assign_speakers(pieces, turns), [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 1000, 'text': '今天开会。'},
            {'speaker_id': 1, 'start_ms': 1200, 'end_ms': 2400, 'text': '我同意。'},
            {'speaker_id': 1, 'start_ms': 3000, 'end_ms': 3200, 'text': '对'},      # half a second after the turn: theirs
            {'speaker_id': None, 'start_ms': 5000, 'end_ms': 5200, 'text': '好'}])  # 2.5 s away: nobody's
        self.assertTrue(all(s['speaker_id'] is None for s in local.assign_speakers(pieces, [])))
        long = [(i * 1.0, i * 1.0 + 1, '字') for i in range(40)]
        self.assertTrue(all(s['end_ms'] - s['start_ms'] <= local.MAX_SEGMENT_MS for s in local.assign_speakers(long, [(0, 40, 0)])))

    def whisper(self, turns=((0, 2.1, 0), (2.1, 4, 1)), fail=None, model_error=None):
        """Fakes for faster-whisper and sherpa-onnx; counts transcription runs."""
        word = lambda start, end, text: types.SimpleNamespace(start=start, end=end, word=text)
        segments = [types.SimpleNamespace(start=0, end=2, text='x', words=[word(0, 1, '先看'), word(1, 2, '预算。')]),
                    types.SimpleNamespace(start=2, end=4, text='x', words=[word(2.2, 3, '可以'), word(3, 4, '。')])]
        runs = self.runs = []
        class Whisper:
            def __init__(self, model, **kwargs): pass
            def transcribe(self, samples, **kwargs): runs.append(kwargs); return iter(segments), None
        turn = lambda start, end, speaker: types.SimpleNamespace(start=start, end=end, speaker=speaker)
        class Result:
            def sort_by_start_time(self): return [turn(*t) for t in turns]
        class Config:
            def __init__(self, **kwargs): pass
            def validate(self): return True
        def diarize(config):
            if fail:
                raise RuntimeError(fail)
            return types.SimpleNamespace(process=lambda samples: Result())
        sherpa = module('sherpa_onnx', OfflineSpeakerDiarizationConfig=Config,
                        OfflineSpeakerSegmentationModelConfig=lambda **k: k, OfflineSpeakerSegmentationPyannoteModelConfig=lambda **k: k,
                        SpeakerEmbeddingExtractorConfig=lambda **k: k, FastClusteringConfig=lambda **k: k, OfflineSpeakerDiarization=diarize)
        fw = module('faster_whisper', WhisperModel=Whisper, decode_audio=lambda path, sampling_rate: [0.0] * 16000)
        def model_file(url, folder, member=None):
            if model_error:
                raise local.LocalError(model_error)
            return Path(folder) / 'm.onnx'
        return mock.patch.dict(sys.modules, {'faster_whisper': fw, 'sherpa_onnx': sherpa}), mock.patch.object(local, 'model_file', side_effect=model_file)

    def test_separation_that_finds_nobody_is_reported_as_none(self):
        modules, models = self.whisper(turns=())
        with modules, models:
            result = local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual((result['speaker_separation'], result['speakers_detected'], result['speaker_coverage']), ('none', 0, 0.0))
        self.assertTrue(all(s['speaker_id'] is None for s in result['segments']))
        state = prepare_speakers.prepare(['1=' + result['result_path']])
        self.assertEqual(state['sources'][0]['separation'], {'status': 'none', 'coverage': 0.0, 'speakers': 0})
        self.assertEqual([(s['key'], s['diarization_unassigned']) for s in state['speakers']], [('1-UNASSIGNED', True)])

    def test_partly_separated_speech_is_reported_with_its_coverage(self):
        modules, models = self.whisper(turns=((0, 1.0, 0),))  # nobody within a second of the second half
        with modules, models:
            result = local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual((result['speaker_separation'], result['speakers_detected']), ('partial', 1))
        self.assertAlmostEqual(result['speaker_coverage'], 5 / 8, places=3)  # 先看预算。 of 先看预算。可以。

    def test_separation_models_are_fetched_before_the_long_transcription(self):
        modules, models = self.whisper(model_error='下载说话人分离模型失败')
        with modules, models, self.assertRaisesRegex(local.LocalError, '下载'):
            local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual(self.runs, [])
        modules, models = self.whisper(model_error='下载说话人分离模型失败')
        with modules, models:  # with consent, the text is still useful
            self.assertEqual(local.transcribe(self.audio, 'whisper', model='small', allow_unseparated=True)['speaker_separation'], 'none')
        modules, models = self.whisper()
        with modules, models:  # once the models download, a normal run separates instead of reusing that result
            result = local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual((result['speaker_separation'], self.runs), ('voice', []))

    def test_a_failed_separation_keeps_the_transcription_for_the_next_run(self):
        modules, models = self.whisper(fail='onnx runtime error')
        with modules, models, self.assertRaisesRegex(local.LocalError, '转写文字已保存'):
            local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual(len(self.runs), 1)
        modules, models = self.whisper()
        with modules, models:
            result = local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual(self.runs, [])  # transcription came from the checkpoint
        self.assertEqual(result['speaker_separation'], 'voice')

    def test_a_damaged_cache_is_set_aside_and_recomputed(self):
        with self.fake_funasr():
            first = local.transcribe(self.audio, 'funasr')
            Path(first['result_path']).write_text('{"segments": [', encoding='utf-8')  # an interrupted write
            again = local.transcribe(self.audio, 'funasr')
        self.assertFalse(again['reused'])
        self.assertTrue(Path(first['result_path'] + '.damaged').exists())
        self.assertEqual(len([c for c in FakeAutoModel.calls if c[0] == 'generate']), 2)
        Path(first['result_path']).write_text('{"model": "x"}', encoding='utf-8')  # readable, but not a result
        with self.fake_funasr():
            self.assertFalse(local.transcribe(self.audio, 'funasr')['reused'])

    def test_old_caches_get_an_honest_status(self):
        with self.fake_funasr():
            first = local.transcribe(self.audio, 'funasr')
        data = json.loads(Path(first['result_path']).read_text(encoding='utf-8'))
        data.update(speaker_separation='voice', segments=[{**s, 'speaker_id': None} for s in data['segments']])
        Path(first['result_path']).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.fake_funasr():
            self.assertEqual(local.transcribe(self.audio, 'funasr')['speaker_separation'], 'none')

    def test_legacy_whisper_assignments_are_rebuilt_from_checkpoints(self):
        modules, models = self.whisper(turns=((0, 1.0, 0),))
        with modules, models:
            first = local.transcribe(self.audio, 'whisper')
        cached = Path(first['result_path'])
        old = json.loads(cached.read_text(encoding='utf-8'))
        old.pop('speaker_assignment_version', None)
        # Before the bounded-pause fix, even distant words were assigned to the nearest turn.
        old['segments'] = [{**s, 'speaker_id': 0} for s in old['segments']]
        old['speaker_separation'] = 'voice'
        cached.write_text(json.dumps(old), encoding='utf-8')
        modules, models = self.whisper()
        with modules, models:
            repaired = local.transcribe(self.audio, 'whisper')
        self.assertFalse(repaired['reused'])
        self.assertEqual(repaired['speaker_separation'], 'partial')
        self.assertIsNone(repaired['segments'][-1]['speaker_id'])
        self.assertEqual(self.runs, [])  # saved text and raw turns are still useful
        modules, models = self.whisper()
        with modules, models:
            self.assertTrue(local.transcribe(self.audio, 'whisper')['reused'])

    def test_force_also_ignores_whisper_stage_checkpoints(self):
        modules, models = self.whisper()
        with modules, models:
            local.transcribe(self.audio, 'whisper')
        modules, models = self.whisper(turns=((0, 4, 3),))
        with modules, models:
            result = local.transcribe(self.audio, 'whisper', force=True)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual({s['speaker_id'] for s in result['segments']}, {3})

    def test_structurally_damaged_cache_rows_are_set_aside(self):
        cases = [('pieces', [[0, 1]]), ('pieces', [[0, float('nan'), '字']]),
                 ('pieces', [[0, 1e308, '字']]), ('turns', [[0, 1, None]]), ('segments', []),
                 ('turns', [[0, 1, []]]), ('segments', [None]),
                 ('segments', [{'start_ms': 0, 'end_ms': 1, 'text': []}])]
        for index, (needs, rows) in enumerate(cases):
            with self.subTest(needs=needs, rows=rows):
                path = Path(self.tmp.name) / f'bad-{index}.json'
                local.save_json(path, {needs: rows})
                self.assertIsNone(local.load_json(path, needs))
                self.assertTrue(path.with_name(path.name + '.damaged').exists())

    def test_damaged_foreign_cache_is_refused_without_moving_it(self):
        path = Path(self.tmp.name) / 'foreign.json'
        path.write_text(json.dumps({'device_id': 'another-device', 'segments': [None]}))
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, '其他设备'):
            local.load_json(path, 'segments')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.with_name(path.name + '.damaged').exists())

    def test_atomic_checkpoint_failure_preserves_previous_data(self):
        path = Path(self.tmp.name) / 'checkpoint.json'
        local.save_json(path, {'pieces': [[0, 1, '原文']]})
        original = path.read_bytes()
        with mock.patch.object(local.device_context.os, 'replace', side_effect=OSError('interrupted')), \
                self.assertRaises(OSError):
            local.save_json(path, {'pieces': [[0, 1, '新文']]})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('.device-*')), [])

    def test_whisper_words_get_speakers_from_diarization(self):
        word = lambda start, end, text: types.SimpleNamespace(start=start, end=end, word=text)
        segments = [types.SimpleNamespace(start=0, end=2, text='x', words=[word(0, 1, '先看'), word(1, 2, '预算。')]),
                    types.SimpleNamespace(start=2, end=4, text='x', words=[word(2.2, 3, '可以'), word(3, 4, '。')])]
        class Whisper:
            def __init__(self, model, **kwargs): self.model = model
            def transcribe(self, samples, **kwargs):
                assert kwargs['language'] == 'zh' and kwargs['word_timestamps'] and kwargs['vad_filter']
                assert kwargs.get('hotwords') in ('贝联、净草签', None) and ('hotwords' in kwargs) == Whisper.biased
                return iter(segments), None
        turn = lambda start, end, speaker: types.SimpleNamespace(start=start, end=end, speaker=speaker)
        class Result:
            def sort_by_start_time(self): return [turn(0, 2.1, 0), turn(2.1, 4, 1)]
        class Config:
            def __init__(self, **kwargs): self.kwargs = kwargs
            def validate(self): return True
        sherpa = module('sherpa_onnx', OfflineSpeakerDiarizationConfig=Config,
                        OfflineSpeakerSegmentationModelConfig=lambda **k: k, OfflineSpeakerSegmentationPyannoteModelConfig=lambda **k: k,
                        SpeakerEmbeddingExtractorConfig=lambda **k: k, FastClusteringConfig=lambda **k: k,
                        OfflineSpeakerDiarization=lambda config: types.SimpleNamespace(process=lambda samples: Result()))
        fw = module('faster_whisper', WhisperModel=Whisper, decode_audio=lambda path, sampling_rate: [0.0] * 16000)
        with mock.patch.dict(sys.modules, {'faster_whisper': fw, 'sherpa_onnx': sherpa}), \
                mock.patch.object(local, 'model_file', side_effect=lambda url, folder, member=None: Path(folder) / 'm.onnx'):
            Whisper.biased = False  # faster-whisper before 1.0 has no hotwords argument at all
            local.transcribe(self.audio, 'whisper', model='small')
            Whisper.biased = True
            result = local.transcribe(self.audio, 'whisper', model='small', hotwords=['贝联', '净草签'])
        self.assertEqual([(s['speaker_id'], s['text']) for s in result['segments']], [(0, '先看预算。'), (1, '可以。')])
        self.assertEqual(result['model'], 'whisper:small')


if __name__ == '__main__':
    unittest.main()
