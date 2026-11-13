"""W11 有界 A/B/C 采集；自动输入/控制标为 experiment-driver，语义独立复核。"""
from __future__ import annotations
import argparse,json,os,selectors,shutil,signal,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent
W10=MATERIALS.parents[2]/'week-10/practices/PRA-W10'
sys.path.insert(0,str(W10))
from prepare_corpus import prepare
from prepare_workspace import prepare as prepare_workspace
LIMITS={'A':8,'B':8,'C':8}
MAX_REQUESTS=24

def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def load(path):return json.loads(Path(path).read_text())
def records(path):return [json.loads(x) for x in path.read_bytes().splitlines() if x] if path.exists() else []
def events(attempt):return [x for p in (attempt/'runs').glob('*/events.jsonl') for x in records(p)]
def snapshot(source,target):
    target.mkdir(parents=True,exist_ok=False)
    for path in source.iterdir():
        if path.is_file() and not path.is_symlink():shutil.copy2(path,target/path.name)
def export_memory(db,target):
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        value={name:[dict(row) for row in connection.execute('SELECT * FROM '+name)] for name in ('meta','memories','commands','audit')}
    save(target,value);return value

def local(entry,args,directory,python):
    directory.mkdir(parents=True,exist_ok=False);argv=[python,str(entry),*args];save(directory/'invocation.json',{'argv':argv,'external_model_requests':0,'actor':'experiment-driver'})
    p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    (directory/'stdout.json').write_bytes(p.stdout);(directory/'stderr.txt').write_bytes(p.stderr);(directory/'exit-code.txt').write_text(str(p.returncode)+'\n')
    try:value=json.loads(p.stdout)
    except ValueError:value=None
    result={'exit_code':p.returncode,'result':value,'directory':str(directory)};save(directory/'collection.json',result);return result

def terminate(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)

LIVE_HOLD=r'''
import sys,json,runpy,time
from pathlib import Path
entry,gate=sys.argv[1:3];sys.path.insert(0,str(Path(entry).parent));import w11_stream
original=w11_stream.call_model_stream;used=False

def call(*args,**kwargs):
    old=kwargs.get('on_gate')
    def observe(record):
        global used
        if old:old(record)
        if record.get('headers_at') and not used:
            used=True;p=Path(gate);p.with_suffix('.hit.json').write_text(json.dumps(record))
            until=time.monotonic()+35
            while not p.exists():
                if time.monotonic()>until:raise RuntimeError('response acceptance gate expired')
                time.sleep(.015)
    kwargs['on_gate']=observe
    return original(*args,**kwargs)
w11_stream.call_model_stream=call
sys.argv=[entry,*sys.argv[3:]];runpy.run_path(entry,run_name='__main__')
'''

