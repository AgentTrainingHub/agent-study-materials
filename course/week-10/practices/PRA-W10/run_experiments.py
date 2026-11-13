"""采集 W10 三个新会话的真实检索回答与 3→5→撤回变化；最多12次模型请求。"""
from __future__ import annotations
import argparse,difflib,json,os,selectors,shutil,signal,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
from prepare_corpus import prepare,change
from prepare_workspace import prepare as prepare_workspace
MATERIALS=Path(__file__).resolve().parent
LIMITS={'A':4,'B':4,'C':4};MAX_REQUESTS=12

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def records(path):
    if not path.exists():return []
    return [json.loads(line) for line in path.read_bytes().split(b'\n')[:-1] if line]

def events(directory):
    return [row for path in sorted((directory/'runs').glob('*/events.jsonl')) for row in records(path)]

def snapshot(source,target):
    target.mkdir(parents=True,exist_ok=False)
    for path in sorted(source.iterdir()):
        if path.is_file() and not path.is_symlink():shutil.copyfile(path,target/path.name)

def export_index(index,target):
    with sqlite3.connect(index.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        tables={name:[dict(row) for row in db.execute('SELECT * FROM '+name)] for name in ('meta','generations','chunks','queries')}
    save(target,tables)
    return tables

def terminate(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)

def collect(command,directory,phase,*,previous_calls=0,evidence_source='live-external-api'):
    directory.mkdir(parents=True,exist_ok=False)
    invocation={'argv':command,'phase':phase,'evidence_source':evidence_source,'started_at':time.time(),
                'phase_model_request_limit':LIMITS[phase],'global_request_limit':MAX_REQUESTS,'previous_calls':previous_calls}
    process=subprocess.Popen(command,cwd=Path(command[1]).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL)
    invocation['pid']=process.pid;save(directory/'invocation.json',invocation)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ,'stdout');selector.register(process.stderr,selectors.EVENT_READ,'stderr')
    buffers={'stdout':bytearray(),'stderr':bytearray()};observed=[];reason=None;deadline=time.monotonic()+315
    try:
        while selector.get_map() or process.poll() is None:
            requested=[row for row in events(directory) if row['event']=='model_requested']
            for row in requested[len(observed):]:
                record={key:row[key] for key in ('run_id','turn','purpose','seq')};record['observed_at']=time.time();observed.append(record)
                with (directory/'request-observations.jsonl').open('a') as handle:handle.write(json.dumps(record)+'\n')
            if reason is None and (len(requested)>LIMITS[phase] or previous_calls+len(requested)>MAX_REQUESTS):reason='request_budget_exceeded';terminate(process);deadline=time.monotonic()+5
            if reason is None and time.monotonic()>deadline:reason='supervisor_timeout';terminate(process);deadline=time.monotonic()+5
            if reason is not None and time.monotonic()>deadline:break
            for key,_ in selector.select(timeout=0.05):
                part=os.read(key.fileobj.fileno(),65536)
                if not part:selector.unregister(key.fileobj)
                else:buffers[key.data].extend(part)
        process.wait(timeout=5)
    finally:
        terminate(process);selector.close()
        for handle in (process.stdout,process.stderr):handle.close()
        (directory/'stdout.json').write_bytes(buffers['stdout']);(directory/'stderr.txt').write_bytes(buffers['stderr']);(directory/'exit-code.txt').write_text(str(process.returncode)+'\n')
    try:result=json.loads(buffers['stdout'])
    except (ValueError,UnicodeError):result=None
    final=events(directory);requests=[row for row in final if row['event']=='model_requested']
    receipts=[json.loads(path.read_text()) for path in sorted((directory/'runs').glob('*/turn-*/receipt.json'))]
    known=bool(requests) and len(receipts)==len(requests) and all(r.get('usage',{}).get('usage_status')=='known' for r in receipts)
    value={'directory':str(directory),'result':result,'exit_code':process.returncode,'actual_model_calls':len(requests),
           'model_count_status':'known' if final else 'no_events','usage':{'usage_status':'known' if known else 'usage_unknown',
               'total_tokens':sum(r['usage']['total_tokens'] for r in receipts) if known else None},
           'supervisor_stop_reason':reason,'receipts':len(receipts),'evidence_source':evidence_source}
    save(directory/'collection.json',value);return value

def local(entry,args,directory,python):
    directory.mkdir(parents=True,exist_ok=False);argv=[python,str(entry),*args];save(directory/'invocation.json',{'argv':argv,'external_model_requests':0,'evidence_source':'real-local-cpu'})
    p=subprocess.run(argv,capture_output=True,timeout=310);(directory/'stdout.json').write_bytes(p.stdout);(directory/'stderr.txt').write_bytes(p.stderr);(directory/'exit-code.txt').write_text(str(p.returncode)+'\n')
    try:result=json.loads(p.stdout)
    except ValueError:result=None
    value={'exit_code':p.returncode,'result':result,'directory':str(directory)};save(directory/'collection.json',value);return value

