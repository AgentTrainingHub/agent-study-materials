"""独立核对 W11 原始材料；仅标准库，不导入被测模块，不裁定自然语言蕴含。"""
from __future__ import annotations
import argparse,hashlib,importlib.util,json,re,sys
from datetime import datetime
from pathlib import Path

def load(path):return json.loads(Path(path).read_text())
def rows(path):return [json.loads(x) for x in Path(path).read_bytes().splitlines() if x] if Path(path).exists() else []
def canonical(v):return json.dumps(v,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
def sha(v):return hashlib.sha256(v).hexdigest()
def parse_time(value):return datetime.fromisoformat(value)
def raw_stream(path):
    text=path.read_bytes().decode('utf-8');frames=[];done=False;content='';calls={};reason=None;usage=None
    for block in text.replace('\r\n','\n').split('\n\n'):
        data='\n'.join(line[5:].lstrip(' ') for line in block.split('\n') if line.startswith('data:'))
        if not data:continue
        if data=='[DONE]':done=True;continue
        value=json.loads(data);frames.append(value)
        if 'usage' in value:usage=value['usage']
        for choice in value.get('choices',[]):
            delta=choice.get('delta',{});content+=delta.get('content') or ''
            if choice.get('finish_reason') is not None:reason=choice['finish_reason']
            for call in delta.get('tool_calls',[]):
                target=calls.setdefault(call['index'],{'id':None,'name':'','arguments':''})
                if call.get('id'):target['id']=call['id']
                function=call.get('function',{});target['name']+=function.get('name','');target['arguments']+=function.get('arguments','')
    return {'done':done,'content':content,'calls':list(calls.values()),'finish_reason':reason,'usage':usage,'frame_count':len(frames)}

def audit(batch):
    batch=Path(batch);errors=[];facts={'phases':{},'actor':'experiment-driver'};coverage={};counts={'reserved':0,'headers':0,'not_started':0,'unknown':0};tokens=0;usage_known=True
    def require(test,label):
        if not test:errors.append(label)
        return bool(test)
    config=load(batch/'configuration.json');summary=load(batch/'summary.json')
    require(config['limits']=={'A':8,'B':8,'C':8} and config['max_model_requests']==24,'预算配置变化')
    require(config['actor']=='experiment-driver' and summary['actor']=='experiment-driver','自动决定被标成人类')
    all_receipts=[];all_controls=[]
    for phase in ('A','B','C'):
        directory=batch/phase
        if not directory.exists():coverage[phase]='not-covered';errors.append(phase+'未采集');continue
        before=load(directory/'memory-before.json');after=load(directory/'memory-after.json');meta_before=json.loads(before['meta'][0]['payload']);meta_after=json.loads(after['meta'][0]['payload']);memories={r['memory_id']:json.loads(r['payload']) for r in after['memories']}
        require(meta_before['db_id']==meta_after['db_id'] and meta_before['project_id']==meta_after['project_id'],phase+'数据库身份变化')
        for old in before['memories']:
            new=memories.get(old['memory_id']);original=json.loads(old['payload']);require(new is not None,phase+'旧历史被删除')
            if new:require(all(new[k]==original[k] for k in ('memory_id','key','value','source_ref','source_text','source_quote','quote_range','version')),phase+'旧来源或正文被改写')
        for item in memories.values():
            source=item['source_ref'];path=Path(source['session_path']);messages=rows(path);found=[r for r in messages if r.get('entry_id')==source['entry_id']]
            require(len(found)==1 and found[0].get('type')=='message' and found[0].get('message',{}).get('role')=='user',phase+'记忆来源不是唯一user')
            if len(found)==1:
                text=found[0]['message']['content'];require(sha(text.encode())==source['content_sha256'] and text==item['source_text'],phase+'记忆来源文字不符');require(text[item['quote_range']['start']:item['quote_range']['end']]==item['source_quote'],phase+'精确引用不符')
            require(source['actor']=='experiment-driver',phase+'构造输入来源被误报')
        command_ids=set()
        for row in after['commands']:
            receipt=json.loads(row['receipt']);parameters=json.loads(row['parameters']);require(row['command_id'] not in command_ids,phase+'重复command记录');command_ids.add(row['command_id'])
            require(receipt['actor']=='experiment-driver' and parameters['actor']=='experiment-driver',phase+'控制actor不符')
            require(parse_time(receipt['requested_at'])<=parse_time(receipt['gate_wait_started_at'])<=parse_time(receipt['gate_acquired_at'])<=parse_time(receipt['committed_at']),phase+'控制时间顺序错误');all_controls.append(receipt)
        phase_count=0;attempt_facts=[]
        for collection_path in sorted(directory.glob('*/collection.json')):
            attempt=collection_path.parent;collection=load(collection_path)
            if 'reserved_model_calls' not in collection:continue
            invocation=load(attempt/'invocation.json');result=load(attempt/'stdout.json');exit_code=int((attempt/'exit-code.txt').read_text());require(exit_code==collection['exit_code']==result['exit_code'],str(attempt)+'退出状态不符')
            logs=list((attempt/'runs').glob('*/events.jsonl'));require(len(logs)==1,str(attempt)+'run事件缺失/不唯一')
            if len(logs)!=1:continue
            run=logs[0].parent;events=rows(logs[0]);requested=[x for x in events if x['event']=='model_requested'];receipts=sorted(run.glob('turn-*/receipt.json'))
            require(len(requested)==len(receipts)==collection['reserved_model_calls'],str(attempt)+'预留/实际回执计数不符');phase_count+=len(requested);counts['reserved']+=len(requested)
            journal=rows(attempt/'session-snapshot.jsonl');by_id={r.get('entry_id'):r for r in journal if r.get('entry_id')};visible_final={};largest=0;receipt_facts=[]
            for path in receipts:
                receipt=load(path);turn=path.parent;request=load(turn/'request.json');projection=load(run/('projection-'+turn.name[5:]+'.json'));gate=receipt.get('memory_gate');label=str(turn)
                require(gate is not None,label+'无真实worker gate');all_receipts.append(receipt)
                if gate is None:continue
                sent=gate['sent_status'];counts['headers' if sent=='response_headers' else 'not_started' if sent=='not_started' else 'unknown']+=1
                require(gate.get('cleanup_confirmed') is True,label+'worker未确认收尾')
                if sent=='response_headers':require(gate.get('transport_started_at') and gate.get('headers_at') and parse_time(gate['gate_checked_at'])<=parse_time(gate['transport_started_at'])<=parse_time(gate['headers_at']),label+'发送时序缺失')
                if sent=='not_started':require(not gate.get('transport_started_at'),label+'未发送状态含实际交接时间')
                require(request['messages']==projection['messages'],label+'实际输入与manifest不符')
                if receipt['purpose']=='compaction':
                    spec=importlib.util.spec_from_file_location('w11_summary_reader',Path(__file__).resolve().parents[4]/'course/week-06/practices/PRA-W06/inspect_evidence.py')
                    checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker);old=checker.load_previous().load_previous()
                    source=load(projection['source_manifest']);plan=checker.source_plan(by_id,source['source_head_id'],source['first_kept_id'],old)
                    require(all(source.get(k)==v for k,v in plan.items()),label+'摘要源范围不是原父链')
                    payload=json.loads(request['messages'][1]['content']);require(payload['new_records']==[{'source_id':ident,'run_id':by_id[ident]['run_id'],'message':by_id[ident]['message']} for ident in plan['new_source_ids']] and payload['previous_summary']==plan['previous_summary'],label+'摘要改写原消息')
                    estimate=(len(canonical(request))+2)//3;require(request['max_tokens']==1024 and projection['budget']['context_tokens']==32768 and estimate==projection['budget']['estimated_input'] and estimate+1024<=32768,label+'摘要预算不同')
                    require(gate['memory_binding']==projection['memory']['binding'],label+'摘要资格绑定不同')
                    raw=raw_stream(turn/'stream.raw');accepted=[r for r in journal if r['type']=='compaction' and r['run_id']==projection['run_id'] and r['summary_request_index']==projection['request_index']]
                    require(len(accepted)<=1,label+'摘要重复接纳')
                    if accepted:
                        normalized=checker.normalize_model_summary(json.loads(raw['content']),plan,old);checker.render_summary(normalized,plan,old)
                        require(raw['done'] and raw['finish_reason']=='stop' and not raw['calls'] and accepted[0]['summary']==normalized,label+'接纳摘要与原响应不同')
                    if receipt['usage']['usage_status']=='known':
                        require(raw['usage'] is not None and all(receipt['usage'][k]==raw['usage'][k] for k in ('prompt_tokens','completion_tokens','total_tokens')),label+'摘要usage无原始依据');tokens+=receipt['usage']['total_tokens']
                    else:usage_known=False
                    receipt_facts.append({'sent_status':sent,'purpose':receipt['purpose'],'estimated_total':estimate+1024,'gate':gate})
                    continue
                original_system=journal[0]['system']['content'];marker='\n宿主资料绑定：'
                _,raw_options=original_system.split(marker,1);original_options=json.loads(raw_options)
                _,sent_options_raw=request['messages'][0]['content'].split(marker,1);sent_options,end=json.JSONDecoder().raw_decode(sent_options_raw)
                expected_options=json.loads(json.dumps(original_options));binding_view=next((x for x in projection['derived'] if x['kind']=='memory_binding_view'),None)
                if binding_view:
                    omitted=[]
                    for key in ('session_path','corpus','index','embedding_model','state_path'):
                        if key in expected_options:del expected_options[key];omitted.append(key)
                    b=expected_options.get('memory_binding')
                    if isinstance(b,dict):
                        for key in ('db_path','workspace'):
                            if key in b:del b[key];omitted.append('memory_binding.'+key)
                        for adopted in b['adopted']:adopted['source_ref'].pop('session_path',None)
                        if b['adopted']:omitted.append('memory_binding.adopted.source_ref.session_path')
                    require(binding_view['omitted_fields']==omitted,label+'宿主路径省略范围不同')
                require(sent_options==expected_options,label+'宿主配置视图改了身份/时效/范围')
                try:user_view=json.loads(request['messages'][1]['content'])
                except ValueError:user_view=None
                if isinstance(user_view,dict) and 'context' in user_view:
                    source=by_id.get(projection['source_ids'][1]);require(source is not None and user_view['current_user_content']==source['message']['content'],label+'当前用户原话变化')
                    original_memories={x['source']:x for x in projection['memory']['context_items']}
                    for item in user_view['context']:
                        if item['kind']=='project_memory':
                            expected=json.loads(original_memories[item['source']]['content']);expected['source_ref'].pop('session_path',None)
                            require(json.loads(item['content'])==expected,label+'记忆正文或来源身份改变')
                        if item['kind']=='research_state':
                            research_view=json.loads(item['content']);task=load(batch/'inputs/research-task.json')
                            require(research_view['original_question']==task['question'] and research_view['conditions']==task['conditions'] and research_view['task_id']==task['task_id'] and research_view['task_revision']==task['task_revision'],label+'研究原问题/条件变化')

                estimate=(len(canonical(request))+2)//3;largest=max(largest,estimate+request['max_tokens']);require(estimate==projection['budget']['estimated_input'] and estimate+request['max_tokens']<=8192,label+'预算核算不符')
                require(gate['memory_binding']==projection['memory']['binding'],label+'worker绑定与实际请求投影不同')
                visible={};pending={};sources=projection['source_ids'];derived={r['index']:r for r in projection['derived'] if r['kind']=='research_tool_view'}
                for index,(message,source_id) in enumerate(zip(request['messages'],sources,strict=True)):
                    if message['role']=='assistant':
                        pending={x['id']:x['function']['name'] for x in message.get('tool_calls',[])}
                        raw_message=by_id[source_id]['message']
                        expected=json.loads(json.dumps(raw_message))
                        for d in projection['derived']:
                            if d['index']!=index or d['kind']!='research_call_view':continue
                            require(d['source_id']==source_id and d['omitted_fields']==['review'],label+'调用投影省略范围不符')
                            c=next(c for c in expected['tool_calls'] if c['id']==d['call_id'])
                            require(c['function']['name']=='search_docs',label+'非检索调用被缩写')
                            matches=[r for r in journal if r.get('message',{}).get('tool_call_id')==c['id']]
                            require(len(matches)==1 and json.loads(matches[0]['message']['content']).get('ok') is True,label+'失败检索调用被缩写')
                            args=json.loads(c['function']['arguments']);del args['review']
                            c['function']['arguments']=canonical(args).decode()
                        require(message==expected,label+'assistant投影改变原调用')
                    elif message['role']=='tool' and pending.get(message['tool_call_id'])=='search_docs':
                        raw_record=by_id.get(source_id);require(raw_record is not None,label+'检索tool来源缺失')
                        if raw_record is None:continue
                        original=json.loads(raw_record['message']['content']);value=json.loads(message['content'])
                        if original.get('ok') is not True:require(value==original,label+'错误工具被改成空结果');continue
                        expected=original['hits'][:2] if index in derived else original['hits']
                        require(len(value['hits'])==len(expected),label+'可见hit数不同')
                        for hit,raw in zip(value['hits'],expected):
                            require(all(hit.get(k)==v for k,v in raw.items() if k not in {'rank','score'}),label+'完整片段或来源被改变')
                            source=directory/'corpus-before'/hit['path'];data=source.read_bytes();lines=data.decode().replace('\r\n','\n').split('\n')
                            if lines and lines[-1]=='':lines.pop()
                            spans=hit['source_ranges'];require(all(set(span)=={'start_line','end_line','kind'} and type(span['start_line']) is int and type(span['end_line']) is int and 1<=span['start_line']<=span['end_line']<=len(lines) for span in spans),label+'来源行范围非法')
                            quoted='\n\n---\n\n'.join('\n'.join(lines[span['start_line']-1:span['end_line']]) for span in spans)
                            require(sha(data)==hit['source_sha256'] and quoted==hit['text'],label+'引用与实际来源字节不符')
                            visible[hit['citation_id']]={**hit,'query_id':value['query_id'],'generation_id':value['generation_id'],'scope':value['scope']}
                        if index in derived:
                            d=derived[index];require(d['source_id']==source_id and d['omitted_citation_ids']==[h['citation_id'] for h in original['hits'][2:]],label+'省略引用记录不符')
                if projection.get('summary_view',{}).get('reason')=='current_research_state_and_original_sources_retained':
                    require(projection.get('pinned_tool_groups',{}).get('limit')==3,label+'研究来源保留上限不符')
                    branch=journal[:next(i for i,r in enumerate(journal) if r['entry_id']==projection['source_head_id'])+1]
                    start=max(i for i,r in enumerate(branch) if r.get('message',{}).get('role')=='user');expected_ids=set();names={}
                    for record in branch[start:]:
                        message=record.get('message',{})
                        if message.get('role')=='assistant':names={c['id']:c['function']['name'] for c in message.get('tool_calls',[])}
                        elif message.get('role')=='tool' and names.get(message['tool_call_id'])=='search_docs':
                            tool_result=json.loads(message['content'])
                            if tool_result.get('ok') is True:expected_ids.add(record['entry_id'])
                    require(expected_ids<=set(projection['source_ids']),label+'研究摘要移除了实际检索来源')
                    require(not any(x['kind']=='summary' for x in user_view['context']) and any(x['kind']=='research_state' for x in user_view['context']),label+'研究状态替代摘要视图不符')
                require({x['citation_id'] for x in projection['retrieval_sources']}==set(visible),label+'可见citation清单不符');visible_final=visible
                raw=raw_stream(turn/'stream.raw')
                if receipt.get('done_seen'):require(raw['done'],label+'DONE声明无原字节')
                if receipt['usage']['usage_status']=='known':
                    require(raw['usage'] is not None and all(receipt['usage'][k]==raw['usage'][k] for k in ('prompt_tokens','completion_tokens','total_tokens')),label+'usage无原始依据');tokens+=receipt['usage']['total_tokens']
                else:usage_known=False
                receipt_facts.append({'sent_status':sent,'purpose':receipt['purpose'],'estimated_total':estimate+request['max_tokens'],'gate':gate})
            corrections=[e for e in events if e['event']=='answer_rejected']
            require(len(corrections)<=1,str(attempt)+'格式纠正次数超过一次')
            for rejected in corrections:
                number=rejected['turn'];candidate=load(run/f'turn-{number:03d}/answer-candidate.json')
                raw_candidate=load(run/f'turn-{number:03d}/raw-response.json')['choices'][0]['message']['content']
                require(candidate['text']==raw_candidate and candidate['accepted'] is False and rejected['effect']=='none',str(attempt)+'被拒答案原件或效果不同')
                try:json.loads(raw_candidate)
                except json.JSONDecodeError:pass
                else:require(False,str(attempt)+'格式错误缺少实际非法JSON')
                next_projection=load(run/f'projection-{number+1:03d}.json')
                envelope=json.loads(next_projection['messages'][1]['content'])
                feedback=[json.loads(x['content']).get('answer_rejection') for x in envelope['context'] if x['kind']=='research_state']
                require(feedback==[rejected['error']],str(attempt)+'纠正请求未包含实际错误')
            answer=run/'answer.md'
            if result['status']=='stop':
                require(answer.exists(),str(attempt)+'成功缺answer.md')
                if answer.exists():
                    require(raw['done'] and raw['finish_reason']=='stop' and not raw['calls'],str(attempt)+'发布无完整stop原响应')
                    expected_answer=json.loads(raw['content'])['answer'] if original_options.get('research_task_identity') else raw['content']
                    text=answer.read_text();require(text==expected_answer,str(attempt)+'发布文字并非原模型响应')
                    accepted=[r for r in journal if r.get('type')=='message' and r.get('run_id')==events[0]['run_id'] and r['message'].get('role')=='assistant']
                    require(bool(accepted) and accepted[-1]['message']['content']==raw['content'],str(attempt)+'Journal最后assistant与原响应不符')
                    require(text==result['text'],str(attempt)+'stdout与发布内容不同');publication=result['publication'];require(publication['text_sha256']==sha(answer.read_bytes()),str(attempt)+'发布摘要不符')
                    markers=list(re.finditer(r'(?<!\[)\[(qry_[0-9a-f]{32}\.c0[1-4])\](?!\])',text));starts={m.start(1) for m in markers};require(all(m.start() in starts for m in re.finditer('qry_',text)),str(attempt)+'引用格式错误');require(all(m.group(1) in visible_final for m in markers),str(attempt)+'引用未实际入请求')
            else:require(not answer.exists(),str(attempt)+'失败仍交付答案')
            if original_options.get('research_task_identity'):require((run/'research.json').exists(),str(attempt)+'研究状态文件缺失')
            if (run/'research.json').exists():
                research=load(run/'research.json');require(research['searches']<=3 and research['reserved_model_calls']<=6,str(attempt)+'研究计数越界')
                seen=set();stagnant=0
                for query in research['queries']:
                    identity=[]
                    for hit in query['returned_sections']:
                        if hit['origin_status']=='known':
                            key=(hit['canonical_origin'],hit['origin_revision'],hit['section_id'])
                            if key not in seen:seen.add(key);identity.append(list(key))
                    require(identity==query['new_sections'],str(attempt)+'同源新增计数错误');stagnant=0 if identity else stagnant+1
                require(stagnant==research['stagnant_searches'],str(attempt)+'停滞计数错误')
                if result['status']=='stop':
                    final=research['final'];require(final is not None and final['semantic_review']=='required',str(attempt)+'缺少独立语义标记')
                    for claim in final['checked_review']:
                        for rel in claim['checked_relations']:
                            hit=visible_final.get(rel['citation_id']);require(hit is not None and rel['quote'] in hit['text'],str(attempt)+'关系不引用当前原文')
                    if final['stop_reason']=='evidence_sufficient':require(all(x['state'] in {'supported','contradicted'} for x in final['review']),str(attempt)+'未决却声称证据充分')
                    if final['stop_reason']=='source_stagnation':require(stagnant>=2,str(attempt)+'未停滞却停止')
                    if final['stop_reason']=='retrieval_budget':require(research['searches']==3,str(attempt)+'检索数未达到预算')
            attempt_facts.append({'label':attempt.name,'status':result['status'],'http_receipts':receipt_facts,'largest_estimated_total':largest,'answer_delivered':answer.exists()})
        require(phase_count<=8,phase+'阶段额度超限');facts['phases'][phase]={'reserved_model_calls':phase_count,'attempts':attempt_facts,'memories':list(memories.values())};coverage[phase]='covered' if attempt_facts else 'not-covered'
        if phase in {'A','B'}:
            active={x['key']:x['value'] for x in memories.values() if x['status']=='active'};require(active=={'docs.default_scope':'semicolon_keep','docs.answer_format':'table' if phase=='A' else 'list'},phase+'最终有效默认不符')
            require(any(x['label']=='research' and x['status']=='stop' for x in attempt_facts),phase+'研究未完整结束')
        else:
            window=load(directory/'withdraw-window.json');require(window['handled'] is True,'C未命中真实headers窗口')
            if window.get('handled'):
                require(parse_time(window['gate']['headers_at'])<=parse_time(window['control']['committed_at']),'C控制早于真实headers');require(window['control']['status']=='stopped_using','C撤回未提交')
            require(any(x['label']=='withdrawal' and x['status']=='memory_view_changed' and not x['answer_delivered'] for x in attempt_facts),'C旧响应未阻断')
            require(any(x['label']=='fresh' and x['status']=='stop' for x in attempt_facts),'C新会话未完成')
            require([x['value'] for x in memories.values() if x['key']=='docs.default_scope' and x['status']=='active']==['semicolon_keep'],'C错误撤回scope')
            require(not any(x['key']=='docs.answer_format' and x['status']=='active' for x in memories.values()),'C格式仍active')
        for name in ('corpus','workspace'):
            old=directory/(name+'-before');new=directory/(name+'-after');require(new.exists(),phase+name+'末尾快照缺失')
            if new.exists():require({p.name:p.read_bytes() for p in old.iterdir()}=={p.name:p.read_bytes() for p in new.iterdir()},phase+name+'只读资料模式发生修改')
    controls={row['command_id']:row for row in all_controls}
    for receipt in all_receipts:
        gate=receipt.get('memory_gate',{});checked=gate.get('gate_checked_at')
        if not checked:continue
        earlier=[row for row in controls.values() if parse_time(row['committed_at'])<=parse_time(checked)]
        epoch=max((row['effective_epoch'] for row in earlier),default=0)
        require(gate['memory_binding']['effective_epoch']==epoch,'实际worker采用了已变更epoch')
    require(counts['reserved']<=24 and counts['reserved']==summary['reserved_model_calls'],'总预算计数不同')
    actual=None if counts['unknown'] else counts['headers'];require(actual==summary['actual_model_calls'],'真实交接计数不同')
    if usage_known:require(summary['usage']=={'usage_status':'known','total_tokens':tokens},'总usage不符')
    else:require(summary['usage']['usage_status']=='usage_unknown','未知usage伪报已知')
    return {'errors':errors,'facts':facts,'coverage':coverage,'coverage_complete':not errors and set(coverage.values())=={'covered'},'counts':counts,'usage':{'usage_status':'known' if usage_known else 'usage_unknown','total_tokens':tokens if usage_known else None},'evidence_source':config['evidence_source'],'semantic_review':'required'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--batch-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:result=audit(a.batch_dir)
    except (OSError,ValueError,KeyError,TypeError) as exc:result={'errors':[type(exc).__name__+': '+str(exc)],'coverage_complete':False,'semantic_review':'required'}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'coverage_complete':result['coverage_complete'],'errors':result['errors']}));return 0 if result['coverage_complete'] else 1
if __name__=='__main__':raise SystemExit(main())
