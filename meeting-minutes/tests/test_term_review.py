"""All confirmations here use isolated synthetic transcripts and memory."""
import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workspace as w
import term_review as tr
import review
import test_workspace as fixtures


class TermChoiceTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    tearDown = fixtures.WorkspaceTests.tearDown

    def prepare(self):
        state = w.read(self.root / 'state.json')
        source = state['sources'][0]
        data = w.read(source['path'])
        data['segments'].extend([
            {'speaker_id': 0, 'start_ms': 5000, 'end_ms': 9000, 'text': '还有一处错词需要确认。'},
            {'speaker_id': 0, 'start_ms': 9000, 'end_ms': 12000, 'text': '这一次是正词。'}])
        w.write(source['path'], data)
        source['transcript_sha256'] = w.file_digest(source['path'])
        state['unconfirmed_term_candidates'] = [{'id': 'test-choice', 'source_id': '4',
            'title': '测试术语', 'forms': ['错词', '正词'], 'scope': '测试'}]
        w.write(self.root / 'state.json', state)
        return {'candidate_id': 'test-choice', 'decision': 'confirm', 'canonical': '正词', 'scope': '测试',
                'selections': [{'heard': '错词', 'segment_indices': [0, 1], 'learn_alias': True},
                               {'heard': '正词', 'segment_indices': [2], 'learn_alias': False}]}

    def test_cards_keep_verbatim_evidence_and_context(self):
        self.prepare()
        card = w.public_state(self.root)['term_candidates'][0]
        self.assertIsNone(card['review'])
        self.assertEqual([o['text'] for o in card['variants'][0]['occurrences']],
                         ['我来提供错词的报价。', '还有一处错词需要确认。'])
        self.assertEqual(card['variants'][1]['occurrences'][0]['context'][0]['index'], 1)
        self.assertEqual(w.read(self.store)['terms'], [])

    def test_preparation_only_accepts_real_original_forms_and_preserves_review(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, '原转写'):
            tr.prepare(self.root, [{'source_id': '4', 'forms': ['未出现的写法']}], 1)
        self.assertEqual(w.read(self.root / 'state.json')['revision'], 1)
        tr.prepare(self.root, [{'id': 'test-choice', 'source_id': '4', 'forms': ['错词', '正词'],
            'title': '待核对', 'options': [{'text': '另一候选', 'basis': '语境推测，未确认'}]}], 1)
        self.assertEqual(w.read(self.store)['terms'], [])
        tr.save(self.root, {'candidate_id': 'test-choice', 'decision': 'defer'}, 2)
        with self.assertRaisesRegex(ValueError, '已有复核'):
            tr.prepare(self.root, [{'id': 'test-choice', 'source_id': '4', 'forms': ['正词']}], 3)
        self.assertEqual(w.read(self.root / 'state.json')['revision'], 3)

    def test_confirm_batch_learns_alias_preserves_sources_and_emits_hotword(self):
        payload = self.prepare()
        source = w.read(self.root / 'state.json')['sources'][0]
        before = w.file_digest(source['path'])
        tr.save(self.root, payload, 1)
        terms = w.read(self.store)['terms']
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0]['canonical'], '正词')
        self.assertEqual(terms[0]['aliases'], ['错词'])
        self.assertTrue(terms[0]['confirmed_by_user'])
        self.assertNotIn('我来提供', json.dumps(terms, ensure_ascii=False))
        latest = w.read(self.root / 'exports/latest.json')
        data = w.read(latest['files']['4']['structured'])['transcript']
        self.assertIn('正词', data[0]['text']); self.assertIn('错词', data[0]['original_text'])
        self.assertIn('错词', Path(latest['files']['5']['transcript']).read_text())
        self.assertEqual(before, w.file_digest(source['path']))
        self.assertEqual(w.hotwords(self.root, ['正词'], self.base / 'hotwords.json'), {'正词': 2})
        with self.assertRaisesRegex(ValueError, '刷新'):
            tr.save(self.root, payload, 1)
        self.assertEqual(len(w.read(self.store)['terms']), 1)

    def test_same_spelling_can_enter_glossary_without_a_replacement(self):
        payload = self.prepare()
        payload['selections'] = [{'heard': '正词', 'segment_indices': [2], 'learn_alias': False}]
        tr.save(self.root, payload, 1)
        self.assertEqual(w.read(self.store)['terms'][0]['canonical'], '正词')
        self.assertEqual(w.read(self.root / 'corrections.json'), [])

    def test_partial_selection_never_learns_alias_or_changes_all_derived_prose(self):
        payload = self.prepare()
        payload['selections'] = [{'heard': '错词', 'segment_indices': [0], 'learn_alias': True}]
        with self.assertRaisesRegex(ValueError, '部分'):
            tr.save(self.root, payload, 1)
        self.assertEqual(w.read(self.store)['terms'], [])
        payload['selections'][0]['learn_alias'] = False
        tr.save(self.root, payload, 1)
        latest = w.read(self.root / 'exports/latest.json')
        transcript = w.read(latest['files']['4']['structured'])['transcript']
        self.assertIn('正词', transcript[0]['text']); self.assertIn('错词', transcript[1]['text'])
        self.assertIn('提供错词报价', Path(latest['files']['4']['minutes']).read_text())
        self.assertEqual(w.read(self.store)['terms'][0]['aliases'], [])
        # Reviewing the remaining occurrences later still does not make a global rule.
        payload.update(canonical='另一词', selections=[{'heard': '错词', 'segment_indices': [1], 'learn_alias': True}])
        with self.assertRaisesRegex(ValueError, '部分'):
            tr.save(self.root, payload, 2)
        payload['selections'][0]['learn_alias'] = False
        tr.save(self.root, payload, 2)
        latest = w.read(self.root / 'exports/latest.json')
        self.assertIn('提供错词报价', Path(latest['files']['4']['minutes']).read_text())
        self.assertTrue(all(not c['apply_to_derived'] for c in w.read(self.root / 'corrections.json')))

    def test_different_and_defer_record_outcome_without_confirming_words(self):
        self.prepare()
        for revision, decision in [(1, 'different'), (2, 'defer')]:
            tr.save(self.root, {'candidate_id': 'test-choice', 'decision': decision}, revision)
            self.assertEqual(w.read(self.store)['terms'], [])
            self.assertEqual(w.read(self.root / 'corrections.json'), [])
        self.assertEqual(w.public_state(self.root)['term_candidates'][0]['review']['decision'], 'defer')
        self.assertTrue((self.root / 'revisions/000002.json').exists())

    def test_invalid_and_conflicting_choices_do_not_partially_persist(self):
        payload = self.prepare()
        for changes in [{'canonical': ''}, {'selections': []}, {'candidate_id': 'missing'},
                        {'selections': [{'heard': '错词', 'segment_indices': [2], 'learn_alias': False}]}]:
            with self.assertRaises(ValueError):
                tr.save(self.root, {**payload, **changes}, 1)
            self.assertEqual(w.read(self.store)['terms'], [])
            self.assertEqual(w.read(self.root / 'state.json')['revision'], 1)
        w.confirmed_memory_upsert(self.store, 'term', {'id': 'existing-term', 'scope': '测试',
            'canonical': '其他词', 'aliases': ['错词'], 'confirmation': '测试用户确认'})
        before = self.store.read_bytes()
        with self.assertRaisesRegex(ValueError, '歧义'):
            tr.save(self.root, payload, 1)
        self.assertEqual(before, self.store.read_bytes())
        self.assertEqual(w.read(self.root / 'corrections.json'), [])

    def test_authenticated_choice_endpoint(self):
        payload = self.prepare()
        server = review.ReviewServer(self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            data = json.dumps({'revision': 1, 'choice': payload}).encode()
            headers = {'X-Meeting-Token': server.token, 'Origin': server.origin, 'Content-Type': 'application/json'}
            with opener.open(urllib.request.Request(server.origin + '/api/term-choice', data=data, headers=headers)) as r:
                self.assertTrue(json.load(r)['saved'])
            self.assertEqual(w.read(self.store)['terms'][0]['canonical'], '正词')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
