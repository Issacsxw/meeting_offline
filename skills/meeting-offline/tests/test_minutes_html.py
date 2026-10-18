import json
import os
import tempfile
from unittest import mock
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workspace as w
import minutes_html
import test_workspace as fixtures
import device_context


class SeedParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.seed = ''; self.in_seed = False; self.tags = []
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == 'script' and dict(attrs).get('id') == 'minutes-data': self.in_seed = True
    def handle_endtag(self, tag):
        if tag == 'script': self.in_seed = False
    def handle_data(self, data):
        if self.in_seed: self.seed += data


class MinutesHtmlTests(unittest.TestCase):
    setUp = fixtures.WorkspaceTests.setUp
    tearDown = fixtures.WorkspaceTests.tearDown

    def analysis(self):
        return {'sources': {'4': {'title': '测试会议', 'meeting_date': None,
            'actions': [{'task': '提供报价', 'owner': '4-A', 'owner_basis': 'self_commitment', 'evidence': [0]}],
            'reader': {'topic': '简明会议', 'summary': '报价资料将用于推进项目。', 'summary_evidence': [0], 'points': [{'title': '观点', 'text': '提供报价。', 'evidence': [0], 'attribution': ['4-A']}],
                       'todos': [{'action_index': 0, 'task': '交付报价单'}]}}}}

    def test_reader_owner_tracks_confirmed_roles_and_date_is_not_invented(self):
        w.set_analysis(self.root, self.analysis(), 1)
        w.save_roles(self.root, [{'key': '4-A', 'profile_id': 'person-b'}], 2)
        output = w.read(self.root / 'exports/latest.json')
        parser = SeedParser(); parser.feed(Path(output['files']['4']['html']).read_text())
        seed = json.loads(parser.seed)
        self.assertEqual(seed['topic'], '简明会议')
        self.assertEqual(seed['meeting_time'], '')
        self.assertEqual(seed['todos'][0], {'task': '交付报价单', 'owner': '乙', 'deadline': '—', 'done': False})
        self.assertEqual(seed['participants'], '乙')
        self.assertEqual(seed['points'][0]['title'], '观点')
        self.assertEqual(seed['summary'], '报价资料将用于推进项目。')

    def seed(self):
        parser = SeedParser()
        parser.feed(Path(w.read(self.root / 'exports/latest.json')['files']['4']['html']).read_text())
        return json.loads(parser.seed)

    def test_names_come_from_profile_not_string_tricks(self):
        w.set_analysis(self.root, self.analysis(), 1)
        store = w.read(self.store)
        store['roles'].append({'id': 'person-self', 'entity_type': 'person', 'name': '用户本人', 'scope': '测试', 'role_title': '负责人'})
        w.write(self.store, store)
        w.save_roles(self.root, [{'key': '4-A', 'profile_id': 'person-self'}], 2)
        speaker = w.read(self.root / 'state.json')['speakers'][0]
        self.assertEqual((speaker['role_name'], speaker['display_name']), ('用户本人｜负责人', '用户本人'))
        seed = self.seed()
        self.assertEqual((seed['participants'], seed['todos'][0]['owner']), ('用户本人', '用户本人'))

    def test_display_names_preference_renames_in_minutes_only(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': data}):
            preferences = device_context.initialize()['preferences']
            value = w.read(preferences); value['meeting_offline']['display_names'] = {'甲': '我'}; w.write(preferences, value)
            w.set_analysis(self.root, self.analysis(), 1)
            seed = self.seed()
            self.assertEqual((seed['participants'], seed['todos'][0]['owner']), ('我', '我'))
            self.assertIn('甲', Path(w.read(self.root / 'exports/latest.json')['files']['4']['transcript']).read_text())
            value['meeting_offline']['display_names'] = {'甲': ''}; w.write(preferences, value)
            with self.assertRaisesRegex(ValueError, 'display_names'):
                w.export(self.root)

    def test_display_names_under_pre_rename_key_still_apply(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': data}):
            preferences = device_context.initialize()['preferences']
            value = w.read(preferences); value.pop('meeting_offline')
            value['meeting_minutes'] = {'display_names': {'甲': '我'}}; w.write(preferences, value)
            w.set_analysis(self.root, self.analysis(), 1)
            self.assertEqual(self.seed()['participants'], '我')

    def test_theme_comes_from_preferences_and_unknown_themes_are_rejected(self):
        self.assertEqual(self.seed_after_analysis()['theme'], 'warm')
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, {'MEETING_OFFLINE_DATA_HOME': data}):
            preferences = device_context.initialize()['preferences']
            value = w.read(preferences); value['meeting_offline']['theme'] = 'fresh'; w.write(preferences, value)
            w.export(self.root)
            self.assertEqual(self.seed()['theme'], 'fresh')
            value['meeting_offline']['theme'] = 'neon'; w.write(preferences, value)
            with self.assertRaisesRegex(ValueError, 'theme'):
                w.export(self.root)
        with self.assertRaisesRegex(ValueError, '未知样式'):
            minutes_html.render({'topic': 't', 'meeting_time': '', 'participants': '', 'points': [], 'todos': [], 'theme': 'neon'})

    def seed_after_analysis(self):
        w.set_analysis(self.root, self.analysis(), 1)
        return self.seed()

    def test_legacy_role_name_and_accepted_unknown(self):
        state = w.read(self.root / 'state.json')
        state['speakers'][0]['role_name'] = '甲｜产品'
        state['speakers'].append({'key': '4-B', 'source_id': '4', 'source_speaker_id': 1, 'role_name': '身份未确认',
            'accepted_unknown': True, 'role_confirmed': True, 'core_points': [], 'utterances': []})
        w.write(self.root / 'state.json', state)
        w.set_analysis(self.root, self.analysis(), 1)
        seed = self.seed()
        self.assertEqual((seed['participants'], seed['todos'][0]['owner']), ('甲等2人', '甲'))

    def test_unconfirmed_roles_cannot_publish_final_html(self):
        state = w.read(self.root / 'state.json')
        state['speakers'][0]['role_confirmed'] = False
        w.write(self.root / 'state.json', state)
        w.set_analysis(self.root, self.analysis(), 1)
        self.assertNotIn('html', w.read(self.root / 'exports/latest.json')['files']['4'])

    def test_reader_references_and_owner_override_rejected_before_persist(self):
        for invalid in ['reference', 'owner', 'summary_reference', 'summary_type']:
            a = self.analysis()
            if invalid == 'reference': a['sources']['4']['reader']['points'][0]['evidence'] = [999]
            elif invalid == 'owner': a['sources']['4']['reader']['todos'][0]['owner'] = '4-B'
            elif invalid == 'summary_reference': a['sources']['4']['reader']['summary_evidence'] = [999]
            else: a['sources']['4']['reader']['summary'] = 12
            with self.assertRaises(ValueError): w.set_analysis(self.root, a, 1)
            self.assertEqual(w.read(self.root / 'state.json')['revision'], 1)

    def test_document_text_cannot_escape_embedded_json_or_inject_html(self):
        malicious = '</script><img src="https://invalid.test/" onerror="alert(1)">'
        data = {'topic': malicious, 'meeting_time': '待确认', 'participants': '甲',
                'points': [{'title': '原话', 'text': malicious}], 'todos': []}
        parser = SeedParser(); parser.feed(minutes_html.render(data))
        self.assertEqual(json.loads(parser.seed), data)
        self.assertEqual(len([tag for tag, attrs in parser.tags if tag == 'script']), 2)
        self.assertEqual([attrs.get('src') for tag, attrs in parser.tags if tag == 'img'], [None])


if __name__ == '__main__': unittest.main()
