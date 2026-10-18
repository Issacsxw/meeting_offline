"""Fresh recovery regressions: cached semantic review is bound to actual user-answer input."""
from copy import deepcopy
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_minutes as a
import workspace as w
import test_auto_minutes as fixtures

class CacheBindingTests(unittest.TestCase):
    setUp = fixtures.AutoMinutesTests.setUp
    def confirmed_final(self):
        candidate = fixtures.draft(); candidate['points'][0].update(text='预算为229元。', evidence=[1])
        runner = fixtures.Runner(candidate); first = a.run(self.root, runner=runner)
        issue = first['issues'][0]
        payload = fixtures.AutoMinutesTests.payload(self, first, issue, 'confirm', '用户明确确认此条预算为229元。')
        final = a.answer(self.root, payload, runner=runner)
        self.assertEqual(final['content_state'], 'review_resolved_final')
        return final

    def test_confirmation_requires_user_actor_exact_source_and_prior_answer_revision(self):
        final = self.confirmed_final(); path = a.directory(self.root, 'M') / 'state.json'
        for key, value in [('actor', 'model'), ('source_id', 'OTHER'), ('answered_revision', final['revision'])]:
            record = deepcopy(final); record['answers'][0][key] = value; w.write(path, record)
            with self.subTest(key=key):
                self.assertIsNone(a.status(self.root)['finalization'])
                result = a.run(self.root, runner=fixtures.Runner())
                self.assertEqual(result['pipeline_state'], 'needs_review')
                self.assertIsNone(result['finalization'])

    def test_changed_answer_note_or_stale_call_hash_cannot_reuse_old_semantic_review(self):
        final = self.confirmed_final(); path = a.directory(self.root, 'M') / 'state.json'
        for target in ('answer_note', 'review_request_hash'):
            record = deepcopy(final)
            if target == 'answer_note': record['answers'][0]['note'] = '这条用户补充已改变，需要重新核对。'
            else: record['calls']['semantic_review']['input_hash'] = 'older-request'
            w.write(path, record)
            with self.subTest(target=target):
                self.assertIsNone(a.status(self.root)['finalization'])
                runner = fixtures.Runner(); result = a.run(self.root, runner=runner)
                self.assertEqual(result['pipeline_state'], 'awaiting_ai'); self.assertEqual(runner.calls, [])
                prepared = a.prepare(self.root)
                self.assertEqual(prepared['stage'], 'semantic_review')
                self.assertIsNone(a.load(self.root, 'M')['review'])
                envelope = fixtures.AutoMinutesTests.envelope(self, prepared, fixtures.review(prepared['request']))
                self.assertEqual(a.submit(self.root, envelope)['content_state'], 'review_resolved_final')

if __name__ == '__main__': unittest.main()
