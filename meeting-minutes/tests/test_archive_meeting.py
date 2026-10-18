import sys
from pathlib import Path
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting as a
import workspace as w
import test_workspace


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'archive'
        self.audio = self.base / '原录音.m4a'
        self.audio.write_bytes(b'unique-audio-1')
        self.html = self.base / 'minutes.html'
        self.html.write_text('original generated minutes')
        self.png = self.base / 'minutes.png'
        self.png.write_bytes(b'png-test-fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def test_move_then_complete_rename_and_repeat(self):
        result = a.collect(self.audio, root=self.root, topic='会议', archive_date='2026-10-18')
        self.assertFalse(self.audio.exists())
        self.assertEqual(Path(result['audio']).read_bytes(), b'unique-audio-1')
        self.assertEqual(Path(result['folder']).name, '2026-10-18（归档）_会议')
        final = a.collect(result['audio'], root=self.root, topic='产品会议', meeting_date='2026-10-12', html=self.html, png=self.png)
        self.assertFalse(Path(result['folder']).exists())
        self.assertEqual(Path(final['folder']).name, '2026-10-12_产品会议')
        repeat = a.collect(final['audio'], root=self.root, html=self.html, png=self.png)
        self.assertEqual(repeat, final)
        self.assertEqual(sorted(p.name for p in Path(final['folder']).iterdir() if not p.name.startswith('.')),
                         ['会议纪要.html', '会议纪要.png', '原录音.m4a'])

    def test_collision_keeps_both_originals(self):
        first = a.collect(self.audio, root=self.root, topic='例会', meeting_date='2026-10-12')
        self.audio.write_bytes(b'unique-audio-2')
        second = a.collect(self.audio, root=self.root, topic='例会', meeting_date='2026-10-12')
        self.assertNotEqual(first['folder'], second['folder'])
        self.assertEqual(Path(first['audio']).read_bytes(), b'unique-audio-1')
        self.assertEqual(Path(second['audio']).read_bytes(), b'unique-audio-2')

    def test_modified_html_preserved(self):
        result = a.collect(self.audio, root=self.root, html=self.html, png=self.png)
        Path(result['html']).write_text('user edited minutes')
        self.html.write_text('new generated minutes')
        updated = a.collect(result['audio'], root=self.root, html=self.html, png=self.png)
        self.assertEqual(Path(result['html']).read_text(), 'user edited minutes')
        self.assertNotEqual(result['html'], updated['html'])
        self.assertEqual(Path(updated['html']).read_text(), 'new generated minutes')

    def test_hash_mismatch_leaves_audio_in_place(self):
        with self.assertRaises(ValueError):
            a.collect(self.audio, root=self.root, expected_hash='wrong')
        self.assertTrue(self.audio.exists())
        self.assertFalse(self.root.exists())

    def test_workspace_relocation_and_stale_publication(self):
        fixture = test_workspace.WorkspaceTests()
        fixture.setUp()
        try:
            latest = w.read(fixture.root / 'exports/latest.json')
            publication = {'export_version': 'old', 'files': {'4': {
                kind: {'path': str(path), 'sha256': w.file_digest(path)}
                for kind, path in [('html', self.html), ('png', self.png)]}}}
            w.write(fixture.root / 'publication.json', publication)
            before = w.read(fixture.root / 'state.json')['sources'][0]['audio_path']
            with self.assertRaises(ValueError):
                a.collect_workspace(fixture.root, '4', archive_root=self.root)
            self.assertTrue(Path(before).exists())
            publication['export_version'] = latest['version']
            w.write(fixture.root / 'publication.json', publication)
            result = a.collect_workspace(fixture.root, '4', archive_root=self.root)
            state = w.read(fixture.root / 'state.json')
            self.assertEqual(state['sources'][0]['audio_path'], result['audio'])
            self.assertEqual(Path(result['audio']).read_bytes()[2:6], b'2345')
            self.assertFalse(Path(before).exists())
            self.assertEqual(w.read(fixture.root / 'exports/latest.json')['revision'], state['revision'])
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
