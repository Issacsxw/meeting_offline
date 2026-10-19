import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import minutes_html
import minutes_md


class MinutesVoicePanelTests(unittest.TestCase):
    def document(self, layout='classic'):
        return {'topic': '会议', 'meeting_time': '', 'participants': '张三、说话人 B',
                'summary': '张三和说话人 B共同推进',
                'points': [{'title': '张三建议', 'text': '交给说话人 B'}],
                'todos': [{'task': '与张三沟通', 'owner': '说话人 B', 'deadline': '明天'}],
                'workspace_id': 'work-1', 'revision': 7, 'source_id': 's', 'layout': layout,
                'speakers': [{'key': 's-A', 'name': '张三', 'role_source': 'voice_match', 'role_confirmed': False},
                             {'key': 's-B', 'name': '说话人 B', 'role_confirmed': False}]}

    def test_prose_boundaries_placeholders_and_pending_privacy(self):
        self.assertEqual(minutes_html.replace_speaker_names('王府井，王；王五，王五郎；王五。', {'王': '李', '王五': '李四'}, ['王五郎']),
                         '王府井，王；李四，王五郎；李四。')
        self.assertEqual(minutes_html.replace_speaker_names('交给说话人 B。s-A负责', {'说话人 B': '李四', 's-A': '张三'}), '交给李四。张三负责')
        for value in ('身份未确认', '身份未确认（s-A）', '说话人未分离', '说话人 Z', '说话人ABC', 's-B'):
            self.assertFalse(minutes_html.valid_speaker_name(value, ['s-A', 's-B']), value)
        self.assertTrue(minutes_html.valid_speaker_name('王', ['s-A']))
        doc = self.document(); doc['speakers'][0]['voice_similarity'] = .987654
        page = minutes_html.render(doc)
        self.assertNotIn('voice_similarity', page); self.assertNotIn('0.987654', page)
        self.assertIn('voice_match', page)
        doc['people'] = [{'name': '张三', 'shown': '张三', 'role_confirmed': False}]
        md = minutes_md.minutes(doc, links={'张三': '张三档案'})
        self.assertIn('张三（待确认）', md); self.assertNotIn('[[张三档案', md)
        transcript = minutes_md.transcript('会议', [], people=doc['people'])
        self.assertNotIn('说话人由用户确认', transcript)
        self.assertIn('说话人由用户确认', minutes_md.transcript('会议', [], all_confirmed=True))

    def test_explicitly_confirmed_unassigned_name_is_rendered(self):
        speaker = {'key': 's-UNASSIGNED', 'source_id': 's', 'diarization_unassigned': True,
                   'identity_group': 'person-a', 'role_confirmed': True, 'role_name': '张三'}
        self.assertEqual(minutes_html.shown_name(speaker), '张三')
        document = minutes_html.from_workspace({'speakers': [speaker]}, 's', {}, [])
        self.assertEqual(document['participants'], '张三')

    def test_all_layouts_empty_confirmation_contract(self):
        for layout in minutes_html.LAYOUTS:
            page = minutes_html.render(self.document(layout))
            payload = json.loads(re.search(r'id="meeting-speaker-edits">(.*?)</script>', page, re.S)[1])
            self.assertEqual(payload, {'schema_version': 1, 'workspace_id': 'work-1', 'revision': 7,
                                       'source_id': 's', 'mappings': []})
            self.assertIn('id="speaker-panel"', page)
            self.assertIn('voice-pending', page)
            handler = page.split('button.onclick=()=>', 1)[1].split('row.append(label,input,button)', 1)[0]
            self.assertLess(handler.index('未登记身份确认'), handler.index('speakerEdits.mappings.push'))
            self.assertIn('return}', handler)
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
for(const name of ['身份未确认','说话人未分离','说话人 Z','s-A']){let refused=false;try{renameSpeakerDocument(next,'s-B',name)}catch(e){refused=true}if(!refused)throw Error('reserved label '+name)}
const chinese={speakers:[{key:'s-A',name:'王'}],participants:'王',summary:'王府井和王',points:[],todos:[{task:'王府井',owner:'王'}]};const renamed=renameSpeakerDocument(chinese,'s-A','李');if(renamed.summary!=='王府井和王'||renamed.participants!=='李'||renamed.todos[0].owner!=='李')throw Error('single character boundary');
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

    def run_rename_js(self, assertions):
        page = minutes_html.render(self.document())
        functions = 'function renameSpeakerDocument' + page.split('function renameSpeakerDocument', 1)[1].split('function renderSpeakerPanel', 1)[0]
        result = subprocess.run(['node', '-e', functions + '\n' + assertions], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime not installed')
    def test_chinese_prose_warning_and_participants_count_suffix(self):
        self.run_rename_js('''
const data={speakers:[{key:'s-A',name:'林悦'},{key:'s-B',name:'周宁'}],participants:'周宁、林悦等3人',topic:'林悦：讨论',summary:'林悦认为报价可行。',points:[{title:'林悦',text:'由林悦推动。'}],todos:[{task:'请林悦确认',owner:'林悦',deadline:'等林悦回复'}]};
const next=renameSpeakerDocument(data,'s-A','张三');
if(next.participants!=='周宁、张三等3人'||next.topic!=='张三：讨论'||next.points[0].title!=='张三'||next.todos[0].owner!=='张三')throw Error('exact field rename');
if(next.summary!==data.summary||next.points[0].text!==data.points[0].text||next.todos[0].task!==data.todos[0].task)throw Error('unsafe prose replacement');
const issues=speakerRenameIssues(next);
if(issues.length!==4||issues.some(issue=>issue.name!=='林悦')||!issues.some(issue=>issue.where==='会议概要'))throw Error('missing unresolved old-name warning');
if(data.speakers[0].previous_names)throw Error('mutated rename history');
for(const [before,after] of [['林悦等2人','张三等2人'],['林悦、周宁等4人','张三、周宁等4人'],['3人','3人'],['林悦','张三']]){
 const changed=renameSpeakerDocument({...data,participants:before},'s-A','张三');if(changed.participants!==after)throw Error('participant suffix '+before);
}
const untouched=renameSpeakerDocument(data,'s-A','林悦');if(speakerRenameIssues(untouched).length)throw Error('confirmation alone creates rename warning');
''')

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime not installed')
    def test_rename_warning_survives_reload_and_clears_after_manual_correction(self):
        self.run_rename_js('''
const data={speakers:[{key:'s-A',name:'林悦'}],participants:'林悦等2人',summary:'林悦认为可行',points:[],todos:[]};
const renamed=renameSpeakerDocument(data,'s-A','张三');
const reloaded=JSON.parse(JSON.stringify(renamed));if(speakerRenameIssues(reloaded).length!==1)throw Error('warning lost on save/reload');
const twice=renameSpeakerDocument(reloaded,'s-A','李四');if(speakerRenameIssues(twice)[0].name!=='林悦')throw Error('first old name lost on repeated rename');
twice.summary='李四认为可行';if(speakerRenameIssues(twice).length)throw Error('warning did not clear after manual correction');
twice.summary='林悦认为可行';if(speakerRenameIssues(twice).length!==1)throw Error('warning does not reappear after undo');
const restored=renameSpeakerDocument(twice,'s-A','林悦');if(speakerRenameIssues(restored).length)throw Error('current name flagged after rename back');
const extended=renameSpeakerDocument({...data,summary:'林悦：可行。'},'s-A','林悦明');if(speakerRenameIssues(extended).length)throw Error('new name prefix falsely flagged');
const distinct={...data,speakers:[...data.speakers,{key:'s-B',name:'林悦明'}],summary:'林悦明认为可行'};
if(speakerRenameIssues(renameSpeakerDocument(distinct,'s-A','张三')).length)throw Error('another known longer name falsely flagged');
''')

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime not installed')
    def test_actual_save_and_png_handlers_warn_before_export(self):
        page = minutes_html.render(self.document())
        handlers = '\n'.join(line for line in page.splitlines() if line.startswith(("$('#save-html').onclick=", "$('#export-png').onclick=")))
        self.run_rename_js('''
const nodes=new Map();const node=()=>({hidden:true,disabled:false,textContent:'',children:[],replaceChildren(...items){this.children=items},append(...items){this.children.push(...items)},removeAttribute(){},showModal(){}});
const $=selector=>{if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector)};
const element=(tag,text)=>({...node(),tag,textContent:text||''});
let lastStatus='',confirmation=true,prompts=[],cloneCalls=0,pngCalls=0,downloads=0,currentImageObjectURL=null,dirty=true,savedData=null;
const status=value=>{lastStatus=value};const window={confirm:message=>{prompts.push(message);return confirmation}};
let data=renameSpeakerDocument({speakers:[{key:'s-A',name:'林悦'}],participants:'林悦等2人',summary:'林悦认为可行',points:[],todos:[]},'s-A','张三');
const collect=()=>data,speakerEdits={mappings:[{key:'s-A',name:'张三'}]},filename=ext=>'meeting.'+ext;
const cloneNodes=new Map();const document={documentElement:{cloneNode(){cloneCalls++;return{querySelector:selector=>{if(!cloneNodes.has(selector))cloneNodes.set(selector,node());return cloneNodes.get(selector)},querySelectorAll:()=>[],get outerHTML(){savedData=JSON.parse(cloneNodes.get('#minutes-data').textContent);return '<html></html>'}}}}};
const URL={createObjectURL:()=> 'blob:test',revokeObjectURL(){}},download=()=>{downloads++},setTimeout=()=>{};
const makePng=async value=>{pngCalls++;return{downloadUrl:'blob:png',url:'data:png',width:100,height:200}};
''' + handlers + '''
(async()=>{
renderSpeakerRenameWarning(data);const box=$('#speaker-rename-warning');if(box.hidden||!box.children.some(item=>item.textContent==='正文里可能还有旧名字，请核对'))throw Error('visible warning missing');
confirmation=false;$('#save-html').onclick();await $('#export-png').onclick();
if(prompts.length!==2||prompts.some(message=>!message.includes('正文里可能还有旧名字')||!message.includes('林悦')))throw Error('export warning missing');
if(cloneCalls||pngCalls||downloads||$('#export-png').disabled)throw Error('cancel did not prevent export');
confirmation=true;$('#save-html').onclick();await $('#export-png').onclick();
if(cloneCalls!==1||pngCalls!==1||downloads!==1||speakerRenameIssues(savedData).length!==1)throw Error('confirmed export/persisted history failed');
data.summary='张三认为可行';renderSpeakerRenameWarning(data);if(!box.hidden)throw Error('manual correction warning persists');
const count=prompts.length;$('#save-html').onclick();await $('#export-png').onclick();if(prompts.length!==count)throw Error('corrected document still prompts');
})().catch(error=>{console.error(error);process.exitCode=1});
''')


if __name__ == '__main__':
    unittest.main()