def collect(command,directory,phase,*,phase_reserved=0,total_reserved=0,evidence_source='live-external-api',on_tick=None):
    directory.mkdir(parents=True,exist_ok=False);p=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    save(directory/'invocation.json',{'argv':command,'phase':phase,'pid':p.pid,'evidence_source':evidence_source,'actor':'experiment-driver','phase_reserved_before':phase_reserved,'total_reserved_before':total_reserved,'started_at':time.time()})
    selector=selectors.DefaultSelector();selector.register(p.stdout,selectors.EVENT_READ,'stdout');selector.register(p.stderr,selectors.EVENT_READ,'stderr');buffers={'stdout':bytearray(),'stderr':bytearray()};deadline=time.monotonic()+315;reason=None;tick_error=None
    try:
        while selector.get_map() or p.poll() is None:
            requested=[r for r in events(directory) if r['event']=='model_requested']
            if reason is None and (phase_reserved+len(requested)>LIMITS[phase] or total_reserved+len(requested)>MAX_REQUESTS):reason='request_budget_exceeded';terminate(p)
            if reason is None and time.monotonic()>deadline:reason='supervisor_timeout';terminate(p)
            if on_tick and p.poll() is None:
                try:on_tick(p,directory)
                except BaseException as exc:tick_error=exc;reason='observer_error';terminate(p);on_tick=None
            for key,_ in selector.select(.03):
                data=os.read(key.fileobj.fileno(),65536)
                if data:buffers[key.data].extend(data)
                else:selector.unregister(key.fileobj)
        p.wait(timeout=5)
    finally:
        terminate(p);selector.close()
        for handle in (p.stdout,p.stderr):handle.close()
        (directory/'stdout.json').write_bytes(buffers['stdout']);(directory/'stderr.txt').write_bytes(buffers['stderr']);(directory/'exit-code.txt').write_text(str(p.returncode)+'\n')
    try:result=json.loads(buffers['stdout'])
    except (ValueError,UnicodeError):result=None
    requested=[r for r in events(directory) if r['event']=='model_requested'];paths=sorted((directory/'runs').glob('*/turn-*/receipt.json'));receipts=[load(x) for x in paths]
    counts={'response_headers':0,'not_started':0,'unknown':0}
    for receipt in receipts:
        sent=receipt.get('memory_gate',{}).get('sent_status','unknown')
        counts[sent if sent in counts else 'unknown']+=1
    counts['unknown']+=max(0,len(requested)-len(receipts));known=bool(receipts) and len(receipts)==len(requested) and all(x.get('usage',{}).get('usage_status')=='known' for x in receipts)
    value={'directory':str(directory),'result':result,'exit_code':p.returncode,'reserved_model_calls':len(requested),'actual_model_calls':None if counts['unknown'] else counts['response_headers'],
           'delivery':counts,'usage':{'usage_status':'known' if known else 'usage_unknown','total_tokens':sum(x['usage']['total_tokens'] for x in receipts) if known else None},
           'supervisor_stop_reason':reason,'evidence_source':evidence_source,'observer_error':None if tick_error is None else {'class':type(tick_error).__name__,'message':str(tick_error)}}
    save(directory/'collection.json',value);return value


