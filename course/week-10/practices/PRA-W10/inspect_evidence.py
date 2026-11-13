"""独立读取原文、SQLite 导出、SSE、请求和 Journal；自动核对不替代事实语义判断。"""
from __future__ import annotations
import argparse,collections,hashlib,json,math,re
from pathlib import Path

def read(path):return json.loads(path.read_bytes())
def lines(path):return [json.loads(line) for line in path.read_bytes().split(b'\n') if line]
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def require(condition,message):
    if not condition:raise ValueError(message)
def answer_citation_labels(text):
    # Reserved query identities must all be contained in exact, single markers.
    pattern=r'(?<!\[)\[(qry_[0-9a-f]{32}\.c0[1-4])\](?!\])'
    labels=[match.group(1) for match in re.finditer(pattern,text)]
    unmarked=re.sub(pattern,'',text)
    require('qry_' not in unmarked,'回答包含裸露、残缺或合并的 qry_ 引用标识')
    return list(dict.fromkeys(labels))

def vector(value):
    require(isinstance(value,list) and len(value)==384 and all(type(x) in (int,float) and math.isfinite(x) for x in value),'384维有限向量')
    require(abs(sum(x*x for x in value)-1)<0.0002,'向量已L2归一化')
def tokens(text):
    identifiers=list(re.finditer(r'[A-Za-z_][A-Za-z0-9_]*',text));result=[m[0].lower() for m in identifiers]
    result.extend(m[0] for m in re.finditer('[0-9]+',text) if not any(v.start()<=m.start()<v.end() for v in identifiers))
    for match in re.finditer(r'[\u4e00-\u9fff]+',text):
        result.extend([match[0]] if len(match[0])==1 else [match[0][i:i+2] for i in range(len(match[0])-1)])
    return result

def bm25(query,chunks):
    bags={r['chunk_id']:collections.Counter(tokens(r['index_text'])) for r in chunks};terms=list(dict.fromkeys(tokens(query)))
    length={k:sum(v.values()) for k,v in bags.items()};average=sum(length.values())/len(bags) if bags else 0
    result=[]
    if not average:return result
    for chunk in chunks:
        ident=chunk['chunk_id'];score=0.;parts=[]
        for term in terms:
            tf=bags[ident][term]
            if not tf:continue
            df=sum(term in bag for bag in bags.values());idf=math.log(1+(len(bags)-df+0.5)/(df+0.5))
            contribution=idf*(tf*2.5)/(tf+1.5*(0.25+0.75*length[ident]/average));score+=contribution
            parts.append({'term':term,'tf':tf,'idf':idf,'score':contribution})
        if score>0:result.append({'chunk_id':ident,'score':score,'terms':parts})
    result.sort(key=lambda r:(-r['score'],r['chunk_id']))
    return [{**row,'rank':i} for i,row in enumerate(result[:8],1)]

def fused(first,second):
    total={};parts={}
    for name,route in (('bm25',first),('semantic',second)):
        unique=list(dict.fromkeys(r['chunk_id'] for r in route))
        for rank,ident in enumerate(unique,1):
            score=1/(60+rank);total[ident]=total.get(ident,0)+score;parts.setdefault(ident,{})[name]={'rank':rank,'contribution':score}
    return [{'chunk_id':ident,'score':score,'contributions':parts[ident]} for ident,score in sorted(total.items(),key=lambda r:(-r[1],r[0]))[:8]]

def close_numbers(actual,expected,label):
    if isinstance(expected,dict):
        require(isinstance(actual,dict) and set(actual)==set(expected),label+' 字段不同')
        for key,value in expected.items():close_numbers(actual[key],value,label+'.'+key)
    elif isinstance(expected,list):
        require(isinstance(actual,list) and len(actual)==len(expected),label+' 数量不同')
        for i,value in enumerate(expected):close_numbers(actual[i],value,label+f'[{i}]')
    elif type(expected) is float:require(type(actual) in (int,float) and math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-12),label+' 数值不同')
    else:require(actual==expected,label+' 不同')

