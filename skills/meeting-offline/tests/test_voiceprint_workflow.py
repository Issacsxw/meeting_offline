"""Synthetic end-to-end gates; no real audio, model, or browser required."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audit
import archive_meeting
import device_context
import minutes_html
import workspace as w
import voiceprint
import test_audit as fixtures
from test_minutes_html import SeedParser


class VoiceprintWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixtures.AuditFlowTests.setUp(self)
        w.set_analysis(self.root, self.analysis(), self.revision())

    tearDown = fixtures.AuditFlowTests.tearDown
    analysis = fixtures.AuditFlowTests.analysis
    revision = fixtures.AuditFlowTests.revision

    def state(self):
        return w.read(self.root / 'state.json')

    def pending(self, matched=True):
        state = self.state()
        for s in state['speakers']:
            s['role_confirmed'] = False
            s['role_source'] = 'voice_match' if matched else 'alias'
            if matched:
                s['voice_similarity'] = .91
        w.write(self.root / 'state.json', state)
        return state

    def payload(self, mappings=None):
        state = self.state()
        return {'schema_version': 1, 'workspace_id': w.workspace_identity(self.root, state),
                'revision': state['revision'], 'source_id': 'm', 'mappings': mappings or []}

    def html(self, payload, extra=''):
        path = self.base / 'edited.html'
        safe = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e')
        path.write_text('<script type="application/json" id="meeting-speaker-edits">' + safe + '</script>' + extra,
                        encoding='utf-8')
        return path

    def test_untouched_auto_names_do_not_confirm_or_write_libraries(self):
        self.pending()
        before = (self.root / 'state.json').read_bytes()
        memory_path = Path(w.memory_path(self.state()))
        memory_before = memory_path.read_bytes() if memory_path.exists() else None
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll, mock.patch.object(w, 'confirmed_memory_upsert') as upsert:
            result = w.import_html(self.root, self.html(self.payload()), self.revision())
        self.assertEqual(result, {'revision': self.revision(), 'imported': 0})
        enroll.assert_not_called(); upsert.assert_not_called()
        self.assertEqual((self.root / 'state.json').read_bytes(), before)
        self.assertEqual(memory_path.read_bytes() if memory_path.exists() else None, memory_before)

    def test_only_explicit_mapping_is_applied_and_other_html_state_is_ignored(self):
        self.pending()
        before = self.state()
        analysis_before = (self.root / 'analysis.json').read_bytes()
        extra = '<script id="minutes-data" type="application/json">' + json.dumps({
            'revision': 999, 'analysis': {'forged': True}, 'speakers': [{'key': 'm-B', 'role_confirmed': True}],
            'audit_issues': [], 'memory_store': '/tmp/not-authorized'}) + '</script>'
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll:
            w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '确认姓名'}]), extra), self.revision())
        state = self.state()
        a, b = state['speakers']
        self.assertEqual(a['display_name'], '确认姓名'); self.assertTrue(a['role_confirmed'])
        self.assertEqual(b, before['speakers'][1])
        self.assertEqual(state['revision'], before['revision'] + 1)
        self.assertEqual((self.root / 'analysis.json').read_bytes(), analysis_before)
        self.assertEqual(state['sources'], before['sources'])
        self.assertEqual(enroll.call_args.args[2], ['m-A'])
        self.assertTrue(any(p['name'] == '确认姓名' for p in w.read(Path(w.memory_path(state)))['roles']))

    def test_import_rejects_foreign_stale_unknown_and_malicious_payloads_atomically(self):
        self.pending()
        valid = self.payload([{'key': 'm-A', 'name': '确认姓名'}])
        cases = []
        for key, value in [('workspace_id', 'foreign'), ('revision', 0), ('source_id', 'unknown'),
                           ('analysis', {}), ('role_confirmed', True), ('mappings', [{'key': 'm-A', 'name': '姓名', 'role_confirmed': True}]),
                           ('mappings', [{'key': 'unknown-A', 'name': '姓名'}]), ('mappings', [{'key': 'm-A', 'name': ''}])]:
            item = copy.deepcopy(valid); item[key] = value; cases.append(item)
        before = (self.root / 'state.json').read_bytes()
        with mock.patch.object(voiceprint, 'enroll_confirmed') as enroll, mock.patch.object(w, 'confirmed_memory_upsert') as upsert:
            for payload in cases:
                with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                    w.import_html(self.root, self.html(payload), self.revision())
                self.assertEqual((self.root / 'state.json').read_bytes(), before)
        enroll.assert_not_called(); upsert.assert_not_called()

    def test_unassigned_cannot_be_imported_even_with_legacy_identity(self):
        state = self.pending()
        state['speakers'][0].update(key='m-UNASSIGNED', diarization_unassigned=True, identity_group='old-person')
        w.write(self.root / 'state.json', state)
        with self.assertRaisesRegex(ValueError, '未分离'):
            w.import_html(self.root, self.html(self.payload([{'key': 'm-UNASSIGNED', 'name': '姓名'}])), self.revision())
        self.assertEqual(minutes_html.shown_name(state['speakers'][0]), '说话人未分离')

    def test_default_off_alias_full_html_and_confirmation_fallbacks(self):
        self.pending(matched=False)
        with mock.patch.object(device_context, 'sandbox', return_value=False), mock.patch.object(device_context, 'minutes_preferences', return_value={}):
            w.set_analysis(self.root, self.analysis(), self.revision())
            files = w.read(self.root / 'exports/latest.json')['files']['m']
            self.assertIn('html', files); self.assertIn('png', files)
            parser = SeedParser(); parser.feed(Path(files['html']).read_text())
            self.assertEqual(json.loads(parser.seed)['todos'][0]['owner'], '说话人 A')
        for sandbox, prefs in [(True, {}), (False, {'voiceprint': {'enabled': True}})]:
            with self.subTest(sandbox=sandbox), mock.patch.object(device_context, 'sandbox', return_value=sandbox), mock.patch.object(device_context, 'minutes_preferences', return_value=prefs):
                files = w.export(self.root)['files']['m']
                self.assertNotIn('html', files)
        self.pending(matched=True)
        with mock.patch.object(device_context, 'sandbox', return_value=False), mock.patch.object(device_context, 'minutes_preferences', return_value={'voiceprint': {'enabled': True}}):
            self.assertIn('html', w.export(self.root)['files']['m'])

    def test_unchanged_display_alias_reuses_profile_and_replay_is_stale(self):
        for field in ('identity_group', 'voice_profile_id'):
            with self.subTest(field=field):
                state = self.pending()
                speaker = state['speakers'][0]
                speaker.pop('identity_group', None)
                speaker.pop('voice_profile_id', None)
                speaker[field] = 'person-lin'
                w.write(self.root / 'state.json', state)
                memory_path = Path(w.memory_path(state))
                w.write(memory_path, {'schema_version': 1, 'device_id': device_context.device_id(),
                    'roles': [{'id': 'person-lin', 'name': '林悦', 'entity_type': 'person', 'scope': '测试'}],
                    'terms': [], 'history': []})
                with mock.patch.object(w, 'display_names', return_value={'林悦': '我'}), mock.patch.object(voiceprint, 'enroll_confirmed'):
                    w.export(self.root)
                    page = self.html(self.payload([{'key': 'm-A', 'name': '我'}]))
                    w.import_html(self.root, page, self.revision())
                    self.assertEqual(self.state()['speakers'][0]['identity_group'], 'person-lin')
                    self.assertEqual(len(w.read(memory_path)['roles']), 1)
                    before = memory_path.read_bytes()
                    with self.assertRaises(ValueError):
                        w.import_html(self.root, page, self.revision())
                    self.assertEqual(memory_path.read_bytes(), before)

    def test_unchanged_unknown_labels_cannot_create_person_but_rename_can(self):
        for accepted in (False, True):
            with self.subTest(accepted_unknown=accepted):
                state = self.state()
                speaker = state['speakers'][0]
                speaker.update(role_confirmed=accepted, accepted_unknown=accepted, role_source='user' if accepted else 'alias',
                               role_name=minutes_html.UNKNOWN_ROLE if accepted else '', display_name=None)
                speaker.pop('identity_group', None)
                speaker.pop('voice_profile_id', None)
                w.write(self.root / 'state.json', state)
                w.export(self.root)
                label = minutes_html.shown_name(speaker)
                before = (self.root / 'state.json').read_bytes()
                with mock.patch.object(w, 'confirmed_memory_upsert') as upsert, mock.patch.object(voiceprint, 'enroll_confirmed') as enroll:
                    with self.assertRaises(ValueError):
                        w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': label}])), self.revision())
                    upsert.assert_not_called(); enroll.assert_not_called()
                self.assertEqual((self.root / 'state.json').read_bytes(), before)
                with mock.patch.object(voiceprint, 'enroll_confirmed'):
                    w.import_html(self.root, self.html(self.payload([{'key': 'm-A', 'name': '用户填写的姓名'}])), self.revision())
                self.assertEqual(self.state()['speakers'][0]['display_name'], '用户填写的姓名')
                self.assertTrue(self.state()['speakers'][0]['role_confirmed'])

    def test_archive_rejects_direct_draft_and_forged_publication(self):
        latest = w.read(self.root / 'exports/latest.json')
        files = latest['files']['m']
        source = self.state()['sources'][0]
        audio = Path(source['audio_path'])
        before = audio.read_bytes()
        with self.assertRaisesRegex(ValueError, '--workspace'):
            archive_meeting.collect(audio, root=self.base / 'archive', html=files['html'], png=files['png'])
        w.write(self.root / 'publication.json', {'export_version': latest['version'], 'files': {}})
        with self.assertRaisesRegex(ValueError, '内容核对'):
            archive_meeting.collect_workspace(self.root, 'm', archive_root=self.base / 'archive')
        self.assertEqual(audio.read_bytes(), before)
        self.assertFalse((self.base / 'archive').exists())

    def test_user_resolution_refreshes_warning_and_binds_publishable_version(self):
        before = w.read(self.root / 'exports/latest.json')['version']
        resolutions = {p['id']: {'resolution': 'user_confirmed', 'note': '用户明确确认此处采用纪要表述'}
                       for p in audit.report(self.root)['issues']}
        self.assertTrue(resolutions)
        result = audit.resolve(self.root, resolutions, self.revision())
        latest = w.read(self.root / 'exports/latest.json')
        self.assertNotEqual(before, latest['version'])
        self.assertEqual(result['version'], latest['version'])
        parser = SeedParser(); parser.feed(Path(latest['files']['m']['html']).read_text())
        self.assertEqual(json.loads(parser.seed)['audit_issues'], [])
        self.assertTrue(audit.is_checked(self.root))
        w.publish(self.root)
        self.assertEqual(w.read(self.root / 'publication.json')['export_version'], latest['version'])

    def test_unresolved_audit_allows_html_png_but_publish_and_ai_approval_fail(self):
        self.pending()
        w.set_analysis(self.root, self.analysis(), self.revision())
        files = w.read(self.root / 'exports/latest.json')['files']['m']
        self.assertTrue(Path(files['html']).exists()); self.assertTrue(Path(files['png']).exists())
        parser = SeedParser(); parser.feed(Path(files['html']).read_text())
        issues = audit.report(self.root)['issues']
        self.assertTrue(issues); self.assertTrue(json.loads(parser.seed)['audit_issues'])
        with self.assertRaisesRegex(ValueError, '核对'):
            w.publish(self.root)
        with self.assertRaisesRegex(ValueError, 'AI 不能批准'):
            audit.resolve(self.root, {issues[0]['id']: {'resolution': 'supported', 'note': 'AI 认为没问题'}}, self.revision())
        self.assertFalse((self.root / 'publication.json').exists())


if __name__ == '__main__':
    unittest.main()
