"""Term replacement must not rewrite longer words, already-correct text or English words."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import terms
import workspace as w
import term_review as tr
import test_workspace as fixtures


class ReplaceTests(unittest.TestCase):
    def test_canonical_containing_heard_is_not_doubled(self):
        self.assertEqual(terms.replace('示例普罗和普罗', '普罗', '示例普罗'), '示例普罗和示例普罗')

    def test_longer_known_form_is_shielded(self):
        text = '王总说王总监下周到'
        self.assertEqual(terms.replace(text, '王总', '王宗', {'王总监'}), '王宗说王总监下周到')
        self.assertEqual(terms.replace(text, '王总', '王宗'), '王宗说王宗监下周到')  # unknown long forms stay a limit

    def test_ascii_words_need_boundaries(self):
        self.assertEqual(terms.replace('Pro 版和 Product 和 示例Pro2', 'Pro', 'PRO'), 'PRO 版和 Product 和 示例Pro2')
        self.assertEqual(terms.replace('用Pro版', 'Pro', 'PRO'), '用PRO版')
        self.assertEqual(terms.replace('A1和A10', 'A1', 'A-1'), 'A-1和A10')

    def test_known_forms_collect_active_spellings_only(self):
        forms = terms.known_forms(
            [{'heard': '错词', 'canonical': '正词'}, {'heard': '旧', 'canonical': '新', 'active': False}],
            [{'forms': ['王总监'], 'options': [{'text': '王宗'}]}],
            [{'canonical': '示例Pro', 'aliases': ['示例普罗']}, {'canonical': '停用', 'active': False}])
        self.assertEqual(forms, {'错词', '正词', '王总监', '王宗', '示例Pro', '示例普罗'})


class WorkspaceTermTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    tearDown = fixtures.WorkspaceTests.tearDown

    def prepare(self):
        state = w.read(self.root / 'state.json')
        source = state['sources'][0]
        data = w.read(source['path'])
        data['segments'] = [
            {'speaker_id': 0, 'start_ms': 0, 'end_ms': 4000, 'text': '王总说报价周五给。'},
            {'speaker_id': 0, 'start_ms': 4000, 'end_ms': 8000, 'text': '王总监负责审批。'}]
        w.write(source['path'], data)
        source['transcript_sha256'] = w.file_digest(source['path'])
        state['speakers'][0]['core_points'] = [{'text': '王总给报价，王总监审批。'}]
        state['unconfirmed_term_candidates'] = [{'id': 'wang', 'source_id': '4', 'title': '人名',
            'forms': ['王总', '王总监'], 'scope': '测试'}]
        w.write(self.root / 'state.json', state)

    def test_card_lists_only_standalone_occurrences(self):
        self.prepare()
        card = w.public_state(self.root)['term_candidates'][0]
        self.assertEqual([[o['index'] for o in v['occurrences']] for v in card['variants']], [[0], [1]])

    def test_confirmed_short_form_leaves_longer_title_intact(self):
        self.prepare()
        tr.save(self.root, {'candidate_id': 'wang', 'decision': 'confirm', 'canonical': '王宗', 'scope': '测试',
                            'selections': [{'heard': '王总', 'segment_indices': [0], 'learn_alias': True}]}, 1)
        files = w.read(self.root / 'exports/latest.json')['files']['4']
        transcript = Path(files['transcript']).read_text(encoding='utf-8')
        self.assertIn('王宗说报价周五给。', transcript)
        self.assertIn('王总监负责审批。', transcript)
        self.assertIn('王宗给报价，王总监审批。', Path(files['minutes']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
