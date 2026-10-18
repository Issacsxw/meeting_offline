"""Local audio-layout probing, with no recordings or network access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audio_duration


class AudioChannelTests(unittest.TestCase):
    def test_wav_layout_uses_standard_library_without_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audio_duration.shutil, 'which', side_effect=AssertionError('WAV needs no ffprobe')):
            for channels in (1, 2):
                path = Path(tmp) / f'{channels}.wav'
                with wave.open(str(path), 'wb') as output:
                    output.setparams((channels, 2, 8000, 0, 'NONE', 'not compressed'))
                    output.writeframes(b'\0\0' * channels * 8)
                self.assertEqual(audio_duration.audio_channels(path), [channels])

    def test_ffprobe_inspects_all_audio_streams(self):
        for counts in ([1], [2], [1, 1]):
            with self.subTest(counts=counts), patch.object(audio_duration.shutil, 'which', return_value='ffprobe'), \
                    patch.object(audio_duration.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps({'streams': [{'index': i, 'channels': n} for i, n in enumerate(counts)]}))) as run:
                self.assertEqual(audio_duration.audio_channels(Path('recording.m4a')), counts)
                args = run.call_args.args[0]
                self.assertEqual(args[args.index('-select_streams') + 1], 'a')
                self.assertIn('stream=index,channels', args)
                self.assertTrue(run.call_args.kwargs['check'])
                self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_unknown_or_invalid_layout_is_never_assumed_mono(self):
        values = ['not json', '[]', '{}', '{"streams":[]}', '{"streams":[null]}',
                  '{"streams":[{}]}', '{"streams":[{"channels":true}]}',
                  '{"streams":[{"channels":0}]}', '{"streams":[{"channels":"1"}]}']
        for value in values:
            with self.subTest(value=value), patch.object(audio_duration.shutil, 'which', return_value='ffprobe'), \
                    patch.object(audio_duration.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, value)):
                with self.assertRaisesRegex(ValueError, '声道'):
                    audio_duration.audio_channels('recording.m4a')

    def test_probe_failures_and_missing_probe_are_actionable(self):
        for failure in (OSError('missing'), subprocess.TimeoutExpired('ffprobe', 30),
                        subprocess.CalledProcessError(1, 'ffprobe')):
            with self.subTest(failure=failure), patch.object(audio_duration.shutil, 'which', return_value='ffprobe'), \
                    patch.object(audio_duration.subprocess, 'run', side_effect=failure):
                with self.assertRaisesRegex(ValueError, '未发起'):
                    audio_duration.audio_channels('recording.m4a')
        with patch.object(audio_duration.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'ffprobe'):
                audio_duration.audio_channels('recording.m4a')


if __name__ == '__main__':
    unittest.main()
