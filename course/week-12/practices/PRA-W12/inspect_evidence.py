"""独立读取原始请求、观察、批准和服务 SQLite；语义标签另行人工核对。"""
from __future__ import annotations
import argparse,hashlib,json,math,os,re,sqlite3
from pathlib import Path
from w12_io import canonical,decode,save
def rows(path):return [decode(x) for x in Path(path).read_bytes().split(b'\n') if x]
def read(path):return decode(Path(path).read_bytes())
def require(condition,message):
    if not condition:raise ValueError(message)
def inspect_phase(directory,*,allow_controlled=False,expected=None):
    directory=Path(directory);result=read(directory/'stdout.json');run=Path(result['artifact_dir']);events=rows(run/'events.jsonl')
    require(result['status'] in {'stop','business_rejected'},'阶段没有完成比较与回执终态')
    require(all(e['seq']==i for i,e in enumerate(events,1)),'事件序号不连续')
    binding=read(run/'browser-binding.json');task=read(directory/'site/task.json');manifest=read(directory/'site/manifest.json')
    require(binding['task']==task and binding['manifest']==manifest,'运行任务或清单绑定不符')
    require(binding['task_file_sha256']==hashlib.sha256((directory/'site/task.json').read_bytes()).hexdigest(),'任务文件字节不同')
    sources={};tool_results=[];journal=rows(directory/'session.jsonl');by_id={r['entry_id']:r for r in journal if 'entry_id'in r}
    for record in journal:
        message=record.get('message',{})
        if message.get('role')=='tool':
            value=decode(message['content']);tool_results.append(value)
            if value.get('observation_id'):
                source=value['source'];ref=source['source_ref'];p=Path(value['evidence'])
                raw=read(p/'raw.json');text=(p/'source.txt').read_text()
                require(raw['text']==text,'观察原文与源文件不符');require(raw['meta']['source_id']==source['source_id'],'来源身份不符')
                require(all(source.get(k)==v for k,v in raw['meta'].items()),'来源版本或规范身份不符')
                require(source['url']==raw['url'] and source['time']==raw['time'],'来源URL或观察时间不符')
                require([{'line':i,'text':v} for i,v in enumerate(text.split('\n'),1)]==value['untrusted_page_lines'],'原始逐行正文不符')
                sources[ref]={**source,'text':text}
    require({'ordered','unique','boundaries','benchmark'} <= {v['source_id'] for v in sources.values()},'四份必要材料未实际取得')
    turns=sorted(run.glob('turn-*'));require(len(turns)==result['model_calls']<=10,'请求计量不符或超限')
    margins=[];usage=[]
    for turn in turns:
        number=int(turn.name.split('-')[1]);request=read(turn/'request.json');projection=read(run/f'projection-{number:03d}.json');receipt=read(turn/'receipt.json')
        require(request['messages']==projection['messages'],'实际请求与投影不同')
        require(len(request['messages'])==len(projection['source_ids']),'消息来源映射长度不符')
        if not allow_controlled:require('controlled' not in canonical(receipt).lower() and all('controlled' not in e['evidence_source'] for e in events),'受控传输不能冒充真实 Provider')
        raw=(turn/'stream.raw').read_text();require('data: [DONE]' in raw,'SSE缺少完成边界')
        observed_usage=None
        for block in raw.split('\n\n'):
            data='\n'.join(line[6:] for line in block.splitlines() if line.startswith('data: '))
            if data and data!='[DONE]':
                frame=decode(data)
                if frame.get('usage'):observed_usage=frame['usage']
        if observed_usage:
            require(all(type(observed_usage.get(k))is int and observed_usage[k]>=0 for k in ('prompt_tokens','completion_tokens','total_tokens')),'原始usage非法')
            require(receipt['usage'].get('usage_status')=='known' and all(receipt['usage'].get(k)==observed_usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens')),'接收回执usage与原始帧不同');usage.append(observed_usage)
        else:require(receipt['usage']['usage_status']=='usage_unknown','缺失usage被改写为已知')
        budget=projection['budget'];size=len(json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode())
        require(budget['request_bytes']==size and budget['estimated_input']==math.ceil(size/3),'请求估计与原始字节不符')
        require(budget['estimated_input']+budget['output_reserve']<=budget['context_tokens']<=8192,'上下文超限')
        margins.append(budget['context_tokens']-budget['estimated_input']-budget['output_reserve'])
        views={v['entry_id']:v for v in projection.get('browser_views',[])}
        for message,source_id in zip(request['messages'],projection['source_ids']):
            if source_id not in by_id:continue
            original=by_id[source_id].get('message')
            if not original:continue
            if source_id in views:
                view=views[source_id];require(view['kind']=='historical_browser_observation' and view['omitted']==['nodes','current_values','evidence'],'历史视图省略范围不符')
                body=decode(original['content']);rebuilt={k:v for k,v in body.items() if k not in view['omitted']}
                require(message=={**original,'content':canonical(rebuilt)},'历史观察无法从Journal重建')
            elif not projection.get('compaction_id'):require(message==original,'未声明的消息改写')
    usage_known=len(usage)==len(turns)
    p=run/'browser';intents=rows(p/'intent.jsonl');approved=[v for v in rows(p/'approvals.jsonl') if v['approved']];consumed=rows(p/'permit-consumed.jsonl')
    require(len(intents)==len(approved)==len(consumed)==1,'批准/意图/许可消费次数不符')
    intent=intents[0];require(consumed[0]['candidate_id']==intent['candidate_id'] and decode(consumed[0]['body'])==intent['payload'],'消费内容与批准不同')
    require(approved[0]['source'] in {'human','experiment-driver','ai-author'} and approved[0]['digest']==intent['digest'],'批准来源或候选摘要不符')
    candidate=read(p/(intent['candidate_id']+'.json'))
    require(all(intent.get(k)==v for k,v in candidate.items()),'持久意图与完整批准候选不同')
    require(approved[0]['candidate_id']==candidate['candidate_id'] and candidate['created_at']<=approved[0]['time']<candidate['expires_at'],'批准身份或期限不符')
    claimed=candidate.pop('digest')
    require(hashlib.sha256(canonical(candidate).encode()).hexdigest()==claimed,'候选内容摘要不符')
    network=rows(p/'network.jsonl');posts=[x for x in network if x.get('method')=='POST' and x.get('dispatch_started')]
    require(len(posts)==1 and posts[0]['body']==consumed[0]['body'],'实际网络POST不匹配或重放')
    access=rows(directory/'site/access.jsonl');received=[r for r in access if r.get('method')=='POST' and r.get('path')=='/api/selection']
    require(len(received)==1 and received[0]['body']==posts[0]['body'],'服务接收次数/字节不符')
    with sqlite3.connect((directory/'site/site.sqlite').absolute().as_uri()+'?mode=ro',uri=True) as db:
        receipts=[decode(r[0]) for r in db.execute('SELECT receipt FROM receipts')];effects=[decode(r[0]) for r in db.execute('SELECT payload FROM effects')]
    require(len(receipts)==1 and receipts[0]['payload']==intent['payload'],'服务实际receipt不匹配')
    receipt=receipts[0];require(len(effects)==(1 if receipt['status']=='committed' else 0),'实际效果计数不符')
    queries=[v for v in tool_results if v.get('receipt')];require(queries and queries[-1]['receipt']==receipt,'没有查询匹配服务回执')
    query=read(Path(queries[-1]['receipt_ref'])/'response.json');response=query.get('http')
    match=re.search(r'<script id="receipt-json" type="application/json">(.*?)</script>',response['body'],re.S) if response else None
    require(response and response['status']==200 and match and decode(match.group(1))==receipt==query['receipt']==decode(query['rendered']),'实际查询HTTP、DOM、结构化回执和服务事实不同')
    submits=[v for v in tool_results if v.get('query_required')];require(len(submits)==1 and submits[0]['business_effect']=='unknown','提交后未保留未知')
    answer=read(run/'answer.json');require(answer['registration']=={'action_id':intent['payload']['action_id'],'status':receipt['status'],'receipt_ref':queries[-1]['receipt_ref']},'报告登记事实不符')
    require(answer['task_id']==task['task_id'] and answer['dataset_version']==manifest['dataset_version'] and answer['selected_scheme']==receipt['payload']['scheme_id'],'报告任务、版本或实际选择不符')
    require(decode(read(turns[-1]/'raw-response.json')['choices'][0]['message']['content'])==answer==decode(result['text']),'已发布报告与原始模型答案或stdout不同')
    require([a['question_id'] for a in answer['answers']]==['Q1','Q2','Q3'],'问题覆盖不完整')
    for item in answer['answers']:
        for c in item['citations']:
            source=sources.get(c['source_ref']);require(source is not None,'引用不在本run实际来源')
            lines=source['text'].split('\n');require(type(c['start_line'])is int and type(c['end_line'])is int and 1<=c['start_line']<=c['end_line']<=len(lines),'引用行号错误')
            require(c['quote'] and c['quote'] in '\n'.join(lines[c['start_line']-1:c['end_line']]),'引用文字不匹配')
    cleanup=read(p/'cleanup.json');require(cleanup['cleanup_confirmed'],'清理未确认')
    for key in ('worker_pid','browser_pid'):
        try:os.kill(int(cleanup[key]),0)
        except ProcessLookupError:pass
        else:raise ValueError('实际进程仍存活：'+key)
    if expected=='B':require(any(r.get('reply_lost') for r in access),'丢失回复条件未实际命中')
    if expected=='C':require(receipt['status']=='rejected' and result['exit_code']==1,'服务拒绝路径未命中')
    if expected in {'A','B'}:require(receipt['status']=='committed' and result['exit_code']==0,'正常提交路径未完成')
    totals={'usage_status':'known' if usage_known else 'usage_unknown',**{k:sum(u[k] for u in usage) if usage_known else None for k in ('prompt_tokens','completion_tokens','total_tokens')}}
    require(all(result['usage'].get(k)==v for k,v in totals.items()),'运行累计usage与原始帧不同')
    return {'mechanical_pass':True,'model_calls':len(turns),'usage':totals,'minimum_estimated_margin':min(margins),'effect_count':len(effects),'query_count':len(queries),'semantic_review':'required','controlled':allow_controlled}

def inspect_batch(batch,*,allow_controlled=False):
    batch=Path(batch);plan=read(batch/'batch-plan.json');require(set(plan['phase_limits'])=={'A','B','C'} and all(type(v)is int and 1<=v<=10 for v in plan['phase_limits'].values()),'批次额度错误')
    require(plan['total_model_call_limit']==sum(plan['phase_limits'].values())<=30,'累计额度错误');reports={}
    for phase in ('A','B','C'):
        try:reports[phase]=inspect_phase(batch/phase,allow_controlled=allow_controlled,expected=phase);require(reports[phase]['model_calls']<=plan['phase_limits'][phase],'本阶段额度超限')
        except (ValueError,OSError,KeyError,TypeError) as e:reports[phase]={'mechanical_pass':False,'error':str(e),'semantic_review':'required'}
    return {'phases':reports,'mechanical_pass':all(r['mechanical_pass'] for r in reports.values()),'semantic_review':'required'}
def main():
    p=argparse.ArgumentParser();p.add_argument('--batch',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--allow-controlled',action='store_true');a=p.parse_args()
    result=inspect_batch(a.batch,allow_controlled=a.allow_controlled);save(a.output,result);print(canonical(result));return 0 if result['mechanical_pass'] else 1
if __name__=='__main__':raise SystemExit(main())
