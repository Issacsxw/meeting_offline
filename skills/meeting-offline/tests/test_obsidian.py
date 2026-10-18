"""Obsidian vault use with a synthetic vault and meeting; nothing here touches a real vault."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting
import device_context
import minutes_html
import memory
import minutes_md
import obsidian
import term_review
import workspace as w


def note(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def fake_png(html, out=None):
    out = Path(out or Path(html).with_suffix('.png')); out.write_bytes(b'\x89PNG fake')
    return {'png': str(out)}


class ObsidianTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'laptop',
                                                'HOME': str(self.base / 'home'), 'XDG_CONFIG_HOME': str(self.base / 'home' / '.config')})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        # memory.DEFAULT_STORE was fixed when the module was imported, before this test's data home.
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        self.vault = self.base / 'home' / 'Documents' / '工作库'
        (self.vault / '.obsidian').mkdir(parents=True)
        note(self.vault / '人物' / '张三.md', '---\naliases: [老张]\ntags:\n  - 同事\n---\n产品负责人\n')
        note(self.vault / '人物' / '李四.md', '# 李四\n')
        note(self.vault / '项目' / '贝联.md', '---\ntags: [项目/链家]\n---\n合作项目\n')
        note(self.vault / '日记' / '2026-10-01.md', '今天开会\n')
        note(self.vault / '会议纪要' / '旧会' / '背联事故.md', '我们自己的旧纪要，不应当成词库\n')

    def tearDown(self):
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def meeting(self):
        transcript = self.base / 'm.json'
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 3000, 'text': '这周背联的上线节奏要定下来。'},
            {'speaker_id': 0, 'start_ms': 3000, 'end_ms': 5000, 'text': '我先把方案写完。'},
            {'speaker_id': 1, 'start_ms': 5200, 'end_ms': 8000, 'text': '背联那边还在等打样。'},
            {'speaker_id': 0, 'start_ms': 8200, 'end_ms': 9000, 'text': '好的。'}]})
        audio = self.base / 'Downloads' / '新录音 3.m4a'; audio.parent.mkdir(); audio.write_bytes(b'audio-bytes')
        speakers = [{'key': 'm-' + k, 'source_id': 'm', 'source_speaker_id': i, 'role_name': name, 'display_name': name,
                     'identity_group': 'person-' + k, 'role_confirmed': True, 'utterances': []}
                    for i, (k, name) in enumerate([('A', '张三'), ('B', '李四')])]
        state = self.base / 'state.json'
        w.write(state, {'sources': [{'source_id': 'm', 'path': str(transcript)}], 'speakers': speakers})
        root = w.initialize(state, self.base / 'meetings' / 'm', {'m': audio})
        w.set_analysis(root, {'sources': {'m': {
            'meeting_date': '2026-10-16',
            'actions': [{'task': '写完背联上线方案', 'owner': 'm-A', 'owner_basis': 'self_commitment', 'evidence': [1]}],
            'reader': {'topic': '背联上线周会', 'summary': '确定背联上线节奏。', 'summary_evidence': [0],
                       'points': [{'title': '上线节奏', 'text': '本周定下背联上线节奏，打样仍在等待。', 'evidence': [0, 2]}],
                       'todos': []}}}}, 1)
        return root

    def test_use_points_archive_into_the_vault_and_vocabulary_skips_own_notes(self):
        config = self.base / 'home' / '.config' / 'obsidian' / 'obsidian.json'
        note(config, json.dumps({'vaults': {'abc': {'path': str(self.vault), 'ts': 1, 'open': True},
                                            'gone': {'path': str(self.base / 'missing')}}}))
        self.assertEqual([v['name'] for v in obsidian.discover()], ['工作库'])
        with self.assertRaisesRegex(ValueError, '不是 Obsidian 库'):
            obsidian.use(self.base / 'home')
        with self.assertRaisesRegex(ValueError, '相对路径'):
            obsidian.use(self.vault, '../外面')
        self.assertEqual(Path(obsidian.use(self.vault)['archive']), self.vault.resolve() / '会议纪要')
        self.assertEqual(device_context.paths()['archive'], self.vault.resolve() / '会议纪要')
        terms = {t['term']: t for t in obsidian.vocabulary()['terms']}
        self.assertTrue(terms['张三']['person'] and terms['老张']['note'] == '张三')
        self.assertEqual(terms['链家']['kind'], 'tag')
        self.assertNotIn('背联事故', terms)
        self.assertNotIn('2026-10-01', terms)
        out = self.base / 'hotwords.json'
        obsidian.hotwords(out)
        self.assertLessEqual({'张三', '老张', '贝联', '李四'}, set(json.loads(out.read_text(encoding='utf-8'))))
        self.assertIsNone(obsidian.status()['note'])
        # A vault that disappears (unmounted drive, renamed folder) falls back to the default archive.
        (self.vault / '.obsidian').rmdir()
        self.assertIsNone(device_context.paths()['vault'])
        self.assertIn('不存在', obsidian.status()['note'])
        declined = obsidian.off()
        self.assertTrue(declined['declined']); self.assertIsNone(declined['note']); self.assertIsNone(declined['vault'])

    def test_vault_spelling_corrects_homophones_after_one_batch_confirmation(self):
        obsidian.use(self.vault)
        root = self.meeting()
        candidates = obsidian.suggest(root)
        self.assertEqual([(c['forms'], c['suggested'], c['segments']) for c in candidates], [(['背联'], '贝联', [0, 2])])
        self.assertEqual(candidates, obsidian.suggest(root))  # stable ids: re-running updates the same card
        term_review.prepare(root, candidates, 2)
        revision = w.read(root / 'state.json')['revision']
        with self.assertRaisesRegex(ValueError, '第 2 条'):
            term_review.confirm_batch(root, [{'candidate_id': candidates[0]['id']}, {'candidate_id': 'nope'}], revision)
        self.assertEqual(w.read(root / 'corrections.json'), [])
        self.assertEqual(memory.read_store(device_context.paths()['memory'])['terms'], [])
        term_review.confirm_batch(root, [{'candidate_id': candidates[0]['id']}], revision)
        term = memory.read_store(device_context.paths()['memory'])['terms'][0]
        self.assertEqual((term['canonical'], term['aliases'], term['scope']), ('贝联', ['背联'], 'Obsidian 库「工作库」'))
        self.assertIn('批量修正清单', term['confirmation'])
        files = w.read(root / 'exports' / 'latest.json')['files']['m']
        self.assertIn('贝联那边还在等打样', Path(files['transcript_md']).read_text(encoding='utf-8'))
        self.assertEqual(obsidian.suggest(root), [])

    def test_markdown_notes_link_people_and_are_archived_with_the_recording(self):
        obsidian.use(self.vault)
        root = self.meeting()
        files = w.read(root / 'exports' / 'latest.json')['files']['m']
        minutes = Path(files['md']).read_text(encoding='utf-8')
        self.assertIn('participants:\n  - "[[张三]]"\n  - "[[李四]]"', minutes)
        self.assertIn('date: "2026-10-16"', minutes)
        self.assertIn('参会人：[[张三]]、[[李四]]', minutes)
        self.assertIn('- [ ] 写完背联上线方案（负责人：张三，期限待明确）', minutes)
        self.assertIn('![录音](新录音%203.m4a)', minutes)
        spoken = Path(files['transcript_md']).read_text(encoding='utf-8')
        self.assertIn('**张三**　`00:00:00`\n这周背联的上线节奏要定下来。我先把方案写完。\n\n**李四**　`00:00:05`', spoken)
        w.publish(root)
        result = archive_meeting.collect_workspace(root, 'm', archive_root=device_context.paths()['archive'])
        folder = Path(result['folder'])
        self.assertEqual(folder.parent, self.vault.resolve() / '会议纪要')
        self.assertEqual(sorted(p.name for p in folder.iterdir() if not p.name.startswith('.')),
                         ['会议纪要.html', '会议纪要.md', '会议纪要.png', '新录音 3.m4a', '逐字稿.md'])
        self.assertEqual([p.name for p in folder.parent.glob('.archive-index*.json')], [f'.archive-index-{device_context.device_id()[:12]}.json'])
        # A note edited in Obsidian is kept; the regenerated one is written beside it.
        Path(result['md']).write_text('我在 Obsidian 里补了一段\n', encoding='utf-8')
        revision = w.read(root / 'state.json')['revision']
        w.save_roles(root, [{'key': 'm-B', 'name': '李四', 'role_title': '供应链', 'entity_type': 'person', 'scope': '测试'}], revision)
        w.publish(root)
        again = archive_meeting.collect_workspace(root, 'm', archive_root=device_context.paths()['archive'])
        self.assertEqual(Path(result['md']).read_text(encoding='utf-8'), '我在 Obsidian 里补了一段\n')
        self.assertRegex(Path(again['md']).name, r'^会议纪要-更新版-[0-9a-f]{8}\.md$')

    def test_markdown_keeps_spoken_text_literal(self):
        document = {'topic': '周会', 'meeting_time': '', 'participants': '我等3人', 'people': [{'name': '张三', 'shown': '我'}],
                    'summary': '', 'points': [], 'todos': []}
        text = minutes_md.minutes(document, links={'张三': '张三'})
        self.assertIn('参会人：[[张三|我]]等3人', text)
        self.assertIn('本次未形成明确待办。', text)
        spoken = minutes_md.transcript('周会', [{'speaker': '我', 'start_ms': 0, 'text': '# 不是标题 [[不是链接]] <b>'}])
        self.assertIn('\\# 不是标题 \\[\\[不是链接\\]\\] \\<b\\>', spoken)


if __name__ == '__main__':
    unittest.main()