def run_batch(student,model,output,*,python=sys.executable,command_prefix=None,evidence_source='live-external-api'):
    batch=output/('batch-'+uuid.uuid4().hex[:12]);batch.mkdir(parents=True,exist_ok=False)
    corpus=batch/'corpus';prepare(corpus);workspace=prepare_workspace(batch/'workspace');index=batch/'index.sqlite';entry=student/'main.py'
    inputs=batch/'inputs';inputs.mkdir()
    for name in ('question-a.md','question-b.md','question-c.md','gold.json','questions.json'):shutil.copyfile(MATERIALS/'inputs'/name,inputs/name)
    save(batch/'configuration.json',{'entry':str(entry),'python':python,'embedding_model':str(model),'corpus':str(corpus),'index':str(index),'workspace':str(workspace),
        'scope':'semicolon_keep','limits':LIMITS,'max_model_requests':12,'max_tokens':1536,'context_tokens':8192,'max_actions':2,'evidence_source':evidence_source,'semantic_review':'required'})
    rows=[];total=0;error=None
    try:
        for phase in ('A','B','C'):
            directory=batch/phase;directory.mkdir();save(directory/'phase-config.json',{'phase':phase,'scope':'semicolon_keep','mode':'hybrid','rerank':'on' if phase=='A' else 'off','evidence_source':evidence_source})
            snapshot(corpus,directory/'corpus-before');snapshot(workspace,directory/'workspace-before')
            if phase!='A':
                action='update' if phase=='B' else 'withdraw';change(corpus,action,directory/'change')
                before=(directory/'change/source-before.md').read_text();after=(directory/'change/source-after.md').read_text()
                (directory/'change/source.diff').write_text(''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='before/support-guide.md',tofile='after/support-guide.md')))
                stale=local(entry,['corpus-query','--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),'--doc-scope','semicolon_keep',
                    '--question-file',str(inputs/f'question-{phase.lower()}.md'),'--output-dir',str(directory/'stale-query/runs')],directory/'stale-query',python)
                if stale['exit_code']!=2 or stale['result']['error']['class'] not in {'source_changed','index_stale'}:raise RuntimeError('旧代次未明确拒绝变化')
            build=local(entry,['corpus-build','--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),'--output-dir',str(directory/'build/runs')],directory/'build',python)
            if build['exit_code']!=0:raise RuntimeError('本地编码/原子发布失败')
            snapshot(corpus,directory/'corpus-current');export_index(index,directory/'index-before.json')
            session=directory/'session.jsonl';attempt=directory/'attempt-01'
            command=[python,str(entry),'new','--session',str(session),'--workspace',str(workspace),'--question',str(inputs/f'question-{phase.lower()}.md'),
                '--output-dir',str(attempt/'runs'),'--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),'--doc-scope','semicolon_keep',
                '--retrieval-mode','hybrid','--rerank','on' if phase=='A' else 'off','--max-turns','4','--max-actions','2','--max-tokens','1536','--context-tokens','8192','--deadline-seconds','300']
            if command_prefix:command=command_prefix(command,phase,attempt)
            collected=collect(command,attempt,phase,previous_calls=total,evidence_source=evidence_source);total+=collected['actual_model_calls']
            row={'phase':phase,'attempt':collected,'status':collected['result']['status'] if collected['result'] else 'invalid_stdout','semantic_review':'required'};rows.append(row)
            snapshot(corpus,directory/'corpus-after');snapshot(workspace,directory/'workspace-after');export_index(index,directory/'index-after.json')
            if session.exists():shutil.copyfile(session,directory/'after-session.jsonl')
            if phase!='A':
                previous=batch/('A' if phase=='B' else 'B')/'session.jsonl'
                inspected=local(entry,['inspect','--session',str(previous)],directory/'old-session-inspect',python)
                if inspected['exit_code']!=0:raise RuntimeError('旧历史不可读取')
            print(json.dumps({'phase':phase,'status':row['status'],'actual_model_calls':collected['actual_model_calls'],'usage':collected['usage']},ensure_ascii=False),flush=True)
            if collected['exit_code'] or row['status']!='stop' or collected['supervisor_stop_reason']:return batch,1
        return batch,0
    except BaseException as exc:
        error={'class':type(exc).__name__,'message':str(exc)};save(batch/'driver-error.json',error);raise
    finally:
        known=bool(rows) and all(r['attempt']['usage']['usage_status']=='known' for r in rows)
        save(batch/'summary.json',{'phases':rows,'completed_phase_count':len(rows),'actual_model_calls':total,'model_request_limit':12,'evidence_source':evidence_source,
            'usage':{'usage_status':'known' if known else 'usage_unknown','total_tokens':sum(r['attempt']['usage']['total_tokens'] for r in rows) if known else None},'error':error,'semantic_review':'required'})

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('student-dir','embedding-model','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--python',default=sys.executable);a=p.parse_args()
    if not os.environ.get('DEEPSEEK_API_KEY') or not os.environ.get('DEEPSEEK_MODEL'):p.error('先设置已有 DEEPSEEK_API_KEY 与 DEEPSEEK_MODEL')
    student=a.student_dir.resolve(strict=True)
    for name in ('w10_io.py','embedding_worker.py','w10_model_manifest.json','bm25.py'):
        if (student/name).read_bytes()!=(MATERIALS/name).read_bytes():p.error('先安装当前 W10 固定供给')
    batch,code=run_batch(student,a.embedding_model.absolute(),a.output_dir.absolute(),python=a.python)
    print(json.dumps({'batch':str(batch),'exit_code':code},ensure_ascii=False));return code
if __name__=='__main__':raise SystemExit(main())