def run_batch(student,model,output,*,python=sys.executable,command_prefix=None,evidence_source='live-external-api'):
    batch=output/('batch-'+uuid.uuid4().hex[:12]);batch.mkdir(parents=True,exist_ok=False);entry=student/'main.py'
    corpus=batch/'corpus';prepare(corpus);workspace=prepare_workspace(batch/'workspace');index=batch/'index.sqlite';db=batch/'memory.sqlite';inputs=batch/'inputs';inputs.mkdir()
    for name in ('research-task.json','research-catalog.json','question-research.md','question-propose.md','question-update.md','manual-decisions.json'):shutil.copy2(MATERIALS/'inputs'/name,inputs/name)
    save(batch/'configuration.json',{'entry':str(entry),'python':python,'embedding_model':str(model),'corpus':str(corpus),'index':str(index),'memory_db':str(db),'workspace':str(workspace),'limits':LIMITS,'max_model_requests':MAX_REQUESTS,'max_tokens':1536,'context_tokens':8192,'actor':'experiment-driver','evidence_source':evidence_source,'semantic_review':'required'})
    phases=[];total=0;error=None
    def require(value,message):
        if value['exit_code']!=0 or not value.get('result'):raise RuntimeError(message)
        return value['result']
    try:
        require(local(entry,['corpus-build','--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),'--output-dir',str(batch/'build/runs')],batch/'build',python),'CPU索引构建失败')
        require(local(entry,['memory-init','--memory-db',str(db),'--workspace',str(workspace)],batch/'initialize',python),'memory初始化失败')
        for phase in ('A','B','C'):
            directory=batch/phase;directory.mkdir();phase_reserved=0;attempts=[];controls=[];row={'phase':phase,'attempts':attempts,'controls':controls,'status':'started','semantic_review':'required'};phases.append(row)
            save(directory/'phase-config.json',{'phase':phase,'actor':'experiment-driver','request_limit':8,'evidence_source':evidence_source});snapshot(corpus,directory/'corpus-before');snapshot(workspace,directory/'workspace-before');export_memory(db,directory/'memory-before.json')
            def listing(label):return require(local(entry,['memory-list','--memory-db',str(db),'--workspace',str(workspace)],directory/label,python),'memory-list失败')
            def control(action,item,label,**extra):
                state=listing(label+'-review');candidate=next(x for x in state['memories'] if x['memory_id']==item['memory_id'])
                save(directory/(label+'-decision.json'),{'actor':'experiment-driver','candidate':candidate,'decision':action,'semantic_review':'required','purpose':'固定实验条件下的显式控制；未替代真人意图判断'})
                command={'schema':1,'command_id':uuid.uuid4().hex,'expected_revision':state['meta']['control_revision'],'memory_id':item['memory_id'],**extra};path=directory/(label+'-command.json');save(path,command)
                result=local(entry,['memory-'+action,'--memory-db',str(db),'--workspace',str(workspace),'--command',str(path),'--output-dir',str(directory/label/'receipt'),'--actor','experiment-driver'],directory/label,python);controls.append(result);return require(result,'memory控制失败')
            def attempt(label,question,*,research=True,scope=None,hold=False):
                nonlocal total,phase_reserved
                remaining=min(6,8-phase_reserved,24-total)
                if remaining<1:raise RuntimeError('阶段或总模型预算用完')
                attempt_dir=directory/label;session=directory/(label+'.jsonl');args=['new','--session',str(session),'--workspace',str(workspace),'--question',str(inputs/question),'--output-dir',str(attempt_dir/'runs'),'--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),'--memory-db',str(db),'--input-source','experiment-driver','--max-turns',str(remaining),'--max-actions','3','--max-tokens','1536','--context-tokens','8192','--deadline-seconds','300']
                if scope:args+=['--doc-scope',scope]
                if research:args+=['--research-task',str(inputs/'research-task.json'),'--research-catalog',str(inputs/'research-catalog.json')]
                command=[python,str(entry),*args];gate=directory/'acceptance-release';observed={'handled':False}
                if hold:
                    wrapper=directory/'hold-response.py';wrapper.write_text(LIVE_HOLD);command=[python,str(wrapper),str(entry),str(gate),*args]
                if command_prefix:command=command_prefix(command,phase,label,attempt_dir,gate if hold else None)
                def tick(process,_):
                    marker=gate.with_suffix('.hit.json')
                    if marker.exists() and not observed['handled']:
                        observed['handled']=True;observed['gate']=load(marker)
                        current=listing('C-observed-current');selected=[x for x in current['memories'] if x['key']=='docs.answer_format' and x['status']=='active']
                        if len(selected)!=1:raise RuntimeError('C没有唯一活动format撤回对象')
                        observed['control']=control('revoke',selected[0],'withdraw-format');save(directory/'withdraw-window.json',observed);gate.touch()
                value=collect(command,attempt_dir,phase,phase_reserved=phase_reserved,total_reserved=total,evidence_source=evidence_source,on_tick=tick if hold else None)
                phase_reserved+=value['reserved_model_calls'];total+=value['reserved_model_calls'];attempts.append(value)
                if session.exists():shutil.copy2(session,attempt_dir/'session-snapshot.jsonl')
                if hold:save(directory/'withdraw-window.json',observed)
                if value['delivery']['unknown'] or value['supervisor_stop_reason']:raise RuntimeError('请求交接未知或监督失败，停止本批')
                return value
            if phase in {'A','B'}:
                proposal=attempt('proposal','question-propose.md' if phase=='A' else 'question-update.md',research=False,scope='semicolon_keep')
                if proposal['exit_code']!=0:raise RuntimeError('提议阶段未成功结束')
                state=listing('proposal-review');expected={'docs.default_scope':'semicolon_keep','docs.answer_format':'table'} if phase=='A' else {'docs.answer_format':'list'}
                for key,value in expected.items():
                    candidates=[x for x in state['memories'] if x['status']=='pending' and x['key']==key and x['value']==value and x['source_ref']['session_path']==str(directory/'proposal.jsonl')]
                    if len(candidates)!=1:raise RuntimeError('固定实验候选缺失或有歧义：'+key)
                    extra={}
                    if phase=='B':
                        old=[x for x in state['memories'] if x['key']==key and x['status']=='active']
                        if len(old)!=1:raise RuntimeError('替代目标不唯一')
                        extra['supersedes']=old[0]['memory_id']
                    control('confirm',candidates[0],'confirm-'+key.rsplit('.',1)[-1],**extra)
                if phase=='B':
                    prior=batch/'A/research.jsonl';before=prior.read_bytes();stale=local(entry,['branch','--session',str(prior),'--from-entry',next(r['entry_id'] for r in records(prior) if r.get('type')=='message' and r['message']['role']=='user')],directory/'old-session-branch',python)
                    if stale['exit_code']!=2 or stale['result']['error']['class']!='memory_view_changed' or prior.read_bytes()!=before:raise RuntimeError('旧会话未按资格阻断')
                result=attempt('research','question-research.md')
                if result['exit_code']!=0:raise RuntimeError('研究会话未成功结束')
            else:
                old=attempt('withdrawal','question-research.md',hold=True)
                if not load(directory/'withdraw-window.json')['handled'] or old['result']['status']!='memory_view_changed':raise RuntimeError('C撤回窗口未命中')
                if any((directory/'withdrawal/runs').glob('*/answer.md')):raise RuntimeError('C旧答案已交付')
                result=attempt('fresh','question-research.md')
                if result['exit_code']!=0:raise RuntimeError('C新视图未成功结束')
            row.update(status='collected',reserved_model_calls=phase_reserved)
            snapshot(corpus,directory/'corpus-after');snapshot(workspace,directory/'workspace-after');export_memory(db,directory/'memory-after.json');save(directory/'phase-result.json',row)
            print(json.dumps({'phase':phase,'status':row['status'],'reserved_model_calls':phase_reserved},ensure_ascii=False),flush=True)
        return batch,0
    except BaseException as exc:
        error={'class':type(exc).__name__,'message':str(exc)};save(batch/'driver-error.json',error)
        if phases:phases[-1]['status']='failed';export_memory(db,batch/phases[-1]['phase']/'memory-after.json')
        return batch,1
    finally:
        attempts=[a for p in phases for a in p['attempts']];known=bool(attempts) and all(a['usage']['usage_status']=='known' for a in attempts)
        save(batch/'summary.json',{'phases':phases,'reserved_model_calls':total,'actual_model_calls':None if any(a['actual_model_calls'] is None for a in attempts) else sum(a['actual_model_calls'] for a in attempts),'limits':LIMITS,'max_model_requests':24,'evidence_source':evidence_source,'actor':'experiment-driver','usage':{'usage_status':'known' if known else 'usage_unknown','total_tokens':sum(a['usage']['total_tokens'] for a in attempts) if known else None},'error':error,'semantic_review':'required'})

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('student-dir','embedding-model','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--python',default=sys.executable);a=p.parse_args()
    if not os.environ.get('DEEPSEEK_API_KEY') or not os.environ.get('DEEPSEEK_MODEL'):p.error('请提供已有DeepSeek环境配置；本命令会执行唯一有界真实批次。')
    batch,code=run_batch(a.student_dir.absolute(),Path(os.path.abspath(a.embedding_model)),a.output_dir.absolute(),python=a.python);print(json.dumps({'batch':str(batch),'exit_code':code}));return code
if __name__=='__main__':raise SystemExit(main())
