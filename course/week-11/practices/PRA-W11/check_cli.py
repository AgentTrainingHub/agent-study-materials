"""W11 公开 CLI 检查；真实本地 HTTP/CPU/SQLite，响应标为 controlled-fixture。"""
from __future__ import annotations
import argparse,copy,hashlib,importlib.util,json,os,shutil,signal,sqlite3,subprocess,sys,time,unittest,uuid
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent
W10=MATERIALS.parents[2]/'week-10/practices/PRA-W10'
sys.path.insert(0,str(W10))
from prepare_corpus import prepare

def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def load(path):return json.loads(Path(path).read_text())
def rows(path):return [json.loads(x) for x in Path(path).read_bytes().splitlines() if x] if Path(path).exists() else []
def wait_for(fn,process=None,timeout=15):
    deadline=time.monotonic()+timeout
    while not fn():
        if process is not None and process.poll() is not None:raise AssertionError('进程在实际门闩前退出')
        if time.monotonic()>=deadline:raise AssertionError('未到达实际门闩')
        time.sleep(.015)

WORKER=r'''
import sys,socket,json,urllib.request
from pathlib import Path
student,endpoint,settings=sys.argv[1:4];sys.path.insert(0,student)
settings=json.loads(Path(settings).read_text())
urllib.request.getproxies=lambda:{}
original=socket.socket.connect
def local(self,address):
    if not isinstance(address,tuple) or address[0]!='127.0.0.1':raise AssertionError('controlled fixture permits only localhost')
    return original(self,address)
socket.socket.connect=local
import w11_stream,w05_support,memory,w11_io
if settings.get('clock'):memory.utc_now=w11_io.utc_now=lambda:settings['clock']
module=w11_stream if '--memory' in sys.argv else w05_support
module.ENDPOINT=endpoint
raise SystemExit(module._stream_worker())
'''
WRAPPER=r'''
import sys,os,json,runpy,time,socket
from pathlib import Path
entry,worker,endpoint,settings=map(str,sys.argv[1:5]);settings_path=settings;settings=json.loads(Path(settings).read_text());sys.path.insert(0,str(Path(entry).parent))
import w11_stream,w05_support,w02_support,w11_io,memory,memory_cli,embedding_worker
w02_support.EVIDENCE_SOURCE='controlled-fixture+real-local-http-cpu'
w11_stream._stream_worker_command=lambda:[sys.executable,worker,str(Path(entry).parent),endpoint,settings_path,'--memory']
w05_support._stream_worker_command=lambda:[sys.executable,worker,str(Path(entry).parent),endpoint,settings_path]
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('parent cannot send network'))
def hold(name):
    if settings.get('hold')!=name:return
    p=Path(settings['gate']);p.with_suffix('.hit').write_text(json.dumps({'pid':os.getpid(),'point':name,'time_ns':time.time_ns()}))
    until=time.monotonic()+15
    while not p.exists():
        if time.monotonic()>until:raise RuntimeError('fixture gate timeout')
        time.sleep(.01)
if settings.get('clock'):memory.utc_now=w11_io.utc_now=lambda:settings['clock']
old_call=w11_stream.call_model_stream
def stream(*a,**kw):
    hold('before_worker');old=kw.get('on_gate')
    def observed(record):
        if old:old(record)
        if record.get('headers_at'):hold('headers')
    kw['on_gate']=observed
    return old_call(*a,**kw)
w11_stream.call_model_stream=stream
old_publish=memory_cli.publish_answer
def publish(*a,**kw):
    hold('before_publish');result=old_publish(*a,**kw);hold('after_publish');return result
memory_cli.publish_answer=publish
if settings.get('expire_after_view'):
    old_effective=memory.MemoryStore.effective
    def effective(self,*a,**kw):
        result=old_effective(self,*a,**kw);memory.utc_now=w11_io.utc_now=lambda:settings['expire_after_view'];return result
    memory.MemoryStore.effective=effective
if settings.get('corrupt_source'):
    old_propose=memory.MemoryStore.propose
    def propose(self,args,record):
        path=Path(record['session_path']);lines=[json.loads(x) for x in path.read_text().splitlines()]
        for row in lines:
            if row.get('entry_id')==record['entry_id']:row['message']['role']='assistant'
        path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in lines))
        return old_propose(self,args,record)
    memory.MemoryStore.propose=propose
if settings.get('embedding_failure'):
    old_init=embedding_worker.EmbeddingWorker.__init__
    def init(self,*a,**kw):
        old_init(self,*a,**kw);self.request['representation_id']='fixture-invalid-representation'
    embedding_worker.EmbeddingWorker.__init__=init
sys.argv=[entry,*settings['argv']];runpy.run_path(entry,run_name='__main__')
'''

class CliChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared=cls.output_root/'shared';cls.shared.mkdir();cls.corpus=cls.shared/'corpus';prepare(cls.corpus)
        cls.index=cls.shared/'index.sqlite';cls.worker=cls.shared/'worker.py';cls.wrapper=cls.shared/'wrapper.py';cls.worker.write_text(WORKER);cls.wrapper.write_text(WRAPPER)
        p=subprocess.run([sys.executable,str(cls.entry),'corpus-build','--corpus',str(cls.corpus),'--index',str(cls.index),'--embedding-model',str(cls.model),'--output-dir',str(cls.shared/'build')],capture_output=True,timeout=180)
        (cls.shared/'build-stdout.json').write_bytes(p.stdout);(cls.shared/'build-stderr.txt').write_bytes(p.stderr)
        if p.returncode:raise RuntimeError(p.stdout.decode()+p.stderr.decode())
        cls.task=load(MATERIALS/'inputs/research-task.json');cls.catalog=load(MATERIALS/'inputs/research-catalog.json')
    def setUp(self):
        self.directory=self.output_root/self._testMethodName;self.directory.mkdir();self.workspace=self.directory/'workspace';self.workspace.mkdir();self.db=self.directory/'memory.sqlite';self.session=self.directory/'session.jsonl';self.invocations=[];self.active=[];self.serial=0
        self.assertEqual(self.control('init')['value']['status'],'initialized')
    def tearDown(self):
        for process in self.active:
            if process.poll() is None:process.terminate()
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.communicate()
        save(self.directory/'facts.json',{'external_model_requests':0,'evidence_source':'controlled-fixture','semantic_review':'required','invocations':self.invocations})
    def control(self,action,command=None,*,workspace=None,db=None,actor='experiment-driver',async_=False):
        self.serial+=1;out=self.directory/f'control-{self.serial:02d}-{action}';out.mkdir()
        argv=[sys.executable,str(self.entry),'memory-'+action,'--memory-db',str(db or self.db),'--workspace',str(workspace or self.workspace)]
        if command is not None:
            save(out/'command.json',command);argv+=['--command',str(out/'command.json'),'--output-dir',str(out/'result'),'--actor',actor]
        p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE);self.active.append(p)
        if async_:return p,out,argv
        return self.collect(p,out,argv)
    def collect(self,p,out,argv,server=None):
        stdout,stderr=p.communicate(timeout=180);(out/'stdout.json').write_bytes(stdout);(out/'stderr.txt').write_bytes(stderr);(out/'exit-code.txt').write_text(str(p.returncode)+'\n')
        try:value=json.loads(stdout)
        except ValueError:value={'raw_stdout':stdout.decode(errors='replace')}
        result={'value':value,'exit_code':p.returncode,'directory':out,'argv':argv,'requests':[]}
        if server:
            sp,sd=server
            sp.terminate();so,se=sp.communicate(timeout=5);(sd/'stdout.txt').write_bytes(so);(sd/'stderr.txt').write_bytes(se)
            result['requests']=[load(x) for x in sorted(sd.glob('request-*.json'))];result['server_dir']=sd
        self.invocations.append({'directory':str(out),'argv':argv,'exit_code':p.returncode,'result':value,'http_requests':len(result['requests'])})
        return result
    def run_cli(self,scenario,*,question='以后使用 semicolon_keep 合同，回答采用 table。',scope='semicolon_keep',format=None,session=None,memory_db=True,research=False,task=None,catalog=None,command='new',extra=(),hook=None,settings=None,server_hold=None):
        self.serial+=1;out=self.directory/f'run-{self.serial:02d}-{command}';out.mkdir();save(out/'scenario.json',scenario);(out/'question.md').write_text(question+'\n')
        sd=out/'server';argv=[sys.executable,str(MATERIALS/'inputs/gate_receiver.py'),'--directory',str(sd),'--scenario',str(out/'scenario.json')]
        if server_hold:argv+=['--hold-'+server_hold,str(out/'release')]
        server=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE);self.active.append(server);wait_for(lambda:(sd/'manifest.json').exists(),server)
        args=[command,'--session',str(session or self.session),'--workspace',str(self.workspace),'--question',str(out/'question.md'),'--output-dir',str(out/'runs'),'--corpus',str(self.corpus),'--index',str(self.index),'--embedding-model',str(self.model),'--max-tokens','1536','--input-source','experiment-driver']
        if memory_db:args+=['--memory-db',str(self.db)]
        if scope:args+=['--doc-scope',scope]
        if format:args+=['--answer-format',format]
        if research:
            save(out/'task.json',task or self.task);save(out/'catalog.json',catalog or self.catalog);args+=['--research-task',str(out/'task.json'),'--research-catalog',str(out/'catalog.json')]
        args+=list(extra);settings={**(settings or {}),'argv':args,'gate':str(out/'release')};save(out/'settings.json',settings)
        endpoint=load(sd/'manifest.json')['endpoint'];cmd=[sys.executable,str(self.wrapper),str(self.entry),str(self.worker),endpoint,str(out/'settings.json')]
        env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}};env.update(DEEPSEEK_API_KEY='controlled-fixture-no-secret',DEEPSEEK_MODEL='controlled-model')
        p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env);self.active.append(p)
        if hook:hook(p,out,server)
        r=self.collect(p,out,args,(server,sd));r['runs']=Path(r['value'].get('artifact_dir',out/'runs'));r['records']=rows(session or self.session);return r
    def listing(self):return self.control('list')['value']
    def pending(self,*,key='docs.answer_format',value='table',question=None,session=None):
        question=question or '以后使用 '+value
        r=self.run_cli([{'kind':'propose','candidates':[{'key':key,'value':value,'source_quote':question}]},{'kind':'literal','text':'候选已保存，等待操作者确认。'}],question=question,session=session or self.directory/('source-'+uuid.uuid4().hex+'.jsonl'))
        self.assertEqual(r['exit_code'],0,r['value']);return self.listing()['memories'][-1]
    def command(self,item,**extra):return {'schema':1,'command_id':uuid.uuid4().hex,'expected_revision':self.listing()['meta']['control_revision'],'memory_id':item['memory_id'],**extra}
    def confirm(self,item,**extra):
        r=self.control('confirm',self.command(item,**extra));self.assertEqual(r['exit_code'],0,r['value']);return r
    def activated(self):
        scope=self.pending(key='docs.default_scope',value='semicolon_keep');self.confirm(scope);fmt=self.pending();self.confirm(fmt);return scope,fmt
    def simple(self,**kw):return self.run_cli([{'kind':'search'},{'kind':'answer'}],question='当前合同是什么？',**kw)
    def assert_sent(self,r,count):self.assertEqual(len(r['requests']),count,r['value'])
    def tools(self,r):return [json.loads(x['message']['content']) for x in r['records'] if x.get('type')=='message' and x.get('message',{}).get('role')=='tool']
    def query(self,group='contract',**extra):return {'kind':'research_search','task':self.task,'query':'原样输入 前导零 重复项 固定处理耗时','source_group':group,'target_claim':'fixed_processing_time' if group=='support' else 'input_validity',**extra}
    def research_run(self,scenario,**kw):return self.run_cli(scenario,research=True,question=self.task['question'],**kw)

    def test_M01_two_legal_candidates(self):
        text='以后使用 semicolon_keep 合同，回答采用 table。';r=self.run_cli([{'kind':'propose','candidates':[{'key':'docs.default_scope','value':'semicolon_keep','source_quote':'semicolon_keep'},{'key':'docs.answer_format','value':'table','source_quote':'table'}]},{'kind':'literal','text':'两个候选等待确认。'}],question=text)
        self.assertEqual(r['exit_code'],0,r['value']);items=self.listing()['memories'];self.assertEqual(len(items),2);self.assertTrue(all(x['status']=='pending' for x in items));self.assertEqual(self.listing()['meta']['effective_epoch'],0)
    def test_M02_source_and_quote_rejected(self):
        for extra,settings in [({'source_ref':{'role':'user'}},{}),({'source_quote':'原文没有这一句'},{}),({}, {'corrupt_source':True})]:
            r=self.run_cli([{'kind':'propose','candidates':[{'key':'docs.answer_format','value':'table','source_quote':'以后使用 table',**extra}]}],question='以后使用 table',session=self.directory/(uuid.uuid4().hex+'.jsonl'),settings=settings)
            self.assertNotEqual(r['exit_code'],0);self.assertEqual(self.listing()['memories'],[]);self.assert_sent(r,1)
    def test_M03_pending_not_injected(self):
        self.pending();r=self.simple();self.assertEqual(r['exit_code'],0,r['value']);p=load(r['runs']/'projection-001.json');self.assertEqual(p['memory']['binding']['adopted'],[]);self.assertEqual(p['memory']['reasons']['docs.answer_format'],'no_effective_memory')
    def test_M04_confirmation_source_chain(self):
        item=self.pending();r=self.confirm(item);listed=self.listing()['memories'][0];self.assertEqual(listed['confirmation_actor'],'experiment-driver');self.assertEqual(listed['source_ref']['actor'],'experiment-driver');self.assertEqual(listed['source_text'][listed['quote_range']['start']:listed['quote_range']['end']],listed['source_quote']);self.assertEqual(r['value']['effective_epoch'],1)
    def test_M05_same_source_and_value(self):
        text='以后使用 table';candidate={'key':'docs.answer_format','value':'table','source_quote':text}
        r=self.run_cli([{'kind':'propose','candidates':[candidate]},{'kind':'propose','candidates':[candidate]},{'kind':'literal','text':'待确认'}],question=text)
        self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(len(self.listing()['memories']),1);self.confirm(self.listing()['memories'][0]);new=self.pending();self.confirm(new);items=self.listing()['memories'];self.assertEqual([x['status'] for x in items],['active','superseded']);self.assertEqual(self.listing()['meta']['effective_epoch'],1)
    def test_M06_new_session_adopts(self):
        self.activated();r=self.simple(scope=None);self.assertEqual(r['exit_code'],0,r['value']);p=load(r['runs']/'projection-001.json');self.assertEqual(p['memory']['values'],{'docs.default_scope':'semicolon_keep','docs.answer_format':'table'});self.assertEqual(len(p['memory']['binding']['adopted']),2)
    def test_M07_other_workspace_rejected(self):
        other=self.directory/'other';other.mkdir();r=self.control('list',workspace=other);self.assertEqual(r['exit_code'],2);self.assertEqual(r['value']['error']['class'],'memory_project_mismatch')
    def test_M08_explicit_override(self):
        self.activated();r=self.simple(scope='comma_keep',format='list');self.assertEqual(r['exit_code'],0,r['value']);p=load(r['runs']/'projection-001.json');self.assertEqual(p['memory']['binding']['adopted'],[]);self.assertEqual(p['memory']['values'],{'docs.default_scope':'comma_keep','docs.answer_format':'list'});request=r['requests'][0];self.assertNotIn('"kind":"project_memory"',json.dumps(request,ensure_ascii=False,separators=(',',':')))
    def test_M09_atomic_replace_stale_revision(self):
        first=self.pending();self.confirm(first);second=self.pending(value='list');command=self.command(second,supersedes=first['memory_id']);self.assertEqual(self.control('confirm',command)['exit_code'],0);stale={**command,'command_id':uuid.uuid4().hex};self.assertEqual(self.control('confirm',stale)['value']['error']['class'],'stale_revision');self.assertEqual([x['status'] for x in self.listing()['memories']],['superseded','active'])
    def test_M10_conflict_and_resolve(self):
        first=self.pending();self.confirm(first);second=self.pending(value='list');self.confirm(second);self.assertEqual([x['status'] for x in self.listing()['memories']],['conflict','conflict']);r=self.simple();self.assertEqual(load(r['runs']/'projection-001.json')['memory']['reasons']['docs.answer_format'],'memory_conflict');self.assertEqual(self.control('resolve',self.command(second))['exit_code'],0);self.assertEqual([x['status'] for x in self.listing()['memories']],['rejected','active'])
    def test_M11_revoke_one_key(self):
        scope,fmt=self.activated();r=self.control('revoke',self.command(fmt));self.assertEqual(r['value']['status'],'stopped_using');self.assertEqual(r['value']['history'],'PRESENT');r=self.simple(scope=None);self.assertEqual([x['memory_id'] for x in r['value']['memory_binding']['adopted']],[scope['memory_id']])
    def test_M12_expiration_boundary(self):
        from datetime import datetime,timezone,timedelta
        item=self.pending();until=datetime.now(timezone.utc)+timedelta(minutes=3);self.confirm(item,valid_until=until.isoformat());clock=lambda d:(until+timedelta(seconds=d)).isoformat()
        for delta,adopted in [(-1,1),(0,0),(1,0)]:
            r=self.simple(session=self.directory/f'expiry-{delta}.jsonl',settings={'clock':clock(delta)});self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(len(r['value']['memory_binding']['adopted']),adopted)
        self.assertEqual(self.listing()['memories'][0]['status'],'expired')
    def test_M13_old_candidate_no_reactivation(self):
        first=self.pending();self.confirm(first);self.control('revoke',self.command(first));command=self.command(first);self.assertEqual(self.control('confirm',command)['value']['error']['class'],'invalid_memory_state');self.assertEqual(self.listing()['memories'][0]['status'],'revoked')
    def test_M14_command_idempotency_conflict(self):
        item=self.pending();command=self.command(item);a=self.control('confirm',command);b=self.control('confirm',command);self.assertEqual(a['value'],b['value']);c=self.control('confirm',{**command,'valid_until':'2099-01-01T00:00:00+00:00'});self.assertEqual(c['value']['error']['class'],'command_conflict');self.assertEqual(len(self.listing()['commands']),1)

    def test_G01_revoke_before_worker_zero_http(self):
        _,item=self.activated()
        def hook(p,out,server):
            wait_for(lambda:(out/'release.hit').exists(),p);self.assertEqual(self.control('revoke',self.command(item))['exit_code'],0);(out/'release').touch()
        r=self.simple(scope=None,settings={'hold':'before_worker'},hook=hook);self.assertEqual(r['value']['status'],'memory_view_changed');self.assert_sent(r,0);self.assertEqual(r['value']['model_calls'],0);self.assertEqual(r['value']['reserved_model_calls'],1)
    def test_G02_worker_first_revoke_waits(self):
        _,item=self.activated();facts={}
        def hook(p,out,server):
            wait_for(lambda:(out/'release.hit').exists(),p);cp,cd,ca=self.control('revoke',self.command(item),async_=True);time.sleep(.12);self.assertIsNone(cp.poll());(out/'release').touch();facts['control']=self.collect(cp,cd,ca)['value']
        r=self.simple(scope=None,server_hold='headers',hook=hook);self.assert_sent(r,1);self.assertEqual(r['value']['status'],'memory_view_changed');receipt=load(r['runs']/'turn-001/receipt.json');self.assertGreater(facts['control']['committed_at'],receipt['memory_gate']['headers_at'])
    def test_G03_publication_order(self):
        for point,delivered in [('before_publish',False),('after_publish',True)]:
            item=self.pending();self.confirm(item)
            def hook(p,out,server):
                wait_for(lambda:(out/'release.hit').exists(),p);self.control('revoke',self.command(item));(out/'release').touch()
            r=self.simple(session=self.directory/(point+'.jsonl'),settings={'hold':point},hook=hook);self.assertEqual((r['runs']/'answer.md').exists(),delivered);self.assertEqual(r['value']['exit_code'],0 if delivered else 1)
            if delivered:self.assertEqual((r['runs']/'answer.md').read_text(),r['value']['text'])
            else:self.assertTrue((r['runs']/'unaccepted-answer.json').exists())
    def test_G04_response_revoke_no_delivery(self):
        _,item=self.activated()
        def hook(p,out,server):
            wait_for(lambda:(out/'release.hit').exists(),p);self.control('revoke',self.command(item));(out/'release').touch()
        r=self.simple(scope=None,settings={'hold':'headers'},hook=hook);self.assert_sent(r,1);self.assertEqual(r['value']['status'],'memory_view_changed');self.assertFalse((r['runs']/'answer.md').exists());self.assertTrue((r['runs']/'unaccepted-response-001.json').exists());events=rows(r['runs']/'events.jsonl');self.assertFalse(any(x.get('text') for x in events if x.get('event')=='text_delta'))
    def test_G05_cancel_and_gate_timeout(self):
        _,item=self.activated()
        def hook(p,out,server):
            wait_for(lambda:(out/'release.hit').exists(),p);os.kill(p.pid,signal.SIGINT);(out/'signal.json').write_text(json.dumps({'pid':p.pid,'signal':'SIGINT'}));time.sleep(.15);(out/'release').touch()
        r=self.simple(scope=None,server_hold='headers',hook=hook);self.assertEqual(r['value']['status'],'cancelled');self.assertFalse((r['runs']/'answer.md').exists());self.assertEqual(self.control('revoke',self.command(item))['exit_code'],0)
        command=self.command(item)
        import fcntl
        handle=open(str(self.db)+'.gate','r+');fcntl.flock(handle,fcntl.LOCK_EX)
        try:
            # 持有真实锁的父进程安排取消；控制 CLI 的轮询退出必须释放自身描述符。
            cp,cd,ca=self.control('revoke',command,async_=True);time.sleep(.15);os.kill(cp.pid,signal.SIGINT);result=self.collect(cp,cd,ca);self.assertEqual(result['value']['error']['class'],'cancelled')
            busy=self.control('revoke',command);self.assertEqual(busy['value']['error']['class'],'gate_busy');self.assertEqual(busy['exit_code'],2)
        finally:fcntl.flock(handle,fcntl.LOCK_UN);handle.close()
    def test_G06_old_resume_branch_blocked(self):
        _,item=self.activated();r=self.simple(scope=None);self.assertEqual(r['exit_code'],0,r['value']);before=self.session.read_bytes();self.control('revoke',self.command(item));r=self.simple(scope=None,command='resume');self.assertEqual(r['exit_code'],2);self.assert_sent(r,0);self.assertEqual(self.session.read_bytes(),before)
        p=subprocess.run([sys.executable,str(self.entry),'branch','--session',str(self.session),'--from-entry',next(x['entry_id'] for x in rows(self.session) if x.get('type')=='message' and x['message']['role']=='user')],capture_output=True);save(self.directory/'branch-result.json',{'exit_code':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()});self.assertEqual(p.returncode,2);self.assertEqual(json.loads(p.stdout)['error']['class'],'memory_view_changed');self.assertEqual(self.session.read_bytes(),before)
    def test_G07_expiration_without_epoch_change(self):
        from datetime import datetime,timezone,timedelta
        item=self.pending();until=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat();self.confirm(item,valid_until=until);epoch=self.listing()['meta']['effective_epoch'];r=self.simple(settings={'expire_after_view':until});self.assertEqual(r['value']['status'],'memory_view_changed');self.assert_sent(r,0);self.assertEqual(self.listing()['meta']['effective_epoch'],epoch)
    def test_G08_fresh_view_preserves_history(self):
        _,item=self.activated();r=self.simple(scope=None);old=self.session.read_bytes();self.control('revoke',self.command(item));r=self.simple(scope=None,session=self.directory/'fresh.jsonl');self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(self.session.read_bytes(),old);self.assertEqual(load(r['runs']/'projection-001.json')['memory']['reasons']['docs.answer_format'],'no_effective_memory');self.assertEqual(self.listing()['memories'][-1]['status'],'revoked')

    def duplicate_catalog(self):
        custom=self.directory/'copies';custom.mkdir();manifest=load(self.corpus/'manifest.json')
        selected=[x for x in manifest['sources'] if x['document_id'] in {'semicolon-keep','port-contract','feature-scope','invalid-inputs'}]
        for row in selected:shutil.copy2(self.corpus/row['path'],custom/row['path'])
        origin=next(x for x in selected if x['document_id']=='semicolon-keep')
        for n in (1,2):
            row={**origin,'document_id':f'semicolon-copy-{n}','path':f'semicolon-copy-{n}.md'};selected.append(row);shutil.copy2(self.corpus/origin['path'],custom/row['path'])
        manifest['sources']=selected;save(custom/'manifest.json',manifest);self.corpus=custom;self.index=self.directory/'copies.sqlite'
        proc=subprocess.run([sys.executable,str(self.entry),'corpus-build','--corpus',str(custom),'--index',str(self.index),'--embedding-model',str(self.model),'--output-dir',str(self.directory/'copy-build')],capture_output=True,timeout=180)
        (self.directory/'copy-build-stdout.json').write_bytes(proc.stdout);self.assertEqual(proc.returncode,0,proc.stdout)
        with sqlite3.connect(self.index.as_uri()+'?mode=ro',uri=True) as db:chunks=[json.loads(x[0]) for x in db.execute('SELECT payload_json FROM chunks')]
        catalog=copy.deepcopy(self.catalog);catalog['sources']=[]
        for name in ('semicolon-keep','semicolon-copy-1','semicolon-copy-2'):
            parts=[x for x in chunks if x['document_id']==name];original=next(x for x in self.catalog['sources'] if x['document_id']=='semicolon-keep')
            catalog['sources'].append({**original,'document_id':name,'source_sha256':parts[0]['source_sha256'],'document_revision':parts[0]['document_revision'],
                'sections':[{'section_id':'same-semicolon-section-'+str(i),'source_ranges':x['source_ranges'],'text_sha256':hashlib.sha256(x['text'].encode()).hexdigest()} for i,x in enumerate(parts)]})
        return catalog
    def test_R01_normal_queries(self):
        r=self.research_run([self.query('contract'),self.query('invalid_inputs'),self.query('support'),{'kind':'research_final'}]);self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(r['value']['searches'],3);self.assert_sent(r,4);state=load(r['runs']/'research.json');self.assertEqual(state['final']['unresolved'],['fixed_processing_time','input_validity']);self.assertEqual(len(state['queries']),3)
        for request in r['requests']:
            raw=json.dumps(request,ensure_ascii=False);self.assertIn('080;80;80',raw);self.assertIn('allow_normalization',raw)
    def test_R02_version_negation_and_bool(self):
        for changes in ({'task_revision':2},{'conditions':{**self.task['conditions'],'leading_zero_removed':True}},{'conditions':{**self.task['conditions'],'duplicates_removed':0}}):
            r=self.research_run([self.query(changes=changes)]*3,session=self.directory/(uuid.uuid4().hex+'.jsonl'));self.assertEqual(r['value']['status'],'research_rejections_exhausted');self.assertEqual(r['value']['searches'],0);self.assert_sent(r,3)
    def test_R03_task_group_batch_rejected(self):
        for scenario in ([self.query(changes={'task_id':'other'})]*3,[self.query(changes={'source_group':'internet'})]*3,[{'kind':'batch','calls':[{'name':'search_docs','arguments':{}},{'name':'search_docs','arguments':{}}]}]*3):
            r=self.research_run(scenario,session=self.directory/(uuid.uuid4().hex+'.jsonl'));self.assertEqual(r['value']['status'],'research_rejections_exhausted',r['value']);self.assertEqual(r['value']['searches'],0);self.assertEqual(r['value']['rejected'],3);self.assert_sent(r,3)
    def test_R04_empty_and_real_cpu_error(self):
        catalog=copy.deepcopy(self.catalog);catalog['sources']=[x for x in catalog['sources'] if x['group']!='support']
        r=self.research_run([self.query('support'),self.query('support'),{'kind':'research_final','stop_reason':'source_stagnation'}],catalog=catalog);self.assertEqual(r['exit_code'],0,r['value']);state=load(r['runs']/'research.json');self.assertTrue(all(x['empty_observation'] for x in state['queries']));self.assertEqual(state['forced_stop'],'source_stagnation')
        r=self.research_run([self.query('contract')],session=self.directory/'cpu-error.jsonl',settings={'embedding_failure':True});self.assertNotEqual(r['exit_code'],0);self.assert_sent(r,1);state=load(r['runs']/'research.json');self.assertEqual(state['queries'],[]);self.assertFalse((r['runs']/'answer.md').exists())
    def test_R05_current_citation_and_quote(self):
        relation={'input_validity':{'match':'ValueError','polarity':'contradicts','state':'contradicted'}}
        for mutation in ('quote','citation'):
            r=self.research_run([self.query('contract'),self.query('support',relations=relation,relation_mutation=mutation),self.query('support',relations=relation,relation_mutation=mutation),self.query('support',relations=relation,relation_mutation=mutation)],session=self.directory/(mutation+'.jsonl'))
            self.assertEqual(r['value']['status'],'research_rejections_exhausted',r['value']);self.assertEqual(r['value']['searches'],1);self.assertFalse((r['runs']/'answer.md').exists())
    def test_R06_actual_same_source_copies(self):
        catalog=self.duplicate_catalog();r=self.research_run([self.query('contract')]*3+[{'kind':'research_final','stop_reason':'source_stagnation'}],catalog=catalog);self.assertEqual(r['exit_code'],0,r['value']);state=load(r['runs']/'research.json');first=state['queries'][0];self.assertGreaterEqual(len(self.tools(r)[0]['hits']),3);self.assertEqual(len(first['returned_sections']),2);self.assertEqual(len(first['new_sections']),1);self.assertEqual([len(x['new_sections']) for x in state['queries']],[1,0,0]);self.assertEqual(state['forced_stop'],'source_stagnation')
    def test_R07_same_origin_new_sections(self):
        r=self.research_run([self.query('invalid_inputs'),self.query('contract'),self.query('support'),{'kind':'research_final'}]);self.assertEqual(r['exit_code'],0,r['value']);state=load(r['runs']/'research.json');origins=state['queries'][0]['new_sections'];self.assertGreaterEqual(len(origins),2);self.assertEqual(len({x[0] for x in origins}),1);self.assertEqual(len({x[2] for x in origins}),len(origins))
    def test_R08_counterevidence_reopens(self):
        support={'input_validity':{'match':'ValueError','polarity':'supports','state':'supported'}};counter={'input_validity':{'match':'ValueError','polarity':'contradicts','state':'contradicted'}}
        r=self.research_run([self.query('contract'),self.query('support',relations=support),self.query('invalid_inputs',relations=counter),{'kind':'research_final','relations':counter}]);self.assertEqual(r['exit_code'],0,r['value']);state=load(r['runs']/'research.json');history=[x for x in state['review_history'] if x['kind']=='model_proposal'];self.assertEqual(next(x for x in history[1]['after'] if x['claim_id']=='input_validity')['state'],'supported');self.assertEqual(next(x for x in history[2]['after'] if x['claim_id']=='input_validity')['state'],'contradicted');self.assertEqual(state['final']['semantic_review'],'required')
    def test_R09_no_fourth_search(self):
        r=self.research_run([self.query('contract'),self.query('invalid_inputs'),self.query('support'),self.query('contract')]);self.assertEqual(r['value']['status'],'research_search_limit',r['value']);self.assertEqual(r['value']['searches'],3);self.assertEqual(len(list(r['runs'].glob('search-*'))),3);self.assertFalse((r['runs']/'answer.md').exists())
        r=self.research_run([self.query('contract'),self.query('invalid_inputs'),self.query('support'),{'kind':'research_final'}],session=self.directory/'partial.jsonl');self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(load(r['runs']/'research.json')['final']['stop_reason'],'retrieval_budget')
    def test_R10_early_stop_and_stagnation(self):
        # 合法来源关系的机器检查不证明语义；此子例故意保留人工待审标签。
        catalog=copy.deepcopy(self.catalog);catalog['groups']['contract']=['input_validity','fixed_processing_time']
        relations={claim:{'match':'ValueError','polarity':'contradicts','state':'contradicted'} for claim in ('input_validity','fixed_processing_time')}
        r=self.research_run([self.query('contract'),{'kind':'research_final','relations':relations,'stop_reason':'evidence_sufficient'}],catalog=catalog);self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(r['value']['searches'],1);self.assert_sent(r,2);self.assertEqual(load(r['runs']/'research.json')['final']['semantic_review'],'required')
        r=self.research_run([self.query('contract')]*3+[{'kind':'research_final','stop_reason':'source_stagnation'}],session=self.directory/'stagnation.jsonl');self.assertEqual(r['exit_code'],0,r['value']);self.assertEqual(load(r['runs']/'research.json')['stagnant_searches'],2)

    def test_R11_invalid_json_has_one_bounded_correction(self):
        bad={'kind':'literal','text':'{"answer":"bad\ncontrol"}'}
        prefix=[self.query('contract'),self.query('invalid_inputs'),self.query('support')]
        corrected=self.research_run([*prefix,bad,{'kind':'research_final'}])
        self.assertEqual(corrected['exit_code'],0,corrected['value'])
        self.assert_sent(corrected,5)
        rejected=[e for e in rows(corrected['runs']/'events.jsonl') if e['event']=='answer_rejected']
        self.assertEqual(len(rejected),1)
        self.assertEqual(rejected[0]['effect'],'none')
        self.assertEqual(load(corrected['runs']/'turn-004/answer-candidate.json')['text'],bad['text'])
        self.assertNotEqual((corrected['runs']/'answer.md').read_text(),bad['text'])
        failed=self.research_run([*prefix,bad,bad],session=self.directory/'invalid-again.jsonl')
        self.assertEqual(failed['value']['status'],'invalid_json',failed['value'])
        self.assert_sent(failed,5)
        self.assertFalse((failed['runs']/'answer.md').exists())

def main():
    p=argparse.ArgumentParser();p.add_argument('--entry',type=Path,required=True);p.add_argument('--embedding-model',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--case',action='append');a=p.parse_args()
    root=a.output_dir.absolute()/('check-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True);CliChecks.entry=a.entry.absolute();CliChecks.model=Path(os.path.abspath(a.embedding_model));CliChecks.output_root=root
    suite=unittest.TestSuite(CliChecks(name) for name in a.case) if a.case else unittest.defaultTestLoader.loadTestsFromTestCase(CliChecks)
    result=unittest.TextTestRunner(verbosity=2).run(suite);save(root/'summary.json',{'evidence_source':'controlled-fixture+real-local-http-cpu','external_model_requests':0,'count':result.testsRun,'failures':[(str(t),v) for t,v in result.failures],'errors':[(str(t),v) for t,v in result.errors],'passed':result.wasSuccessful(),'semantic_review':'required'})
    print(json.dumps({'directory':str(root),'passed':result.wasSuccessful(),'count':result.testsRun}));return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
