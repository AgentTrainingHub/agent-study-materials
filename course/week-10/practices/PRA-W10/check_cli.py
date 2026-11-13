"""W10 公开检查：真实 CPU/SQLite/进程，模型只用明确标记的禁网 SSE。"""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,math,os,shutil,signal,sqlite3,subprocess,sys,time,unittest,uuid
from pathlib import Path
from prepare_corpus import prepare,change
MATERIALS=Path(__file__).resolve().parent

def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def rows(path):
    return [json.loads(line) for line in path.read_bytes().split(b'\n') if line] if path.exists() else []

def index_rows(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        active=db.execute("SELECT value FROM meta WHERE key='active_generation'").fetchone()[0]
        gen=json.loads(db.execute('SELECT payload_json FROM generations WHERE generation_id=?',(active,)).fetchone()[0])
        records=[(json.loads(r[0]),json.loads(r[1])) for r in db.execute('SELECT payload_json,vector_json FROM chunks WHERE generation_id=? ORDER BY chunk_id',(active,))]
    return gen,records

WORKER=r'''
import base64,json,os,signal,socket,sys,time
from pathlib import Path
socket.socket.connect=lambda *a,**k:(_ for _ in ()).throw(AssertionError('controlled SSE forbids network'))
scenario,audit=map(Path,sys.argv[1:3]);incoming=json.load(sys.stdin);request=incoming['request']
items=json.loads(scenario.read_text());number=len(audit.read_text().splitlines()) if audit.exists() else 0
with audit.open('a') as handle:handle.write(json.dumps({'request':request,'pid':os.getpid(),'index':number})+'\n')
item=items[number];kind=item['kind'];usage={'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}
finish=item.get('finish','stop');content='';tools=[]
if kind=='search':
    finish='tool_calls';tools=[{'index':0,'id':item.get('call_id','search-1'),'type':'function','function':{'name':item.get('name','search_docs'),'arguments':json.dumps(item.get('arguments',{'query':item.get('query','当前分号合同 parse_ports 重复项 吞吐性能')}),ensure_ascii=False)}}]
elif kind=='rerank':
    value=json.loads(request['messages'][-1]['content']);order=[r['chunk_id'] for r in value['candidates']][::-1]
    mutation=item.get('mutation')
    if mutation=='missing':order=order[:-1]
    if mutation=='extra':order+=['chk_not-in-candidates']
    if mutation=='duplicate':order[-1]=order[0]
    content='{bad json' if mutation=='json' else json.dumps({'order':order})
    if mutation=='usage':usage=None
    if mutation=='length':finish='length'
    if mutation=='error':finish='content_filter'
    if mutation=='cancel':os.kill(os.getppid(),signal.SIGINT);time.sleep(0.12)
else:
    available=[]
    for message in request['messages']:
        if message['role']=='tool':
            value=json.loads(message['content']);available.extend(value.get('hits',[]))
    if kind=='answer':
        if not available:raise AssertionError('controlled answer requires actual visible hits')
        content='已取得片段，语义判断仍须人工核对。['+available[0]['citation_id']+']'
    elif kind=='malformed_citation':
        if not available:raise AssertionError('malformed fixture requires actual visible hits')
        label=available[0]['citation_id'];second=available[-1]['citation_id']
        formats={'bare':label,'chinese':'（'+label+'）','unclosed':'['+label,
                 'two_in_one':'['+label+', '+second+']','nested':'[['+label+']]',
                 'valid_and_bare':'['+label+'] '+label,'valid_and_nested':'['+label+'] [['+label+']]'}
        content='格式错误的受控答案：'+formats[item['form']]
    elif kind=='uncited':content='资料不足，无法给出该数值。'
    elif kind=='fake':content='错误引用。['+item.get('citation','qry_'+'0'*32+'.c01')+']'
    elif kind=='literal':content=item['text']
    else:raise AssertionError('unknown scenario')
if item.get('source_change'):
    path=Path(item['source_change']);path.write_bytes(path.read_bytes()+b'\nexternal-change\n')
    audit.with_name('source-change.json').write_text(json.dumps({'path':str(path),'pid':os.getpid(),'before_answer':True}))
def emit(value):print(json.dumps(value),flush=True)
emit({'type':'headers','status':200,'request_id':'controlled-'+str(number)})
def frame(delta,reason=None,with_usage=False):
    value={'id':'w10-controlled','model':'controlled-model','choices':[{'index':0,'delta':delta,'finish_reason':reason}]}
    if with_usage and usage is not None:value['usage']=usage
    return 'data: '+json.dumps(value,ensure_ascii=False)+'\n\n'
blocks=[frame({'role':'assistant'})]
if content:blocks.append(frame({'content':content}))
if tools:blocks.append(frame({'tool_calls':tools}))
blocks.append(frame({},finish,True))
if not item.get('missing_done'):blocks.append('data: [DONE]\n\n')
for text in blocks:
    raw=text.encode();cut=min(17,len(raw))
    for part in (raw[:cut],raw[cut:]):
        if part:emit({'type':'chunk','data':base64.b64encode(part).decode(),'arrival_ms':0})
emit({'type':'end'})
'''
WRAPPER=r'''
import json,os,runpy,socket,sys,time
from pathlib import Path
entry,worker,scenario,audit,config=map(Path,sys.argv[1:6]);settings=json.loads(config.read_text())
sys.path.insert(0,str(entry.parent))
import w05_support,embedding_worker,w10_io,w02_support
w02_support.EVIDENCE_SOURCE='controlled-sse+real-local-cpu'
w05_support._stream_worker_command=lambda:[sys.executable,str(worker),str(scenario),str(audit)]
def forbidden(*a,**k):raise AssertionError('W10 docs mode must not start old business components or network')
socket.socket.connect=forbidden;socket.create_connection=forbidden
import w03_shell,w09_shell,mcp_tools,resources
w03_shell.preflight=forbidden;w09_shell.preflight=forbidden;mcp_tools.McpHost.open=forbidden;resources.discover=forbidden
if settings.get('embedding_gate') or settings.get('embedding_failure'):
    original=embedding_worker.EmbeddingWorker.__init__
    def init(self,*a,**kw):
        if settings.get('embedding_gate'):kw['control']={'gate':settings['embedding_gate']}
        original(self,*a,**kw)
        if settings.get('embedding_failure'):self.request['representation_id']='rep_wrong-in-worker'
    embedding_worker.EmbeddingWorker.__init__=init
if settings.get('change_before_publish'):
    original=w10_io.IndexStore.publish
    def publish(self,generation,chunks,vectors,*,before_commit):
        def changed():
            path=Path(settings['change_before_publish']);path.write_bytes(path.read_bytes()+b'\npublish-race\n');before_commit()
        return original(self,generation,chunks,vectors,before_commit=changed)
    w10_io.IndexStore.publish=publish
sys.argv=[str(entry),*settings['argv']]
runpy.run_path(str(entry),run_name='__main__')
'''

class CliChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared=cls.output_root/'shared';cls.shared.mkdir();cls.base_corpus=cls.shared/'corpus';prepare(cls.base_corpus)
        cls.base_index=cls.shared/'index.sqlite';cls.worker=cls.shared/'worker.py';cls.wrapper=cls.shared/'wrapper.py'
        cls.worker.write_text(WORKER);cls.wrapper.write_text(WRAPPER)
        args=[sys.executable,str(cls.entry),'corpus-build','--corpus',str(cls.base_corpus),'--index',str(cls.base_index),
              '--embedding-model',str(cls.model),'--output-dir',str(cls.shared/'build')]
        p=subprocess.run(args,capture_output=True,timeout=310)
        (cls.shared/'stdout.json').write_bytes(p.stdout);(cls.shared/'stderr.txt').write_bytes(p.stderr)
        if p.returncode:raise RuntimeError('实际CLI索引构建失败：'+p.stdout.decode())
        cls.generation,cls.chunk_vectors=index_rows(cls.base_index)
    def setUp(self):
        self.directory=self.output_root/self._testMethodName;self.directory.mkdir();self.invocations=[];self.facts={}
        self.corpus=self.base_corpus;self.index=self.base_index;self.workspace=self.directory/'workspace';self.workspace.mkdir()
        self.session=self.directory/'session.jsonl'
    def tearDown(self):save(self.directory/'facts.json',{'invocations':self.invocations,'facts':self.facts,'external_model_requests':0,'semantic_review':'required'})
    def invoke(self,command,scenario=(),*,scope='semicolon_keep',mode='hybrid',rerank='off',question='当前分号列表规则与未提供的性能数据。',extra=(),cancel_gate=None,**settings):
        out=self.directory/f'cli-{len(self.invocations)+1:02d}-{command}';out.mkdir()
        question_path=out/'question.txt';question_path.write_text(question+'\n')
        args=[command]
        if command in {'corpus-build','corpus-query','new','resume'}:
            args+=['--corpus',str(self.corpus),'--index',str(self.index),'--embedding-model',str(self.model),'--output-dir',str(out/'runs')]
        if command in {'corpus-query','new','resume'}:args+=['--doc-scope',scope,'--retrieval-mode',mode]
        if command=='corpus-query':args+=['--question-file',str(question_path)]
        if command in {'new','resume'}:
            args+=['--session',str(self.session),'--workspace',str(self.workspace),'--question',str(question_path),'--rerank',rerank,'--max-tokens','1536']
        if command in {'inspect','branch','continue','recover'}:args+=['--session',str(self.session)]
        if command=='corpus-inspect':args+=['--index',str(self.index),'--output-dir',str(out/'runs')]
        args+=list(extra);save(out/'scenario.json',list(scenario));save(out/'settings.json',{**settings,'argv':args})
        audit=out/'requests.jsonl';argv=[sys.executable,str(self.wrapper),str(self.entry),str(self.worker),str(out/'scenario.json'),str(audit),str(out/'settings.json')]
        env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}}
        env.update(DEEPSEEK_API_KEY='w10-controlled-not-a-secret',DEEPSEEK_MODEL='controlled-model')
        process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        signal_fact=None
        if cancel_gate:
            marker=Path(cancel_gate).with_suffix('.hit.json');until=time.monotonic()+25
            while not marker.exists() and process.poll() is None and time.monotonic()<until:time.sleep(0.02)
            self.assertTrue(marker.exists(),'CPU worker 未实际加载到门闩')
            value=json.loads(marker.read_text());os.kill(process.pid,signal.SIGINT)
            signal_fact={'pid':process.pid,'signal':'SIGINT','worker':value,'sent_at':time.time()};save(out/'signal.json',signal_fact)
        try:stdout,stderr=process.communicate(timeout=310)
        except subprocess.TimeoutExpired:process.kill();stdout,stderr=process.communicate();raise
        (out/'stdout.json').write_bytes(stdout);(out/'stderr.txt').write_bytes(stderr);(out/'exit-code.txt').write_text(str(process.returncode)+'\n')
        try:value=json.loads(stdout)
        except ValueError:value={'unparsed_stdout':stdout.decode(errors='replace')}
        observed={'directory':str(out),'argv':args,'result':value,'exit_code':process.returncode,'requests':rows(audit),'signal':signal_fact}
        self.invocations.append({'directory':str(out),'exit_code':process.returncode,'result':value,'model_requests':len(observed['requests'])})
        return observed
    def successful(self,item):self.assertEqual(item['exit_code'],0,item['result'])
    def rejected(self,item,*,requests=None):
        self.assertIn(item['exit_code'],(1,2),item['result'])
        if requests is not None:self.assertEqual(len(item['requests']),requests)
    def trace(self,item):return json.loads((Path(item['result']['artifact_dir'])/'query.json').read_text())
    def run_dir(self,item):return Path(item['result']['artifact_dir'])
    def fresh(self):
        self.corpus=self.directory/'corpus';prepare(self.corpus);self.index=self.directory/'index.sqlite';self.successful(self.invoke('corpus-build'))
    def test_cpu_ingestion_and_original_ranges(self):
        self.assertEqual(len(self.chunk_vectors),23)
        for chunk,vector in self.chunk_vectors:
            self.assertEqual(len(vector),384);self.assertTrue(all(math.isfinite(x) for x in vector));self.assertAlmostEqual(sum(x*x for x in vector),1,places=4)
            raw=(self.corpus/chunk['path']).read_bytes();lines=raw.decode().replace('\r\n','\n').split('\n')
            text='\n\n---\n\n'.join('\n'.join(lines[r['start_line']-1:r['end_line']]) for r in chunk['source_ranges'])
            self.assertEqual(text,chunk['text']);self.assertEqual(text,chunk['index_text']);self.assertLessEqual(len(text),800)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),chunk['source_sha256'])
            self.assertEqual(chunk['source_ranges'][0]['kind'],'heading')
        encoded=json.loads((self.shared/'build/embedding/result.json').read_text());self.assertLessEqual(max(encoded['token_lengths']),512)
        self.assertEqual(encoded['metrics']['providers'],['CPUExecutionProvider']);self.assertEqual(encoded['metrics']['network_attempts'],[])
        self.assertTrue(json.loads((self.shared/'build/embedding/cleanup.json').read_text())['cleanup_confirmed'])
        self.facts.update(chunk_count=len(self.chunk_vectors),max_token_length=max(encoded['token_lengths']),encoding_source='real-local-cpu')
    def test_table_ranges_and_lf_normalization(self):
        table=[c for c,v in self.chunk_vectors if c['document_id']=='invalid-inputs'];self.assertGreater(len(table),1)
        for chunk in table:
            self.assertEqual([r['kind'] for r in chunk['source_ranges']],['heading','condition','header','body'])
            self.assertEqual(chunk['source_ranges'][2],{'kind':'header','start_line':5,'end_line':6})
            self.assertIn('本表仅用于 semicolon_keep',chunk['text'])
        self.corpus=self.directory/'corpus';prepare(self.corpus);self.index=self.directory/'index.sqlite'
        path=self.corpus/'project-note.md';original=path.read_text();path.write_bytes(original.replace('项目工单','项目\u2028工单').replace('\n','\r\n').encode())
        self.successful(self.invoke('corpus-build'));g,cv=index_rows(self.index)
        first=next(c for c,v in cv if c['document_id']=='project-note' and c['source_ranges'][-1]['start_line']==3)
        self.assertIn('\u2028',first['text']);self.assertEqual(first['source_ranges'][-1]['end_line'],3)
    def test_bm25_terms_and_three_routes(self):
        query='parse_ports 80 分号 重复'
        traces=[]
        for mode in ('bm25','semantic','hybrid'):
            item=self.invoke('corpus-query',mode=mode,question=query);self.successful(item);trace=self.trace(item);traces.append(trace)
            self.assertIn('parse_ports',trace['tokens']['bm25']);self.assertIn('80',trace['tokens']['bm25']);self.assertIn('分号',trace['tokens']['bm25'])
            self.assertTrue(all(x['score']>0 for x in trace['bm25']))
            self.assertLessEqual(len(trace['fused']),8);self.assertLessEqual(len(trace['final']),4)
            for key in ('bm25','semantic'):self.assertEqual(trace[key],sorted(trace[key],key=lambda r:(-r['score'],r['chunk_id'])))
        self.assertEqual(len({t['representation_id'] for t in traces}),1)
        self.assertEqual(traces[0]['bm25'],traces[1]['bm25']);self.assertEqual(traces[1]['semantic'],traces[2]['semantic'])
    def test_scope_prefilter_and_withdrawn(self):
        item=self.invoke('corpus-query',question='comma_unique parse_ports 80,443,80 第一次 去重 99 失败');self.successful(item);trace=self.trace(item)
        allowed={c['chunk_id'] for c,v in self.chunk_vectors if 'semicolon_keep' in c['scopes'] and c['status']=='active'}
        self.assertEqual(set(trace['input_pool']),allowed)
        for key in ('bm25','semantic','fused','final'):self.assertTrue({r['chunk_id'] for r in trace[key]}<=allowed,key)
        self.assertNotIn('withdrawn-support',{c['document_id'] for c,v in self.chunk_vectors})
    def test_duplicate_text_keeps_source_identity(self):
        self.corpus=self.directory/'corpus';prepare(self.corpus);self.index=self.directory/'index.sqlite'
        (self.corpus/'conflict-memo.md').write_bytes((self.corpus/'support-guide.md').read_bytes())
        self.successful(self.invoke('corpus-build'));g,cv=index_rows(self.index)
        pairs={c['document_id']:c for c,v in cv if c['document_id'] in {'support-guide','conflict-memo'} and '3 条' in c['text']}
        self.assertEqual(pairs['support-guide']['text'],pairs['conflict-memo']['text']);self.assertNotEqual(pairs['support-guide']['chunk_id'],pairs['conflict-memo']['chunk_id'])
        item=self.invoke('corpus-query',question='支持 失败 输入 最多 3 条');self.successful(item)
        self.assertTrue({r['chunk_id'] for r in pairs.values()}<=set(self.trace(item)['input_pool']))
    def test_fusion_first_occurrence(self):
        path=self.directory/'fusion.py';path.write_text("import sys,json;sys.path.insert(0,sys.argv[1]);from retrieval import fuse_routes;print(json.dumps(fuse_routes([{'chunk_id':'a'},{'chunk_id':'a'},{'chunk_id':'b'},{'chunk_id':'text-other-source'}],[{'chunk_id':'b'},{'chunk_id':'b'},{'chunk_id':'a'}])))")
        p=subprocess.run([sys.executable,str(path),str(self.entry.parent)],capture_output=True,text=True,check=True)
        result=json.loads(p.stdout);save(self.directory/'fusion-result.json',result);by={r['chunk_id']:r for r in result}
        self.assertAlmostEqual(by['a']['score'],1/61+1/62);self.assertAlmostEqual(by['b']['score'],1/62+1/61)
        self.assertEqual(by['b']['contributions']['bm25']['rank'],2);self.assertIn('text-other-source',by)
        self.facts['evidence_source']='deterministic_fusion';self.facts['cli_invocations']=0
    def test_rerank_full_pool_and_citations(self):
        item=self.invoke('new',[{'kind':'search'},{'kind':'rerank'},{'kind':'answer'}],rerank='on');self.successful(item)
        self.assertEqual(item['result']['model_calls'],3);self.assertEqual(item['result']['rerank_calls'],1)
        requests=[r['request'] for r in item['requests']];self.assertNotIn('tools',requests[1]);self.assertEqual(requests[1]['max_tokens'],384)
        pool=json.loads(requests[1]['messages'][-1]['content'])['candidates'];self.assertLessEqual(len(pool),8)
        search=json.loads(next(self.run_dir(item).glob('search-*/query.json')).read_text())
        self.assertEqual([r['chunk_id'] for r in search['final']],[r['chunk_id'] for r in pool][::-1][:4])
        messages=requests[2]['messages'];feedback=[r for r in messages if r['role']=='tool'];self.assertEqual(feedback[0]['tool_call_id'],'search-1')
        visible=json.loads(feedback[0]['content'])['hits'];projection=json.loads((self.run_dir(item)/'projection-003.json').read_text())
        self.assertEqual([r['text'] for r in projection['retrieval_sources']],[r['text'] for r in visible])
        citations=json.loads((self.run_dir(item)/'citations.json').read_text());self.assertTrue(citations['accepted']);self.assertEqual(citations['semantic_review'],'required')
        self.assertTrue(all(r['tool_entry_id'] for r in projection['retrieval_sources']))
        self.assertTrue(all(p.get('budget',{}).get('fits') for p in map(lambda p:json.loads(p.read_text()),self.run_dir(item).glob('projection-*.json'))))
    def test_rerank_invalid_permutations(self):
        for mutation in ('missing','extra','duplicate','json'):
            self.session=self.directory/(mutation+'.jsonl')
            item=self.invoke('new',[{'kind':'search'},{'kind':'rerank','mutation':mutation}],rerank='on');self.rejected(item,requests=2)
            self.assertEqual(item['result']['text'],'');self.assertEqual(item['result']['rerank_calls'],1)
    def test_rerank_terminal_and_usage(self):
        for mutation in ('length','error','cancel','usage'):
            self.session=self.directory/(mutation+'.jsonl')
            item=self.invoke('new',[{'kind':'search'},{'kind':'rerank','mutation':mutation}],rerank='on');self.rejected(item,requests=2)
            self.assertEqual(item['result']['text'],'')
            if mutation=='usage':self.assertEqual(item['result']['status'],'rerank_usage_unknown');self.assertEqual(item['result']['usage']['usage_status'],'usage_unknown')
            self.assertTrue(next(self.run_dir(item).glob('search-*/rerank-response.json')).is_file())
    def test_citation_missing_fake_unseen_and_old(self):
        first=self.invoke('new',[{'kind':'search'},{'kind':'answer'}]);self.successful(first)
        old=json.loads((self.run_dir(first)/'citations.json').read_text())['citations'][0]['citation_id']
        for ident in ('qry_'+'0'*32+'.c01',old):
            self.session=self.directory/(uuid.uuid4().hex+'.jsonl')
            item=self.invoke('new',[{'kind':'search'},{'kind':'fake','citation':ident}]);self.rejected(item,requests=2)
            self.assertEqual(item['result']['status'],'invalid_citation')
        for form in ('bare','chinese','unclosed','two_in_one','nested','valid_and_bare','valid_and_nested'):
            with self.subTest(form=form):
                self.session=self.directory/('malformed-'+form+'.jsonl')
                item=self.invoke('new',[{'kind':'search'},{'kind':'malformed_citation','form':form}])
                self.rejected(item,requests=2)
                self.assertEqual(item['result']['status'],'invalid_citation')
                self.assertEqual(item['result']['text'],'')
                candidate=json.loads((self.run_dir(item)/'answer-candidate.json').read_text())
                self.assertIn('qry_',candidate['text'])
                self.assertFalse((self.run_dir(item)/'citations.json').exists())
        self.session=self.directory/'no-search.jsonl'
        item=self.invoke('new',[{'kind':'literal','text':'任意事实。'}]);self.rejected(item,requests=1);self.assertEqual(item['result']['status'],'retrieval_required')
    def test_no_answer_and_execution_error_distinct(self):
        item=self.invoke('new',[{'kind':'search','query':'云端每月部署费'},{'kind':'uncited'}]);self.successful(item)
        self.assertEqual(json.loads((self.run_dir(item)/'citations.json').read_text())['citations'],[])
        self.session=self.directory/'failure.jsonl'
        failed=self.invoke('new',[{'kind':'search'}],embedding_failure=True);self.rejected(failed,requests=1)
        self.assertEqual(failed['result']['text'],'');self.assertNotEqual(failed['result']['status'],'no_lexical_hit')
    def test_same_revision_bytes_change_blocks(self):
        self.fresh();path=self.corpus/'support-guide.md';path.write_text(path.read_text().replace('3 条','8 条'))
        item=self.invoke('corpus-query');self.rejected(item,requests=0)
        self.assertIn(item['result']['error']['class'],('source_changed','index_stale'))
    def test_update_withdraw_generation_and_old_history(self):
        self.fresh();first=self.invoke('new',[{'kind':'search'},{'kind':'answer'}]);self.successful(first)
        old=index_rows(self.index)[0]['generation_id'];change(self.corpus,'update',self.directory/'update')
        self.rejected(self.invoke('corpus-query'),requests=0)
        self.successful(self.invoke('corpus-build'));new=index_rows(self.index)[0]['generation_id'];self.assertNotEqual(old,new)
        updated=self.invoke('corpus-query',question='工单 最多 失败输入 样例 数量');self.successful(updated)
        hits=updated['result']['hits'];self.assertTrue(any('5 条' in r['text'] for r in hits));self.assertFalse(any('3 条' in r['text'] for r in hits))
        self.rejected(self.invoke('resume',[{'kind':'answer'}]),requests=0)
        self.successful(self.invoke('inspect'))
        change(self.corpus,'withdraw',self.directory/'withdraw');self.successful(self.invoke('corpus-build'))
        withdrawn=self.invoke('corpus-query',question='最多失败输入样例');self.successful(withdrawn)
        self.assertNotIn('support-guide',{c['document_id'] for c,v in index_rows(self.index)[1]})
        self.assertFalse(any('5 条' in r['text'] or '3 条' in r['text'] for r in withdrawn['result']['hits']))
        self.successful(self.invoke('inspect'))
    def test_missing_symlink_hardlink(self):
        for kind in ('missing','symlink','hardlink'):
            self.corpus=self.directory/('corpus-'+kind);prepare(self.corpus);self.index=self.directory/(kind+'.sqlite')
            target=self.corpus/'project-note.md';data=target.read_bytes();target.unlink()
            outside=self.directory/(kind+'.md');outside.write_bytes(data)
            if kind=='symlink':target.symlink_to(outside)
            if kind=='hardlink':os.link(outside,target)
            self.rejected(self.invoke('corpus-build'),requests=0)
    def test_publish_race_and_worker_failure_preserve_active(self):
        self.fresh();old=index_rows(self.index)[0]['generation_id']
        failed=self.invoke('corpus-build',embedding_failure=True);self.rejected(failed,requests=0);self.assertEqual(index_rows(self.index)[0]['generation_id'],old)
        changed=self.invoke('corpus-build',change_before_publish=str(self.corpus/'project-note.md'));self.rejected(changed,requests=0)
        self.assertEqual(index_rows(self.index)[0]['generation_id'],old);self.rejected(self.invoke('corpus-query'),requests=0)
    def test_change_during_final_answer(self):
        self.fresh();item=self.invoke('new',[{'kind':'search'},{'kind':'answer','source_change':str(self.corpus/'project-note.md')}])
        self.rejected(item,requests=2);self.assertEqual(item['result']['text'],'')
        self.assertTrue((self.run_dir(item)/'answer-candidate.json').exists());self.assertFalse((self.run_dir(item)/'citations.json').exists())
    def test_resume_branch_scope_and_mode_binding(self):
        self.fresh();first=self.invoke('new',[{'kind':'search'},{'kind':'answer'}]);self.successful(first)
        self.rejected(self.invoke('resume',scope='comma_keep'),requests=0)
        self.rejected(self.invoke('resume',mode='bm25'),requests=0)
        head=first['result']['head_id'];change(self.corpus,'update',self.directory/'update');self.successful(self.invoke('corpus-build'))
        self.rejected(self.invoke('branch',extra=['--from-entry',head]),requests=0)
        self.session=self.directory/'new-generation.jsonl';new=self.invoke('new',[{'kind':'search'},{'kind':'answer'}]);self.successful(new)
        self.assertFalse(any(m['role']=='tool' for m in new['requests'][0]['request']['messages']))
    def test_vector_and_representation_errors(self):
        for kind in ('dimension','nonfinite','zero','representation'):
            self.index=self.directory/(kind+'.sqlite');shutil.copyfile(self.base_index,self.index)
            with sqlite3.connect(self.index) as db:
                if kind=='representation':
                    row=db.execute('SELECT generation_id,payload_json FROM generations').fetchone();value=json.loads(row[1]);value['representation']['pooling']='changed'
                    db.execute('UPDATE generations SET payload_json=? WHERE generation_id=?',(json.dumps(value),row[0]))
                else:
                    ident=db.execute('SELECT chunk_id FROM chunks LIMIT 1').fetchone()[0];vector=[0.0]*384
                    if kind=='dimension':vector=[1.0]
                    elif kind=='nonfinite':vector[0]=float('nan')
                    db.execute('UPDATE chunks SET vector_json=? WHERE chunk_id=?',(json.dumps(vector),ident))
            item=self.invoke('corpus-query');self.rejected(item,requests=0);self.assertFalse(Path(item['directory'],'runs','embedding').exists())
    def test_cpu_sigint_cleanup(self):
        gate=self.directory/'release';item=self.invoke('new',[{'kind':'search'}],embedding_gate=str(gate),cancel_gate=gate)
        self.rejected(item,requests=1);self.assertEqual(item['result']['status'],'cancelled')
        cleanup=json.loads(next(self.run_dir(item).glob('search-*/embedding/cleanup.json')).read_text())
        self.assertTrue(cleanup['cleanup_confirmed']);self.assertTrue(cleanup['cancel_requested'])
        self.assertEqual(cleanup['pid'],item['signal']['worker']['pid']);self.assertNotEqual(cleanup['returncode'],0)
    def test_rerank_budget_zero_send(self):
        item=self.invoke('new',[{'kind':'search'}],rerank='on',extra=['--max-turns','2']);self.rejected(item,requests=1)
        self.assertEqual(item['result']['rerank_calls'],0);self.assertEqual(item['result']['status'],'model_budget_exhausted')
    def test_rerank_context_budget_zero_send(self):
        item=self.invoke('new',[{'kind':'search'}],rerank='on',extra=['--context-tokens','2600']);self.rejected(item,requests=1)
        self.assertEqual(item['result']['status'],'context_budget_exceeded');self.assertEqual(item['result']['rerank_calls'],0)
        candidate=json.loads((self.run_dir(item)/'context-candidate-002.json').read_text())
        self.assertEqual(candidate['purpose'],'retrieval_rerank');self.assertFalse(candidate['budget']['fits'])
    def test_cpu_deadline_and_untruncated_token_limit(self):
        gate=self.directory/'release'
        item=self.invoke('new',[{'kind':'search'}],embedding_gate=str(gate),extra=['--deadline-seconds','5'])
        self.rejected(item,requests=1);self.assertEqual(item['result']['status'],'deadline_exceeded')
        cleanup=json.loads(next(self.run_dir(item).glob('search-*/embedding/cleanup.json')).read_text());self.assertTrue(cleanup['cleanup_confirmed'])
        self.corpus=self.directory/'long-corpus';prepare(self.corpus);self.index=self.directory/'long.sqlite'
        # E5 的正规化会展开兼容字符，400 个字符在该固定 tokenizer 下超过512 token。
        (self.corpus/'project-note.md').write_text('# 项目笔记\n\n'+'㌕'*400+'\n')
        failed=self.invoke('corpus-build');self.rejected(failed,requests=0)
        self.assertEqual(failed['result']['error']['class'],'embedding_input_too_long')
    def test_missing_and_changed_model_identity(self):
        real_model=self.model
        try:
            self.model=self.directory/'missing-model';self.model.mkdir()
            missing=self.invoke('corpus-query');self.rejected(missing,requests=0)
            (self.model/'model.onnx').write_bytes(b'changed fixed model bytes')
            changed=self.invoke('corpus-query');self.rejected(changed,requests=0)
            self.assertEqual(changed['result']['error']['class'],'representation_mismatch')
            self.assertEqual(list(self.model.iterdir()),[self.model/'model.onnx'])
        finally:self.model=real_model
    def test_resume_without_document_binding_rejected_before_old_tools(self):
        first=self.invoke('new',[{'kind':'search'},{'kind':'answer'}]);self.successful(first)
        out=self.directory/'old-mode';out.mkdir();args=['resume','--session',str(self.session),'--workspace',str(self.workspace),
            '--question',str(Path(first['directory'])/'question.txt'),'--output-dir',str(out/'runs'),'--runtime',str(out/'does-not-exist.json')]
        save(out/'settings.json',{'argv':args});save(out/'scenario.json',[])
        env={k:v for k,v in os.environ.items() if k in {'PATH','LANG'}};env.update(DEEPSEEK_API_KEY='controlled-key',DEEPSEEK_MODEL='controlled-model')
        p=subprocess.run([sys.executable,str(self.wrapper),str(self.entry),str(self.worker),str(out/'scenario.json'),str(out/'requests.jsonl'),str(out/'settings.json')],env=env,capture_output=True)
        (out/'stdout.json').write_bytes(p.stdout);(out/'stderr.txt').write_bytes(p.stderr)
        self.assertEqual(p.returncode,2,p.stdout.decode());self.assertEqual(json.loads(p.stdout)['error']['class'],'retrieval_binding_required')
        self.assertFalse((out/'requests.jsonl').exists())
    def test_mode_allowlist_and_option_exclusion(self):
        item=self.invoke('new',[{'kind':'search','name':'shell','arguments':{'command':'echo unauthorized'}}]);self.rejected(item,requests=1)
        self.assertEqual([t['function']['name'] for t in item['requests'][0]['request']['tools']],['search_docs'])
        self.assertEqual(item['result']['status'],'tool_not_allowed')
        recovery=self.invoke('recover',extra=['--state-db',str(self.directory/'no-state.sqlite'),'--receiver-manifest',str(self.directory/'no-receiver.json'),'--output-dir',str(self.directory/'no-recovery')])
        self.rejected(recovery,requests=0);self.assertEqual(recovery['result']['error']['class'],'invalid_mode')
        self.assertFalse((self.directory/'no-state.sqlite').exists())
        self.session=self.directory/'conflict.jsonl';bad=self.invoke('new',extra=['--runtime',str(self.directory/'none.json')]);self.rejected(bad,requests=0)
    def test_truncated_stream_zero_search(self):
        item=self.invoke('new',[{'kind':'search','missing_done':True}]);self.rejected(item,requests=1)
        self.assertEqual(item['result']['action_count'],0);self.assertFalse(list(self.run_dir(item).glob('search-*')))