def audit_generation(directory,export):
    active=next(r['value'] for r in export['meta'] if r['key']=='active_generation')
    generations={r['generation_id']:json.loads(r['payload_json']) for r in export['generations']};g=generations[active]
    require(g['generation_id']==active,'active代次身份不同')
    source=directory/'corpus-current';manifest_raw=(source/'manifest.json').read_bytes();manifest=json.loads(manifest_raw)
    require(hashlib.sha256(manifest_raw).hexdigest()==g['manifest_digest'] and manifest==g['manifest'],'清单原字节或对象不符')
    expected={r['document_id']:r for r in manifest['sources'] if r['status']=='active'};require(set(expected)==set(g['sources']),'全部active来源不同')
    for ident,item in expected.items():
        raw=(source/item['path']).read_bytes();saved=g['sources'][ident]
        require(hashlib.sha256(raw).hexdigest()==saved['sha256'],'来源原字节不同:'+ident)
        require(all(saved[k]==item[k] for k in item),'来源修订或适用范围不同')
        require(saved['object']['nlink']==1 and saved['object']['size']==len(raw),'来源对象记录不同')
    chunks={};vectors={}
    for row in export['chunks']:
        if row['generation_id']!=active:continue
        c=json.loads(row['payload_json']);v=json.loads(row['vector_json']);vector(v)
        require(c['chunk_id']==row['chunk_id'] and c['chunk_id'] not in chunks,'片段身份重复')
        item=expected[c['document_id']];raw=(source/item['path']).read_bytes();parts=raw.decode().replace('\r\n','\n').split('\n')
        if parts[-1]=='':parts.pop()
        selected=[]
        for span in c['source_ranges']:
            require(set(span)=={'kind','start_line','end_line'} and span['kind'] in {'heading','condition','header','body'},'range结构错误')
            require(type(span['start_line']) is int and type(span['end_line']) is int and 1<=span['start_line']<=span['end_line']<=len(parts),'range界限错误')
            selected.append('\n'.join(parts[span['start_line']-1:span['end_line']]))
        text='\n\n---\n\n'.join(selected)
        require(text==c['text']==c['index_text'] and len(text)<=800,'range不能重建完整索引/返回原文')
        require(c['source_sha256']==g['sources'][c['document_id']]['sha256'] and c['document_revision']==item['revision'],'chunk版本或bytes不同')
        require(c['scopes']==item['scopes'] and c['status']=='active','chunk权限不同')
        require(c['chunk_id']=='chk_'+digest({k:c[k] for k in ('document_id','document_revision','source_sha256','pipeline_version','source_ranges','text')}),'chunk_id不由完整来源身份生成')
        chunks[c['chunk_id']]=c;vectors[c['chunk_id']]=v
    require(len(chunks)==g['chunk_count'] and 1<=len(chunks)<=32,'完整代次数量不同')
    require(g['representation_id']=='rep_'+digest(g['representation']),'表示身份摘要不同')
    rep=g['representation'];require(rep['dimension']==384 and rep['query_prefix']=='query: ' and rep['passage_prefix']=='passage: ' and rep['pooling']=='masked_mean' and rep['normalization']=='l2' and rep['max_tokens']==512,'表示参数不同')
    build=directory/'build/runs';encoded=read(build/'embedding/result.json');require(encoded['representation_id']==g['representation_id'],'实际build表示不同')
    original_chunks=read(build/'chunks.json');require(len(encoded['vectors'])==len(original_chunks)==len(chunks),'build向量数量不同')
    for c,v,length in zip(original_chunks,encoded['vectors'],encoded['token_lengths'],strict=True):
        require(c==chunks[c['chunk_id']] and v==vectors[c['chunk_id']],'SQLite未保存实际编码结果')
        require(type(length) is int and 1<=length<=512 and g['token_lengths'][c['chunk_id']]==length,'实际token长度不同')
    require(read(build/'embedding/cleanup.json')['cleanup_confirmed'] is True,'build worker未确认清理')
    require(encoded['metrics']['providers']==['CPUExecutionProvider'] and not encoded['metrics']['network_attempts'],'编码未使用固定离线CPU')
    envkeys=read(build/'embedding/spawn.json')['environment_keys'];require(not any('KEY' in k or 'TOKEN' in k and k!='TOKENIZERS_PARALLELISM' for k in envkeys),'编码worker继承密钥变量')
    return g,chunks,vectors

