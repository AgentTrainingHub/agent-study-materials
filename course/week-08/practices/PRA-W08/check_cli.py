"""W08 CLI 检查：受控 SSE、实际本机服务/数据库/子进程；零模型 API。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid

MATERIALS = Path(__file__).resolve().parent
W07 = MATERIALS.parents[2] / "week-07/practices/PRA-W07"
spec = importlib.util.spec_from_file_location("w08_previous_checks", W07 / "check_cli.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
call, response, read_jsonl = previous.call, previous.response, previous.read_jsonl
WORKER = previous.WORKER
HOOKS = r'''
import threading
import recovery
import w08_read
from w08_io import write_json
original_shell_fixture = run_check
def run_check(workspace,image,evidence_dir,**kwargs):
    result = original_shell_fixture(workspace,image,evidence_dir,**kwargs)
    result["cleanup_confirmed"] = True
    write_json(evidence_dir / "result.json",result)
    write_json(evidence_dir / "manifest.json",{"input_files":list(result["input_files"])})
    return result

def hold(stage, **fields):
    marker = settings.get("fault_marker")
    if settings.get("fault_stage") != stage or not marker:
        return
    write_json(Path(marker),{"stage":stage,"pid":os.getpid(),"monotonic":time.monotonic(),**fields})
    observe("fault_hit",stage=stage)
    while True:
        time.sleep(0.01)
for method,stage in (("save_intent","intent"),("save_result","result")):
    original = getattr(recovery.OperationStore,method)
    def wrapped(*args,_original=original,_stage=stage,**kwargs):
        value = _original(*args,**kwargs)
        hold(_stage,op_key=value["op_key"])
        return value
    setattr(recovery.OperationStore,method,wrapped)
if settings.get("journal_fault"):
    import journal
    method = settings["journal_fault"]
    original = getattr(journal.Journal,method)
    def journal_wrapped(*args,**kwargs):
        value = original(*args,**kwargs)
        hold("journal",method=method)
        return value
    setattr(journal.Journal,method,journal_wrapped)
if settings.get("read_controls"):
    original_init = w08_read.ReadWorker.__init__
    def read_init(worker, arguments, **kwargs):
        index = kwargs["identity"]["input_index"]
        controls = settings["read_controls"]
        kwargs["control"] = controls[index] if index < len(controls) else None
        return original_init(worker,arguments,**kwargs)
    w08_read.ReadWorker.__init__ = read_init
'''
HOOKS += r'''
if settings.get("lookup_fault"):
    from w08_io import ReceiverClient, SupportError
    original_lookup = ReceiverClient.lookup
    def lookup_fault(receiver, operation, **kwargs):
        if settings["lookup_fault"] == "timeout":
            observe("controlled_lookup_timeout")
            raise SupportError("controlled_timeout", "controlled lookup transport timeout")
        result = original_lookup(receiver,operation,**kwargs)
        observe("controlled_receipt_hash_changed",original=result)
        result["payload_hash"] = "0" * 64
        return result
    ReceiverClient.lookup = lookup_fault
'''
HOOKS += 'if settings.get("interrupt_ordinary_call"):\n    original_ordinary_append = append_line\n    def append_line(storage,data):\n        value = original_ordinary_append(storage,data)\n        row = json.loads(data)\n        message = row.get("message",{})\n        if message.get("role")=="assistant" and message.get("tool_calls"):\n            hold("ordinary_call",call_id=message["tool_calls"][0]["id"])\n        return value\n'
LATE_WRITER = "\nimport json, os\nfrom pathlib import Path\nimport sys, time\nentry, request_path, directory = map(Path,sys.argv[1:])\nsys.path.insert(0,str(entry.parent))\nimport recovery\nfrom w08_io import write_json, process_identity\nrequest=json.loads(request_path.read_text())\nwrite_json(directory/'late-ready.json',{'pid':os.getpid(),'process_identity':process_identity(os.getpid())})\nlimit=time.monotonic()+15\nwhile not (directory/'late-release').exists():\n    if time.monotonic()>limit:\n        raise RuntimeError('late writer release not reached')\n    time.sleep(0.01)\nwith recovery.OperationStore.open(request['state_db'],session_id=request['session_id'],workspace=request['workspace']) as store:\n    before=store.get(request['op_key'])\n    try:\n        store.save_result(request['op_key'],request['receipt'],request['epoch'],'apply')\n        outcome='accepted'\n    except Exception as error:\n        outcome=getattr(error,'code',type(error).__name__)\n    after=store.get(request['op_key'])\n    write_json(directory/'late-result.json',{'pid':os.getpid(),'outcome':outcome,\n        'old_epoch':request['epoch'],'current_epoch':store.connection.execute('SELECT epoch FROM owner').fetchone()[0],\n        'before_status':before['status'],'after_status':after['status'],'result_json':after['result_json']})\n"
HOOKS += "\nif settings.get('late_writer_directory'):\n    import subprocess\n    from w08_io import process_identity\n    original_late_save=recovery.OperationStore.save_result\n    def late_save(store,op_key,receipt,epoch,source):\n        directory=Path(settings['late_writer_directory'])\n        operation=store.get(op_key)\n        request={'state_db':str(store.connection.execute('PRAGMA database_list').fetchone()[2]),\n                 'session_id':store.session_id,'workspace':store.workspace,'op_key':op_key,\n                 'receipt':receipt,'epoch':epoch,'operation':operation}\n        write_json(directory/'late-request.json',request)\n        with (directory/'late-process.log').open('wb') as output:\n            child=subprocess.Popen([sys.executable,settings['late_writer_script'],str(entry),str(directory/'late-request.json'),str(directory)],\n                                   stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)\n        limit=time.monotonic()+5\n        while not (directory/'late-ready.json').exists():\n            if child.poll() is not None or time.monotonic()>limit:\n                raise AssertionError('late writer未就绪')\n            time.sleep(0.01)\n        hold('late_result',op_key=op_key,late_pid=child.pid,late_identity=process_identity(child.pid))\n        raise AssertionError('本次旧主执行者应被真实SIGKILL')\n    recovery.OperationStore.save_result=late_save\nif settings.get('release_late_writer'):\n    original_claim_epoch=recovery.OperationStore.claim\n    def claim_epoch(store,*args,**kwargs):\n        epoch=original_claim_epoch(store,*args,**kwargs)\n        directory=Path(settings['release_late_writer'])\n        (directory/'late-release').write_text('release\\n')\n        limit=time.monotonic()+5\n        while not (directory/'late-result.json').exists():\n            if time.monotonic()>limit:\n                raise AssertionError('旧writer未返回写入检查')\n            time.sleep(0.01)\n        observe('late_writer_after_new_claim',epoch=epoch,result=json.loads((directory/'late-result.json').read_text()))\n        return epoch\n    recovery.OperationStore.claim=claim_epoch\n    from w08_io import ReceiverClient, SupportError\n    original_fence_epoch=ReceiverClient.fence\n    def fence_epoch(receiver,resource,epoch,**options):\n        result=original_fence_epoch(receiver,resource,epoch,**options)\n        directory=Path(settings['release_late_writer'])\n        request=json.loads((directory/'late-request.json').read_text())\n        try:\n            receiver.apply(request['operation'],request['epoch'],**options)\n            observation={'outcome':'accepted','old_epoch':request['epoch'],'new_epoch':epoch}\n        except SupportError as error:\n            observation={'outcome':error.code,'old_epoch':request['epoch'],'new_epoch':epoch,'evidence':error.evidence}\n        write_json(directory/'late-service-result.json',observation)\n        observe('late_service_after_fence',**observation)\n        return result\n    ReceiverClient.fence=fence_epoch\n"
WRAPPER = previous.WRAPPER.replace('sys.argv = [str(entry), *settings["argv"]]',
                                   HOOKS + '\nsys.argv = [str(entry), *settings["argv"]]')


class CliChecks(previous.CliChecks):
    def setUp(self):
        super().setUp()
        for path in (MATERIALS / "inputs/sample_project").iterdir():
            shutil.copyfile(path, self.workspace / path.name)
        self.initial = self.workspace_bytes()
        self.services = []
        self.state_db = self.directory / "state.sqlite3"
        self.runtime = self.directory / "runtime.json"
        self.runtime.write_text(json.dumps({"image":"python@sha256:" + "1" * 64}) + "\n")

    def tearDown(self):
        for process, directory, streams in self.services:
            self.export_service(directory)
            (directory / "release-after-effect").touch()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in streams:
                stream.close()
            (directory / "controller-cleanup.json").write_text(json.dumps({"pid":process.pid,"returncode":process.returncode,"cleanup_confirmed":process.poll() is not None}) + "\n")
        super().tearDown()

    def service(self, *, hold=False):
        directory = self.directory / ("receiver-" + str(len(self.services)))
        directory.mkdir()
        streams = [(directory / name).open("wb") for name in ("stdout.bin","stderr.bin")]
        args = [sys.executable,str(MATERIALS / "receiver.py"),"--directory",str(directory)]
        if hold:
            args.append("--hold-after-effect")
        process = subprocess.Popen(args,stdout=streams[0],stderr=streams[1],env={"PATH":os.environ.get("PATH","")})
        self.services.append((process,directory,streams))
        manifest = directory / "receiver.json"
        limit = time.monotonic() + 5
        while not manifest.exists() and process.poll() is None and time.monotonic() < limit:
            time.sleep(0.01)
        self.assertTrue(manifest.is_file(), "真实本机服务未启动")
        self.receiver_manifest = manifest
        return directory

    def export_service(self,directory):
        manifest = directory / "receiver.json"
        if not manifest.exists():
            return {}
        value=json.loads(manifest.read_text())
        with sqlite3.connect(value["database"]) as db:
            db.row_factory=sqlite3.Row
            result={name:[dict(row) for row in db.execute(f"SELECT * FROM {name}")] for name in ("effects","requests")}
        (directory/"controller-export.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
        return result

    def state(self):
        with sqlite3.connect(self.state_db) as db:
            db.row_factory=sqlite3.Row
            return {name:[dict(row) for row in db.execute(f"SELECT * FROM {name}")] for name in ("operations","owner","budget_groups","request_reservations")}

    def invoke(self,command,raws=(),*,question="读取并解释当前文件，不要修改。",session=None,extra=(),approval="approve",no_config=False,**settings):
        directory=self.directory/f"cli-{len(self.invocations)+1:02d}-{command}"
        directory.mkdir()
        question_path=directory/"question.txt"
        question_path.write_text(question)
        argv=[command,"--session",str(session or self.session)]
        if command in {"new","resume"}:
            argv += ["--workspace",str(self.workspace),"--question",str(question_path),"--output-dir",str(directory/"runs"),
                     "--runtime",str(self.runtime),"--max-turns",str(settings.pop("max_turns",8)),
                     "--deadline-seconds",str(settings.pop("deadline_seconds",90))]
        if settings.pop("with_receipt",False) or command=="recover":
            argv += ["--state-db",str(self.state_db),"--receiver-manifest",str(self.receiver_manifest)]
        if command=="recover":
            argv += ["--output-dir",str(directory/"runs")]
        argv += list(extra)
        control=settings.pop("control",None)
        kill_marker=settings.pop("kill_marker",None)
        settings["argv"]=argv
        for name,contents in (("worker.py",WORKER),("wrapper.py",WRAPPER)):
            (directory/name).write_text(contents)
        scripts=[item if "sse" in item else {"sse":previous.previous.previous.frames(item)} for item in raws]
        (directory/"scenario.json").write_text(json.dumps(scripts,ensure_ascii=False))
        (directory/"options.json").write_text(json.dumps(settings))
        env={"PATH":os.environ.get("PATH",""),"PYTHONDONTWRITEBYTECODE":"1"}
        if not no_config:
            env.update(DEEPSEEK_API_KEY="w04-controlled-secret-do-not-log",DEEPSEEK_MODEL="controlled-request-model",DEEPSEEK_TIMEOUT_SECONDS="2")
        audit,operations=directory/"transport.jsonl",directory/"operations.jsonl"
        process=subprocess.Popen([sys.executable,str(directory/"wrapper.py"),str(self.entry_point),str(directory/"worker.py"),
            str(directory/"scenario.json"),str(audit),str(operations),str(directory/"options.json")],cwd=directory,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        out,err,pending=[],[],queue.Queue()
        def reader(stream,target,notify=False):
            for line in stream:
                target.append(line)
                if notify: pending.put(line)
        threads=[threading.Thread(target=reader,args=(process.stdout,out),daemon=True),threading.Thread(target=reader,args=(process.stderr,err,True),daemon=True)]
        for thread in threads:thread.start()
        approvals=[]
        start=time.monotonic()
        killed=False
        try:
            while process.poll() is None or not pending.empty():
                self.assertLess(time.monotonic()-start,25,"受控 CLI 超过监督上限")
                if control is not None:
                    control(process,directory)
                if kill_marker is not None and Path(kill_marker).exists() and not killed and process.poll() is None:
                    marker=json.loads(Path(kill_marker).read_text())
                    (directory/"controller-signal.json").write_text(json.dumps({"signal":"SIGKILL","target_pid":process.pid,"marker":marker,"monotonic":time.monotonic()})+"\n")
                    process.kill()
                    killed=True
                try:line=pending.get(timeout=0.02)
                except queue.Empty:continue
                if not (line.startswith("W03_APPROVAL ") or line.startswith("W08_APPROVAL ")):continue
                prompt=json.loads(line.split(" ",1)[1])
                proposal_dir=Path(prompt["proposal_dir"])
                proposal=json.loads((proposal_dir/"proposal.json").read_text())
                observed=directory/"driver-observations"/prompt["proposal_id"]
                shutil.copytree(proposal_dir,observed)
                decision=f"{approval} {prompt['proposal_id']}"
                approvals.append({"prompt":prompt,"decision":decision,"proposal":proposal})
                if process.poll() is None:
                    process.stdin.write(decision+"\n")
                    process.stdin.flush()
            process.wait(timeout=2)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)
            for thread in threads:thread.join(timeout=2)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        stdout,stderr="".join(out),"".join(err)
        (directory/"stdout.txt").write_text(stdout)
        (directory/"stderr.txt").write_text(stderr)
        evidence={"pid":process.pid,"exit_code":process.returncode,"argv":argv,"approvals":approvals,
                  "requests":read_jsonl(audit),"operations":read_jsonl(operations),"killed_after_marker":killed}
        (directory/"driver.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
        self.invocations.append(directory)
        self.assertNotIn("w04-controlled-secret-do-not-log",stdout+stderr+json.dumps(evidence))
        if killed:
            self.assertEqual(process.returncode,-signal.SIGKILL)
            result=None
            # A hard-killed parent cannot reap its HTTP child. The controller
            # verifies the recorded process identity before cleaning this child.
            cleanup_records=[]
            for spawn in directory.rglob("worker.json"):
                metadata=json.loads(spawn.read_text())
                pid=metadata.get("pid")
                if pid:
                    def identity_now():
                        return subprocess.run(["ps","-p",str(pid),"-o","lstart=","-o","command="],capture_output=True,text=True).stdout.strip()
                    actions=[]
                    identity=identity_now()
                    if identity and identity==metadata["process_identity"]:
                        os.kill(pid,signal.SIGTERM)
                        actions.append("terminate")
                        until=time.monotonic()+1
                        while identity_now()==identity and time.monotonic()<until:time.sleep(0.02)
                        if identity_now()==identity:
                            os.kill(pid,signal.SIGKILL)
                            actions.append("kill")
                            until=time.monotonic()+1
                            while identity_now()==identity and time.monotonic()<until:time.sleep(0.02)
                    confirmed=identity_now()!=metadata["process_identity"]
                    cleanup_records.append({**metadata,"actions":actions,"cleanup_confirmed":confirmed})
                    self.assertTrue(confirmed,"SIGKILL 后遗留的自有 HTTP worker 仍存活")
            (directory/"controller-worker-cleanup.json").write_text(json.dumps(cleanup_records,indent=2)+"\n")
        else:
            try:result=json.loads(stdout)
            except ValueError:self.fail(f"stdout 不是一个 JSON：{stdout!r}；stderr={stderr[-2000:]!r}")
            self.assertEqual(result.get("exit_code"),process.returncode)
        return {**evidence,"result":result,"directory":directory}

    def calls(self,count=2):
        return [call(f"read-{i}","read",{"path":("validator.py" if i%2==0 else "test_validator.py"),"offset":1,"limit":2}) for i in range(count)]

    def read_evidence(self,observed):
        directory=Path(observed["result"]["output_directory"])
        cleanups=[json.loads(path.read_text()) for path in directory.rglob("read-*/cleanup.json")]
        for item in cleanups:
            self.assertIs(item["cleanup_confirmed"],True,item)
            with self.assertRaises(ProcessLookupError):os.kill(item["pid"],0)
        return directory,cleanups,read_jsonl(directory/"events.jsonl")

    def gates(self,count,*,ignore=False):
        return [{"gate":str(self.directory/f"gate-{i}.open"),"ignore_terminate":ignore} for i in range(count)]

    def released(self,controls,index):
        Path(controls[index]["gate"]).touch()

    def hit(self,controls,index):
        return Path(controls[index]["gate"]).with_suffix(".hit.json").exists()

    def test_parallel_identity_order(self):
        controls=self.gates(2)
        stages=[]
        def control(process,directory):
            if all(self.hit(controls,i) for i in (0,1)) and not stages:
                self.released(controls,1);stages.append("second")
            if stages==["second"] and list(directory.rglob("read-01/cleanup.json")):
                self.released(controls,0);stages.append("first")
        observed=self.invoke("new",[response(calls=self.calls()),response("读取完成。")],read_controls=controls,control=control)
        self.assert_ok(observed)
        root,cleanups,events=self.read_evidence(observed)
        self.assertEqual(stages,["second","first"])
        finished=[row["call_id"] for row in events if row["event"]=="tool_finished"]
        self.assertEqual(finished,["read-1","read-0"])
        tools=[m for m in observed["requests"][1]["request"]["messages"] if m["role"]=="tool"]
        self.assertEqual([m["tool_call_id"] for m in tools],["read-0","read-1"])
        records=[]
        for path in sorted(root.rglob("read-*/result.json")):
            record=json.loads(path.read_text());records.append(record)
            raw=(path.parent/"input.bin").read_bytes()
            self.assertEqual(record["result"]["version"],hashlib.sha256(raw).hexdigest())
            tool=next(m for m in tools if m["tool_call_id"]==record["identity"]["call_id"])
            self.assertEqual(json.loads(tool["content"])["lines"],record["result"]["lines"])
        self.assertLess(max(r["started"] for r in records),min(r["finished"] for r in records))
        self.facts={"real_intervals_overlap":True,"finished_order":finished,"journal_order":["read-0","read-1"],"workers_reaped":len(cleanups)}

    def test_queue_limit_one_two(self):
        facts=[]
        for concurrency in (1,2):
            with self.subTest(concurrency=concurrency):
                self.session=self.directory/f"session-{concurrency}.jsonl"
                controls=[{"gate":str(self.directory/f"queue-{concurrency}-{i}.open")} for i in range(3)]
                released=set()
                def control(process,directory):
                    hits=[i for i in range(3) if self.hit(controls,i)]
                    if len(hits)>=concurrency and not released:
                        self.assertEqual(hits,list(range(concurrency)))
                        self.released(controls,0);released.add(0)
                    if self.hit(controls,2):
                        for i in range(3):self.released(controls,i)
                        released.update(range(3))
                    if concurrency==1 and self.hit(controls,1):self.released(controls,1);released.add(1)
                value=self.invoke("new",[response(calls=self.calls(3)),response()],extra=["--read-concurrency",str(concurrency)],read_controls=controls,control=control)
                self.assert_ok(value)
                _,cleanups,events=self.read_evidence(value)
                starts=[r for r in events if r["event"]=="tool_started"]
                self.assertEqual([r["call_id"] for r in starts],["read-0","read-1","read-2"])
                self.assertLessEqual(max(r["active_count"] for r in starts),concurrency)
                self.assertEqual(len(cleanups),3)
                facts.append({"concurrency":concurrency,"max_observed_active":max(r["active_count"] for r in starts)})
        self.facts={"configurations":facts}

    def test_group_failure_cleanup(self):
        controls=self.gates(3,ignore=True)
        calls=self.calls(3)
        calls[0]=call("read-0","read",{"path":"validator.py","offset":999,"limit":1})
        def control(process,directory):
            if all(self.hit(controls,i) for i in (0,1)):self.released(controls,0)
        value=self.invoke("new",[response(calls=calls)],read_controls=controls,control=control)
        self.assertEqual(value["exit_code"],1)
        root,cleanups,events=self.read_evidence(value)
        self.assertEqual(len(value["requests"]),1)
        self.assertEqual(len(cleanups),2)
        self.assertFalse(self.hit(controls,2))
        self.assertTrue(any("kill" in row["actions"] for row in cleanups))
        tools=[r["message"] for r in read_jsonl(self.session) if r.get("message",{}).get("role")=="tool"]
        self.assertEqual([m["tool_call_id"] for m in tools],["read-0","read-1","read-2"])
        self.assertFalse(any(json.loads(m["content"]).get("ok") for m in tools))
        self.facts={"workers_started":2,"queued_never_started":True,"paired_results":3,"subsequent_requests":0}

    def test_queue_deadline(self):
        controls=self.gates(3,ignore=True)
        value=self.invoke("new",[response(calls=self.calls(3))],read_controls=controls,deadline_seconds=1)
        self.assertEqual(value["exit_code"],1)
        _,cleanups,events=self.read_evidence(value)
        self.assertEqual(len(cleanups),2)
        self.assertFalse(self.hit(controls,2))
        self.assertEqual(len(value["requests"]),1)
        self.facts={"shared_deadline":True,"queued_never_started":True,"workers_reaped":2}

    def test_real_sigint_group(self):
        controls=self.gates(2,ignore=True)
        sent=[]
        def control(process,directory):
            if all(self.hit(controls,i) for i in (0,1)) and not sent:
                os.kill(process.pid,signal.SIGINT);sent.append({"pid":process.pid,"signal":"SIGINT","monotonic":time.monotonic()})
                (directory/"controller-signal.json").write_text(json.dumps(sent[0])+"\n")
        value=self.invoke("new",[response(calls=self.calls())],read_controls=controls,control=control)
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"cancelled")
        _,cleanups,events=self.read_evidence(value)
        self.assertEqual(len(cleanups),2)
        self.assertTrue(all("kill" in row["actions"] for row in cleanups))
        self.assertEqual(len(value["requests"]),1)
        self.facts={"real_signal":sent[0],"workers_reaped":2,"kill_wait_used":True}

    def workflow(self):
        return [response(calls=[call("checked","shell",{"command":"check"})]),
                response(calls=[call("submit","submit_result",{"text":"受控本机回执：检查结果已核对。"})]),response("已取得本机回执。")]

    def test_normal_receipt(self):
        directory=self.service()
        observed=self.invoke("new",self.workflow(),with_receipt=True)
        self.assert_ok(observed)
        exported=self.export_service(directory)
        self.assertEqual(len(exported["effects"]),1)
        state=self.state()
        self.assertEqual(len(state["operations"]),1)
        self.assertEqual(state["operations"][0]["status"],"journaled")
        self.assertEqual(len(observed["approvals"]),1)
        feedback=next(m for m in observed["requests"][2]["request"]["messages"] if m.get("tool_call_id")=="submit")
        result=json.loads(feedback["content"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["op_key"],state["operations"][0]["op_key"])
        self.facts={"effects":1,"model_requests":3,"approved_submission":1,"service_requests":len(exported["requests"])}

    def crash(self,stage,*,max_turns=8,raws=None):
        directory=self.service(hold=stage=="effect")
        marker=directory/"after-effect-commit.json" if stage=="effect" else self.directory/f"{stage}-{uuid.uuid4().hex[:8]}-hit.json"
        value=self.invoke("new",(raws or self.workflow())[:2],with_receipt=True,max_turns=max_turns,
            fault_stage=stage,fault_marker=str(marker),kill_marker=marker)
        self.assertTrue(value["killed_after_marker"])
        self.assertEqual(len(value["requests"]),2)
        prefix=self.session.read_bytes()
        if stage=="effect":(directory/"release-after-effect").touch()
        return directory,value,prefix

    def check_recovered(self,stage):
        directory,old,prefix=self.crash(stage)
        before=self.export_service(directory)
        value=self.invoke("recover",[response("查证回执后继续完成。")])
        self.assert_ok(value)
        after=self.export_service(directory)
        self.assertEqual(len(after["effects"]),1)
        self.assertTrue(self.session.read_bytes().startswith(prefix))
        rows=read_jsonl(self.session)
        ends=[r for r in rows if r["type"]=="run_end"]
        self.assertEqual([r["status"] for r in ends],["interrupted","stop"])
        self.assertNotEqual(ends[0]["run_id"],ends[1]["run_id"])
        self.assertEqual(len([r for r in rows if r["type"]=="recovery"]),1)
        self.assertEqual(len([r for r in rows if r["type"]=="run_continue"]),1)
        tools=[r for r in rows if r.get("message",{}).get("tool_call_id")=="submit"]
        self.assertEqual(len(tools),1)
        self.assertEqual(value["requests"][0]["request"]["messages"][-1],tools[0]["message"])
        self.assertEqual(len(value["approvals"]),1 if stage=="intent" else 0)
        apply_before=sum(row["route"]=="/apply" for row in before["requests"])
        apply_after=sum(row["route"]=="/apply" for row in after["requests"])
        self.assertEqual(apply_after-apply_before,1 if stage=="intent" else 0)
        self.facts={"kill_stage":stage,"effects":1,"recovery_apply_count":apply_after-apply_before,"original_prefix_preserved":True,"actual_new_cli_model_request":1}

    def test_kill_after_intent(self):self.check_recovered("intent")
    def test_kill_after_effect(self):self.check_recovered("effect")
    def test_kill_after_result(self):self.check_recovered("result")

    def test_absent_declined(self):
        directory,old,prefix=self.crash("intent")
        value=self.invoke("recover",[],approval="deny")
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"declined")
        self.assertEqual(value["requests"],[])
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertEqual(self.state()["operations"][0]["status"],"intent")
        self.assertEqual(len(self.export_service(directory)["effects"]),0)
        self.facts={"new_approval_declined":True,"effects":0,"model_requests":0,"intent_preserved":True}

    def test_budget_survives_restart(self):
        raws=self.workflow()
        raws[0].pop("usage")
        directory,old,prefix=self.crash("effect",max_turns=3,raws=raws)
        value=self.invoke("recover",[response(calls=[call("over-budget-read")])])
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"turn_limit")
        self.assertEqual(len(value["requests"]),1)
        state=self.state()
        self.assertEqual(len(state["request_reservations"]),3)
        self.assertEqual([row["request_index"] for row in state["request_reservations"]],[1,2,3])
        self.assertFalse(any(row["operation"]=="read" for row in value["operations"]))
        self.assertIn("unknown",json.dumps(value["result"]["usage"]).lower())
        self.assertEqual(len(self.export_service(directory)["effects"]),1)
        self.facts={"old_requests_retained":2,"new_requests":1,"logical_limit":3,"usage_unknown_retained":True,"effects":1}

    def test_checked_files_changed(self):
        directory,old,prefix=self.crash("intent")
        path=self.workspace/"validator.py"
        path.write_text(path.read_text()+"# changed after approval\n")
        value=self.invoke("recover",[])
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["requests"],[])
        self.assertEqual(value["approvals"],[])
        self.assertEqual(len(self.export_service(directory)["effects"]),0)
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertIn("checked_files_changed",json.dumps(value["result"]))
        self.facts={"old_approval_did_not_bypass_current_file_check":True,"effects":0}

    def test_mixed_batch_serial(self):
        change=call("edit-type","edit",{"path":"validator.py","old_text":"if not text or","new_text":"if not isinstance(text, str) or not text or"})
        calls=[self.calls()[0],change,self.calls()[1]]
        value=self.invoke("new",[response(calls=calls),response("串行修改完成。")])
        self.assert_ok(value)
        self.assertEqual([r["operation"] for r in value["operations"] if r["operation"] in {"read","commit_enter","commit_returned"}],["read","commit_enter","commit_returned","read"])
        self.assertFalse(list(value["directory"].rglob("read-*/spawn.json")))
        self.session=self.directory/"mixed-submit.jsonl"
        self.service()
        rejected=self.invoke("new",[response(calls=[call("submit","submit_result",{"text":"mixed"}),self.calls()[0]])],with_receipt=True)
        self.assertEqual(rejected["exit_code"],1)
        self.assertEqual(rejected["approvals"],[])
        self.assertFalse(self.state()["operations"])
        self.assertFalse(any(row["operation"]=="read" for row in rejected["operations"]))
        self.facts={"mixed_read_edit_serial":True,"mixed_submission_whole_batch_rejected":True}


    def test_journal_gap_idempotence(self):
        gaps=("append_reconciled_tool","close_interrupted","append_recovery")
        facts=[]
        for method in gaps:
            with self.subTest(gap=method):
                self.session=self.directory/(method+".jsonl")
                self.state_db=self.directory/(method+".sqlite3")
                directory,old,prefix=self.crash("result")
                marker=self.directory/(method+"-hit.json")
                middle=self.invoke("recover",[],journal_fault=method,fault_stage="journal",fault_marker=str(marker),kill_marker=marker)
                self.assertTrue(middle["killed_after_marker"])
                before=self.session.read_bytes()
                final=self.invoke("recover",[response("续接完成。")])
                self.assert_ok(final)
                self.assertTrue(self.session.read_bytes().startswith(before))
                rows=read_jsonl(self.session)
                self.assertEqual(sum(r.get("message",{}).get("tool_call_id")=="submit" for r in rows),1)
                self.assertEqual(sum(r["type"]=="recovery" for r in rows),1)
                self.assertEqual(sum(r["type"]=="run_end" and r["status"]=="interrupted" for r in rows),1)
                exported=self.export_service(directory)
                self.assertEqual(len(exported["effects"]),1)
                self.assertEqual(sum(r["route"]=="/apply" for r in exported["requests"]),1)
                facts.append({"gap":method,"original_records_preserved":True,"effects":1,"tool_count":1})
        self.facts={"gaps":facts}

    def test_unknown_query_stops(self):
        directory,old,prefix=self.crash("intent")
        value=self.invoke("recover",[],lookup_fault="timeout")
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"effect_unknown")
        self.assertEqual(value["requests"],[])
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertEqual(self.state()["operations"][0]["status"],"effect_unknown")
        before=self.export_service(directory)
        again=self.invoke("recover",[])
        self.assertEqual(again["exit_code"],1)
        self.assertEqual(again["requests"],[])
        after=self.export_service(directory)
        self.assertEqual(before,after)
        self.assertEqual(after["effects"],[])
        self.facts={"query_failure":"controlled-timeout","unknown_persisted":True,"repeat_zero_http":True,"effects":0}

    def test_receipt_mismatch_stops(self):
        directory,old,prefix=self.crash("effect")
        value=self.invoke("recover",[],lookup_fault="wrong_hash")
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"effect_unknown")
        self.assertEqual(value["requests"],[])
        self.assertEqual(self.session.read_bytes(),prefix)
        exported=self.export_service(directory)
        self.assertEqual(len(exported["effects"]),1)
        self.assertEqual(sum(r["route"]=="/apply" for r in exported["requests"]),1)
        self.assertEqual(self.state()["operations"][0]["status"],"effect_unknown")
        self.facts={"actual_lookup_then_controlled_hash_corruption":True,"extra_apply":0,"model_requests":0}

    def test_receiver_changed_stops(self):
        directory,old,prefix=self.crash("intent")
        original=self.receiver_manifest
        original_bytes=original.read_bytes()
        other=self.service()
        self.receiver_manifest=original
        changed=json.loads((other/"receiver.json").read_text())
        self.receiver_manifest.write_text(json.dumps(changed)+"\n")
        before=self.state()
        value=self.invoke("recover",[])
        self.assertEqual(value["exit_code"],1)
        self.assertEqual(value["result"]["status"],"effect_unknown")
        self.assertEqual(value["requests"],[])
        after=self.state()
        self.assertEqual(after["operations"][0]["status"],"effect_unknown")
        self.assertEqual(after["operations"][0]["op_key"],before["operations"][0]["op_key"])
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertEqual(self.export_service(other)["requests"],[])
        original.write_bytes(original_bytes)
        self.facts={"replacement_receiver_has_distinct_id":True,"new_empty_db_not_used_as_absent":True}

    def test_permission_removed(self):
        directory,old,prefix=self.crash("intent")
        before=self.state()
        value=self.invoke("recover",[],extra=["--disable-submit"])
        self.assertEqual(value["exit_code"],2)
        self.assertIn("permission_removed",json.dumps(value["result"]))
        self.assertEqual(value["requests"],[])
        self.assertEqual(self.state(),before)
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertEqual(self.export_service(directory)["requests"],[])
        self.facts={"current_permission_removed":True,"old_approval_not_reused":True,"http_requests":0}

    def test_legacy_incomplete_stays_blocked(self):
        directory=self.service()
        for name,args in (("edit",{"path":"validator.py","old_text":"if not text or","new_text":"if isinstance(text,str) and not text or"}),
                          ("shell",{"command":"check"}),
                          ("mcp__rules__lookup",{"document_id":"port-format/r1"})):
            with self.subTest(ordinary_tool=name):
                self.session=self.directory/(name+".jsonl")
                self.state_db=self.directory/(name+".sqlite3")
                marker=self.directory/(name+"-hit.json")
                initial=self.invoke("new",[response(calls=[call("ordinary",name,args)])],with_receipt=True,
                    interrupt_ordinary_call=True,fault_stage="ordinary_call",fault_marker=str(marker),kill_marker=marker)
                self.assertTrue(initial["killed_after_marker"])
                prefix=self.session.read_bytes()
                self.assertEqual(self.state()["operations"],[])
                value=self.invoke("recover",[])
                self.assertEqual(value["exit_code"],2)
                self.assertEqual(value["requests"],[])
                self.assertEqual(self.session.read_bytes(),prefix)
                self.assertEqual(self.export_service(directory)["requests"],[])
        self.session=self.directory/"consumed.jsonl"
        self.state_db=self.directory/"consumed.sqlite3"
        directory,old,prefix=self.crash("result")
        marker=self.directory/"continuation-hit.json"
        middle=self.invoke("recover",[],journal_fault="begin_continuation",fault_stage="journal",fault_marker=str(marker),kill_marker=marker)
        self.assertTrue(middle["killed_after_marker"])
        before=self.session.read_bytes()
        http_before=self.export_service(directory)
        final=self.invoke("recover",[])
        self.assertEqual(final["exit_code"],2)
        self.assertIn("continuation_interrupted",json.dumps(final["result"]))
        self.assertEqual(final["requests"],[])
        self.assertEqual(self.session.read_bytes(),before)
        self.assertEqual(self.export_service(directory),http_before)
        self.facts={"ordinary_incomplete_tools":["edit","shell","mcp__rules__lookup"],"no_intent_rejected":True,"consumed_recovery_rejected":True,"extra_http_or_model":0}

    def test_incompatible_state(self):
        directory,old,prefix=self.crash("intent")
        with sqlite3.connect(self.state_db) as db:db.execute("UPDATE metadata SET schema_version=999")
        before=self.export_service(directory)
        value=self.invoke("recover",[])
        self.assertEqual(value["exit_code"],2)
        self.assertEqual(value["requests"],[])
        self.assertEqual(self.session.read_bytes(),prefix)
        self.assertEqual(self.export_service(directory),before)
        self.facts={"unknown_schema_refused_before_external_action":True}

    def prepare_read(self,text=previous.previous.previous.previous.A_TEXT):
        value=self.invoke("new",[response(calls=[call()]),response(text)])
        self.assert_ok(value)
        tools=[m for m in value["requests"][1]["request"]["messages"] if m.get("tool_call_id")=="call-1"]
        self.assertEqual(len(tools),1)
        result=json.loads(tools[0]["content"])
        expected=self.initial["validator.py"].decode().replace("\r\n","\n").replace("\r","\n").split("\n")
        if expected[-1:]==[""]:expected.pop()
        self.assertEqual(result["lines"],[{"line":i+1,"text":line} for i,line in enumerate(expected[:40])])
        return value

    def test_regression_diff_and_approval(self):
        mutation=call("edit","edit",{"path":"validator.py","old_text":"if not text or","new_text":"if not isinstance(text, str) or not text or"})
        denied=self.invoke("new",[response(calls=[mutation])],approval="deny")
        self.assertEqual(denied["exit_code"],1)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.session=self.directory/"approved.jsonl"
        value=self.invoke("new",[response(calls=[mutation]),response("修改已批准。")])
        self.assert_ok(value)
        self.assertEqual(len(value["approvals"]),1)
        snapshot=next((value["directory"]/"driver-observations").iterdir())
        self.assertEqual((snapshot/"after.bin").read_bytes(),(self.workspace/"validator.py").read_bytes())
        self.assertIn(b"isinstance",(snapshot/"diff.patch").read_bytes())
        self.facts={"denied_no_diff":True,"approved_candidate_equals_actual_file":True}

    def test_regression_resume_branch(self):
        self.test_resume_new_process()
        first=next(r["entry_id"] for r in read_jsonl(self.session) if r["type"]=="run_end" and r["status"]=="stop")
        branched=self.invoke("branch",extra=["--from-entry",first],no_config=True)
        self.assert_ok(branched)
        self.facts["normal_completed_branch"]=True

    def test_regression_bad_pairing(self):self.test_pairing_incomplete_rejected()
    def test_regression_truncated_stream(self):self.test_stream_error_no_execution()
    def test_regression_cancel_stream(self):self.test_cancel_model_sigint()
    def test_regression_summary_budget(self):self.test_summary_committed_before_continuation()
    def test_regression_skill_permission(self):self.test_skill_resource_requires_body_in_issuing_request()
    def test_regression_mcp_binding(self):
        self.assertIsNotNone(self.sdk_python,"此回归需要 --sdk-python 指向 W07 固定 SDK 环境")
        self.test_mcp_double_service_binding()
        self.session=self.directory/"mcp-error.jsonl"
        (self.directory/"mcp.json").unlink()
        self.test_mcp_business_error_is_paired()


    def test_epoch_old_writer_cannot_save(self):
        service_directory=self.service()
        late=self.directory/'late-writer'
        late.mkdir()
        script=late/'late_writer.py'
        script.write_text(LATE_WRITER)
        marker=self.directory/'late-hit.json'
        identity=None
        try:
            old=self.invoke('new',self.workflow()[:2],with_receipt=True,
                late_writer_directory=str(late),late_writer_script=str(script),
                fault_stage='late_result',fault_marker=str(marker),kill_marker=marker)
            self.assertTrue(old['killed_after_marker'])
            identity=json.loads((late/'late-ready.json').read_text())
            self.assertEqual(self.state()['operations'][0]['status'],'intent')
            self.assertEqual(len(self.export_service(service_directory)['effects']),1)
            recovered=self.invoke('recover',[response('新执行代查证后继续。')],release_late_writer=str(late))
            self.assert_ok(recovered)
            observed=json.loads((late/'late-result.json').read_text())
            self.assertEqual(observed['old_epoch'],1)
            self.assertEqual(observed['current_epoch'],2)
            self.assertEqual(observed['outcome'],'stale_epoch','旧执行代实际保存结果未被epoch拒绝')
            self.assertEqual(observed['before_status'],'intent')
            self.assertEqual(observed['after_status'],'intent','旧执行代越过epoch门禁实际写入result_saved')
            self.assertIsNone(observed['result_json'])
            remote=json.loads((late/'late-service-result.json').read_text())
            self.assertEqual(remote['outcome'],'stale_epoch','接收端新fence后仍接受旧epoch写入')
            self.assertIs(remote['evidence']['sent'],True)
            self.assertIs(remote['evidence']['cleanup']['cleanup_confirmed'],True)
            responses=[item for item in remote['evidence']['messages'] if item.get('stage')=='response']
            self.assertEqual(responses[0]['http_status'],409)
            self.assertEqual(self.state()['operations'][0]['status'],'journaled')
            self.assertEqual(len(self.export_service(service_directory)['effects']),1)
            self.facts={'real_old_main_sigkill':True,'old_writer_pid':identity['pid'],
                        'new_epoch':2,'stale_write_rejected':True,'receiver_old_epoch_http_status':409,'actual_new_cli_request':1}
        finally:
            (late/'late-release').touch()
            if identity is None and (late/'late-ready.json').exists():
                identity=json.loads((late/'late-ready.json').read_text())
            if identity is not None:
                def actual_identity():
                    return subprocess.run(['ps','-p',str(identity['pid']),'-o','lstart=','-o','command='],capture_output=True,text=True).stdout.strip()
                limit=time.monotonic()+2
                while actual_identity()==identity['process_identity'] and time.monotonic()<limit:
                    time.sleep(0.02)
                actions=[]
                if actual_identity()==identity['process_identity']:
                    os.kill(identity['pid'],signal.SIGTERM)
                    actions.append('terminate')
                    limit=time.monotonic()+1
                    while actual_identity()==identity['process_identity'] and time.monotonic()<limit:
                        time.sleep(0.02)
                if actual_identity()==identity['process_identity']:
                    os.kill(identity['pid'],signal.SIGKILL)
                    actions.append('kill')
                    limit=time.monotonic()+1
                    while actual_identity()==identity['process_identity'] and time.monotonic()<limit:
                        time.sleep(0.02)
                confirmed=actual_identity()!=identity['process_identity']
                (late/'controller-cleanup.json').write_text(json.dumps({'pid':identity['pid'],'process_identity':identity['process_identity'],
                    'actions':actions,'cleanup_confirmed':confirmed})+'\n')
                self.assertTrue(confirmed,'派生旧writer仍存活')


TESTS = ["test_normal_receipt","test_kill_after_intent","test_kill_after_effect","test_kill_after_result",
         "test_absent_declined","test_budget_survives_restart","test_checked_files_changed",
         "test_parallel_identity_order","test_queue_limit_one_two","test_group_failure_cleanup",
         "test_queue_deadline","test_real_sigint_group","test_mixed_batch_serial",
         "test_epoch_old_writer_cannot_save","test_journal_gap_idempotence","test_unknown_query_stops","test_receipt_mismatch_stops",
         "test_receiver_changed_stops","test_permission_removed","test_legacy_incomplete_stays_blocked","test_incompatible_state",
         "test_regression_diff_and_approval","test_regression_resume_branch","test_regression_bad_pairing",
         "test_regression_truncated_stream","test_regression_cancel_stream","test_regression_summary_budget",
         "test_regression_skill_permission","test_regression_mcp_binding"]


def run_suite(entry,sdk_python,output_root,names=None):
    entry,output_root=entry.resolve(),output_root.absolute()
    output_root.mkdir(parents=True,exist_ok=False)
    class Bound(CliChecks):pass
    Bound.entry_point,Bound.sdk_python,Bound.output_root=entry,sdk_python.absolute() if sdk_python else None,output_root
    names=names or TESTS
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(name) for name in names))
    failed=len({case.id() for case,_ in result.failures+result.errors})
    report={"evidence_source":"controlled-local","external_model_requests":0,"docker":"controlled-boundary-no-container",
            "receiver":"real-loopback-http-sqlite","entry":str(entry),"selected_cases":names,"count":result.testsRun,
            "passed":result.testsRun-failed,"failed":failed,"failures":len(result.failures),"errors":len(result.errors),
            "verdict":"pass" if result.wasSuccessful() else "fail",
            "failure_details":[{"case":str(case),"traceback":error} for case,error in result.failures+result.errors]}
    (output_root/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--entry",type=Path)
    target.add_argument("--student-dir",type=Path)
    parser.add_argument("--sdk-python",type=Path)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--case",choices=TESTS)
    args=parser.parse_args()
    report=run_suite(args.entry or args.student_dir/"main.py",args.sdk_python,args.output_dir/("check-"+uuid.uuid4().hex[:8]),[args.case] if args.case else TESTS)
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report["verdict"]=="pass" else 1

if __name__=="__main__":raise SystemExit(main())
