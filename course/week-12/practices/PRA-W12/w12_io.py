"""浏览器 worker 的固定 JSONL 通信与独占原子发布。"""
from __future__ import annotations
import json, os, selectors, signal, subprocess, sys, time
from pathlib import Path

class BrowserError(Exception):
    def __init__(self,code,message):super().__init__(message);self.code=code

def canonical(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def decode(text):
    def pairs(items):
        d={}
        for k,v in items:
            if k in d:raise BrowserError('invalid_json','重复 JSON 字段。')
            d[k]=v
        return d
    try:return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('非有限数')))
    except (ValueError,UnicodeError) as e:raise BrowserError('invalid_json',str(e)) from e
def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def append(path,value):
    with Path(path).open('a',encoding='utf-8') as f:f.write(canonical(value)+'\n');f.flush();os.fsync(f.fileno())
def publish(path,text):
    path=Path(path);temporary=path.with_name('.'+path.name+'.'+str(os.getpid()))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(text.encode('utf-8'));f.flush();os.fsync(f.fileno())
        os.link(temporary,path)
        dfd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(dfd)
        finally:os.close(dfd)
    finally:temporary.unlink(missing_ok=True)
def check_running(deadline,cancelled):
    if cancelled():raise BrowserError('cancelled','已取消。')
    if time.monotonic()>=deadline:raise BrowserError('deadline_exceeded','运行期限已到。')
def alive(pid):
    try:os.kill(int(pid),0);return True
    except ProcessLookupError:return False

def process_identity(pid):
    value=subprocess.run(['ps','-p',str(pid),'-o','lstart=,pgid=,command='],capture_output=True,text=True,check=True).stdout.strip()
    return value

class BrowserClient:
    def __init__(self,python,entry,directory,manifest,task,session_id,run_id,*,deadline,cancelled):
        self.directory=Path(directory);self.directory.mkdir();self.deadline=deadline;self.cancelled=cancelled;self.count=0;self.buffer=b'';self.closed=False
        self.stderr=(self.directory/'worker-stderr.txt').open('wb')
        environment={k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','LANG','LC_ALL','LC_CTYPE'}}
        argv=[str(python),str(Path(__file__).with_name('w12_browser.py')),'--entry',str(Path(entry).absolute()),'--directory',str(self.directory)]
        self.process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.stderr,env=environment,start_new_session=True)
        self.selector=selectors.DefaultSelector();self.selector.register(self.process.stdout,selectors.EVENT_READ)
        save(self.directory/'worker-start.json',{'pid':self.process.pid,'argv':argv,'environment_keys':sorted(environment),'model_credentials_inherited':False})
        try:self.metadata=self.call('init',{'manifest':manifest,'task':task,'session_id':session_id,'run_id':run_id},timeout=20)
        except Exception:self.close();raise
    def call(self,method,args,*,timeout=8):
        check_running(self.deadline,self.cancelled);self.count+=1;identifier=self.count
        append(self.directory/'commands.jsonl',{'id':identifier,'method':method,'args':args,'time':time.time()})
        try:self.process.stdin.write((canonical({'id':identifier,'method':method,'args':args})+'\n').encode());self.process.stdin.flush()
        except BrokenPipeError as exc:raise BrowserError('worker_lost','浏览器 worker 通信已断开。') from exc
        until=min(self.deadline,time.monotonic()+timeout)
        while True:
            check_running(until,self.cancelled)
            if b'\n' in self.buffer:
                raw,self.buffer=self.buffer.split(b'\n',1);value=decode(raw)
                if value.get('id')!=identifier:raise BrowserError('worker_protocol','响应身份不匹配。')
                append(self.directory/'replies.jsonl',value)
                if not value.get('ok'):raise BrowserError(value['error']['class'],value['error']['message'])
                return value['result']
            if self.selector.select(0.03):
                data=os.read(self.process.stdout.fileno(),65536)
                if not data:raise BrowserError('worker_lost','浏览器 worker 已退出。')
                self.buffer+=data
    def close(self):
        if self.closed:return self.cleanup
        self.closed=True;requested=time.time();graceful=False
        if self.process.poll() is None:
            try:
                self.process.stdin.write((canonical({'id':-1,'method':'close','args':{}})+'\n').encode());self.process.stdin.flush()
                self.process.wait(timeout=10);graceful=self.process.returncode==0
            except (BrokenPipeError,subprocess.TimeoutExpired):
                os.killpg(self.process.pid,signal.SIGTERM)
                try:self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=3)
        self.selector.close();self.process.stdin.close();self.process.stdout.close();self.stderr.close()
        meta=decode((self.directory/'browser-start.json').read_text()) if (self.directory/'browser-start.json').exists() else {}
        browser_pid=meta.get('browser_pid');forced=[]
        if browser_pid and alive(browser_pid) and meta.get('process_identity'):
            try:unchanged=process_identity(browser_pid)==meta['process_identity']
            except subprocess.CalledProcessError:unchanged=False
            if unchanged and meta.get('browser_pgid')==int(browser_pid):
                for sig in (signal.SIGTERM,signal.SIGKILL):
                    if not alive(browser_pid):break
                    os.killpg(int(browser_pid),sig);forced.append(sig.name);until=time.monotonic()+2
                    while alive(browser_pid) and time.monotonic()<until:time.sleep(.05)
        until=time.monotonic()+3
        while browser_pid and alive(browser_pid) and time.monotonic()<until:time.sleep(0.05)
        confirmed=self.process.poll() is not None and (not browser_pid or not alive(browser_pid))
        self.cleanup={'worker_pid':self.process.pid,'browser_pid':browser_pid,'exit_code':self.process.returncode,'graceful':graceful,'browser_forced_signals':forced,
                      'cleanup_confirmed':confirmed,'requested_at':requested,'checked_at':time.time()}
        save(self.directory/'cleanup.json',self.cleanup);return self.cleanup