TESTS=[name for name in CliChecks.__dict__ if name.startswith('test_')]

def run_suite(entry,model,output,names=None):
    output.mkdir(parents=True,exist_ok=False)
    class Bound(CliChecks):pass
    Bound.entry=entry;Bound.model=model;Bound.output_root=output
    selected=names or TESTS;result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(n) for n in selected))
    failed=len({str(case) for case,_ in result.failures+result.errors})
    report={'evidence_source':'controlled-sse+real-local-cpu+sqlite','external_model_requests':0,'selected_cases':selected,
        'count':result.testsRun,'passed':result.testsRun-failed,'failed':failed,'failures':len(result.failures),'errors':len(result.errors),
        'verdict':'pass' if result.wasSuccessful() else 'fail','failure_details':[{'case':str(c),'traceback':e} for c,e in result.failures+result.errors],
        'semantic_review':'required'}
    save(output/'summary.json',report);return report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('entry','embedding-model','output-dir'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--case',choices=TESTS);args=parser.parse_args()
    result=run_suite(args.entry.absolute(),args.embedding_model.absolute(),args.output_dir.absolute()/('check-'+uuid.uuid4().hex[:8]),[args.case] if args.case else None)
    print(json.dumps(result,ensure_ascii=False));return 0 if result['verdict']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