def assemble_sse(path):
    raw=path.read_bytes().decode('utf-8').replace('\r\n','\n');require(raw.endswith('\n\n'),'SSE末帧未提交')
    text='';toolmap={};finish=None;usage=None;done=False;ident=model=None
    for block in raw.split('\n\n'):
        if not block:continue
        data='\n'.join(line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:'))
        if not data:continue
        if data=='[DONE]':done=True;continue
        require(not done,'DONE后仍有数据');value=json.loads(data)
        for key in ('id','model'):
            incoming=value.get(key)
            if incoming:
                old=ident if key=='id' else model;require(old is None or old==incoming,'SSE响应身份变化')
                if key=='id':ident=incoming
                else:model=incoming
        if 'usage' in value:usage=value['usage']
        for choice in value.get('choices',[]):
            require(choice['index']==0,'SSE多choice');delta=choice.get('delta') or {};text+=delta.get('content') or ''
            for item in delta.get('tool_calls') or []:
                target=toolmap.setdefault(item['index'],{'id':'','type':'function','function':{'name':'','arguments':''}})
                if item.get('id'):
                    require(not target['id'] or target['id']==item['id'],'SSE工具身份冲突');target['id']=item['id']
                for key in ('name','arguments'):target['function'][key]+=item.get('function',{}).get(key) or ''
            if choice.get('finish_reason') is not None:require(finish is None,'重复finish');finish=choice['finish_reason']
    require(done and finish is not None,'SSE缺少finish或DONE')
    message={'role':'assistant','content':text}
    if toolmap:message['tool_calls']=[toolmap[k] for k in sorted(toolmap)]
    result={'id':ident,'model':model,'choices':[{'index':0,'message':message,'finish_reason':finish}]}
    if usage is not None:result['usage']=usage
    return result

def query_audit(path,trace,g,chunks,vectors):
    require(re.fullmatch(r'qry_[0-9a-f]{32}',trace['query_id']) is not None,'query_id格式')
    for key in ('generation_id','representation_id','manifest_digest','corpus_id'):require(trace[key]==g[key],'query绑定不同:'+key)
    require(trace['source_digests']=={k:v['sha256'] for k,v in g['sources'].items()},'query源digest集合不同')
    ordered=[chunks[k] for k in sorted(chunks)];eligible=[c for c in ordered if trace['scope'] in c['scopes'] and c['status']=='active']
    require(trace['input_pool']==[r['chunk_id'] for r in eligible],'scope/status没有在top-k前过滤')
    expected_filter=[{'chunk_id':c['chunk_id'],'document_id':c['document_id'],'scopes':c['scopes'],'status':c['status'],'eligible':c in eligible} for c in ordered]
    require(trace['filter']==expected_filter,'过滤观察记录不同')
    encoded=read(path/'embedding/result.json');require(encoded['representation_id']==g['representation_id'],'query编码表示不同')
    require(len(encoded['vectors'])==1,'query需要一份向量');v=encoded['vectors'][0];vector(v)
    require(read(path/'embedding/input.json')['texts']==[trace['query']] and read(path/'embedding/input.json')['query'] is True,'query编码输入不同')
    require(read(path/'embedding/cleanup.json')['cleanup_confirmed'] is True,'query worker清理未确认')
    require(trace['tokens']=={'bm25':tokens(trace['query']),'embedding':encoded['token_lengths'][0]},'词项或token不同')
    lexical=bm25(trace['query'],eligible)
    semantic=sorted([{'chunk_id':c['chunk_id'],'score':sum(a*b for a,b in zip(v,vectors[c['chunk_id']],strict=True))} for c in eligible],key=lambda r:(-r['score'],r['chunk_id']))[:8]
    semantic=[{**r,'rank':i} for i,r in enumerate(semantic,1)];fusion=fused(lexical,semantic)
    for key,value in (('bm25',lexical),('semantic',semantic),('fused',fusion)):close_numbers(trace[key],value,key)
    original=fusion if trace['mode']=='hybrid' else lexical if trace['mode']=='bm25' else semantic
    if trace['rerank']:
        payload=read(path/'rerank-input.json');normalized=read(path/'rerank-response.json')
        require(normalized['status']=='stop' and not normalized['calls'] and normalized['error'] is None,'重排非完整成功')
        require(normalized['usage']['usage_status']=='known','重排usage未知')
        order=json.loads(normalized['text']);ids=[r['chunk_id'] for r in original]
        require(set(order)=={'order'} and len(order['order'])==len(ids) and set(order['order'])==set(ids) and len(set(order['order']))==len(ids),'重排不是完整排列')
        require([c['chunk_id'] for c in payload['candidates']]==ids,'重排输入没有完整原候选')
        for c in payload['candidates']:
            chunk=chunks[c['chunk_id']];require(c['text']==chunk['text'],'重排正文截短或更改')
            require(c['source']=={k:chunk[k] for k in ('document_id','document_revision','path','source_sha256','source_ranges')},'重排来源不同')
        candidate=[next(r for r in original if r['chunk_id']==ident) for ident in order['order']]
        close_numbers(trace['reranked'],candidate,'reranked')
    else:require(trace['reranked'] is None,'off发生重排');candidate=original
    close_numbers(trace['final'],candidate[:4],'final')
    require(trace['error'] is None,'成功查询带错误')
    result=read(path/'result.json');require(result['ok'] is True and result['query_id']==trace['query_id'],'工具结果query不同')
    require(result['scope']==trace['scope'] and result['generation_id']==g['generation_id'],'工具结果绑定不同')
    require([h['chunk_id'] for h in result['hits']]==[r['chunk_id'] for r in trace['final']],'工具结果不是最终排名')
    for i,hit in enumerate(result['hits'],1):
        c=chunks[hit['chunk_id']];require(all(hit[k]==c[k] for k in ('document_id','document_revision','path','source_sha256','source_ranges','text')),'返回片段失去完整来源')
        require(hit['citation_id']==trace['query_id']+f'.c{i:02d}' and hit['rank']==i,'citation不是本次query唯一身份')
    return result

def phase_audit(directory):
    phase=read(directory/'phase-config.json');export=read(directory/'index-after.json');g,chunks,vectors=audit_generation(directory,export)
    invocation=read(directory/'attempt-01/invocation.json');collection=read(directory/'attempt-01/collection.json')
    stdout=read(directory/'attempt-01/stdout.json');exit_code=int((directory/'attempt-01/exit-code.txt').read_text())
    require(exit_code==stdout['exit_code']==collection['exit_code']==0 and stdout['status']=='stop','phase没有成功终态')
    require(phase['scope']=='semicolon_keep' and phase['mode']=='hybrid','本批固定scope/mode不同')
    expected_binding={key:g[key] for key in ('corpus_id','manifest_digest','generation_id','representation_id')}
    expected_binding.update(scope=phase['scope'],mode=phase['mode'],rerank=phase['rerank']=='on')
    require(stdout['retrieval_binding']==expected_binding,'stdout资料绑定偏离本阶段许可')
    run_paths=list((directory/'attempt-01/runs').iterdir());require(len(run_paths)==1,'phase必须恰好一个新run');run=run_paths[0]
    events=lines(run/'events.jsonl');require(events[0]['event']=='run_started' and events[-1]['event']=='run_finished','run事件缺首尾')
    require([e['seq'] for e in events]==list(range(1,len(events)+1)),'事件序号不同')
    require(all(e['run_id']==stdout['run_id'] and e['session_id']==stdout['session_id'] for e in events),'事件归属不同')
    requests=[e for e in events if e['event']=='model_requested'];returned=[e for e in events if e['event']=='model_returned']
    require(len(requests)==len(returned)==stdout['model_calls']==collection['actual_model_calls']<=4,'真实请求计数不同或超限')
    require([r['turn'] for r in requests]==list(range(1,len(requests)+1)),'请求编号不是共同顺序')
    journal=lines(directory/'after-session.jsonl');by_id={r['entry_id']:r for r in journal};require(len(by_id)==len(journal),'Journal身份重复')
    parent=None
    for row in journal:require(row['parent_id']==parent,'新会话父链不同');parent=row['entry_id']
    require(journal[0]['type']=='header' and journal[-1]['type']=='run_end' and journal[-1]['status']=='stop','Journal首尾不同')
    require(stdout['head_id']==parent,'stdout head与Journal不同')
    require(not any(r['type'] in {'compaction','branch','wait'} for r in journal),'本采集固定为短新会话，不应有旧摘要/分支/等待')
    messages=[r for r in journal if r['type']=='message'];calls={};feedback={};pending={}
    for row in messages:
        msg=row['message']
        if msg['role']=='assistant':
            require(not pending,'前一assistant工具未配对')
            for call in msg.get('tool_calls',[]):
                require(call['id'] not in pending,'同批重复call_id')
                key=(row['entry_id'],call['id']);calls[key]=call;pending[call['id']]=key
        elif msg['role']=='tool':
            ident=msg['tool_call_id'];require(ident in pending,'tool缺call或重复');feedback[pending.pop(ident)]=row
    require(set(calls)==set(feedback) and 1<=len(calls)<=2,'search工具没有完整配对或数量错误')
    for call in calls.values():require(call['function']['name']=='search_docs' and set(json.loads(call['function']['arguments']))=={'query'},'工具扩大许可')
    query_records={json.loads(r['payload_json'])['query_id']:json.loads(r['payload_json']) for r in export['queries']}
    traces=[];tools={}
    for path in sorted(run.glob('search-*')):
        trace=read(path/'query.json');require(trace==query_records[trace['query_id']],'原trace与SQLite不同')
        require(all(trace[key]==expected_binding[key] for key in expected_binding),'query偏离本阶段绑定')
        keys=[k for k,r in feedback.items() if json.loads(r['message']['content']).get('query_id')==trace['query_id']]
        require(len(keys)==1,'query没有唯一原tool');key=keys[0]
        require(trace['run_id']==stdout['run_id'] and trace['call_id']==key[1] and trace['origin']=='agent_tool','trace归属错误')
        require(trace['query']==json.loads(calls[key]['function']['arguments'])['query'],'query与实际模型参数不同')
        tool=query_audit(path,trace,g,chunks,vectors);require(tool==json.loads(feedback[key]['message']['content']),'Journal tool不等于实际query结果')
        traces.append(trace);tools[trace['call_id']]=tool
    require(len(traces)==len(calls),'search trace数量不同')
    uses=[];last_visible=None;last_message=None;total_tokens=0;known=True;max_budget=0;purposes=[]
    for event in requests:
        number=event['turn'];turn=run/f'turn-{number:03d}';request=read(turn/'request.json');raw=read(turn/'raw-response.json');receipt=read(turn/'receipt.json');projection=read(run/f'projection-{number:03d}.json')
        require(assemble_sse(turn/'stream.raw')==raw,'原SSE与归并响应不同')
        require(receipt['done_seen'] is True and receipt['http_status']==200 and receipt['error'] is None,'模型回执不完整')
        require(projection['messages']==request['messages'],'projection与实际request消息不同')
        purpose=event['purpose'];purposes.append(purpose);require(receipt['purpose']==projection['purpose']==purpose and projection['request_index']==number and projection['run_id']==stdout['run_id'],'purpose/编号不共享')
        size=len(canonical(request));budget=projection['budget'];require(budget['request_bytes']==size and budget['estimated_input']==(size+2)//3 and budget['output_reserve']==request['max_tokens'],'预算未计入完整请求')
        require(budget['fits'] is True and budget['estimated_input']+budget['output_reserve']<=budget['context_tokens']<=8192,'请求预算超限')
        max_budget=max(max_budget,budget['estimated_input']+budget['output_reserve'])
        require(receipt['budget']==budget,'回执预算不同')
        usage=raw.get('usage')
        if isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens','total_tokens')):
            require(usage['prompt_tokens']+usage['completion_tokens']==usage['total_tokens'],'usage合计不同')
            require(receipt['usage']=={'usage_status':'known',**{k:usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')}},'回执usage不同');total_tokens+=usage['total_tokens']
        else:known=False;require(receipt['usage']['usage_status']=='usage_unknown','缺usage被写已知')
        if purpose=='retrieval_rerank':
            require(phase['rerank']=='on' and 'tools' not in request and request['max_tokens']==384 and request['temperature']==0,'重排请求许可或参数错误')
            require(receipt['usage']['usage_status']=='known','重排unknown仍继续')
            payload=json.loads(request['messages'][-1]['content']);origin=projection['origin_tool'];require(origin['call_id'] in tools,'重排缺原call')
            matching=next(t for t in traces if t['call_id']==origin['call_id']);path=next(p for p in run.glob('search-*') if read(p/'query.json')['query_id']==matching['query_id'])
            require(payload==read(path/'rerank-input.json'),'重排真实输入和工具记录不同')
            normalized=read(path/'rerank-response.json');require(raw['choices'][0]['message']['content']==normalized['text'],'重排原响应与接纳文本不同')
            require(projection['candidates']==payload['candidates'],'重排候选投影不同')
        else:
            require(purpose=='task','本短任务不应额外摘要')
            require([t['function']['name'] for t in request['tools']]==['search_docs'] and request['max_tokens']==1536,'资料模式工具表或输出上限不同')
            require(len(request['messages'])==len(projection['source_ids']),'实际请求来源数量不同')
            actual_visible=[];pending={}
            for message,source in zip(request['messages'],projection['source_ids'],strict=True):
                require(source in by_id,'请求消息没有原Journal身份')
                record=by_id[source];original=record['system'] if record['type']=='header' else record.get('message')
                require(message==original,'资料正文被截短、替换或来自摘要')
                if message['role']=='assistant':pending={c['id']:c['function']['name'] for c in message.get('tool_calls',[])}
                if message['role']=='tool' and pending.get(message['tool_call_id'])=='search_docs':
                    value=json.loads(message['content'])
                    if value.get('ok'):
                        for hit in value['hits']:
                            actual_visible.append({**hit,'query_id':value['query_id'],'generation_id':value['generation_id'],'scope':value['scope'],'tool_entry_id':source,'text_sha256':hashlib.sha256(hit['text'].encode()).hexdigest()})
            require(projection['retrieval_sources']==actual_visible,'retrieval_sources并非实际输入重建')
            require(projection['retrieval_binding']==stdout['retrieval_binding'],'task绑定不同')
            last_visible=actual_visible;last_message=raw['choices'][0]
        uses.append({'request_index':number,'purpose':purpose,'usage':receipt['usage'],'budget':budget})
    require(last_message and last_message['finish_reason']=='stop' and last_message['message']['content']==stdout['text'],'最终stdout不是原最终stop回答')
    require(messages[-1]['message']==last_message['message'],'最终Journal不是原stop回答')
    visible={r['citation_id']:r for r in last_visible};labels=answer_citation_labels(stdout['text'])
    require(all(re.fullmatch(r'qry_[0-9a-f]{32}\.c0[1-4]',label) and label in visible for label in labels),'回答引用不在实际可见片段中')
    citation=read(run/'citations.json');require(citation['accepted'] is True and citation['searched'] is True and citation['semantic_review']=='required','引用接纳/语义边界不同')
    require(citation['citations']==[visible[label] for label in labels] and citation['visible_count']==len(last_visible),'引用映射不同')
    require(read(run/'answer-candidate.json')['text']==stdout['text'],'未接纳候选原文不同')
    rerank_count=purposes.count('retrieval_rerank')
    require(1 <= rerank_count <= 2 if phase['phase']=='A' else rerank_count==0,'阶段重排目标未命中')
    require(stdout['rerank_calls']==purposes.count('retrieval_rerank') and stdout['task_calls']==purposes.count('task') and stdout['summary_calls']==0,'分类请求计数不同')
    for name in ('validator.py','test_validator.py'):require((directory/'workspace-before'/name).read_bytes()==(directory/'workspace-after'/name).read_bytes(),'资料模式修改业务文件')
    for path in (directory/'corpus-current').iterdir():require(path.read_bytes()==(directory/'corpus-after'/path.name).read_bytes(),'回答期间来源变化未阻断')
    if phase['phase']!='A':
        stale=read(directory/'stale-query/collection.json');require(stale['exit_code']==2 and stale['result']['error']['class']=='index_stale','变化后旧代次未阻断')
        require(read(directory/'old-session-inspect/collection.json')['exit_code']==0,'旧历史不能inspect')
    fact={'phase':phase['phase'],'generation_id':g['generation_id'],'representation_id':g['representation_id'],'chunk_count':len(chunks),
        'model_calls':len(requests),'search_calls':len(traces),'rerank_calls':purposes.count('retrieval_rerank'),'max_complete_request_estimate':max_budget,
        'usage':{'usage_status':'known' if known else 'usage_unknown','total_tokens':total_tokens if known else None},'answer':stdout['text'],
        'citations':citation['citations'],'queries':[{'query_id':t['query_id'],'query':t['query'],'final':t['final']} for t in traces],
        'semantic_review':'required','evidence_source':invocation['evidence_source']}
    return fact

def audit_batch(batch):
    configuration=read(batch/'configuration.json');summary=read(batch/'summary.json');errors=[];facts=[];coverage={}
    for phase in ('A','B','C'):
        path=batch/phase
        if not path.is_dir():coverage[phase]='not_run';continue
        try:facts.append(phase_audit(path));coverage[phase]='hit'
        except (OSError,ValueError,KeyError,TypeError,StopIteration,IndexError) as error:
            errors.append({'phase':phase,'class':type(error).__name__,'message':str(error)});coverage[phase]='failed_or_incomplete'
    if len(facts)==3:
        try:
            require(len({f['generation_id'] for f in facts})==3,'三阶段未使用不同代次')
            require(len({f['representation_id'] for f in facts})==1,'三阶段表示改变')
            for phase,revision,status,count in (('A','1','active','3 条'),('B','2','active','5 条'),('C','3','withdrawn','5 条')):
                manifest=read(batch/phase/'corpus-current/manifest.json');item=next(r for r in manifest['sources'] if r['document_id']=='support-guide')
                require(manifest['revision']==revision and item['status']==status,'阶段变化清单不同')
                require(count in (batch/phase/'corpus-current/support-guide.md').read_text(),'阶段支持规则原文不同')
            total=sum(f['model_calls'] for f in facts);require(total==summary['actual_model_calls']<=12,'批次模型请求合计不同')
            known=all(f['usage']['usage_status']=='known' for f in facts)
            require(summary['usage']=={'usage_status':'known' if known else 'usage_unknown','total_tokens':sum(f['usage']['total_tokens'] for f in facts) if known else None},'批次usage合计不同')
        except (ValueError,KeyError,StopIteration) as error:errors.append({'phase':'batch','class':type(error).__name__,'message':str(error)})
    return {'schema':1,'batch':str(batch),'evidence_source':configuration['evidence_source'],'facts':facts,'errors':errors,'coverage':coverage,
        'coverage_complete':not errors and all(coverage.get(p)=='hit' for p in ('A','B','C')),'semantic_review':'required',
        'task_success':'unreviewed','independent_human_trial':'pending'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:result=audit_batch(a.batch.absolute())
    except (OSError,ValueError,KeyError,TypeError) as error:result={'coverage_complete':False,'errors':[{'class':type(error).__name__,'message':str(error)}],'semantic_review':'required'}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x') as handle:json.dump(result,handle,ensure_ascii=False,indent=2);handle.write('\n')
    print(json.dumps({'coverage_complete':result['coverage_complete'],'errors':result['errors'],'semantic_review':'required','output':str(a.output)},ensure_ascii=False))
    return 0 if result['coverage_complete'] else 1
if __name__=='__main__':raise SystemExit(main())
