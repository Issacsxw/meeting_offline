"""Private insight with two synthetic meetings of the same confirmed people."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_meeting
import device_context
import insights
import memory
import minutes_html
import workspace as w
from test_obsidian import fake_png


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'laptop'})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        self.meetings = device_context.initialize()['meetings']
        self.earlier = self.meeting('earlier', '2026-10-05', '定价预沟通', [
            (0, '首发价我建议定在 229 元，不要打折。'), (1, '那毛利能保住吗？'), (0, '229 元的话毛利没问题，打折就难说了。')])
        self.root = self.meeting('now', '2026-10-12', '新品上市筹备会', [
            (0, '我还是想守住 229 元，渠道那边压力太大。'), (1, '竞品已经 199 了。'),
            (0, '如果首发 30 天后能恢复原价，199 我也能接受。'), (1, '好，我来做毛利测算。')])

    def tearDown(self):
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def meeting(self, name, date, topic, lines):
        transcript = self.base / (name + '.json')
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': who, 'start_ms': i * 30000, 'end_ms': i * 30000 + 20000, 'text': text} for i, (who, text) in enumerate(lines)]})
        audio = self.base / (name + '.m4a'); audio.write_bytes(name.encode())
        speakers = [{'key': f'{name}-{k}', 'source_id': 'm', 'source_speaker_id': i, 'role_name': person, 'display_name': person,
                     'identity_group': 'person-' + k, 'role_confirmed': True, 'utterances': []}
                    for i, (k, person) in enumerate([('A', '林悦'), ('B', '陈思远')])]
        state = self.base / (name + '-state.json')
        w.write(state, {'sources': [{'source_id': 'm', 'path': str(transcript)}], 'speakers': speakers})
        root = w.initialize(state, self.meetings / name, {'m': audio})
        w.set_analysis(root, {'sources': {'m': {'meeting_date': date, 'actions': [], 'reader': {
            'topic': topic, 'summary': '讨论首发定价。', 'summary_evidence': [0],
            'points': [{'title': '定价', 'text': '围绕首发价讨论。', 'evidence': [0], 'attribution': [f'{name}-A']}], 'todos': []}}}}, 1)
        return root

    def document(self):
        return {'sources': {'m': {'people': [
            {'speaker': 'now-A',
             'focus': [{'text': '希望守住 229 元首发价，可以接受有期限的降价。', 'evidence': [0, 2]}],
             'considerations': [{'text': '可能担心降价后难以恢复原价、影响渠道。', 'basis': 'inference', 'confidence': 'medium', 'evidence': [0]}],
             'shifts': [{'text': '从坚持 229 元转为接受 30 天限时 199 元。', 'basis': 'fact', 'evidence': [0, 2]}],
             'history': [{'text': '上次已明确反对打折，这次让步更大。', 'basis': 'inference', 'confidence': 'low', 'evidence': [2],
                          'past': [{'meeting': 'earlier', 'source_id': 'm', 'segment': 0}]}]},
            {'speaker': 'now-B', 'focus': [{'text': '关注与竞品的价格差，主动承担毛利测算。', 'evidence': [1, 3]}]}],
            'dynamics': [{'text': '价格分歧通过“限时降价”找到折中。', 'basis': 'inference', 'confidence': 'high',
                          'evidence': [1, 2], 'speakers': ['now-A', 'now-B']}]}}}

    def test_history_lists_earlier_meetings_of_the_same_confirmed_person(self):
        found = {p['name']: p for p in insights.history(self.root)['people']}
        earlier = found['林悦']['meetings']
        self.assertEqual([(m['meeting'], m['topic'], m['meeting_date']) for m in earlier], [('earlier', '定价预沟通', '2026-10-05')])
        self.assertEqual(earlier[0]['points'], ['定价：围绕首发价讨论。'])
        self.assertEqual([q['segment'] for q in earlier[0]['quotes']], [0, 2])
        self.assertEqual([q['segment'] for q in found['陈思远']['meetings'][0]['quotes']], [])  # "那毛利能保住吗？" is too short to quote

    def test_private_file_cites_quotes_and_stays_out_of_the_shared_minutes(self):
        exported = insights.save(self.root, self.document(), w.read(self.root / 'state.json')['revision'])
        files = exported['files']['m']
        text = Path(files['insight_md']).read_text(encoding='utf-8')
        self.assertIn('tags:\n  - "深度洞察"\n  - "私密"\nprivate: true\n', text)
        self.assertIn('## 林悦', text)
        self.assertIn('- 可能担心降价后难以恢复原价、影响渠道。（推断 · 把握中等）\n  > `00:00:00` 林悦：我还是想守住 229 元，渠道那边压力太大。', text)
        self.assertIn('（事实）\n  > `00:00:00` 林悦：我还是想守住 229 元，渠道那边压力太大。\n  > `00:01:00` 林悦：如果首发 30 天后', text)
        self.assertIn('> 2026-10-05《定价预沟通》 `00:00:00`：首发价我建议定在 229 元，不要打折。', text)
        self.assertIn('## 整体', text)
        for shared in ('html', 'md', 'transcript_md'):
            self.assertNotIn('难以恢复原价', Path(files[shared]).read_text(encoding='utf-8'), shared)
        self.assertNotIn('难以恢复原价', w.read(device_context.paths()['memory']).__repr__())
        w.publish(self.root)
        archived = archive_meeting.collect_workspace(self.root, 'm', archive_root=self.base / 'archive')
        self.assertEqual(Path(archived['insight_md']).name, '深度洞察.md')

    def test_claims_need_evidence_from_the_person_and_labelled_inferences(self):
        revision = w.read(self.root / 'state.json')['revision']
        cases = [
            (('considerations', 0, 'basis'), 'fact', '背后考虑是推断'),
            (('considerations', 0, 'confidence'), None, 'confidence'),
            (('shifts', 0, 'evidence'), [0], '前后至少两段'),
            (('focus', 0, 'evidence'), [1], '本人的发言'),
            (('focus', 0, 'text'), '性格比较强势', '不评价性格'),
            (('history', 0, 'past'), [{'meeting': 'earlier', 'source_id': 'm', 'segment': 1}], '不是同一位'),
            (('history', 0, 'past'), [{'meeting': 'missing', 'source_id': 'm', 'segment': 0}], '找不到'),
        ]
        for (kind, n, key), value, message in cases:
            document = self.document()
            item = document['sources']['m']['people'][0][kind][n]
            if value is None:
                item.pop(key)
            else:
                item[key] = value
            with self.subTest(kind=kind, key=key), self.assertRaisesRegex(ValueError, message):
                insights.save(self.root, document, revision)
        self.assertFalse((self.root / 'insights.json').exists())

    def test_history_skips_a_damaged_workspace_and_a_lost_citation_does_not_break_export(self):
        broken = self.meetings / 'broken'; broken.mkdir()
        w.write(broken / 'state.json', {'device_id': device_context.device_id(), 'sources': [{'source_id': 'm', 'path': str(self.base / 'gone.json')}],
                                        'speakers': [{'key': 'x-A', 'source_id': 'm', 'source_speaker_id': 0, 'role_name': '林悦',
                                                      'identity_group': 'person-A', 'role_confirmed': True}]})
        self.assertEqual([m['meeting'] for m in insights.history(self.root)['people'][0]['meetings']], ['earlier'])
        insights.save(self.root, self.document(), w.read(self.root / 'state.json')['revision'])
        Path(w.read(self.earlier / 'state.json')['sources'][0]['path']).unlink()  # the cited earlier transcript is gone
        files = w.export(self.root)['files']['m']
        self.assertNotIn('insight_md', files); self.assertIn('insight_error', files)

    def test_a_stale_insight_is_withdrawn_from_the_archive_unless_edited(self):
        insights.save(self.root, self.document(), w.read(self.root / 'state.json')['revision'])
        w.publish(self.root)
        archived = archive_meeting.collect_workspace(self.root, 'm', archive_root=self.base / 'archive')
        revision = w.read(self.root / 'state.json')['revision']
        w.save_roles(self.root, [{'key': 'now-A', 'name': '王五', 'entity_type': 'person', 'scope': '测试'}], revision)
        w.publish(self.root)
        again = archive_meeting.collect_workspace(self.root, 'm', archive_root=self.base / 'archive')
        self.assertNotIn('insight_md', again)
        self.assertFalse(Path(archived['insight_md']).exists())

    def test_reassigning_a_speaker_makes_the_insight_stale(self):
        insights.save(self.root, self.document(), w.read(self.root / 'state.json')['revision'])
        revision = w.read(self.root / 'state.json')['revision']
        w.save_roles(self.root, [{'key': 'now-A', 'name': '王五', 'entity_type': 'person', 'scope': '测试'}], revision)
        files = w.read(self.root / 'exports' / 'latest.json')['files']['m']
        self.assertNotIn('insight_md', files)
        self.assertIn('重新生成', files['insight_error'])


if __name__ == '__main__':
    unittest.main()
