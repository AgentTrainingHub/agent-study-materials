"""公开 CLI 检查：禁网受控 SSE；真实 Chrome、HTTP、SQLite 与批准输入。"""
from __future__ import annotations
import argparse, importlib.util, json, os, selectors, signal, socket, subprocess, sys, threading, time, unittest, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from w12_site import Site,task,question,canonical
MATERIALS=Path(__file__).resolve().parent
def save(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def rows(path):return [json.loads(v) for v in Path(path).read_text().splitlines() if v] if Path(path).exists() else []

WORKER=r'''
import base64,json,os,socket,sys,time
from pathlib import Path
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('controlled model worker forbids network'))
scenario,audit=map(Path,sys.argv[1:3]);request=json.load(sys.stdin)['request'];steps=json.loads(scenario.read_text());index=len(audit.read_text().splitlines()) if audit.exists() else 0
with audit.open('a') as f:f.write(json.dumps({'request':request,'index':index,'pid':os.getpid(),'controlled':True},ensure_ascii=False)+'\n')
step=steps[index]
if step.get('control'):
 marker=audit.with_name('control-'+str(index)+'.json');marker.write_text(json.dumps(step['control']))
 until=time.monotonic()+5
 while not marker.with_suffix('.done').exists():
  if time.monotonic()>until:raise RuntimeError('external control did not acknowledge')
  time.sleep(.01)
messages=[]
for m in request['messages']:
 if m['role']=='tool':
  try:messages.append(json.loads(m['content']))
  except ValueError:pass
observations=[m for m in messages if m.get('observation_id')];latest=observations[-1] if observations else {}
kind=step['kind'];finish='tool_calls';content='';calls=[]
if kind=='open':name='browser_open';args={'path':step.get('path','/ordered')}
elif kind=='observe':name='browser_observe';args={}
elif kind=='act':
 name='browser_act';args={'observation_id':step.get('observation_id',latest.get('observation_id','missing')),'node_id':step['node'],'action':step['action']}
 if 'value' in step:args['value']=step['value']
elif kind=='query':name='browser_query';args={'action_id':step.get('action_id',next((m['action_id'] for m in reversed(messages) if m.get('action_id')),'none'))}
elif kind=='custom':name=step['name'];args=step['args']
elif kind=='final':
 finish='stop';binding=json.loads(request['messages'][0]['content'].split('宿主浏览器绑定：',1)[1]);task=binding['task'];manifest=binding['manifest']
 refs={}
 for obs in observations:
  sid=obs['source']['source_id'];line=obs['untrusted_page_lines'][0];refs[sid]={'source_ref':obs['source']['source_ref'],'start_line':line['line'],'end_line':line['line'],'quote':line['text']}
 aid=next((m['action_id'] for m in reversed(messages) if m.get('action_id')),None)
 receipt=next((m for m in reversed(messages) if m.get('receipt')),None)
 selected='ordered-v1' if task['duplicate_requirement']=='preserve' else 'unique-v1'
 claims=[('Q1','supported','选择与重复项条件相符。','ordered' if selected=='ordered-v1' else 'unique'),('Q2','supported','两方案保留原输入边界。','boundaries'),('Q3','unknown','旧版小样本不能保证当前固定处理耗时。','benchmark')]
 answers=[{'question_id':q,'status':status if sid in refs else 'unknown','claim':claim,'citations':[refs[sid]] if sid in refs else [],'condition_note':'保持原始输入；缺少当前全输入性能保证。'} for q,status,claim,sid in claims]
 obj={'task_id':task['task_id'],'dataset_version':manifest['dataset_version'],'selected_scheme':selected,'answers':answers,'limitations':['受控答案仅检查结构，语义需要独立复核。'],
      'registration':{'action_id':aid,'status':receipt['receipt']['status'] if receipt else 'not_attempted','receipt_ref':receipt.get('receipt_ref') if receipt else None}}
 if step.get('multiline_quotes'):
  for answer in obj['answers']:
   for citation in answer['citations']:
    obs=next(o for o in observations if o['source']['source_ref']==citation['source_ref']);lines=obs['untrusted_page_lines']
    citation.update(start_line=lines[0]['line'],end_line=lines[-1]['line'],quote='\n'.join(v['text'] for v in lines))
 mutation=step.get('mutation')
 if mutation=='quote':obj['answers'][0]['citations'][0]['quote']='不存在的引文'
 if mutation=='source':obj['answers'][0]['citations'][0]['source_ref']='other-run-unknown'
 if mutation=='coverage':obj['answers']=obj['answers'][:-1]
 if mutation=='receipt':obj['registration']['status']='committed'
 if mutation=='task':obj['task_id']='old-task'
 content=json.dumps(obj,ensure_ascii=False)
elif kind=='literal':finish='stop';content=step['text']
elif kind=='cancel':os.kill(os.getppid(),2);time.sleep(.2);finish='stop';content='cancelled'
else:raise RuntimeError('unknown controlled scenario')
if finish=='tool_calls':
 calls=[{'index':0,'id':'c-'+str(index),'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]
 if step.get('multi'):calls.append({'index':1,'id':'extra','type':'function','function':{'name':name,'arguments':json.dumps(args)}})
finish=step.get('finish',finish)
def emit(v):print(json.dumps(v),flush=True)
emit({'type':'headers','status':200,'request_id':'controlled-'+str(index)})
def frame(delta,reason=None,usage=False):
 value={'id':'controlled-w12','model':'controlled-model','choices':[{'index':0,'delta':delta,'finish_reason':reason}]}
 if usage:value['usage']={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
 return ('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode()
frames=[frame({'role':'assistant'})]
if content:frames.append(frame({'content':content}))
if calls:frames.append(frame({'tool_calls':calls}))
frames+=[frame({},finish,True),b'data: [DONE]\n\n']
for raw in frames:
 for part in (raw[:13],raw[13:]):
  if part:emit({'type':'chunk','data':base64.b64encode(part).decode(),'arrival_ms':0})
emit({'type':'end'})
'''
WRAPPER=r'''
import os,runpy,socket,sys
from pathlib import Path
entry,worker,scenario,audit=map(Path,sys.argv[1:5]);sys.path.insert(0,str(entry.parent))
import w05_support,w02_support
w05_support._stream_worker_command=lambda:[sys.executable,str(worker),str(scenario),str(audit)]
w02_support.EVIDENCE_SOURCE='controlled-sse+real-local-browser'
def forbidden(*a,**k):raise AssertionError('browser mode started unrelated component')
import w03_shell,w09_shell,mcp_tools,resources
w03_shell.preflight=forbidden;w09_shell.preflight=forbidden;mcp_tools.McpHost.open=forbidden;resources.discover=forbidden
socket.socket.connect=forbidden;socket.create_connection=forbidden
sys.argv=[str(entry),*sys.argv[5:]];runpy.run_path(str(entry),run_name='__main__')
'''

def normal_steps():
    return [*({'kind':'open','path':p} for p in ('/ordered','/unique','/boundaries','/benchmark')),
            {'kind':'act','node':'select-ordered','action':'click'},{'kind':'act','node':'note','action':'fill','value':'保留原样重复项'},
            {'kind':'act','node':'submit','action':'submit'},{'kind':'query'},{'kind':'final'}]

class Sink(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):self.server.received.append(self.path);self.send_response(200);self.end_headers()

class CliChecks(unittest.TestCase):
    def setUp(self):
        self.directory=self.output_root/self._testMethodName;self.directory.mkdir();self.invocations=[]
        self.sink=ThreadingHTTPServer(('127.0.0.1',0),Sink);self.sink.received=[];self.sinkthread=threading.Thread(target=self.sink.serve_forever,daemon=True);self.sinkthread.start()
    def tearDown(self):
        self.sink.shutdown();self.sink.server_close();self.sinkthread.join(2)
        save(self.directory/'facts.json',{'invocations':self.invocations,'other_origin_received':self.sink.received,'external_model_requests':0})
    def invoke(self,steps,*,mode='normal',duplicate='preserve',approve='approve',approval_mode=None,approval_delay=0,extra=(),cancel_approval=False,question_bad=False,init_failure=False):
        directory=self.directory/f'cli-{len(self.invocations)+1:02d}';directory.mkdir();site=Site(directory/'site',task(duplicate),mode=mode).start();site.other_origin='http://127.0.0.1:'+str(self.sink.server_port)
        workspace=directory/'workspace';workspace.mkdir();worker=directory/'model-worker.py';worker.write_text(WORKER);wrapper=directory/'wrapper.py';wrapper.write_text(WRAPPER)
        save(directory/'scenario.json',steps);audit=directory/'requests.jsonl';session=directory/'session.jsonl';output=directory/'runs'
        if question_bad:(directory/'site/question.md').write_text('old question')
        if init_failure:save(directory/'site/manifest.json',{**site.manifest,'browser_channel':'missing-channel'})
        args=['new','--session',str(session),'--workspace',str(workspace),'--question',str(directory/'site/question.md'),'--output-dir',str(output),
              '--browser-manifest',str(directory/'site/manifest.json'),'--browser-task',str(directory/'site/task.json'),'--browser-python',str(self.browser_python),'--approval-source','experiment-driver',*extra]
        argv=[sys.executable,str(wrapper),str(self.entry),str(worker),str(directory/'scenario.json'),str(audit),*args]
        env={k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','LANG','LC_ALL','LC_CTYPE'}};env.update(DEEPSEEK_API_KEY='controlled-not-a-key',DEEPSEEK_MODEL='controlled-model')
        process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        collected=[];approval_count=0
        def read_stderr():
            nonlocal approval_count
            for raw in iter(process.stderr.readline,b''):
                collected.append(raw)
                if raw.startswith(b'APPROVAL '):
                    approval_count+=1
                    if approval_mode:site.mode=approval_mode;site.seq+=1
                    if cancel_approval:os.kill(process.pid,signal.SIGINT);save(directory/'signal.json',{'pid':process.pid,'signal':'SIGINT','at':'approval'});continue
                    if approval_delay:time.sleep(approval_delay)
                    try:
                        if approve is None:process.stdin.close()
                        else:process.stdin.write((approve+'\n').encode());process.stdin.flush()
                    except (BrokenPipeError,ValueError):pass
        thread=threading.Thread(target=read_stderr,daemon=True);thread.start()
        try:
            until=time.monotonic()+100
            while process.poll() is None:
                if time.monotonic()>until:process.kill();raise RuntimeError('公开 CLI 超过检查期限')
                for marker in directory.glob('control-*.json'):
                    done=marker.with_suffix('.done')
                    if not done.exists():
                        settings=json.loads(marker.read_text())
                        if settings['mode']=='kill_worker':
                            meta=json.loads(next(output.glob('*/browser/browser-start.json')).read_text());os.kill(meta['worker_pid'],signal.SIGKILL)
                            save(directory/'worker-kill.json',{'worker_pid':meta['worker_pid'],'browser_pid':meta['browser_pid'],'signal':'SIGKILL'})
                        else:site.mode=settings['mode'];site.seq+=1
                        done.write_text('ready')
                time.sleep(.015)
            stdout=process.stdout.read();thread.join(65)
            (directory/'stdout.json').write_bytes(stdout);(directory/'stderr.txt').write_bytes(b''.join(collected));(directory/'exit-code.txt').write_text(str(process.returncode))
            result=json.loads(stdout);export=site.export();save(directory/'site-export.json',export)
            run=Path(result['artifact_dir']) if 'artifact_dir'in result else None
            facts={'exit_code':process.returncode,'result':result,'directory':str(directory),'requests':rows(audit),'export':export,'approvals':approval_count,'session':str(session),'run':str(run) if run else None}
            self.invocations.append({k:v for k,v in facts.items() if k not in {'requests'}})
            if run and (run/'browser/cleanup.json').exists():
                cleanup=json.loads((run/'browser/cleanup.json').read_text());self.assertTrue(cleanup['cleanup_confirmed'],cleanup)
                for key in ('worker_pid','browser_pid'):
                    if cleanup.get(key):
                        with self.assertRaises(ProcessLookupError):os.kill(int(cleanup[key]),0)
            return facts
        finally:
            if process.poll() is None:process.kill();process.wait()
            site.close()
            if not process.stdin.closed:process.stdin.close()
            process.stdout.close();process.stderr.close()
    def events(self,r):return rows(Path(r['run'])/'events.jsonl')
    def tools(self,r):return [e['result'] for e in self.events(r) if e['event']=='tool_finished']
    def network(self,r):return rows(Path(r['run'])/'browser/network.jsonl')
    def rejected(self,r):self.assertNotEqual(r['exit_code'],0,r['result'])
    def no_post(self,r):self.assertEqual([x for x in rows(Path(r['directory'])/'site/access.jsonl') if x['method']=='POST'],[])
    def test_b01_independent_browser(self):
        a=self.invoke([{'kind':'open'},{'kind':'final'}]);b=self.invoke([{'kind':'open'},{'kind':'final'}]);pids=[]
        for r in (a,b):
            meta=json.loads((Path(r['run'])/'browser/browser-start.json').read_text());pids.append(meta['browser_pid']);self.assertEqual(meta['cookies'],[]);self.assertEqual(meta['storage_state'],{'cookies':[],'origins':[]})
        self.assertNotEqual(*pids)
    def test_b02_current_value_and_input_events(self):
        r=self.invoke([{'kind':'open'},{'kind':'act','node':'filter','action':'fill','value':'changed'},{'kind':'final'}]);events=json.loads((Path(r['run'])/'browser/ui-events.json').read_text());self.assertIn({'name':'filter','value':'changed'},events['input']);self.assertIn('value="initial"',(Path(r['run'])/'browser/observation-002/dom.html').read_text());self.assertIn({'name':'filter','value':'changed','disabled':False},self.tools(r)[1]['current_values'])
    def test_b03_aria_full_ax_and_screenshot(self):
        r=self.invoke([{'kind':'open'},{'kind':'final'}]);p=Path(r['run'])/'browser/observation-001';self.assertIn('选择 ordered-v1',(p/'aria.yml').read_text());ax=json.loads((p/'ax.json').read_text());self.assertTrue(any(x.get('role',{}).get('value')=='button' for x in ax['nodes']));self.assertTrue((p/'screenshot.png').read_bytes().startswith(b'\x89PNG'))
    def test_b04_duplicate_and_missing(self):
        r=self.invoke([{'kind':'open'},{'kind':'final'}],mode='duplicate');self.assertEqual(self.tools(r)[0]['reason'],'ambiguous')
        r=self.invoke([{'kind':'open'},{'kind':'act','node':'absent','action':'click'},{'kind':'final'}]);self.assertEqual(self.tools(r)[1]['reason'],'missing');self.no_post(r)
    def changed_target(self,mode):
        r=self.invoke([{'kind':'open'},{'kind':'act','node':'select-ordered','action':'click','control':{'mode':mode}},{'kind':'final'}]);self.assertEqual(self.tools(r)[1]['reason'],'stale_observation');self.no_post(r);return r
    def test_b05_reorder_invalidates(self):self.changed_target('reorder')
    def test_b06_identical_html_replacement(self):self.changed_target('replace')
    def test_b07_form_value_change(self):self.changed_target('form')
    def test_b08_capture_mutation(self):
        r=self.invoke([{'kind':'open'},{'kind':'final'}],mode='capture');self.assertEqual(self.tools(r)[0]['reason'],'stale_observation');self.no_post(r)
    def test_b09_read_allowlist_and_parameter_contract(self):
        for step in ({'kind':'open','path':'/private'},{'kind':'custom','name':'browser_open','args':{'path':'/ordered','url':'http://127.0.0.1'}},{'kind':'custom','name':'browser_act','args':{'selector':'button','action':'click'}}):
            r=self.invoke([step]);self.rejected(r);self.no_post(r)
    def test_b10_page_injection_does_not_expand_tools(self):
        r=self.invoke([{'kind':'open'},{'kind':'custom','name':'shell','args':{'command':'env'}}],mode='inject');self.rejected(r);self.assertEqual(self.tools(r)[-1]['reason'],'tool_not_allowed');self.assertEqual({x['function']['name'] for x in r['requests'][1]['request']['tools']},{'browser_open','browser_observe','browser_act','browser_query'});self.no_post(r)
    def test_n01_other_origin_get(self):
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'xhr'}}]);self.assertEqual(self.sink.received,[]);self.assertTrue(any('/xhr' in x.get('url','') and x.get('allowed') is False for x in self.network(r)))
    def test_n02_same_origin_private(self):
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'forbidden'}},{'kind':'final'}]);self.assertFalse(any(x['path']=='/private' for x in rows(Path(r['directory'])/'site/access.jsonl')));self.assertTrue(any('/private' in x.get('url','') and x.get('allowed')is False for x in self.network(r)))
    def test_n03_popup_and_frame(self):
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'frame'}}]);self.assertEqual(self.sink.received,[]);self.assertTrue(any('/frame'in x.get('url','') and x.get('allowed')is False for x in self.network(r)))
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'popup'}}]);self.assertEqual(self.sink.received,[]);network=self.network(r)
        attempted=[v for p in (Path(r['run'])/'browser').glob('observation-*/page-events.json') for v in json.loads(p.read_text())['attackEvents']]
        self.assertTrue(any(v['kind']=='popup' and v['created'] for v in attempted),'实际window.open未产生窗口，不能计popup覆盖')
        self.assertTrue(any(x.get('kind')=='extra_page' or '/popup'in x.get('url','') for x in network))
    def test_n04_sw_and_websocket(self):
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'socket'}},{'kind':'final'}]);self.assertEqual(self.sink.received,[]);self.assertEqual(self.tools(r)[-1]['reason'],'network_denied');self.assertTrue(any(x.get('kind')=='websocket_closed' for x in self.network(r)))
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'serviceworker'}},{'kind':'final'}]);self.assertFalse(any(x['path']=='/sw.js' for x in rows(Path(r['directory'])/'site/access.jsonl')))
        attempted=[v for p in (Path(r['run'])/'browser').glob('observation-*/page-events.json') for v in json.loads(p.read_text())['attackEvents']]
        self.assertTrue(any(v['kind']=='serviceworker_attempt' for v in attempted))
        self.assertTrue(any(v['kind']=='serviceworker' and v.get('registration')is False and v.get('registrations')==0 and v.get('controller')is False for v in attempted))
    def test_n05_redirect_zero_target(self):
        r=self.invoke([{'kind':'open'}],mode='redirect');self.rejected(r);self.assertEqual(self.sink.received,[]);self.assertTrue(any(x.get('status')==302 for x in self.network(r)))
    def test_n06_unapproved_post_zero_receive(self):
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'unapproved'}},{'kind':'final'}]);self.no_post(r);self.assertTrue(any(x.get('method')=='POST' and x.get('allowed')is False for x in self.network(r)))
    def test_e01_exact_approval_and_one_consumption(self):
        r=self.invoke(normal_steps());self.assertEqual(r['exit_code'],0,r['result']);self.assertEqual(len(r['export']['effects']),1);p=Path(r['run'])/'browser';intent=rows(p/'intent.jsonl');consumed=rows(p/'permit-consumed.jsonl');self.assertEqual(len(intent),1);self.assertEqual(len(consumed),1);self.assertEqual(json.loads(consumed[0]['body']),intent[0]['payload']);self.assertEqual(rows(p/'approvals.jsonl')[0]['source'],'experiment-driver')
        steps=[{'kind':'open','path':'/'},*normal_steps()];steps[-1]['multiline_quotes']=True
        r=self.invoke(steps);self.assertEqual(r['exit_code'],0,r['result']);self.assertEqual(len(r['requests']),10)
        for frame in r['requests']:
            definitions={t['function']['name']:t['function'] for t in frame['request']['tools']}
            for name in ('browser_open','browser_observe'):
                self.assertIn('quote 须保留原文换行',definitions[name]['description']);self.assertIn('连续片段',definitions[name]['description'])
            self.assertIn('最终 receipt_ref 原样取查询同名字段',definitions['browser_query']['description']);self.assertIn('禁用 receipt_id',definitions['browser_query']['description'])
        answer=json.loads((Path(r['run'])/'answer.json').read_text());query=next(v for v in reversed(self.tools(r)) if v.get('receipt'))
        self.assertEqual(answer['registration']['receipt_ref'],query['receipt_ref']);self.assertNotEqual(query['receipt_ref'],query['receipt']['receipt_id'])
        self.assertTrue(all('\n' in c['quote'] for a in answer['answers'] for c in a['citations']))
        budgets=[json.loads(p.read_text())['budget'] for p in Path(r['run']).glob('projection-*.json')]
        self.assertGreaterEqual(min(v['context_tokens']-v['estimated_input']-v['output_reserve'] for v in budgets),512)
    def test_e02_deny_eof_expiry(self):
        steps=[{'kind':'open'},{'kind':'act','node':'select-ordered','action':'click'},{'kind':'act','node':'submit','action':'submit'}]
        for choice in ('deny',None):r=self.invoke(steps,approve=choice);self.rejected(r);self.no_post(r)
        r=self.invoke(steps,approval_delay=60.2);self.rejected(r);self.no_post(r);self.assertEqual(r['result']['status'],'deadline_exceeded')
    def test_e03_changed_during_approval(self):
        r=self.invoke([{'kind':'open'},{'kind':'act','node':'select-ordered','action':'click'},{'kind':'act','node':'submit','action':'submit'},{'kind':'final'}],approval_mode='form');self.assertEqual(self.tools(r)[2]['reason'],'stale_observation');self.no_post(r)
    def test_e04_pointerdown_changes_post(self):
        r=self.invoke(normal_steps(),mode='pointerdown');self.rejected(r);self.no_post(r);self.assertTrue(any(x.get('method')=='POST' and x.get('allowed')is False for x in self.network(r)))
    def test_e05_query_matches_actual_receipt(self):
        r=self.invoke(normal_steps());self.assertEqual(r['exit_code'],0,r['result']);self.assertEqual(self.tools(r)[6]['business_effect'],'unknown');self.assertEqual(self.tools(r)[7]['business_effect'],'applied');self.assertEqual(self.tools(r)[7]['receipt'],r['export']['receipts'][0])
    def test_e06_lost_reply_queries_without_replay(self):
        r=self.invoke(normal_steps(),mode='lose_reply');self.assertEqual(r['exit_code'],0,r['result']);self.assertEqual(len(r['export']['effects']),1);self.assertEqual(sum(x.get('method')=='POST' and x.get('allowed')is True for x in self.network(r)),1);self.assertTrue(any(x.get('error') for x in self.network(r)))
    def test_e07_optimistic_ui_rejected(self):
        r=self.invoke(normal_steps(),mode='reject');self.assertEqual(r['result']['status'],'business_rejected');self.assertEqual(r['export']['effects'],[]);self.assertIn('页面显示成功',json.loads((Path(r['run'])/'browser/ui-events.json').read_text())['status']);self.assertEqual(self.tools(r)[7]['receipt']['status'],'rejected')
    def test_e08_wrong_or_unknown_receipt_stops(self):
        for mode in ('old_receipt','query_unknown'):
            steps=normal_steps();steps[-1]={'kind':'query'};r=self.invoke(steps,mode=mode);self.assertEqual(r['result']['status'],'effect_unknown');self.assertEqual(len(r['requests']),9);self.assertEqual(len(r['export']['effects']),1);self.assertFalse((Path(r['run'])/'answer.json').exists())
    def test_i01_final_contract(self):
        for mutation in ('quote','source','coverage','task','receipt'):
            r=self.invoke([{'kind':'open'},{'kind':'final','mutation':mutation}]);self.rejected(r);self.assertFalse((Path(r['run'])/'answer.json').exists())
    def test_i02_budgets_and_batch_zero_execution(self):
        r=self.invoke([{'kind':'open','multi':True}]);self.assertEqual(r['result']['status'],'tool_batch_refused');self.assertFalse(any(e.get('executed') for e in self.events(r) if e['event']=='tool_finished'))
        r=self.invoke([{'kind':'open','finish':'length'}]);self.assertEqual(r['result']['status'],'length');self.assertFalse(any(e.get('executed') for e in self.events(r) if e['event']=='tool_finished'))
        r=self.invoke([{'kind':'open'}],extra=['--max-turns','1']);self.rejected(r);self.assertFalse(any(e.get('executed') for e in self.events(r) if e['event']=='tool_finished'))
        r=self.invoke([{'kind':'open'},{'kind':'open'}],extra=['--max-actions','1']);self.assertEqual(r['result']['status'],'action_limit');self.assertEqual(sum(e.get('executed',False) for e in self.events(r) if e['event']=='tool_finished'),1)
    def test_i03_cancel_and_initialization_cleanup(self):
        r=self.invoke([{'kind':'cancel'}]);self.assertEqual(r['result']['status'],'cancelled')
        r=self.invoke([{'kind':'open'},{'kind':'act','node':'select-ordered','action':'click'},{'kind':'act','node':'submit','action':'submit'}],cancel_approval=True);self.assertEqual(r['result']['status'],'cancelled');self.no_post(r);self.assertEqual(self.tools(r)[-1]['business_effect'],'none');self.assertEqual(r['result']['browser']['business_effect'],'none')
        r=self.invoke([{'kind':'open'}],question_bad=True);self.assertEqual(len(r['requests']),0);self.assertFalse(r['run'])
        r=self.invoke([{'kind':'open'}],init_failure=True);self.assertEqual(len(r['requests']),0);self.rejected(r)
        r=self.invoke([{'kind':'open'},{'kind':'observe','control':{'mode':'kill_worker'}}]);self.rejected(r);self.assertTrue((Path(r['directory'])/'worker-kill.json').exists());self.no_post(r)
    def test_i04_session_refusal_and_new_scope(self):
        r=self.invoke([{'kind':'open'},{'kind':'final'}]);directory=Path(r['directory']);p=subprocess.run([sys.executable,str(self.entry),'branch','--session',r['session'],'--from-entry','old','--target',str(directory/'branch.jsonl')],capture_output=True);self.assertNotEqual(p.returncode,0);save(directory/'branch-result.json',{'returncode':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()})
        old=self.tools(r)[0]['observation_id'];r=self.invoke([{'kind':'open'},{'kind':'act','node':'select-ordered','action':'click','observation_id':old},{'kind':'final'}]);self.assertEqual(self.tools(r)[1]['reason'],'stale_observation');self.no_post(r)

def main():
    p=argparse.ArgumentParser();p.add_argument('--entry',type=Path,required=True);p.add_argument('--browser-python',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--case');a=p.parse_args()
    root=a.output_dir/('check-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
    CliChecks.entry=a.entry.absolute();CliChecks.browser_python=a.browser_python.absolute();CliChecks.output_root=root
    suite=unittest.defaultTestLoader.loadTestsFromName(a.case,CliChecks) if a.case else unittest.defaultTestLoader.loadTestsFromTestCase(CliChecks)
    result=unittest.TextTestRunner(verbosity=2).run(suite);save(root/'summary.json',{'methods':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'passed':result.wasSuccessful(),'external_model_requests':0,'evidence_source':'controlled-sse+real-local-browser','failure_details':[(str(t),s) for t,s in result.failures+result.errors]})
    print(canonical({'directory':str(root),'passed':result.wasSuccessful(),'methods':result.testsRun}));return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
