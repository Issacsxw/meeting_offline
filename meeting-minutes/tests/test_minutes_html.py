import json
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import workspace as w
import minutes_html
import test_workspace as fixtures


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
