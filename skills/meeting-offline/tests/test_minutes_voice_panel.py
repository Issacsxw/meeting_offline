import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import minutes_html


class MinutesVoicePanelTests(unittest.TestCase):
    def document(self, layout='classic'):
        return {'topic': '会议', 'meeting_time': '', 'participants': '张三、说话人 B',
                'summary': '张三和说话人 B共同推进',
                'points': [{'title': '张三建议', 'text': '交给说话人 B'}],
                'todos': [{'task': '与张三沟通', 'owner': '说话人 B', 'deadline': '明天'}],
                'workspace_id': 'work-1', 'revision': 7, 'source_id': 's', 'layout': layout,
                'speakers': [{'key': 's-A', 'name': '张三', 'role_source': 'voice_match', 'role_confirmed': False},
                             {'key': 's-B', 'name': '说话人 B', 'role_confirmed': False}]}

    def test_all_layouts_empty_confirmation_contract(self):
        for layout in minutes_html.LAYOUTS:
            page = minutes_html.render(self.document(layout))
            payload = json.loads(re.search(r'id="meeting-speaker-edits">(.*?)</script>', page, re.S)[1])
            self.assertEqual(payload, {'schema_version': 1, 'workspace_id': 'work-1', 'revision': 7,
                                       'source_id': 's', 'mappings': []})
            self.assertIn('id="speaker-panel"', page)
            self.assertIn('voice-pending', page)
            self.assertIn("clone.querySelector('#meeting-speaker-edits').textContent=JSON.stringify(speakerEdits)", page)
            # Canvas PNG uses meeting fields, not the identity panel or its badge.
            canvas = page.split('async function makePng(data)', 1)[1].split("$('#export-png').onclick", 1)[0]
            self.assertNotIn('voice-pending', canvas)
            self.assertNotIn('speakerRows', canvas)

    def test_safe_json_and_warning(self):
        doc = self.document(); doc['workspace_id'] = '</script><script>alert(1)</script>'
        doc['audit_issues'] = [{'detail': '<img src=x onerror=alert(1)>'}]
        page = minutes_html.render(doc)
        payload = json.loads(re.search(r'id="meeting-speaker-edits">(.*?)</script>', page, re.S)[1])
        self.assertEqual(payload['workspace_id'], doc['workspace_id'])
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn('这几处和原话对不上，请核对', page)

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime not installed')
    def test_actual_rename_function_updates_all_fields_without_cascade(self):
        page = minutes_html.render(self.document())
        js = page.split('function renameSpeakerDocument', 1)[1].split('function renderSpeakerPanel', 1)[0]
        script = 'const renameSpeakerDocument=function renameSpeakerDocument' + js + ';\n'
        script += 'const data=' + json.dumps(self.document(), ensure_ascii=False) + ';\n'
        script += '''const next=renameSpeakerDocument(data,'s-B','李四');
if(next.participants!=='张三、李四'||next.points[0].text!=='交给李四'||next.todos[0].owner!=='李四'||next.summary!=='张三和李四共同推进')throw Error('replacement');
if(data.speakers[1].role_confirmed)throw Error('mutated input');
if(next.speakers[0].role_confirmed)throw Error('confirmed untouched auto match');
if(!next.speakers[1].role_confirmed)throw Error('explicit confirmation missing');
let rejected=false;try{renameSpeakerDocument(next,'s-B','张三')}catch(e){rejected=true}if(!rejected)throw Error('collision');
rejected=false;try{renameSpeakerDocument(next,'s-UNASSIGNED','张四')}catch(e){rejected=true}if(!rejected)throw Error('unassigned');
const unsafe=renameSpeakerDocument(next,'s-B','<img src=x>');if(unsafe.todos[0].owner!=='<img src=x>')throw Error('text-only name');
console.log('speaker panel replacement passed');'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workspace_voice_name_is_pending_and_unassigned_excluded(self):
        state = {'revision': 2, 'workspace_id': 'w', 'speakers': [
            {'key': 's-A', 'source_id': 's', 'role_name': '张三', 'role_confirmed': False, 'role_source': 'voice_match'},
            {'key': 's-B', 'source_id': 's', 'role_confirmed': False},
            {'key': 's-UNASSIGNED', 'source_id': 's', 'diarization_unassigned': True}]}
        doc = minutes_html.from_workspace(state, 's', {}, [])
        self.assertEqual(doc['speakers'][0]['name'], '张三')
        self.assertFalse(doc['speakers'][0]['role_confirmed'])
        self.assertEqual(len(doc['speakers']), 2)
        self.assertIn('说话人 B', doc['participants'])


if __name__ == '__main__':
    unittest.main()
