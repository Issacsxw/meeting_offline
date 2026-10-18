import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import workspace as w
import review
import device_context


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/'workspace'
        self.store=self.base/'memory.json'
        w.write(self.store,{'schema_version':1,'device_id':device_context.device_id(),'roles':[
            {'id':'person-a','entity_type':'person','name':'甲','scope':'测试','role_title':'产品'},
            {'id':'person-b','entity_type':'person','name':'乙','scope':'测试','role_title':'业务'}], 'terms':[], 'history':[]})
        sources=[];speakers=[];audios={}
        for sid,pid in [('4','person-a'),('5','person-b')]:
            raw=self.base/(sid+'.json');w.write(raw,{'model':'synthetic','duration_seconds':6,'text':'我来提供错词的报价。',
                'segments':[{'speaker_id':0,'start_ms':0,'end_ms':5000,'text':'我来提供错词的报价。'}]})
            audio=self.base/(sid+'.m4a');audio.write_bytes(b'0123456789');audios[sid]=audio
            sources.append({'source_id':sid,'path':str(raw)})
            speakers.append({'key':sid+'-A','source_id':sid,'source_speaker_id':0,'role_name':'甲' if sid=='4' else '乙',
                'identity_group':pid,'role_confirmed':True,'core_points':[{'text':'提供错词报价。'}],
                'utterances':[{'source_segment_index':0,'start_ms':0,'end_ms':5000,'text':'我来提供错词的报价。'}]})
        state=self.base/'old.json';w.write(state,{'sources':sources,'speakers':speakers,'memory_store':str(self.store)})
        w.initialize(state,self.root,audios)
    def tearDown(self):self.tmp.cleanup()
    def test_role_edit_rebuilds_without_touching_audio_or_raw(self):
        state=w.read(self.root/'state.json');before=[w.file_digest(s['path']) for s in state['sources']]
        first=w.read(self.root/'exports/latest.json')['version']
        w.save_roles(self.root,[{'key':'4-A','profile_id':'person-b'}],1)
        out=w.read(self.root/'exports/latest.json');self.assertNotEqual(first,out['version'])
        self.assertIn('乙｜业务',Path(out['files']['4']['transcript']).read_text())
        self.assertEqual(before,[w.file_digest(s['path']) for s in state['sources']])
        self.assertFalse((self.root/'audio').exists())
        # Correcting a mistaken assignment back restores the original transcript content.
        w.save_roles(self.root,[{'key':'4-A','profile_id':'person-a'}],2)
        self.assertIn('甲｜产品',Path(w.read(self.root/'exports/latest.json')['files']['4']['transcript']).read_text())
    def test_scoped_correction_preserves_original_and_other_source(self):
        w.add_term(self.root,{'source_id':'4','heard':'错词','canonical':'正词','scope':'测试'},1)
        out=w.read(self.root/'exports/latest.json')
        self.assertIn('正词',Path(out['files']['4']['transcript']).read_text())
        self.assertIn('错词',Path(out['files']['5']['transcript']).read_text())
        data=w.read(out['files']['4']['structured']);self.assertIn('错词',data['transcript'][0]['original_text'])
        with self.assertRaisesRegex(ValueError,'歧义'):
            w.add_term(self.root,{'source_id':'5','heard':'错词','canonical':'另一词','scope':'测试'},2)
        self.assertEqual(len(w.read(self.store)['terms']),1)
    def test_stale_page_and_invalid_evidence_rejected(self):
        with self.assertRaises(ValueError):w.save_roles(self.root,[{'key':'4-A','profile_id':'person-b'}],0)
        with self.assertRaises(ValueError):w.set_analysis(self.root,{'sources':{'4':{'actions':[{'task':'无依据','owner':'4-A','evidence':[99]}]}}},1)
        with self.assertRaises(ValueError):w.set_analysis(self.root,{'sources':{'4':{'actions':[{'task':'不能猜负责人','owner':'4-A','evidence':[0]}]}}},1)
        self.assertEqual(w.read(self.root/'state.json')['revision'],1)
    def test_evidence_is_reconstructed_and_owner_tracks_role(self):
        analysis={'sources':{'4':{'actions':[{'task':'提供报价','owner':'4-A','owner_basis':'self_commitment','evidence':[0]}]}}}
        w.set_analysis(self.root,analysis,1);w.save_roles(self.root,[{'key':'4-A','profile_id':'person-b'}],2)
        out=w.read(self.root/'exports/latest.json');claim=w.read(out['files']['4']['structured'])['analysis']['actions'][0]
        self.assertEqual(claim['owner_name'],'乙｜业务');self.assertEqual(claim['source_evidence'][0]['text'],'我来提供错词的报价。')
    def test_new_role_is_not_a_guessed_person(self):
        w.save_roles(self.root,[{'key':'4-A','name':'渠道负责人','scope':'测试'}],1)
        profiles=w.read(self.store)['roles']
        created=next(p for p in profiles if p['name']=='渠道负责人')
        self.assertEqual(created['entity_type'],'role')
        self.assertEqual(w.read(self.store)['terms'],[])
        w.save_roles(self.root,[{'key':'5-A','name':'测试同事','scope':'测试','entity_type':'person'}],2)
        self.assertEqual(w.read(self.store)['terms'][0]['canonical'],'测试同事')

    def test_http_auth_host_origin_and_audio_range(self):
        server=review.ReviewServer(self.root);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as failure:opener.open(server.origin+'/api/state')
            self.assertEqual(failure.exception.code,403)
            headers={'X-Meeting-Token':server.token,'Range':'bytes=2-5'}
            with opener.open(urllib.request.Request(server.origin+'/audio/4',headers=headers)) as r:
                self.assertEqual(r.status,206);self.assertEqual(r.read(),b'2345')
            req=urllib.request.Request(server.origin+'/api/roles',data=b'{}',headers={'X-Meeting-Token':server.token,'Origin':'http://other.invalid'},method='POST')
            with self.assertRaises(urllib.error.HTTPError) as f:opener.open(req)
            self.assertEqual(f.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
