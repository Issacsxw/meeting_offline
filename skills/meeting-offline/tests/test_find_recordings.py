import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import find_recordings as f


def m4a(path, seconds, age_days=0):
    """Minimal MP4 container whose movie header says `seconds` long."""
    mvhd = struct.pack('>I4sB3xIIII', 28, b'mvhd', 0, 0, 0, 1000, int(seconds * 1000))
    moov = struct.pack('>I4s', 8 + len(mvhd), b'moov') + mvhd
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack('>I4s4s', 16, b'ftyp', b'M4A ') + b'\0' * 4 + moov)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


class FindRecordingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)
        self.cloud = self.home / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs'

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, **kw):
        return [r['name'] for r in f.find(home=self.home, **kw)['recordings']]

    def test_lists_recent_recordings_newest_first_with_duration(self):
        m4a(self.home / 'Downloads' / '周会.m4a', 1820, age_days=2)
        m4a(self.home / 'Desktop' / '客户沟通.m4a', 610, age_days=1)
        m4a(self.cloud / '录音' / '复盘.m4a', 95)
        (self.home / 'Downloads' / '纪要.txt').write_text('x')
        result = f.find(home=self.home)
        self.assertEqual([r['name'] for r in result['recordings']], ['复盘.m4a', '客户沟通.m4a', '周会.m4a'])
        self.assertEqual([r['location'] for r in result['recordings']], ['iCloud 云盘', '桌面', '下载'])
        self.assertEqual(result['recordings'][2]['duration_seconds'], 1820)
        self.assertEqual([s['location'] for s in result['searched']], ['iCloud 云盘', '下载', '桌面'])

    def test_time_window_limit_and_depth(self):
        m4a(self.home / 'Downloads' / '旧会议.m4a', 60, age_days=90)
        for i in range(5):
            m4a(self.home / 'Downloads' / f'会议{i}.m4a', 60, age_days=i)
        m4a(self.home / 'Downloads' / 'a' / 'b' / 'c' / 'd' / 'e' / '太深.m4a', 60)
        self.assertNotIn('旧会议.m4a', self.names())
        self.assertIn('旧会议.m4a', self.names(days=120))
        self.assertEqual(self.names(limit=2), ['会议0.m4a', '会议1.m4a'])
        self.assertNotIn('太深.m4a', self.names())
        self.assertIn('太深.m4a', self.names(depth=6))

    def test_icloud_placeholders_are_listed_as_not_downloaded(self):
        placeholder = self.cloud / '.产品评审.m4a.icloud'
        placeholder.parent.mkdir(parents=True); placeholder.write_bytes(b'bplist')
        (self.cloud / '.备注.txt.icloud').write_bytes(b'bplist')
        [item] = f.find(home=self.home)['recordings']
        self.assertEqual((item['name'], item['downloaded'], item['duration_seconds']), ('产品评审.m4a', False, None))
        self.assertEqual(item['path'], str(self.cloud / '产品评审.m4a'))

    def test_skips_hidden_folders_bundles_and_archived_meetings(self):
        m4a(self.home / 'Downloads' / '.cache' / '隐藏.m4a', 60)
        m4a(self.home / 'Desktop' / 'Some.app' / '内置.m4a', 60)
        archive = self.home / 'Desktop' / '会议纪要'
        m4a(archive / '2026-10-12_周会' / '已归档.m4a', 60)
        m4a(self.home / 'Desktop' / '新录音.m4a', 60)
        self.assertEqual(f.find(home=self.home, excluded=[archive])['recordings'][0]['name'], '新录音.m4a')
        self.assertEqual(self.names(excluded=[archive]), ['新录音.m4a'])

    def test_windows_onedrive_desktop_and_extra_folders(self):
        m4a(self.home / 'OneDrive' / 'Desktop' / '桌面录音.m4a', 60)
        m4a(self.home / 'Recordings' / '会议室.wav', 0)
        self.assertEqual(self.names(), ['桌面录音.m4a'])
        self.assertIn('会议室.wav', self.names(extra=[self.home / 'Recordings']))

    def test_missing_locations_are_not_searched(self):
        self.assertEqual(f.find(home=self.home), {'searched': [], 'since': f.find(home=self.home)['since'], 'recordings': []})


if __name__ == '__main__':
    unittest.main()
