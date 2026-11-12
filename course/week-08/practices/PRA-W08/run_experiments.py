"""采集真实 W08 修复、SIGKILL 恢复及双读取取消；每组只运行一次。"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from prepare_workspace import prepare

MATERIALS=Path(__file__).resolve().parent
LIMITS={"A":8,"B":10,"C":2}
MAXIMUM_REQUESTS=20
NORMAL_MAX_TOKENS=1536
CONTEXT_TOKENS=8192

# This wrapper only delays real file-read workers. The model transport, file
# mutation, approval, Docker and recovery paths are unchanged.
READ_WRAPPER=r'''
import json,runpy,sys
from pathlib import Path
entry,controls=map(Path,sys.argv[1:3])
argv=sys.argv[3:]
sys.path.insert(0,str(entry.parent))
import w08_read
original=w08_read.ReadWorker.__init__
def init(worker,arguments,**kwargs):
    ident=kwargs['identity']
    gate=controls/(ident['assistant_id']+'-'+ident['call_id']+'.open')
    kwargs['control']={'gate':str(gate),'ignore_terminate':True}
    return original(worker,arguments,**kwargs)
w08_read.ReadWorker.__init__=init
sys.argv=[str(entry),*argv]
runpy.run_path(str(entry),run_name='__main__')
'''


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")


def lines(path):
    return [json.loads(line) for line in path.read_bytes().split(b"\n") if line] if path.exists() else []


def completed_marker(path):
    if path is None or not path.exists():return None
    raw=path.read_bytes()
    return json.loads(raw) if raw.endswith(b"\n") else None


def identity(pid):
    result=subprocess.run(["ps","-p",str(pid),"-o","lstart=","-o","command="],capture_output=True,text=True)
    return result.stdout.strip() or None


def snapshot(workspace,directory):
    directory.mkdir()
    names=[]
    for name in ("validator.py","test_validator.py"):
        source=workspace/name
        if source.exists():
            shutil.copyfile(source,directory/name);names.append(name)
    save(directory/"files.json",names)


def events(directory):
    # A writer can still be appending its final record while the supervisor reads.
    result=[]
    for path in sorted((directory/"runs").glob("*/events.jsonl")):
        data=path.read_bytes()
        result.extend(json.loads(line) for line in data.split(b"\n")[:-1] if line)
    return result


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=2)


def owned_cleanup(pid,expected):
    actions=[]
    for sig,label in ((signal.SIGTERM,"terminate"),(signal.SIGKILL,"kill")):
        current=identity(pid)
        if current!=expected:break
        os.kill(pid,sig);actions.append(label)
        end=time.monotonic()+2
        while identity(pid)==expected and time.monotonic()<end:time.sleep(0.02)
    current=identity(pid)
    return {"pid":pid,"process_identity":expected,"actions":actions,
            "identity_after":current,"cleanup_confirmed":current!=expected}


def export_database(path,destination):
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        names=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result={name:[dict(r) for r in connection.execute('SELECT * FROM "'+name+'"')] for name in names}
    save(destination,result)
    return result


def start_receiver(directory,*,hold=False,python=sys.executable):
    directory.mkdir()
    command=[python,str(MATERIALS/"receiver.py"),"--directory",str(directory)]
    if hold:command.append("--hold-after-effect")
    streams=[(directory/name).open("wb") for name in ("stdout.bin","stderr.bin")]
    process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=streams[0],stderr=streams[1],
        env={k:v for k,v in os.environ.items() if k in {"PATH","LANG","LC_ALL"}})
    try:
        end=time.monotonic()+5
        while not (directory/"receiver.json").exists():
            if process.poll() is not None or time.monotonic()>=end:raise RuntimeError("本机回执服务未就绪")
            time.sleep(0.02)
        manifest=json.loads((directory/"receiver.json").read_text())
        if manifest["pid"]!=process.pid or manifest["process_identity"]!=identity(process.pid):
            raise RuntimeError("本机服务身份与实际进程不同")
        save(directory/"controller-start.json",{"argv":command,"pid":process.pid,"process_identity":manifest["process_identity"]})
        return process,streams,manifest
    except BaseException:
        stop(process)
        for stream in streams:stream.close()
        raise


def collect(command,directory,workspace,phase,*,previous_calls=0,phase_previous_calls=0,
            kill_marker=None,read_controls=None,evidence_source="live-external-api"):
    directory.mkdir(parents=True,exist_ok=False)
    invocation={"argv":command,"cwd":str(Path(command[1]).parent),"phase":phase,
                "evidence_source":evidence_source,"started_at":time.time(),
                "model_request_limit":LIMITS[phase],"previous_model_calls":previous_calls,
                "phase_previous_model_calls":phase_previous_calls}
    process=subprocess.Popen(command,cwd=invocation["cwd"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    invocation.update(pid=process.pid,process_identity=identity(process.pid))
    save(directory/"invocation.json",invocation)
    selector=selectors.DefaultSelector()
    selector.register(process.stdout,selectors.EVENT_READ,"stdout")
    selector.register(process.stderr,selectors.EVENT_READ,"stderr")
    buffers={"stdout":bytearray(),"stderr":bytearray()}
    pending=bytearray();approvals=[];observed=[]
    stopped=None;signal_record=None
    deadline=time.monotonic()+340
    try:
        while selector.get_map() or process.poll() is None:
            requested=[r for r in events(directory) if r["event"]=="model_requested"]
            for event in requested[len(observed):]:
                item={"run_id":event["run_id"],"turn":event["turn"],"purpose":event["purpose"],"seq":event["seq"],"observed_at":time.time()}
                observed.append(item)
                with (directory/"request-observations.jsonl").open("a") as handle:handle.write(json.dumps(item)+"\n")
            if stopped is None and (len(requested)+phase_previous_calls>LIMITS[phase] or len(requested)+previous_calls>MAXIMUM_REQUESTS):
                stopped="request_budget_exceeded";stop(process);deadline=time.monotonic()+5
            if stopped is None and time.monotonic()>=deadline:
                stopped="supervisor_timeout";stop(process);deadline=time.monotonic()+5
            if stopped is not None and time.monotonic()>=deadline:break
            if signal_record is None and process.poll() is None and stopped is None:
                marker=completed_marker(kill_marker)
                if marker is not None:
                    if marker.get("committed") is not True or marker.get("boundary")!="after_effect_commit":
                        raise ValueError("故障门闩不是效果提交后的明确回执")
                    signal_record={"signal":"SIGKILL","pid":process.pid,"process_identity":invocation["process_identity"],
                        "sent_at":time.time(),"sent_monotonic":time.monotonic(),"marker_path":str(kill_marker),"marker":marker,"source":"experiment-driver"}
                    save(directory/"signal.json",signal_record);process.kill();deadline=time.monotonic()+10
                elif read_controls is not None:
                    hits=[{"path":str(path),"record":record} for path in sorted(read_controls.glob("*.hit.json"))
                          if (record:=completed_marker(path)) is not None]
                    groups={item["record"]["identity"]["assistant_id"] for item in hits}
                    if len(hits)>=2 and len(groups)==1:
                        signal_record={"signal":"SIGINT","pid":process.pid,"process_identity":invocation["process_identity"],
                            "sent_at":time.time(),"sent_monotonic":time.monotonic(),"read_arrivals":hits,"source":"experiment-driver"}
                        save(directory/"signal.json",signal_record);process.send_signal(signal.SIGINT);deadline=time.monotonic()+15
            for key,_ in selector.select(timeout=0.05):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk:selector.unregister(key.fileobj);continue
                buffers[key.data].extend(chunk)
                if key.data!="stderr":continue
                pending.extend(chunk)
                while b"\n" in pending:
                    line,_,tail=pending.partition(b"\n");pending[:]=tail
                    if not (line.startswith(b"W03_APPROVAL ") or line.startswith(b"W08_APPROVAL ")):continue
                    prompt=json.loads(line.split(b" ",1)[1])
                    proposal_dir=Path(prompt["proposal_dir"])
                    observation=directory/"driver-observations"/prompt["proposal_id"]
                    shutil.copytree(proposal_dir,observation)
                    # Keep the same actual bytes the human approval UI presented.
                    proposal=json.loads((observation/"proposal.json").read_text())
                    decision="approve" if phase in {"A","B"} and stopped is None else "deny"
                    record={"time":time.time(),"proposal":prompt,"decision":decision,"source":"experiment-driver",
                            "observation_directory":str(observation),"external_change_before_response":False}
                    if line.startswith(b"W03_APPROVAL "):
                        snapshot(workspace,observation/"workspace-before-response")
                    approvals.append(record)
                    with (directory/"driver.jsonl").open("a") as handle:handle.write(json.dumps(record,ensure_ascii=False)+"\n")
                    process.stdin.write(f"{decision} {prompt['proposal_id']}\n".encode());process.stdin.flush()
        process.wait(timeout=5)
    finally:
        selector.close();stop(process)
        for handle in (process.stdin,process.stdout,process.stderr):handle.close()
        (directory/"stdout.json").write_bytes(buffers["stdout"])
        (directory/"stderr.txt").write_bytes(buffers["stderr"])
        (directory/"exit-code.txt").write_text(str(process.returncode)+"\n")
        final=[r for r in events(directory) if r["event"]=="model_requested"]
        save(directory/"supervision.json",{"stop_reason":stopped,"model_requests_observed":len(final),
            "request_observations":observed,"approval_count":len(approvals),"signal":signal_record})
        cleanups=[]
        for path in directory.rglob("worker.json"):
            metadata=json.loads(path.read_text())
            cleanups.append({"source":str(path),**owned_cleanup(metadata["pid"],metadata["process_identity"])})
        save(directory/"controller-worker-cleanup.json",cleanups)
        readers=[]
        for path in directory.glob("runs/*/reads-*/read-*/spawn.json"):
            metadata=json.loads(path.read_text())
            readers.append({"source":str(path),**owned_cleanup(metadata["pid"],metadata["process_identity"])})
        save(directory/"controller-read-cleanup.json",readers)
    killed=signal_record is not None and signal_record["signal"]=="SIGKILL"
    if killed:
        if process.returncode!=-signal.SIGKILL:raise ValueError("SIGKILL 未命中主进程")
        result=None
    else:
        result=json.loads(buffers["stdout"])
        if result["exit_code"]!=process.returncode:raise ValueError("CLI stdout 与实际退出码不同")
    if len(final)+phase_previous_calls>LIMITS[phase] or len(final)+previous_calls>MAXIMUM_REQUESTS:
        raise ValueError("实际观察的请求超过固定预算")
    if not all(item["cleanup_confirmed"] for item in cleanups):raise RuntimeError("自有HTTP worker清理未确认")
    if not all(item["cleanup_confirmed"] for item in readers):raise RuntimeError("自有read worker清理未确认")
    return {"phase":phase,"directory":str(directory),"pid":process.pid,"exit_code":process.returncode,
            "result":result,"actual_model_calls":len(final),"signal":signal_record,"supervisor_stop_reason":stopped,
            "approval_count":len(approvals)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir",type=Path,required=True)
    parser.add_argument("--runtime",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--python",default=sys.executable)
    args=parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_MODEL"):
        parser.error("先在当前shell设置DEEPSEEK_API_KEY与DEEPSEEK_MODEL")
    student=args.student_dir.resolve(strict=True);runtime=args.runtime.resolve(strict=True)
    entry=student/"main.py"
    for name in ("w08_io.py","w08_read.py","runtime/worker.py"):
        if (student/name).read_bytes()!=(MATERIALS/name).read_bytes():parser.error("先安装W08供给与固定worker")
    batch=args.output_dir.resolve()/("batch-"+uuid.uuid4().hex[:12]);batch.mkdir(parents=True)
    save(batch/"experiment.json",{"evidence_source":"live-external-api","limits":LIMITS,"maximum_model_requests":MAXIMUM_REQUESTS,
        "normal_max_tokens":NORMAL_MAX_TOKENS,"context_tokens":CONTEXT_TOKENS,"summary_max_tokens":1024,"automatic_retries":0,"started_at":time.time()})
    (batch/"runtime.json").write_bytes(runtime.read_bytes());(batch/"runtime-worker.py").write_bytes((student/"runtime/worker.py").read_bytes())
    shutil.copytree(MATERIALS/"inputs",batch/"inputs")
    print(json.dumps({"batch_directory":str(batch),"maximum_model_requests":MAXIMUM_REQUESTS}),flush=True)
    rows=[];total=0
    try:
        for phase in LIMITS:
            directory=batch/phase;directory.mkdir()
            workspace=prepare(directory/"workspace");snapshot(workspace,directory/"before")
            session=directory/"session.jsonl";state=directory/"state.sqlite3"
            service,streams,manifest=start_receiver(directory/"receiver",hold=phase=="B",python=args.python)
            attempts=[]
            try:
                command=[args.python,str(entry),"new","--session",str(session),"--workspace",str(workspace),
                    "--question",str(batch/"inputs"/f"question-{phase.lower()}.md"),"--output-dir",str(directory/"initial/runs"),
                    "--runtime",str(batch/"runtime.json"),"--max-turns",str(LIMITS[phase]),"--max-actions","16",
                    "--max-tokens",str(NORMAL_MAX_TOKENS),"--context-tokens",str(CONTEXT_TOKENS),"--read-view-lines","80",
                    "--deadline-seconds","300","--command-timeout","10","--read-concurrency","2",
                    "--state-db",str(state),"--receiver-manifest",str(directory/"receiver/receiver.json")]
                controls=None
                if phase=="C":
                    controls=directory/"read-controls";controls.mkdir()
                    wrapper=directory/"read-timing-wrapper.py";wrapper.write_text(READ_WRAPPER)
                    command=[args.python,str(wrapper),str(entry),str(controls),*command[2:]]
                initial=collect(command,directory/"initial",workspace,phase,previous_calls=total,
                    kill_marker=directory/"receiver/after-effect-commit.json" if phase=="B" else None,read_controls=controls)
                attempts.append(initial);total+=initial["actual_model_calls"]
                if phase=="B" and initial["signal"] and initial["signal"]["signal"]=="SIGKILL":
                    (directory/"receiver/release-after-effect").touch()
                    (directory/"before-recovery-session.jsonl").write_bytes(session.read_bytes())
                    export_database(state,directory/"before-recovery-state.json")
                    export_database(Path(manifest["database"]),directory/"before-recovery-receiver.json")
                    command=[args.python,str(entry),"recover","--session",str(session),"--state-db",str(state),
                        "--receiver-manifest",str(directory/"receiver/receiver.json"),"--output-dir",str(directory/"recovery/runs")]
                    resumed=collect(command,directory/"recovery",workspace,phase,previous_calls=total,
                                    phase_previous_calls=initial["actual_model_calls"])
                    attempts.append(resumed);total+=resumed["actual_model_calls"]
                last=attempts[-1]
                row={"phase":phase,"attempts":attempts,"actual_model_calls":sum(a["actual_model_calls"] for a in attempts),
                     "status":last["result"]["status"] if last["result"] else "interrupted","semantic_verdict":"unevaluated"}
                rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
                if any(a["supervisor_stop_reason"] for a in attempts) or (phase in {"A","B"} and row["status"]!="stop"):return 1
            finally:
                snapshot(workspace,directory/"after")
                if session.exists():(directory/"after-session.jsonl").write_bytes(session.read_bytes())
                if state.exists():export_database(state,directory/"state-export.json")
                export_database(Path(manifest["database"]),directory/"receiver-export.json")
                (directory/"receiver/release-after-effect").touch();stop(service)
                for stream in streams:stream.close()
                save(directory/"receiver/controller-cleanup.json",{"pid":service.pid,"process_identity":manifest["process_identity"],
                    "returncode":service.returncode,"cleanup_confirmed":service.poll() is not None})
        return 0
    except BaseException as error:
        save(batch/"driver-error.json",{"class":type(error).__name__,"message":str(error),"completed_phases":[r["phase"] for r in rows]})
        raise
    finally:save(batch/"summary.json",rows)

if __name__=="__main__":raise SystemExit(main())
