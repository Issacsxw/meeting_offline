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
from audio_fixtures import aac_config, atom, audio_entry, descriptor, m4a, track


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

    def probe_m4a(self, data):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audio_duration.shutil, 'which', return_value=None), \
                patch.object(audio_duration.subprocess, 'run', side_effect=AssertionError('no external probe')):
            path = Path(tmp) / 'recording.m4a'
            path.write_bytes(data)
            return audio_duration.audio_channels(path)

    def test_mp4a_channel_field_without_external_probe(self):
        for channels in (1, 2, 6):
            with self.subTest(channels=channels):
                self.assertEqual(self.probe_m4a(m4a(track([audio_entry(channels)]))), [channels])

    def test_aac_config_overrides_placeholder_mp4a_field(self):
        for channels, expected in ((1, 1), (2, 2), (7, 8)):
            for padded in (False, True):
                with self.subTest(channels=channels, padded=padded):
                    data = m4a(track([audio_entry(2, aac_config(channels), padded=padded)]))
                    self.assertEqual(self.probe_m4a(data), [expected])

    def test_aac_no_sbr_sync_extension_and_quicktime_wave(self):
        data = m4a(track([audio_entry(2, aac_config(1, sync_extension=True), version=1, wave=True)]))
        self.assertEqual(self.probe_m4a(data), [1])

    def test_extended_atoms_and_size_to_end(self):
        data = m4a(track([audio_entry(1)], extended=True), extended=True)
        self.assertEqual(self.probe_m4a(data), [1])
        moov = atom(b'moov', track([audio_entry(1)]))
        self.assertEqual(self.probe_m4a(b'\0' * 4 + moov[4:]), [1])

    def test_all_audio_tracks_are_reported_and_video_is_ignored(self):
        data = m4a(track([], handler=b'vide'), track([audio_entry(1)]), track([audio_entry(2)]))
        self.assertEqual(self.probe_m4a(data), [1, 2])

    def test_unknown_audio_track_never_returns_partial_mono_layout(self):
        for unknown in (audio_entry(1, codec=b'alac'), audio_entry(0), audio_entry(1, version=2)):
            with self.subTest(unknown=unknown):
                with self.assertRaises(ValueError):
                    self.probe_m4a(m4a(track([audio_entry(1)]), track([unknown])))

    def test_unsupported_aac_layout_does_not_trust_mono_sample_header(self):
        for config in (aac_config(0), aac_config(15), aac_config(1, object_type=5),
                       aac_config(1, object_type=29), b'', b'\x12', b'\x12\x08\xff'):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    self.probe_m4a(m4a(track([audio_entry(1, config)])))

    def test_truncated_or_invalid_mp4_metadata_is_unknown(self):
        valid = m4a(track([audio_entry(1, aac_config(1))]))
        invalid = [valid[:-1], valid + b'x', b'\0\0\0\1moov',
                   atom(b'moov', atom(b'trak', atom(b'mdia', b''))),
                   m4a(track([])), m4a(track([], handler=b'vide')),
                   m4a(track([audio_entry(1)]), track([audio_entry(1)], handler=b'')),
                   m4a(track([audio_entry(1), audio_entry(2)]))]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.probe_m4a(data)

    def test_malformed_descriptors_never_fall_back_to_sample_header(self):
        for data in (b'\x03\x80\x80\x80\x80', b'\x03\x7f', b'\x03\x01\0'):
            entry = audio_entry(1)
            extension = atom(b'esds', b'\0' * 4 + data)
            entry = atom(b'mp4a', entry[8:] + extension)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.probe_m4a(m4a(track([entry])))

    def test_optional_es_descriptor_fields(self):
        decoder = bytes([0x40, 0x15]) + b'\0' * 11 + descriptor(5, aac_config(1))
        # streamDependenceFlag, URL_Flag and OCRstreamFlag are all set.
        es = b'\0\1\xe0\0\2\3url\0\3' + descriptor(4, decoder)
        entry = audio_entry(2)
        extension = atom(b'esds', b'\0' * 4 + descriptor(3, es))
        self.assertEqual(self.probe_m4a(m4a(track([atom(b'mp4a', entry[8:] + extension)]))), [1])

    def test_unsupported_mp4_uses_ffprobe_as_final_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(audio_duration.shutil, 'which', return_value='ffprobe'), \
                patch.object(audio_duration.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, '{"streams":[{"channels":2}]}')) as run:
            path = Path(tmp) / 'recording.m4a'
            path.write_bytes(m4a(track([audio_entry(1, codec=b'alac')])))
            self.assertEqual(audio_duration.audio_channels(path), [2])
            run.assert_called_once()

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
