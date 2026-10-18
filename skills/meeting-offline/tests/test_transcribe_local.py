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

    def test_speaker_count_is_part_of_the_cache_key(self):
        with self.fake_funasr():
            local.transcribe(self.audio, 'funasr')
            self.assertFalse(local.transcribe(self.audio, 'funasr', speakers=3)['reused'])

    def test_assign_speakers_by_overlap_and_merges_runs(self):
        pieces = [(0.0, 0.5, '今天'), (0.5, 1.0, '开会。'), (1.2, 1.6, '我'), (1.6, 2.0, '同意'), (2.0, 2.4, '。'), (5.0, 5.2, '好')]
        turns = [(0.0, 1.1, 0), (1.1, 2.5, 1)]
        self.assertEqual(local.assign_speakers(pieces, turns), [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 1000, 'text': '今天开会。'},
            {'speaker_id': 1, 'start_ms': 1200, 'end_ms': 2400, 'text': '我同意。'},
            {'speaker_id': 1, 'start_ms': 5000, 'end_ms': 5200, 'text': '好'}])
        long = [(i * 1.0, i * 1.0 + 1, '字') for i in range(40)]
        self.assertTrue(all(s['end_ms'] - s['start_ms'] <= local.MAX_SEGMENT_MS for s in local.assign_speakers(long, [(0, 40, 0)])))

    def test_whisper_words_get_speakers_from_diarization(self):
        word = lambda start, end, text: types.SimpleNamespace(start=start, end=end, word=text)
        segments = [types.SimpleNamespace(start=0, end=2, text='x', words=[word(0, 1, '先看'), word(1, 2, '预算。')]),
                    types.SimpleNamespace(start=2, end=4, text='x', words=[word(2.2, 3, '可以'), word(3, 4, '。')])]
        class Whisper:
            def __init__(self, model, **kwargs): self.model = model
            def transcribe(self, samples, **kwargs):
                assert kwargs['language'] == 'zh' and kwargs['word_timestamps'] and kwargs['vad_filter']
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
            result = local.transcribe(self.audio, 'whisper', model='small')
        self.assertEqual([(s['speaker_id'], s['text']) for s in result['segments']], [(0, '先看预算。'), (1, '可以。')])
        self.assertEqual(result['model'], 'whisper:small')


if __name__ == '__main__':
    unittest.main()
