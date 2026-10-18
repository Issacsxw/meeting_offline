"""Reconstructed synthetic controller regressions. No model, sockets, or real recordings."""
from copy import deepcopy
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_minutes as a
from auto_minutes_runner import AIUnavailable, InvalidOutput, bounded_json
import device_context
import workspace as w


def draft():
    return {'source_id': 'M', 'topic': '方案讨论', 'summary': '方案仍在讨论。', 'summary_evidence': [0],
        'points': [{'title': '方案', 'text': '方案留待讨论。', 'evidence': [0], 'status': 'open', 'attribution': []}],
        'actions': [], 'decisions': [], 'open_questions': []}


def review(request):
    return {'source_hash': request['input']['source_hash'], 'candidate_hash': request['candidate_hash'],
        'items': [{'claim_id': c['id'], 'verdict': 'supported', 'reason': '合成逐证据检查，无内容的维度按不适用检查。',
            'evidence': c['evidence'], 'dimensions': c['dimensions']} for c in request['claims']],
        'source_checks': [{'segment_index': s['index'], 'verdict': 'non_material', 'claim_ids': [],
            'reason': '合成逐段检查，没有遗漏影响当前结论的关键条件。'} for s in request['input']['segments']]}


class Runner:
    provenance = {'provider': 'synthetic', 'reviewer': 'separate-test-pass', 'live_verified': False}
    def __init__(self, candidate=None, review_edit=None):
        self.candidate = draft() if candidate is None else candidate
        self.review_edit = review_edit
        self.calls = []
    def generate(self, stage, request):
        self.calls.append((stage, deepcopy(request)))
        if stage == 'draft': return deepcopy(self.candidate)
        value = review(request)
        if self.review_edit: self.review_edit(value)
        return value


class AutoMinutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='automatic-core-test-')
        self.base = Path(self.tmp.name); self.root = self.base / 'workspace'
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {'MEETING_OFFLINE_SANDBOX': '1',
            'MEETING_OFFLINE_DATA_HOME': str(self.base / 'home')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = self.base / 'memory.json'
        w.write(self.store, {'schema_version': 1, 'device_id': device_context.device_id(),
                             'roles': [], 'terms': [], 'history': []})
        self.segments = [
            {'speaker_id': 'unusual-Z', 'start_ms': 0, 'end_ms': 4000, 'text': '方案留待下次讨论。'},
            {'speaker_id': 'unusual-A', 'start_ms': 4000, 'end_ms': 8000, 'text': '预算是100元，期限待明确。'}]
        raw = self.base / 'raw.json'
        w.write(raw, {'model': 'synthetic', 'duration_seconds': 8, 'segments': self.segments})
        source = self.base / 'source.json'
        w.write(source, {'memory_store': str(self.store), 'sources': [{'source_id': 'M', 'path': str(raw)}],
            'speakers': [{'key': 'M-A', 'source_id': 'M', 'source_speaker_id': 'unusual-Z',
                'role_name': '候选甲', 'identity_group': None, 'role_confirmed': False, 'core_points': [],
                'utterances': [{'source_segment_index': 0, **self.segments[0]}]},
                {'key': 'M-B', 'source_id': 'M', 'source_speaker_id': 'unusual-A',
                'role_name': '候选乙', 'identity_group': None, 'role_confirmed': False, 'core_points': [],
                'utterances': [{'source_segment_index': 1, **self.segments[1]}]}]})
        w.initialize(source, self.root, {})

    def payload(self, record, issue, action='keep_unknown', note='该内容现阶段明确保留未知。'):
        return {'source_id': 'M', 'revision': record['revision'], 'issue_id': issue['id'],
                'claim_hash': issue['claim_hash'], 'action': action, 'note': note}

    def envelope(self, prepared, output):
        return {**{k: prepared[k] for k in ('stage', 'source_id', 'input_signature', 'revision', 'request_hash')},
            'output': output, 'provenance': {'provider': 'current-host', 'reviewer': 'separate-pass',
                                           'method': 'evidence-by-evidence'}}

    def clear_auto(self):
        path = a.directory(self.root, 'M') / 'state.json'
        if path.exists(): path.unlink()

    def pending_review(self):
        prepared = a.prepare(self.root)
        a.submit(self.root, self.envelope(prepared, draft()))
        return a.prepare(self.root)

    def test_low_risk_finishes_without_forging_roles_or_changing_manual_inputs(self):
        paths = [self.root / 'state.json', self.root / 'analysis.json', self.store]
        before = [p.read_bytes() for p in paths]; runner = Runner()
        result = a.run(self.root, runner=runner)
        self.assertEqual(('completed', 'auto_final'), (result['pipeline_state'], result['content_state']))
        self.assertEqual({'pass'}, set(result['checks'].values()))
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertFalse(result['finalization']['review_provenance']['live_verified'])
        self.assertEqual(['draft', 'semantic_review'], [stage for stage, _ in runner.calls])
        self.assertTrue(a.finalized(self.root, result, a.context(self.root)))

    def test_minimized_payload_has_explicit_speaker_mapping_and_no_candidate_names(self):
        payload = a.prepare(self.root)['request']['input']
        self.assertEqual('unusual-Z', payload['speakers'][0]['source_speaker_id'])
        self.assertIsNone(payload['speakers'][0]['confirmed_name'])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('候选甲', serialized); self.assertNotIn(str(self.base), serialized)
        self.assertEqual(self.segments[0]['text'], payload['segments'][0]['original_text'])

    def test_production_adapter_fails_closed_without_subprocess(self):
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no subprocess')):
            result = a.run(self.root)
        self.assertEqual('capability_unverified', result['error']['code'])
        self.assertEqual('awaiting_ai', result['pipeline_state']); self.assertIsNone(result['finalization'])

    def test_uncertain_call_is_not_repeated_without_explicit_retry(self):
        runner = Runner()
        with mock.patch.object(runner, 'generate', side_effect=RuntimeError('interrupted')) as call:
            first = a.run(self.root, runner=runner); second = a.run(self.root, runner=runner)
            self.assertEqual(1, call.call_count)
        self.assertEqual('uncertain_ai_result', first['error']['code'])
        self.assertEqual('awaiting_ai', second['pipeline_state'])
        self.assertEqual('completed', a.run(self.root, runner=runner, retry_ai=True)['pipeline_state'])

    def test_confirmation_resolves_only_the_specific_rule_issue(self):
        candidate = draft(); candidate['points'][0].update(text='预算为229元，5月3日交付。', evidence=[1])
        runner = Runner(candidate); first = a.run(self.root, runner=runner)
        self.assertEqual(2, len(first['issues']))
        issue = next(i for i in first['issues'] if '229' in i['detail'])
        result = a.answer(self.root, self.payload(first, issue, 'confirm', '用户明确确认预算为229元。'), runner=runner)
        self.assertEqual('needs_review', result['pipeline_state']); self.assertEqual(1, len(result['issues']))
        self.assertIn('5月3日', result['issues'][0]['detail']); self.assertIsNone(result['finalization'])

    def test_generic_continue_even_with_punctuation_cannot_resolve_an_issue(self):
        first = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))
        for note in ('继续', '继续！', 'OK.', '好', '通过。'):
            with self.subTest(note=note), self.assertRaises(ValueError):
                a.answer(self.root, self.payload(first, first['issues'][0], note=note))
        self.assertEqual([], a.load(self.root, 'M')['answers'])

    def test_answer_is_tied_to_source_revision_issue_and_claim_hash(self):
        first = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))
        for field, value in [('source_id', 'foreign'), ('revision', 999), ('revision', True),
                             ('issue_id', 'old'), ('claim_hash', 'old')]:
            payload = self.payload(first, first['issues'][0]); payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): a.answer(self.root, payload)
        self.assertEqual([], a.load(self.root, 'M')['answers'])

    def test_keep_unknown_archives_previous_revision_and_rechecks(self):
        first = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))
        runner = Runner(); result = a.answer(self.root, self.payload(first, first['issues'][0]), runner=runner)
        self.assertEqual('review_resolved_final', result['content_state'])
        self.assertEqual('本场讨论的结论仍有待明确。', result['candidate']['summary'])
        self.assertEqual('user', result['answers'][0]['actor'])
        self.assertEqual(['semantic_review'], [stage for stage, _ in runner.calls])
        self.assertTrue((a.directory(self.root, 'M') / 'revisions' / '000001.json').is_file())

    def test_default_answer_only_saves_and_waits_for_host_without_starting_model(self):
        first = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'][1].update(verdict='inconclusive')))
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')), \
             mock.patch.object(a, 'run', side_effect=AssertionError('host owns continuation')):
            result = a.answer(self.root, self.payload(first, first['issues'][0]))
        self.assertEqual(first['revision'] + 1, result['revision'])
        self.assertEqual('awaiting_host_review', result['error']['code'])
        self.assertIsNone(result['finalization']); self.assertEqual(1, len(result['answers']))

    def test_all_claims_dimensions_and_every_source_segment_are_required(self):
        edits = [lambda v: v['items'].pop(), lambda v: v['items'][0].update(dimensions=['condition']),
                 lambda v: v['source_checks'].pop(), lambda v: v.update(candidate_hash='old'),
                 lambda v: v.update(source_hash='old')]
        for edit in edits:
            self.clear_auto()
            result = a.run(self.root, runner=Runner(review_edit=edit))
            self.assertNotEqual('completed', result['pipeline_state']); self.assertIsNone(result['finalization'])

    def test_missing_critical_segments_block_with_distinct_scoped_issues(self):
        def missing(value):
            for check in value['source_checks']: check.update(verdict='missing_critical', reason='同样的缺失理由')
        result = a.run(self.root, runner=Runner(review_edit=missing))
        self.assertEqual('needs_review', result['pipeline_state']); self.assertEqual(2, len(result['issues']))
        self.assertNotEqual(result['issues'][0]['id'], result['issues'][1]['id'])
        self.assertNotEqual(result['issues'][0]['evidence'], result['issues'][1]['evidence'])

    def test_contradicted_question_blocks_but_unknown_question_can_remain_unknown(self):
        candidate = draft(); candidate['open_questions'] = [{'text': '是否仍需讨论？', 'evidence': [0]}]
        rejected = a.run(self.root, runner=Runner(candidate, lambda v: v['items'][-1].update(verdict='contradicted')))
        self.assertNotEqual('completed', rejected['pipeline_state'])
        self.clear_auto()
        result = a.run(self.root, runner=Runner(candidate, lambda v: v['items'][-1].update(verdict='inconclusive')))
        self.assertEqual('auto_final', result['content_state'])

    def test_hashes_include_title_attribution_and_owner_basis(self):
        original = draft(); original['points'][0]['attribution'] = ['M-A']
        before = next(c['hash'] for c in a.claims(original) if c['id'] == 'points[0]')
        for field, value in [('title', '已确定'), ('attribution', ['M-B'])]:
            changed = deepcopy(original); changed['points'][0][field] = value
            self.assertNotEqual(before, next(c['hash'] for c in a.claims(changed) if c['id'] == 'points[0]'))
        original['actions'] = [{'task': '继续讨论', 'evidence': [0], 'owner': 'M-A', 'owner_basis': 'self_commitment', 'deadline': None}]
        before = a.claims(original)[-1]['hash']; original['actions'][0]['owner_basis'] = 'explicit_assignment'
        self.assertNotEqual(before, a.claims(original)[-1]['hash'])

    def test_untrusted_json_types_extra_fields_duplicates_and_bounds_fail_closed(self):
        changes = [lambda c: c.update(summary_evidence=[{}]), lambda c: c.update(summary_evidence=[True]),
                   lambda c: c['points'][0].update(status=[]), lambda c: c['points'][0].update(attribution=[{}]),
                   lambda c: c.update(source_id='foreign'), lambda c: c.update(approved=True)]
        for change in changes:
            self.clear_auto(); candidate = draft(); change(candidate)
            result = a.run(self.root, runner=Runner(candidate))
            self.assertEqual('awaiting_ai', result['pipeline_state']); self.assertIsNone(result['finalization'])
        for value in ('{"verdict":"contradicted","verdict":"supported"}', '{"x":NaN}', 'x' * 1_000_001):
            with self.subTest(value=value[:50]), self.assertRaises(InvalidOutput): bounded_json(value)

    def test_effective_corrections_and_external_protected_forms_invalidate_signature(self):
        w.write(self.root / 'corrections.json', [{'id': 'x', 'source_id': 'M', 'heard': '100', 'canonical': '300',
            'scope': '本场', 'segment_indices': [1], 'active': True}])
        before = a.context(self.root)
        self.assertEqual('预算是300元，期限待明确。', before['input']['segments'][1]['text'])
        self.assertEqual(self.segments[1]['text'], before['input']['segments'][1]['original_text'])
        store = w.read(self.store); store['terms'].append({'id': 't', 'canonical': '100元', 'aliases': [], 'active': True})
        w.write(self.store, store); self.assertNotEqual(before['signature'], a.context(self.root)['signature'])

    def test_stale_result_does_not_overwrite_changed_workspace(self):
        runner = Runner(); generate = runner.generate
        def mutate(stage, request):
            state = w.read(self.root / 'state.json'); state['revision'] += 1; w.write(self.root / 'state.json', state)
            return generate(stage, request)
        runner.generate = mutate
        with self.assertRaises(a.StaleInput): a.run(self.root, runner=runner)
        self.assertIsNone(a.status(self.root)['finalization'])

    def test_corrupt_completed_checkpoint_has_no_read_authority(self):
        result = a.run(self.root, runner=Runner()); result['review']['items'].pop()
        w.write(a.directory(self.root, 'M') / 'state.json', result)
        self.assertIsNone(a.status(self.root)['finalization'])
        self.assertIsNone(a.run(self.root, runner=Runner())['finalization'])

    def test_host_prepare_submit_requires_separate_draft_and_review(self):
        prepared = a.prepare(self.root); self.assertEqual('draft', prepared['stage'])
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')):
            saved = a.submit(self.root, self.envelope(prepared, draft()))
            self.assertEqual('awaiting_host_review', saved['error']['code'])
            prepared = a.prepare(self.root); self.assertEqual('semantic_review', prepared['stage'])
            result = a.submit(self.root, self.envelope(prepared, review(prepared['request'])))
        self.assertEqual('auto_final', result['content_state'])
        self.assertEqual('host_assistant', result['finalization']['review_provenance']['provider_kind'])

    def test_wrong_host_bindings_and_draft_or_completed_replays_are_rejected(self):
        prepared = a.prepare(self.root)
        for field, value in [('revision', 88), ('request_hash', 'old'), ('input_signature', 'old'), ('stage', 'semantic_review')]:
            payload = self.envelope(prepared, draft()); payload[field] = value
            with self.subTest(field=field), self.assertRaises(a.StaleInput): a.submit(self.root, payload)
        a.submit(self.root, self.envelope(prepared, draft()))
        with self.assertRaises(a.StaleInput): a.submit(self.root, self.envelope(prepared, draft()))
        prepared = a.prepare(self.root); payload = self.envelope(prepared, review(prepared['request']))
        a.submit(self.root, payload)
        with self.assertRaises(a.StaleInput): a.submit(self.root, payload)

    def test_valid_blocked_review_cannot_be_silently_overwritten_by_new_submission(self):
        prepared = self.pending_review(); output = review(prepared['request']); output['items'][1]['verdict'] = 'inconclusive'
        result = a.submit(self.root, self.envelope(prepared, output)); self.assertEqual('needs_review', result['pipeline_state'])
        self.assertEqual('needs_review', a.prepare(self.root)['stage'])
        with self.assertRaises(a.StaleInput): a.submit(self.root, self.envelope(prepared, review(prepared['request'])))

    def test_host_request_is_bound_to_workspace_not_only_identical_transcript(self):
        prepared = a.prepare(self.root); other = self.base / 'other-workspace'; shutil.copytree(self.root, other)
        self.assertNotEqual(a.context(self.root)['signature'], a.context(other)['signature'])
        with self.assertRaises(a.StaleInput): a.submit(other, self.envelope(prepared, draft()))

    def test_invalid_review_requires_explicit_retry_and_resumes_without_redrafting(self):
        result = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'].pop()))
        self.assertEqual('invalid_output', result['error']['code']); runner = Runner()
        self.assertEqual('uncertain_ai_result', a.run(self.root, runner=runner)['error']['code']); self.assertEqual([], runner.calls)
        self.assertEqual('auto_final', a.run(self.root, runner=runner, retry_ai=True)['content_state'])
        self.assertEqual(['semantic_review'], [stage for stage, _ in runner.calls])

    def test_new_input_preserves_previous_final_and_invalidates_old_context(self):
        first = a.run(self.root, runner=Runner()); ctx = a.context(self.root)
        state = w.read(self.root / 'state.json'); state['revision'] += 1; w.write(self.root / 'state.json', state)
        self.assertFalse(a.finalized(self.root, first, ctx)); self.assertIsNone(a.status(self.root)['finalization'])
        self.assertEqual('awaiting_ai', a.run(self.root)['pipeline_state'])
        archived = w.read(a.directory(self.root, 'M') / 'revisions' / '000001.json')
        self.assertEqual(first['finalization'], archived['finalization'])

    def test_unchanged_rerun_keeps_finalization_identity_and_does_not_repeat_model(self):
        runner = Runner(); first = a.run(self.root, runner=runner); second = a.run(self.root, runner=runner)
        self.assertEqual(first['finalization'], second['finalization'])
        self.assertEqual(['draft', 'semantic_review'], [stage for stage, _ in runner.calls])

    def test_workspace_change_after_run_lock_acquisition_rejects_host_output(self):
        prepared = a.prepare(self.root); real_lock = a.locked_file; changed = [False]
        @contextmanager
        def mutate_after_lock(path, *args, **kwargs):
            with real_lock(path, *args, **kwargs) as handle:
                if Path(path).name == '.run.lock' and not changed[0]:
                    with w.locked(self.root):
                        state = w.read(self.root / 'state.json'); state['revision'] += 1
                        w.write(self.root / 'state.json', state)
                    changed[0] = True
                yield handle
        with mock.patch.object(a, 'locked_file', side_effect=mutate_after_lock), self.assertRaises(a.StaleInput):
            a.submit(self.root, self.envelope(prepared, draft()))
        self.assertIsNone(a.load(self.root, 'M')['candidate'])

    def test_concurrent_identical_host_reviews_apply_once_then_replay_is_stale(self):
        prepared = self.pending_review(); payload = self.envelope(prepared, review(prepared['request']))
        entered = threading.Event(); release = threading.Event(); results = []; real_call = a.call
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError('test continuation was not released')
            return real_call(*args, **kwargs)
        def first_submit():
            try: results.append(a.submit(self.root, payload))
            except BaseException as error: results.append(error)
        with mock.patch.object(a, 'call', side_effect=delayed):
            thread = threading.Thread(target=first_submit); thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(ValueError): a.submit(self.root, payload)
            finally:
                release.set(); thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(1, len(results)); self.assertIsInstance(results[0], dict)
        self.assertEqual('completed', results[0]['pipeline_state'])
        with self.assertRaises(a.StaleInput): a.submit(self.root, payload)

    def test_read_submission_is_bounded_and_rejects_duplicate_or_nonfinite_json(self):
        path = self.base / 'submission.json'
        path.write_bytes(b'{"safe":true}')
        self.assertEqual({'safe': True}, a.read_submission(path))
        handle = mock.MagicMock(); handle.__enter__.return_value = handle
        handle.read.return_value = b'{\"safe\":true}'
        with mock.patch.object(Path, 'open', return_value=handle):
            self.assertEqual({'safe': True}, a.read_submission(path))
        handle.read.assert_called_once_with(1_000_001)
        for raw in (b'{"x":1,"x":2}', b'{"x":Infinity}', b' ' * 1_000_001):
            path.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(InvalidOutput): a.read_submission(path)

    def test_topic_issue_can_be_resolved_without_claim_parser_failure(self):
        result = a.run(self.root, runner=Runner(review_edit=lambda v: v['items'][0].update(verdict='inconclusive')))
        result = a.answer(self.root, self.payload(result, result['issues'][0]), runner=Runner())
        self.assertEqual('会议讨论', result['candidate']['topic']); self.assertEqual('review_resolved_final', result['content_state'])


if __name__ == '__main__': unittest.main()
