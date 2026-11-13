"""先冻结原文金标到当前分块，再从实际 CLI 采集三路排名；不调用模型 API。"""
from __future__ import annotations
import argparse,hashlib,json,os,sqlite3,subprocess,sys,time
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent

def save(path,value):
    with path.open('x') as handle:json.dump(value,handle,ensure_ascii=False,indent=2);handle.write('\n')

def current(index):
    with sqlite3.connect(index.as_uri()+'?mode=ro',uri=True) as db:
        active=db.execute("SELECT value FROM meta WHERE key='active_generation'").fetchone()[0]
        gen=json.loads(db.execute('SELECT payload_json FROM generations WHERE generation_id=?',(active,)).fetchone()[0])
        chunks=[json.loads(r[0]) for r in db.execute('SELECT payload_json FROM chunks WHERE generation_id=? ORDER BY chunk_id',(active,))]
    return gen,chunks

def freeze(corpus,index,directory):
    generation,chunks=current(index)
    source=MATERIALS/'inputs/gold.json';gold=json.loads(source.read_text())
    bound=[]
    for question in gold['questions']:
        relevant=set()
        for support in question['support_ranges']:
            doc=generation['sources'][support['document_id']]
            raw=(corpus/doc['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=doc['sha256']:raise ValueError('冻结金标前源文件变化')
            lines=raw.decode().replace('\r\n','\n').split('\n')
            if '\n'.join(lines[support['start_line']-1:support['end_line']])!=support['text']:raise ValueError('固定金标原文范围不符')
            matching=[row for row in chunks if row['document_id']==support['document_id'] and question['scope'] in row['scopes']
                and any(r['start_line']<=support['start_line']<=support['end_line']<=r['end_line'] for r in row['source_ranges'])]
            if not matching:raise ValueError('金标范围没有完整片段')
            for row in matching:
                for required in question.get('required_context',[]):
                    if required['document_id']==row['document_id'] and not any(r['start_line']<=required['start_line']<=required['end_line']<=r['end_line'] for r in row['source_ranges']):
                        raise ValueError('相关片段遗漏适用条件')
                relevant.add(row['chunk_id'])
        bound.append({**question,'relevant_chunk_ids':sorted(relevant)})
    value={'schema':1,'generation_id':generation['generation_id'],'representation_id':generation['representation_id'],
        'gold_source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'frozen_at':time.time(),
        'binding_rule':'each fixed supporting range must be wholly present; required context must be carried',
        'questions':bound,'comparison_started':False,'semantic_review':'required'}
    save(directory/'gold-binding.json',value)
    return value

def metrics(relevant,returned):
    if len(returned)!=len(set(returned)) or len(returned)>4:raise ValueError('返回身份重复或超过4')
    if not relevant:return {'returned_count':len(returned),'gold_count':0,'recall_at_4':None,'precision_at_upto_4':None,'mrr_at_4':None,'gap_judgment':'required'}
    overlap=len(set(returned)&set(relevant))
    return {'returned_count':len(returned),'gold_count':len(relevant),'recall_at_4':overlap/len(relevant),
        'precision_at_upto_4':overlap/len(returned) if returned else 0,
        'mrr_at_4':next((1/i for i,c in enumerate(returned,1) if c in relevant),0)}

def run(entry,corpus,index,model,directory):
    directory.mkdir(parents=True,exist_ok=False);gold=freeze(corpus,index,directory);rows=[]
    env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}}
    for question in gold['questions']:
        path=directory/(question['question_id']+'.txt');path.write_text(question['question']+'\n')
        for mode in ('bm25','semantic','hybrid'):
            out=directory/(question['question_id']+'-'+mode);out.mkdir()
            command=[sys.executable,str(entry),'corpus-query','--corpus',str(corpus),'--index',str(index),'--embedding-model',str(model),
                '--doc-scope',question['scope'],'--retrieval-mode',mode,'--question-file',str(path),'--output-dir',str(out/'query')]
            save(out/'invocation.json',{'argv':command,'evidence_source':'real-local-cpu','external_model_requests':0,'started_at':time.time()})
            process=subprocess.run(command,capture_output=True,env=env,timeout=310)
            (out/'stdout.json').write_bytes(process.stdout);(out/'stderr.txt').write_bytes(process.stderr);(out/'exit-code.txt').write_text(str(process.returncode)+'\n')
            result=json.loads(process.stdout)
            if process.returncode or result.get('ok') is not True:raise RuntimeError('CLI 比较失败：'+str(out))
            trace=json.loads((out/'query/query.json').read_text())
            if trace['generation_id']!=gold['generation_id'] or trace['representation_id']!=gold['representation_id']:raise ValueError('比较中绑定变化')
            returned=[v['chunk_id'] for v in trace['final']]
            rows.append({'question_id':question['question_id'],'category':question['category'],'mode':mode,'directory':str(out),
                         'returned':returned,**metrics(question['relevant_chunk_ids'],returned)})
    means={}
    for mode in ('bm25','semantic','hybrid'):
        eligible=[r for r in rows if r['mode']==mode and r['gold_count']]
        means[mode]={'answerable_question_count':len(eligible),**{key:sum(row[key] for row in eligible)/len(eligible)
            for key in ('recall_at_4','precision_at_upto_4','mrr_at_4')}}
    result={'schema':1,'evidence_source':'real-local-cpu','external_model_requests':0,'query_count':len(rows),'generation_id':gold['generation_id'],
            'representation_id':gold['representation_id'],'per_question':rows,'macro':means,'semantic_review':'required','hybrid_win_required':False}
    save(directory/'summary.json',result);return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('entry','corpus','index','embedding-model','output-dir'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();result=run(args.entry.absolute(),args.corpus.absolute(),args.index.absolute(),args.embedding_model.absolute(),args.output_dir.absolute())
    print(json.dumps({'summary':str(args.output_dir/'summary.json'),'query_count':result['query_count'],'macro':result['macro']},ensure_ascii=False))
if __name__=='__main__':main()
