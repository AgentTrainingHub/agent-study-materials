"""采集 W09 真实 keep/unique 任务、持久决定中断与分隔符修订；零自动重试。"""
from __future__ import annotations
import argparse
import difflib
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
LIMITS={"A":10,"B":10,"C":8}
MAXIMUM_REQUESTS=28
NORMAL_MAX_TOKENS=1536
CONTEXT_TOKENS=8192

# This observer pauses only after the actual decide transaction committed.
# It does not change the candidate, permit, model transport or commit function.
DECISION_WRAPPER=r'''
import json,os,runpy,sys,time
from pathlib import Path
entry,marker=map(Path,sys.argv[1:3])
sys.path.insert(0,str(entry.parent))
import decisions
from w08_io import write_json
original=decisions.DecisionStore.decide
def decide(store,command):
    receipt=original(store,command)
    if receipt.get('status')!='approved':
        raise RuntimeError('the declared approval was not committed')
    write_json(marker,{'boundary':'after_decision_commit','committed':True,'pid':os.getpid(),
        'command_id':command['command_id'],'wait_id':command['wait_id'],
        'status':receipt['status'],'decision_id':receipt['decision_id'],'monotonic':time.monotonic()})
    while True:time.sleep(0.02)
decisions.DecisionStore.decide=decide
sys.argv=[str(entry),*sys.argv[3:]]
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


def collect(command,directory,workspace,phase,*,previous_calls=0,phase_previous_calls=0,
            kill_marker=None,evidence_source="live-external-api"):
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
    deadline=time.monotonic()+620
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
                    if marker.get("committed") is not True or marker.get("boundary")!="after_decision_commit" or marker.get("pid")!=process.pid or marker.get("status")!="approved":
                        raise ValueError("故障门闩必须是本决定进程已提交的批准回执")
                    signal_record={"signal":"SIGKILL","pid":process.pid,"process_identity":invocation["process_identity"],
                        "sent_at":time.time(),"sent_monotonic":time.monotonic(),"marker_path":str(kill_marker),"marker":marker,"source":"experiment-driver"}
                    save(directory/"signal.json",signal_record);process.kill();deadline=time.monotonic()+10
            for key,_ in selector.select(timeout=0.05):
                chunk=os.read(key.fileobj.fileno(),65536)
                if not chunk:selector.unregister(key.fileobj);continue
                buffers[key.data].extend(chunk)
                if key.data!="stderr":continue
                pending.extend(chunk)
                while b"\n" in pending:
                    line,_,tail=pending.partition(b"\n");pending[:]=tail
                    if line.startswith((b"W03_APPROVAL ",b"W08_APPROVAL ")):
                        raise ValueError("workflow任务出现即时审批；应保存候选后退出等待")
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


def wait_record(state,wait_id):
    with sqlite3.connect(f"file:{state}?mode=ro",uri=True) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM workflow_waits WHERE wait_id=?",(wait_id,)).fetchone()
        if row is None:raise ValueError("CLI等待没有对应持久记录")
        row=dict(row)
    return {**json.loads(row["origin_json"]),**row}


def run_batch(student,runtime,output_root,*,python=sys.executable,evidence_source="live-external-api",command_prefix=None):
    student=student.resolve(strict=True);runtime=runtime.resolve(strict=True);entry=student/"main.py"
    batch=output_root.resolve()/("batch-"+uuid.uuid4().hex[:12]);batch.mkdir(parents=True)
    save(batch/"experiment.json",{"evidence_source":evidence_source,"limits":LIMITS,"maximum_model_requests":MAXIMUM_REQUESTS,
        "normal_max_tokens":NORMAL_MAX_TOKENS,"context_tokens":CONTEXT_TOKENS,"summary_max_tokens":1024,
        "request_projection":"workflow-tool-view-v9","automatic_retries":0,"started_at":time.time()})
    (batch/"runtime.json").write_bytes(runtime.read_bytes())
    for name in ("worker","runner"):(batch/f"runtime-{name}.py").write_bytes((student/f"runtime/{name}.py").read_bytes())
    shutil.copytree(MATERIALS/"inputs",batch/"inputs")
    print(json.dumps({"batch_directory":str(batch),"maximum_model_requests":MAXIMUM_REQUESTS}),flush=True)
    rows=[];total=0;source_a=None
    try:
        for phase in LIMITS:
            directory=batch/phase;directory.mkdir();attempts=[];phase_calls=0
            if phase=="C":
                workspace,session,state=source_a
                (directory/"before-session.jsonl").write_bytes(session.read_bytes())
                export_database(state,directory/"before-state.json")
            else:
                workspace=prepare(directory/"workspace");session=directory/"session.jsonl";state=directory/"state.sqlite3"
                if phase=="A":source_a=(workspace,session,state)
            snapshot(workspace,directory/"before")
            save(directory/"phase-config.json",{"phase":phase,"workspace":str(workspace),"session":str(session),
                "state_db":str(state),"reuse_from":"A" if phase=="C" else None,
                "question_value":"unique" if phase=="B" else "keep","maximum_model_requests":LIMITS[phase]})

            def invoke(command,arguments,*,kill_marker=None,control_kind=None):
                nonlocal total,phase_calls
                if len(attempts)>=48:raise RuntimeError("超过固定48次CLI进程上限")
                destination=directory/f"attempt-{len(attempts)+1:03d}"
                argv=[python,str(entry),command,"--session",str(session),*arguments]
                if command in {"continue","new"}:argv += ["--output-dir",str(destination/"runs")]
                if kill_marker is not None:
                    wrapper=directory/"decision-boundary.py"
                    if not wrapper.exists():wrapper.write_text(DECISION_WRAPPER)
                    argv=[python,str(wrapper),str(entry),str(kill_marker),*argv[2:]]
                # The public CLI never supplies this hook. Local author checks use
                # a separate network-forbidden transport; real runs pass None.
                if command_prefix is not None:argv=command_prefix(argv,phase,command,destination)
                observed=collect(argv,destination,workspace,phase,previous_calls=total,
                    phase_previous_calls=phase_calls,kill_marker=kill_marker,evidence_source=evidence_source)
                observed.update(command=command,control_kind=control_kind)
                attempts.append(observed);total+=observed["actual_model_calls"];phase_calls+=observed["actual_model_calls"]
                save(directory/"attempts.json",attempts)
                return observed

            state_args=["--state-db",str(state)]
            first_approval=True
            try:
                if phase=="C":
                    with sqlite3.connect(state) as connection:
                        revision=json.loads(connection.execute("SELECT state_json FROM workflow_states ORDER BY rowid DESC LIMIT 1").fetchone()[0])["requirements"]["revision"]
                    change=directory/"change.json"
                    hints={}
                    for name in ("validator.py","test_validator.py"):
                        original=(batch/"A/before"/name).read_text().splitlines()
                        current_text=(workspace/name).read_text().splitlines()
                        hints[name]=[[j1+1,j2] for tag,i1,i2,j1,j2 in difflib.SequenceMatcher(None,original,current_text).get_opcodes() if tag!="equal" and j2>j1]
                    save(directory/"change-line-hints.json",{"source":"A-before-and-current-file-diff","ranges":hints})
                    save(change,{"command_id":"change-"+uuid.uuid4().hex,"expected_requirement_revision":revision,
                        "separator":";","reason":"仅将parse_ports分隔符改为分号，保留重复项选择和原合同。A实际修改行范围："+json.dumps(hints,separators=(",",":"))+"。read受影响的连续片段，offset从所示范围起点开始，limit覆盖该片段且最多80行；可同批读取两文件，无需重读旧测试；使用最小连续edit片段，重新check并记录受影响步骤的当前来源。"})
                    changed=invoke("revise",[*state_args,"--change",str(change)])
                    if changed["exit_code"]!=0:raise RuntimeError("条件修订未被当前宿主接纳")
                    current=invoke("continue",[*state_args,"--max-turns",str(LIMITS[phase])])
                else:
                    current=invoke("new",["--workspace",str(workspace),"--question",str(batch/"inputs"/f"question-{phase.lower()}.md"),
                        "--runtime",str(batch/"runtime.json"),"--max-turns",str(LIMITS[phase]),"--max-actions","16",
                        "--max-tokens",str(NORMAL_MAX_TOKENS),"--context-tokens",str(CONTEXT_TOKENS),"--read-view-lines","80",
                        "--deadline-seconds","300","--command-timeout","10","--read-concurrency","2","--workflow",*state_args])
                while current["result"] and current["result"]["status"] in {"awaiting_question","awaiting_approval"}:
                    wait=current["result"]["wait"];record=wait_record(state,wait["wait_id"])
                    if current["result"]["status"]=="awaiting_question":
                        if phase=="C":raise RuntimeError("仅分隔符变化时再次要求重复项回答；保留未覆盖")
                        target=directory/"question-observations"/wait["wait_id"];target.mkdir(parents=True)
                        save(target/"wait.json",record)
                        answer=invoke("answer",[*state_args,"--question-id",wait["question_id"],"--expected-revision",
                            str(wait["requirement_revision"]),"--command-id","answer-"+uuid.uuid4().hex,"--value","unique" if phase=="B" else "keep"])
                        if answer["exit_code"]!=0:raise RuntimeError("答案未被原问题接纳")
                    else:
                        target=directory/"approval-observations"/wait["wait_id"];target.mkdir(parents=True)
                        save(target/"wait.json",record);snapshot(workspace,target/"workspace-before")
                        proposal=Path(wait["proposal_path"]).parent
                        for name in ("proposal.json","before.bin","after.bin","diff.patch"):
                            shutil.copyfile(proposal/name,target/name)
                        args=[*state_args,"--wait-id",wait["wait_id"],"--expected-revision",str(wait["requirement_revision"]),
                              "--command-id","approve-"+uuid.uuid4().hex,"--decision","approve"]
                        interrupt=first_approval and phase in {"A","B"}
                        marker=target/"after-decision-commit.json" if interrupt else None
                        decided=invoke("decide",args,kill_marker=marker)
                        if interrupt:
                            if not decided["signal"]:raise RuntimeError("决定提交后的SIGKILL没有命中")
                            export_database(state,target/"after-kill-state.json")
                            (target/"after-kill-session.jsonl").write_bytes(session.read_bytes())
                            snapshot(workspace,target/"workspace-after-kill")
                            decided=invoke("decide",args,control_kind="approval_replay")
                        if decided["exit_code"]!=0 or decided["result"]["status"]!="approved":raise RuntimeError("精确候选未被批准")
                        first_approval=False
                    current=invoke("continue",state_args)
                row={"phase":phase,"attempts":attempts,"actual_model_calls":phase_calls,
                    "status":current["result"]["status"] if current["result"] else "interrupted","semantic_verdict":"unevaluated"}
                rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
                if row["status"]!="stop" or any(a["supervisor_stop_reason"] for a in attempts):return batch,1
            finally:
                snapshot(workspace,directory/"after")
                if session.exists():(directory/"after-session.jsonl").write_bytes(session.read_bytes())
                if state.exists():export_database(state,directory/"state-export.json")
        return batch,0
    except BaseException as error:
        save(batch/"driver-error.json",{"class":type(error).__name__,"message":str(error),"completed_phases":[r["phase"] for r in rows]})
        raise
    finally:save(batch/"summary.json",rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-dir",type=Path,required=True);parser.add_argument("--runtime",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True);parser.add_argument("--python",default=sys.executable)
    args=parser.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("DEEPSEEK_MODEL"):
        parser.error("先在当前shell设置DEEPSEEK_API_KEY与DEEPSEEK_MODEL")
    student=args.student_dir.resolve(strict=True)
    for name in ("w09_io.py","w09_shell.py","runtime/worker.py","runtime/runner.py"):
        if (student/name).read_bytes()!=(MATERIALS/name).read_bytes():parser.error("先安装W09供给与固定worker")
    if (student/"supplies/w09/baseline-contract.json").read_bytes()!=(MATERIALS/"inputs/baseline-contract.json").read_bytes():
        parser.error("基线合同与W09供给不同")
    _,code=run_batch(student,args.runtime,args.output_dir,python=args.python)
    return code

if __name__=="__main__":raise SystemExit(main())
