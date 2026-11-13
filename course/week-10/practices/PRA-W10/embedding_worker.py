"""固定 E5 的离线 CPU 子进程；传输、取消与清理供给。"""
from __future__ import annotations

import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from w10_io import RetrievalError, canonical, decode, digest, snapshot, write_json

DEPENDENCIES = {'onnxruntime': '1.23.2', 'tokenizers': '0.22.2', 'numpy': '2.2.6'}
PARAMETERS = {'model': 'intfloat/multilingual-e5-small',
              'revision': '614241f622f53c4eeff9890bdc4f31cfecc418b3',
              'query_prefix': 'query: ', 'passage_prefix': 'passage: ',
              'pooling': 'masked_mean', 'normalization': 'l2', 'dimension': 384,
              'max_tokens': 512, 'tokenizer_version': 'tokenizers-0.22.2',
              'chunking': 'markdown-lf-ranges-480-v1', 'provider': 'CPUExecutionProvider',
              'intra_op': 2, 'inter_op': 1, 'batch_size': 4}
MAX_CAPTURE = 1024 * 1024


def validate_vector(vector):
    if (not isinstance(vector, (list,tuple)) or len(vector) != 384
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)):
        raise RetrievalError('embedding_invalid_vector','向量必须为384维有限数值。')
    norm=math.sqrt(sum(v*v for v in vector))
    if abs(norm-1)>1e-4:
        raise RetrievalError('embedding_invalid_vector','向量必须非零且已L2归一化。')
    return list(vector)


def validate_model(directory):
    directory = Path(directory).absolute()
    manifest = decode(Path(__file__).with_name('w10_model_manifest.json').read_bytes())
    actual = {}
    for name, expected in manifest['files'].items():
        item = snapshot(directory, name, max_bytes=expected['bytes'])
        if len(item.data)!=expected['bytes'] or item.sha256!=expected['sha256']:
            raise RetrievalError('representation_mismatch', '固定模型文件身份不符：' + name)
        actual[name] = {'bytes': len(item.data), 'sha256': item.sha256}
    representation = {**PARAMETERS, 'files': actual, 'dependencies':DEPENDENCIES,
                      'file_provenance':manifest['file_provenance']}
    return {'representation_id':'rep_'+digest(representation),'representation':representation}


def runtime_python():
    path=Path(__file__).parent/'supplies/w10/embedding-runtime.json'
    config=decode(path.read_bytes())
    if not isinstance(config,dict) or set(config)!={'python'} or not isinstance(config['python'],str):
        raise RetrievalError('embedding_runtime_missing','解释器配置无效。')
    python=Path(config['python'])
    if not python.is_absolute() or not python.is_file() or not os.access(python,os.X_OK):
        raise RetrievalError('embedding_runtime_missing','需要显式准备的绝对解释器路径。')
    return str(python)


