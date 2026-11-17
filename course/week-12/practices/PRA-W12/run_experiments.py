"""采集唯一有界的真实 A/B/C；不会替换模型响应。"""
from __future__ import annotations
import argparse,hashlib,json,os,signal,subprocess,sys,threading,time,uuid
from pathlib import Path
from w12_site import Site,task,canonical,FIELDS
from w12_io import save,append

def allowed_candidate(candidate,site,seen):
    payload=candidate.get('payload',{});scheme='ordered-v1' if site.task['duplicate_requirement']=='preserve' else 'unique-v1'
    digest=hashlib.sha256(canonical({k:v for k,v in candidate.items() if k!='digest'}).encode()).hexdigest()
    expected={'site_id':site.site_id,'task_id':site.task['task_id'],'dataset_version':site.version,'scheme_id':scheme,'scheme_version':'v1',
              'input_text':site.task['input_text'],'duplicate_requirement':site.task['duplicate_requirement']}
    return (candidate.get('origin')==site.origin and candidate.get('post_url')==site.origin+'/api/selection'
        and tuple(payload)==FIELDS and all(payload.get(k)==v for k,v in expected.items())
        and isinstance(payload.get('action_id'),str) and payload['action_id'].startswith('act_')
        and isinstance(payload.get('note'),str) and 0<len(payload['note'])<=200
        and candidate.get('candidate_id') not in seen and candidate.get('valid_for_seconds')==60
        and type(candidate.get('created_at')) in {int,float} and type(candidate.get('expires_at')) in {int,float}
        and candidate['created_at']<=time.time()<candidate['expires_at'] and candidate['expires_at']-candidate['created_at']==60
        and candidate.get('digest')==digest and isinstance(candidate.get('worker_pid'),int))

def collect(entry,browser_python,output,*,budget=10,deadline=600):
    batch=Path(output)/('batch-'+uuid.uuid4().hex[:12]);batch.mkdir(parents=True)
    save(batch/'batch-plan.json',{'schema':1,'phase_limits':{'A':budget,'B':budget,'C':budget},'total_model_call_limit':budget*3,
        'provider':'DeepSeek','model':os.environ.get('DEEPSEEK_MODEL'),'response_source':'real-provider',
        'approval_source':'experiment-driver','approval_policy':'exact fixed task, scheme, target and bounded note; record each candidate',
        'deadline_seconds':deadline,'created_at':time.time()})
    phases=[]
    for name,duplicate,mode in (('A','preserve','normal'),('B','reject','lose_reply'),('C','preserve','reject')):
        directory=batch/name;directory.mkdir();site=Site(directory/'site',task(duplicate),mode=mode).start()
        workspace=directory/'workspace';workspace.mkdir();session=directory/'session.jsonl'
        argv=[sys.executable,str(entry),'new','--session',str(session),'--workspace',str(workspace),'--question',str(directory/'site/question.md'),
              '--output-dir',str(directory/'runs'),'--browser-manifest',str(directory/'site/manifest.json'),'--browser-task',str(directory/'site/task.json'),
              '--browser-python',str(browser_python),'--approval-source','experiment-driver','--max-turns',str(budget),'--deadline-seconds',str(deadline)]
        save(directory/'invocation.json',{'argv':argv,'site_mode':mode,'site_origin':site.origin,'duplicate_requirement':duplicate})
        process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE);stderr=[];stdout_parts=[];seen=set()
        def decisions():
            for raw in iter(process.stderr.readline,b''):
                stderr.append(raw)
                if raw.startswith(b'APPROVAL '):
                    candidate=json.loads(raw[len(b'APPROVAL '):]);approved=allowed_candidate(candidate,site,seen);seen.add(candidate.get('candidate_id'))
                    append(directory/'driver-decisions.jsonl',{'source':'experiment-driver','candidate':candidate,'approved':approved,'at':time.time()})
                    try:process.stdin.write(('approve\n' if approved else 'deny\n').encode());process.stdin.flush()
                    except BrokenPipeError:pass
        thread=threading.Thread(target=decisions,daemon=True);thread.start()
        reader=threading.Thread(target=lambda:stdout_parts.append(process.stdout.read()),daemon=True);reader.start()
        try:
            try:process.wait(timeout=deadline+30)
            except subprocess.TimeoutExpired:
                append(directory/'driver-errors.jsonl',{'class':'process_timeout','at':time.time(),'pid':process.pid})
                process.send_signal(signal.SIGINT)
                try:process.wait(timeout=30)
                except subprocess.TimeoutExpired:process.kill();process.wait()
            reader.join(5);thread.join(5);stdout=b''.join(stdout_parts)
            (directory/'stdout.json').write_bytes(stdout);(directory/'stderr.txt').write_bytes(b''.join(stderr));(directory/'exit-code.txt').write_text(str(process.returncode)+'\n')
            try:result=json.loads(stdout)
            except ValueError:result={'status':'invalid_stdout','exit_code':process.returncode,'model_calls':None}
            save(directory/'site-export.json',site.export())
            phases.append({'phase':name,'directory':str(directory),'result':result,'exit_code':process.returncode})
        finally:
            if process.poll() is None:process.kill();process.wait()
            site.close();process.stdin.close();process.stdout.close();process.stderr.close()
        counts=[v['result'].get('model_calls') for v in phases]
        save(batch/'batch.json',{'phases':phases,'model_calls':sum(counts) if all(type(v)is int for v in counts) else None,'complete':len(phases)==3,'semantic_review':'required'})
    return batch
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--entry',type=Path,required=True);p.add_argument('--browser-python',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--max-turns',type=int,default=10);a=p.parse_args()
    if not 1<=a.max_turns<=10:p.error('每组请求上限必须为 1～10。')
    print(canonical({'batch':str(collect(a.entry.absolute(),a.browser_python.absolute(),a.output_dir.absolute(),budget=a.max_turns))}))
if __name__=='__main__':main()
