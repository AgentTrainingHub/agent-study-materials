"""受控模型响应，只用于公开机制检查，不产生外部模型证据。"""
import json


def visible(request):
    result=[]
    for message in request['messages']:
        if message.get('role')=='tool':
            value=json.loads(message['content'])
            if value.get('ok') is True:result.extend(value.get('hits',[]))
    return result


def make_review(item,request):
    rows=[];hits=visible(request)
    for claim in ('fixed_processing_time','input_validity'):
        settings=item.get('relations',{}).get(claim)
        if settings:
            match=settings['match'];selected=next(hit for hit in reversed(hits) if match in hit['text'])
            relations=[{'citation_id':selected['citation_id'],'quote':match,'polarity':settings['polarity']}]
            rows.append({'claim_id':claim,'state':settings['state'],'relations':relations,'reason':'controlled-fixture 关系提议，语义仍需人工核对。'})
        else:rows.append({'claim_id':claim,'state':'unresolved','relations':[],'reason':'当前材料不足以确认该结论。'})
    if item.get('relation_mutation'):
        relation=next(row['relations'][0] for row in rows if row['relations'])
        if item['relation_mutation']=='quote':relation['quote']='这一句话不存在于实际检索原文'
        else:relation['citation_id']='qry_'+'0'*32+'.c01'
    return rows


def build(item,request):
    kind=item['kind'];finish=item.get('finish','stop');content='';calls=[]
    if kind in {'propose','search','research_search','batch'}:
        finish='tool_calls'
        if kind=='propose':specs=[{'name':'memory_propose','arguments':value} for value in item['candidates']]
        elif kind=='search':specs=[{'name':'search_docs','arguments':item.get('arguments',{'query':'当前合同'})}]
        elif kind=='batch':specs=item['calls']
        else:
            task=item['task'];args={'query':item['query'],'task_id':task['task_id'],'task_revision':task['task_revision'],
                'target_claim':item.get('target_claim','input_validity'),'conditions':task['conditions'],
                'scope':item.get('scope','semicolon_keep'),'source_group':item['source_group'],'review':make_review(item,request)}
            args.update(item.get('changes',{}));specs=[{'name':'search_docs','arguments':args}]
        for index,spec in enumerate(specs):calls.append({'index':index,'id':item.get('call_id','controlled-call')+'-'+str(index),
            'type':'function','function':{'name':spec['name'],'arguments':json.dumps(spec['arguments'],ensure_ascii=False)}})
    elif kind=='research_final':
        review=make_review(item,request);hits=visible(request)
        labels=' '.join('['+hit['citation_id']+']' for hit in hits[-4:])
        answer=item.get('answer','当前材料尚未完成独立语义复核，原输入的适用性和处理耗时保证均需保留未决。')+' '+labels
        content=json.dumps({'answer':answer,'review':review,'stop_reason':item.get('stop_reason','retrieval_budget')},ensure_ascii=False)
    elif kind=='answer':
        hits=visible(request);content='已取得当前资料；以下是受控回答，语义仍待人工复核。'+('['+hits[0]['citation_id']+']' if hits else '')
    elif kind=='literal':content=item['text']
    else:raise ValueError('unknown controlled fixture')
    usage={'prompt_tokens':100,'completion_tokens':30,'total_tokens':130}
    def frame(delta,reason=None):return ('data: '+json.dumps({'id':'controlled-w11','model':'controlled-model',
        'choices':[{'index':0,'delta':delta,'finish_reason':reason}],**({'usage':usage} if reason else {})},ensure_ascii=False)+'\n\n').encode()
    result=frame({'role':'assistant'})
    if content:result+=frame({'content':content})
    if calls:result+=frame({'tool_calls':calls})
    result+=frame({},finish)
    if not item.get('missing_done'):result+=b'data: [DONE]\n\n'
    return result