def _worker():
    # 固定依赖不能从父进程获得 API Key；Python 出站拦截是附加约束，不是 OS 沙箱。
    import socket
    attempts=[]
    def denied(*args,**kwargs):
        attempts.append('blocked_python_socket')
        raise RuntimeError('embedding_network_forbidden')
    socket.socket.connect=denied
    socket.socket.connect_ex=denied
    socket.create_connection=denied
    request=decode(sys.stdin.buffer.readline(131073))
    print(canonical({'event':'started','pid':os.getpid(),'monotonic':time.monotonic()}).decode(),flush=True)
    started, cpu = time.monotonic(), time.process_time()
    try:
        if sys.version_info[:2]!=(3,12):
            raise RetrievalError('embedding_dependencies_missing','编码器需要Python3.12。')
        from importlib.metadata import version
        actual={name:version(name) for name in DEPENDENCIES}
        if actual!=DEPENDENCIES:
            raise RetrievalError('embedding_dependencies_mismatch',str(actual))
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        import resource
        model=Path(request['model'])
        representation=validate_model(model)
        if representation['representation_id']!=request['representation_id']:
            raise RetrievalError('representation_mismatch','worker 加载前模型身份变化。')
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        session=ort.InferenceSession(str(model/'model.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        tokenizer=Tokenizer.from_file(str(model/'tokenizer.json'))
        tokenizer.no_truncation();tokenizer.enable_padding(pad_id=1,pad_token='<pad>')
        providers=session.get_providers()
        if providers!=['CPUExecutionProvider']:
            raise RetrievalError('embedding_provider_mismatch',str(providers))
        loaded=time.monotonic(); cpu_loaded=time.process_time()
        print(canonical({'event':'loaded','pid':os.getpid(),'monotonic':loaded,'providers':providers}).decode(),flush=True)
        control=request.get('control') or {}
        if control.get('gate'):
            gate=Path(control['gate'])
            write_json(gate.with_suffix('.hit.json'),{'pid':os.getpid(),'loaded_monotonic':loaded})
            while not gate.exists():time.sleep(0.01)
        texts=request['texts']
        prefix=PARAMETERS['query_prefix'] if request['query'] else PARAMETERS['passage_prefix']
        vectors,lengths=[],[]
        for start in range(0,len(texts),4):
            encoded=tokenizer.encode_batch([prefix+text for text in texts[start:start+4]])
            actual_lengths=[sum(e.attention_mask) for e in encoded]
            if any(n>512 for n in actual_lengths):
                raise RetrievalError('embedding_input_too_long','E5前缀后超过512 token，拒绝静默截断。',evidence={'token_lengths':actual_lengths,'batch_start':start})
            inputs={'input_ids':np.asarray([e.ids for e in encoded],dtype=np.int64),
                    'attention_mask':np.asarray([e.attention_mask for e in encoded],dtype=np.int64),
                    'token_type_ids':np.asarray([e.type_ids for e in encoded],dtype=np.int64)}
            hidden=np.asarray(session.run(['last_hidden_state'],inputs)[0],dtype=np.float32)
            mask=inputs['attention_mask'][:,:,None]
            pooled=(hidden*mask).sum(axis=1)/mask.sum(axis=1)
            norms=np.linalg.norm(pooled,axis=1,keepdims=True)
            if not np.isfinite(pooled).all() or not np.isfinite(norms).all() or (norms==0).any():
                raise RetrievalError('embedding_invalid_vector','模型输出非有限或零向量。')
            pooled/=norms
            vectors.extend(validate_vector(row.tolist()) for row in pooled);lengths.extend(actual_lengths)
        result={'ok':True,**representation,'vectors':vectors,'token_lengths':lengths,
                'metrics':{'load_wall_seconds':loaded-started,'encode_wall_seconds':time.monotonic()-loaded,
                           'load_cpu_seconds':cpu_loaded-cpu,'encode_cpu_seconds':time.process_time()-cpu_loaded,
                           'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                           'providers':providers,'dependencies':actual,'python':sys.version,'network_attempts':attempts}}
    except Exception as error:
        result={'ok':False,'error':{'class':getattr(error,'code','embedding_failed'),'message':str(error)},
                'detail':getattr(error,'evidence',None),'network_attempts':attempts}
    print(canonical({'event':'result','pid':os.getpid(),'result':result}).decode(),flush=True)


class EmbeddingWorker:
    def __init__(self, model, texts, *, query, representation_id, evidence_dir, python=None, control=None):
        if (type(query) is not bool or not isinstance(texts,list) or not 1<=len(texts)<=32
                or any(not isinstance(t,str) or not t.strip() or len(t)>4096 for t in texts)):
            raise RetrievalError('invalid_embedding_input','编码输入必须为1到32个非空文本。')
        self.request={'model':str(Path(model).absolute()),'texts':texts,'query':query,
                      'representation_id':representation_id,'control':control}
        self.python=python or runtime_python()
        if not Path(self.python).is_absolute():
            raise RetrievalError('embedding_runtime_missing','解释器必须为绝对路径。')
        self.directory=Path(evidence_dir);self.process=None;self.selector=selectors.DefaultSelector()
        self.output={'stdout':bytearray(),'stderr':bytearray()};self.cleanup=None

    def start(self):
        self.directory.mkdir(parents=True,exist_ok=False)
        write_json(self.directory/'input.json',self.request)
        env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}}
        env.update(PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
        self.process=subprocess.Popen([self.python,str(Path(__file__).absolute()),'--embedding-worker'],cwd=self.directory,
            env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        for name in self.output:self.selector.register(getattr(self.process,name),selectors.EVENT_READ,name)
        self.process.stdin.write(canonical(self.request)+b'\n');self.process.stdin.close()
        write_json(self.directory/'spawn.json',{'pid':self.process.pid,'pgid':self.process.pid,'python':self.python,
            'argv':[self.python,str(Path(__file__).absolute()),'--embedding-worker'],'environment_keys':sorted(env),
            'monotonic':time.monotonic()})
        return self.process.pid

    def poll(self):
        for key,_ in self.selector.select(timeout=0):
            chunk=os.read(key.fileobj.fileno(),65536)
            if not chunk:self.selector.unregister(key.fileobj)
            else:self.output[key.data].extend(chunk)
            if sum(map(len,self.output.values()))>MAX_CAPTURE:
                raise RetrievalError('embedding_output_limit','worker输出超过1MiB。')
        return self.process.poll() is not None and not self.selector.get_map()

    def result(self):
        if not self.poll() or self.process.returncode!=0:
            raise RetrievalError('embedding_failed','worker未正常完整退出。')
        try:
            rows=[decode(line) for line in bytes(self.output['stdout']).split(b'\n') if line]
            if (not rows or rows[0]['event']!='started' or rows[-1]['event']!='result'
                    or len(rows) not in {2,3} or any(row['pid']!=self.process.pid for row in rows)
                    or len(rows)==3 and rows[1]['event']!='loaded'):
                raise ValueError('worker协议或进程身份无效')
            result=rows[-1]['result']
            if result['ok'] is not True:
                raise RetrievalError(result['error']['class'],result['error']['message'],evidence=result)
            if (result['representation_id']!=self.request['representation_id'] or len(result['vectors'])!=len(self.request['texts'])
                    or len(result['token_lengths'])!=len(self.request['texts'])
                    or any(type(n) is not int or not 1<=n<=512 for n in result['token_lengths'])):
                raise ValueError('worker表示或数量不符')
            for vector in result['vectors']:validate_vector(vector)
            write_json(self.directory/'result.json',result)
            return result
        except (KeyError,TypeError,ValueError) as error:
            raise RetrievalError('embedding_failed',str(error)) from error

    def close(self, *, cancel=False):
        if self.cleanup is not None:return self.cleanup
        actions=[];errors=[]
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    os.killpg(self.process.pid,signal.SIGTERM);actions.append('SIGTERM')
                    try:self.process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid,signal.SIGKILL);actions.append('SIGKILL');self.process.wait(timeout=1)
                while self.selector.get_map():self.poll()
        except (OSError,subprocess.TimeoutExpired,RetrievalError) as error:
            errors.append({'class':type(error).__name__,'message':str(error)})
        finally:
            self.selector.close()
            if self.process:
                for handle in (self.process.stdin,self.process.stdout,self.process.stderr):handle.close()
        confirmed=self.process is None or self.process.poll() is not None
        self.cleanup={'pid':self.process.pid if self.process else None,'returncode':self.process.returncode if self.process else None,
                      'cleanup_confirmed':confirmed,'cancel_requested':cancel,'actions':actions,'errors':errors,'monotonic':time.monotonic()}
        if self.directory.is_dir():
            for name,data in self.output.items():
                with (self.directory/(name+'.bin')).open('xb') as handle:handle.write(data)
            write_json(self.directory/'cleanup.json',self.cleanup)
        return self.cleanup


def encode(model, texts, *, query, representation_id, evidence_dir, deadline, cancelled,
           python=None, on_started=None, worker_factory=EmbeddingWorker):
    worker=worker_factory(model,texts,query=query,representation_id=representation_id,evidence_dir=evidence_dir,python=python)
    original=None;result=None;stopping=False
    try:
        if cancelled():raise RetrievalError('cancelled','编码启动前已取消。')
        if time.monotonic()>=deadline:raise RetrievalError('deadline_exceeded','编码启动前期限已到。')
        pid=worker.start()
        if on_started:on_started(pid)
        while not worker.poll():
            if cancelled():stopping=True;raise RetrievalError('cancelled','CPU编码已取消。')
            if time.monotonic()>=deadline:stopping=True;raise RetrievalError('deadline_exceeded','CPU编码超过期限。')
            time.sleep(0.01)
        if cancelled():stopping=True;raise RetrievalError('cancelled','编码返回时已取消。')
        if time.monotonic()>=deadline:raise RetrievalError('deadline_exceeded','编码返回时期限已到。')
        result=worker.result()
    except BaseException as error:
        original=error
    finally:
        try:cleanup=worker.close(cancel=stopping or original is not None)
        except BaseException as cleanup_error:
            if original is not None:
                cleanup_error.original_error={'class':type(original).__name__,'message':str(original)}
            raise
    if not cleanup['cleanup_confirmed']:
        raise RetrievalError('cleanup_unconfirmed','编码worker退出未确认。',evidence=cleanup) from original
    if original is not None:
        original.embedding_cleanup=cleanup
        raise original
    return {**result,'cleanup':cleanup,'evidence_dir':str(Path(evidence_dir).absolute())}


if __name__=='__main__':
    if sys.argv[1:]!=['--embedding-worker']:raise SystemExit('本供给仅由宿主启动。')
    _worker()
