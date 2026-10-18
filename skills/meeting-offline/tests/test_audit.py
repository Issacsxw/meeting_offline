"""The content check: minutes text against the words it cites, with synthetic meetings."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import device_context
import memory
import minutes_html
import workspace as w
from test_obsidian import fake_png

SAID = ['首发价我坚持 229 元，这个月不打折。', '要不再考虑一下渠道的事。', '毛利我十六号前算完，周五前给结论。', '没问题，就这么定。']


class FactTests(unittest.TestCase):
    def test_numbers_dates_and_weekdays_are_normalised(self):
        self.assertEqual(audit.cn_number('两万五'), 25000)
        self.assertLessEqual({('number', '229'), ('day', '16'), ('week', 'this-5')}, audit.facts(''.join(SAID)))
        self.assertEqual(audit.facts('三成预算，1.5万，百分之二十'), {('percent', '30'), ('number', '15000'), ('percent', '20')})
        self.assertEqual(audit.facts('一起看看三方的意见，万一不行再说'), set())
        evidence = audit.facts(SAID[2])
        self.assertTrue(audit.supported(('date', '10-16'), evidence))       # 十六号 backs “10 月 16 日”
        self.assertFalse(audit.supported(('week', 'next-5'), evidence))     # 周五 does not back 下周五
        self.assertTrue(audit.supported(('week', 'this-5'), audit.facts('这周五')))
        self.assertFalse(audit.negated('没问题，就这么定。'))
        self.assertTrue(audit.negated('这个月不打折。'))

    def test_each_kind_of_mismatch_is_caught_and_correct_paraphrase_passes(self):
        def kinds(claim, refs, **k):
            return sorted(p['kind'] for p in audit.check_claim('m', 'x', claim, refs, SAID, ['陈思远', '林悦'], **k))
        self.assertEqual(kinds('首发价定为 199 元', [0]), ['fact'])
        self.assertEqual(kinds('首发价 229 元，本月打折促销', [0]), ['negation'])
        self.assertEqual(kinds('渠道方案再议', [1], status='decided'), ['status'])
        self.assertEqual(kinds('渠道方案再议', [1], status='open'), [])
        self.assertEqual(kinds('陈思远负责渠道', [1]), ['name'])
        self.assertEqual(kinds('陈思远负责渠道', [1], allowed_names=['陈思远']), [])
        self.assertEqual(kinds('10 月 16 日前完成毛利测算，周五给结论', [2]), [])
        self.assertEqual(kinds('下周五给结论', [2]), ['fact'])
        self.assertEqual(kinds('方案已经定下来', [3], status='decided'), [])
        # ordinary meeting speech that is not a negation, and one that is
        for claim, said in [('下周给出报价', '能不能下周给？'), ('改为执行方案B', '方案A不行，换方案B'), ('不同渠道分开定价', '各个渠道分开定价')]:
            self.assertEqual(audit.check_claim('m', 'x', claim, [0], [said]), [], said)
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '本季度招人', [0], ['这个季度不招。'])], ['negation'])
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '方案通过', [0], ['我不同意，再考虑'], status='decided')], ['status'])
        self.assertEqual([p['kind'] for p in audit.check_claim('m', 'x', '预算 5 万', [0], ['预算一点五万'])], ['fact'])
        self.assertEqual(audit.facts('一百多万，五六十万，1万5，二零二六年'),
                         {('number', '1000000'), ('number', '500000'), ('number', '600000'), ('number', '15000'), ('year', '2026')})


class AuditFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': str(self.base / 'data'), 'MEETING_OFFLINE_DEVICE_ID': 'pc'})
        self.env.start()
        self.png = mock.patch.object(minutes_html, 'png_for', side_effect=fake_png); self.png.start()
        self.store = mock.patch.object(memory, 'DEFAULT_STORE', device_context.paths()['memory']); self.store.start()
        transcript = self.base / 'm.json'
        w.write(transcript, {'model': 'synthetic', 'segments': [
            {'speaker_id': i % 2, 'start_ms': i * 4000, 'end_ms': i * 4000 + 3500, 'text': t} for i, t in enumerate(SAID)]})
        speakers = [{'key': f'm-{k}', 'source_id': 'm', 'source_speaker_id': i, 'role_name': n, 'display_name': n,
                     'identity_group': 'p-' + k, 'role_confirmed': True, 'utterances': []} for i, (k, n) in enumerate([('A', '林悦'), ('B', '陈思远')])]
        w.write(self.base / 's.json', {'sources': [{'source_id': 'm', 'path': str(transcript)}], 'speakers': speakers})
        audio = self.base / 'm.m4a'; audio.write_bytes(b'a')
        self.root = w.initialize(self.base / 's.json', self.base / 'meetings' / 'm', {'m': audio})

    def tearDown(self):
        self.store.stop(); self.png.stop(); self.env.stop(); self.tmp.cleanup()

    def analysis(self, price='199', status='decided'):
        return {'sources': {'m': {'meeting_date': '2026-10-12',
            'actions': [{'task': '完成毛利测算', 'owner': 'm-A', 'owner_basis': 'self_commitment', 'deadline_text': '10 月 16 日前', 'evidence': [2]}],
            'reader': {'topic': '定价会', 'summary': f'首发价 {price} 元，渠道再议。', 'summary_evidence': [0, 1],
                       'points': [{'title': '首发价', 'text': f'定为 {price} 元，这个月不打折。', 'evidence': [0], 'status': status},
                                  {'title': '渠道', 'text': '渠道方案再考虑。', 'evidence': [1], 'status': 'open'}],
                       'todos': [{'action_index': 0, 'deadline': '下周五'}]}}}}

    def revision(self):
        return w.read(self.root / 'state.json')['revision']

    def test_publishing_waits_until_every_mismatch_is_fixed_or_settled(self):
        w.set_analysis(self.root, self.analysis(), self.revision())
        found = audit.report(self.root)
        self.assertEqual(sorted((p['where'], p['kind']) for p in found['issues']),
                         [('actions[0].deadline', 'deadline'), ('points[0]', 'fact'), ('summary', 'fact')])
        with self.assertRaisesRegex(ValueError, '还没核对完：有 3 处'):
            w.publish(self.root)
        # fixing the text makes the amount problems disappear by themselves
        w.set_analysis(self.root, self.analysis(price='229'), self.revision())
        found = audit.report(self.root)
        self.assertEqual([p['kind'] for p in found['issues']], ['deadline'])
        with self.assertRaisesRegex(ValueError, 'note|依据'):
            audit.resolve(self.root, {found['issues'][0]['id']: {'resolution': 'user_confirmed'}}, self.revision())
        with self.assertRaisesRegex(ValueError, '不存在'):
            audit.resolve(self.root, {'nope': {'resolution': 'supported', 'note': 'x'}}, self.revision())
        record = audit.resolve(self.root, {found['issues'][0]['id']: {'resolution': 'user_confirmed', 'note': '用户确认是下周五'}}, self.revision())
        self.assertEqual(record['status'], 'checked')
        w.publish(self.root)
        # any later change to the minutes needs a fresh check; settled items carry over
        revision = self.revision()
        w.save_roles(self.root, [{'key': 'm-B', 'name': '陈思远', 'role_title': '财务', 'entity_type': 'person', 'scope': '测试'}], revision)
        with self.assertRaisesRegex(ValueError, '核对之后又改过'):
            w.publish(self.root)
        self.assertEqual(audit.resolve(self.root, {}, self.revision())['status'], 'checked')
        w.publish(self.root)
        # a confirmation belongs to the wording it was given for
        w.set_analysis(self.root, self.analysis(price='229'), self.revision())
        analysis = self.analysis(price='229'); analysis['sources']['m']['reader']['todos'] = [{'action_index': 0, 'deadline': '下下周五'}]
        w.set_analysis(self.root, analysis, self.revision())
        self.assertEqual(audit.report(self.root)['unresolved'], 1)

    def test_status_is_required_and_shown_in_the_minutes(self):
        analysis = self.analysis(price='229')
        del analysis['sources']['m']['reader']['points'][1]['status']
        analysis['sources']['m']['reader']['todos'] = []
        w.set_analysis(self.root, analysis, self.revision())
        self.assertEqual([(p['where'], p['kind']) for p in audit.report(self.root)['issues']], [('points[1]', 'status')])
        with self.assertRaisesRegex(ValueError, 'status'):
            w.set_analysis(self.root, {'sources': {'m': {'reader': {'points': [{'title': 't', 'text': 'x', 'evidence': [0], 'status': 'maybe'}], 'todos': []}}}}, self.revision())
        files = w.read(self.root / 'exports' / 'latest.json')['files']['m']
        self.assertIn('### 1. 首发价　`已决定`', Path(files['md']).read_text(encoding='utf-8'))
        self.assertIn('"status": "decided"', Path(files['html']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
