"""独立核对 W08 请求、恢复父链、两份数据库、批准与实际运行证据。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

MATERIALS=Path(__file__).resolve().parent
LIMITS={"A":8,"B":10,"C":2}
FILES={"validator.py","test_validator.py"}


def require(condition,message):
    if not condition:raise AssertionError(message)


def read(path):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,"JSON重复字段");result[key]=value
        return result
    return json.loads(path.read_text(),object_pairs_hook=pairs)


def lines(path):
    raw=path.read_bytes();require(not raw or raw.endswith(b"\n"),"JSONL尾部残缺")
    return [json.loads(line) for line in raw.split(b"\n") if line]


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_previous():
    path=MATERIALS.parents[2]/"week-06/practices/PRA-W06/inspect_evidence.py"
    spec=importlib.util.spec_from_file_location("w08_previous_inspector",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def database(path):
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True) as db:
        db.row_factory=sqlite3.Row
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:[dict(r) for r in db.execute('SELECT * FROM "'+name+'"')] for name in names}


def snapshot(path):
    names=read(path/"files.json")
    require(set(names)==FILES and len(names)==2,"工作区快照缺失或扩展文件")
    require({p.name for p in path.iterdir()}==FILES|{"files.json"},"快照含未登记文件")
    return {name:(path/name).read_bytes() for name in names}


def index_journal(rows,phase):
    index={};head=None;active=None;recoveries={};users={};run_ends={}
    for row in rows:
        ident,kind=row["entry_id"],row["type"]
        require(ident not in index and row["schema_version"]==1 and row["parent_id"]==head,"Journal身份、版本或线性父链不同")
        if not index:require(kind=="header","缺少header")
        elif kind in {"run_start","run_continue"}:
            require(active is None,"run重叠")
            active=row["run_id"];require(active not in users,"run_id重复");users[active]=0
            if kind=="run_continue":
                require(phase=="B" and index[head]["type"]=="recovery","继续run没有紧接恢复记录")
                recovered=index[head]
                require(row["recovery_id"]==recovered["recovery_id"] and row["previous_run_id"]==recovered["interrupted_run_id"],"继续身份不匹配")
                require(row["recovery_id"] not in recoveries,"恢复被重复消耗")
                recoveries[row["recovery_id"]]=row
        elif kind=="message":
            require(row["run_id"]==active,"message归属不同")
            if row["message"]["role"]=="user":users[active]+=1
        elif kind=="run_end":
            require(row["run_id"]==active and active not in run_ends,"结束归属或次数不同")
            run_ends[active]=row;active=None
        elif kind=="recovery":
            require(active is None and phase=="B" and index[head]["type"]=="run_end" and index[head]["status"]=="interrupted","恢复记录未紧接旧中断终态")
            require(row["interrupted_run_id"]==index[head]["run_id"] and row["outcome"]=="effect_reconciled","恢复状态不匹配")
            assistant=index[row["assistant_id"]];result=index[row["result_entry_id"]]
            calls=assistant["message"].get("tool_calls",[])
            require(assistant["run_id"]==row["interrupted_run_id"] and len(calls)==1 and calls[0]["id"]==row["call_id"] and calls[0]["function"]["name"]=="submit_result","恢复未绑定原单一提交调用")
            require(result["run_id"]==row["interrupted_run_id"] and result["message"].get("tool_call_id")==row["call_id"],"恢复结果未配对原调用")
            content=json.loads(result["message"]["content"])
            require(content["ok"] is True and content["receipt"]["op_key"]==row["op_key"] and digest(content["receipt"])==row["receipt_digest"],"恢复回执摘要或成功事实不同")
        else:require(False,"本组独立新任务出现不支持的Journal类型")
        index[ident]=row;head=ident
    require(active is None and bool(index),"最终Journal未收尾")
    starts=[r for r in rows if r["type"]=="run_start"]
    continues=[r for r in rows if r["type"]=="run_continue"]
    require(len(starts)==1 and users[starts[0]["run_id"]]==1,"原始任务user数量不同")
    require(all(users[r["run_id"]]==0 for r in continues),"恢复伪造新user")
    expected=["interrupted","stop"] if phase=="B" else ["cancelled"] if phase=="C" else ["stop"]
    require([r["status"] for r in run_ends.values()]==expected,"原/新run终态不符合实验")
    require(len(continues)==(1 if phase=="B" else 0),"恢复继续次数不同")
    return index


def mutation_evidence(attempt,run,events,files):
    drivers={r["proposal"]["proposal_id"]:r for r in lines(attempt/"driver.jsonl")} if (attempt/"driver.jsonl").exists() else {}
    completed=[]
    for proposal in [e for e in events if e["event"]=="mutation_proposed"]:
        ident=proposal["proposal_id"];directory=run/"proposals"/ident
        metadata=read(directory/"proposal.json");name=metadata["path"]
        require(name in FILES and metadata["tool_name"]=="edit","修改范围不是两个文件的edit")
        before=(directory/"before.bin").read_bytes();after=(directory/"after.bin").read_bytes()
        require(before==files[name],"修改前字节不是已知当前版本")
        args=metadata["args"];old=args["old_text"].encode();new=args["new_text"].encode();pos=before.find(old)
        require(old and pos>=0 and before.find(old,pos+1)<0 and after==before[:pos]+new+before[pos+len(old):],"edit候选不对应唯一字节替换")
        require((directory/"diff.patch").read_text()==metadata["diff"],"diff与提案不同")
        observed=drivers[ident];copied=Path(observed["observation_directory"])
        require(copied==attempt/"driver-observations"/ident and observed["source"]=="experiment-driver","批准观察来自其他来源")
        for name2 in ("proposal.json","before.bin","after.bin","diff.patch"):
            require((directory/name2).read_bytes()==(copied/name2).read_bytes(),"批准后候选被替换")
        decision=read(directory/"decision.json")
        require(decision["decision"]==observed["decision"]=="approve" and decision["binding"]==metadata["binding"] and decision["input"]==f"approve {ident}","批准未绑定提案")
        requested=next(e for e in events if e["event"]=="approval_requested" and e["proposal_id"]==ident)
        decided=next(e for e in events if e["event"]=="approval_decided" and e["proposal_id"]==ident)
        started=next(e for e in events if e["event"]=="commit_started" and e["proposal_id"]==ident)
        done=next(e for e in events if e["event"]=="commit_finished" and e["proposal_id"]==ident)
        require(proposal["seq"]<requested["seq"]<decided["seq"]<started["seq"]<done["seq"],"文件提交先于批准")
        require(done["bytes_written"]==len(after) and done["path"]==name,"实际提交字节数或目标不同")
        files[name]=after;completed.append(name)
    return completed


def usage_total(usages):
    known=bool(usages) and all(u["usage_status"]=="known" for u in usages)
    return {"usage_status":"known" if known else "usage_unknown",**{key:sum(u[key] for u in usages) if known else None for key in ("prompt_tokens","completion_tokens","total_tokens")}}


def paired_feedback(entries,assistant,turn,events,interrupted):
    calls=assistant["message"].get("tool_calls",[])
    position=entries.index(assistant)+1
    feedback=[]
    while position<len(entries) and entries[position]["type"]=="message" and entries[position]["message"]["role"]=="tool":
        feedback.append(entries[position]);position+=1
    require([r["message"]["tool_call_id"] for r in feedback]==[c["id"] for c in calls],"tool反馈未按原调用身份/顺序配对")
    require(len({c["id"] for c in calls})==len(calls),"工具调用ID重复")
    for input_index,(call,record) in enumerate(zip(calls,feedback)):
        started=[e for e in events if e["event"]=="tool_started" and e["turn"]==turn and e["call_id"]==call["id"]]
        done=[e for e in events if e["event"]=="tool_finished" and e["turn"]==turn and e["call_id"]==call["id"]]
        require(len(started)==1 and started[0]["tool_name"]==call["function"]["name"],"派发身份或次数与模型调用不同")
        arguments=json.loads(call["function"]["arguments"])
        if call["function"]["name"]=="read":arguments={"offset":1,"limit":40,**arguments}
        actual=started[0].get("arguments",json.loads(started[0].get("arguments_raw","null")))
        require(actual==arguments,"派发参数与原模型候选不同")
        if not done:
            require(interrupted and call["function"]["name"]=="submit_result" and len(calls)==1,"缺少工具结果事件")
            continue
        require(len(done)==1 and done[0]["seq"]>started[0]["seq"] and done[0]["tool_name"]==call["function"]["name"],"工具结果事件身份或顺序不同")
        projected={key:value for key,value in done[0]["result"].items() if key not in {"control","artifact_dir","input_files","proposal_dir"}}
        require(projected==json.loads(record["message"]["content"]),"工具事件与Journal公开结果不同")
        if call["function"]["name"]=="read":
            expected={"run_id":assistant["run_id"],"assistant_id":assistant["entry_id"],"call_id":call["id"],"input_index":input_index}
            artifact=Path(done[0]["result"]["artifact_dir"])
            require(read(artifact/"request.json")["identity"]==expected and read(artifact/"request.json")["arguments"]==arguments and read(artifact/"spawn.json")["identity"]==expected,"真实read worker归属或参数不同")


def service_evidence(directory,phase,manifest,operation,effect,request_rows):
    paths=list(directory.glob("*/runs/*/receiver/service-*/request.json"))
    paths.sort(key=lambda p:int(p.parent.name.split("-")[1]))
    require(len(paths)==len(request_rows),"HTTP发送证据与服务数据库计数不同")
    for path,row in zip(paths,request_rows):
        request=read(path);payload=request["payload"]
        require(int(path.parent.name.split("-")[1])==row["sequence"] and request["route"]==row["route"] and request["url"]==manifest["url"],"HTTP调用顺序或接收地址不同")
        require(payload["receiver_id"]==manifest["receiver_id"] and payload["resource"]==operation["resource"]==row["resource"] and payload.get("epoch")==row["epoch"] and payload.get("op_key")==row["op_key"],"HTTP身份/epoch/key与接收端记录不同")
        if row["route"]=="/apply":
            require(payload["payload"]==json.loads(operation["payload_json"]) and payload["payload_hash"]==effect["payload_hash"],"HTTP提交内容与批准效果不同")
        exchange=path.with_name("exchange.json")
        if not exchange.exists():
            require(phase=="B" and row["route"]=="/apply" and row["sequence"]==2,"缺少非kill窗口的HTTP结果")
            continue
        result=read(exchange);worker=read(path.with_name("worker.json"));cleanup=result["cleanup"]
        responses=[m for m in result["messages"] if m.get("stage")=="response"]
        require(result["request_index"]==row["sequence"] and result["route"]==row["route"] and result["sent"] is True and result["stop_reason"] is None and len(responses)==1 and responses[0]["http_status"]==row["http_status"]==200,"HTTP响应与实际服务结果不同")
        require(cleanup["cleanup_confirmed"] and cleanup["returncode"]==0 and all(cleanup[k]==worker[k] for k in ("pid","process_identity")),"HTTP worker结果缺少实际收尾")
        expected={"receiver_id":manifest["receiver_id"],"resource":operation["resource"]}
        if row["route"]=="/fence":expected["epoch"]=row["epoch"]
        else:expected.update(op_key=operation["op_key"],state="applied",payload_hash=effect["payload_hash"],receipt=effect["receipt"])
        require(responses[0]["body"]==expected,"实际HTTP响应不是本次效果回执")


def inspect(batch,*,evidence_source="live-external-api"):
    previous=load_previous();stream=previous.load_previous();old=stream.load_previous();w03=old.load_w03_inspector()
    experiment=read(batch/"experiment.json");rows=read(batch/"summary.json")
    require(experiment["evidence_source"]==evidence_source and experiment["limits"]==LIMITS and experiment["maximum_model_requests"]==20 and experiment["normal_max_tokens"]==1536 and experiment["context_tokens"]==8192 and experiment["automatic_retries"]==0,"采集身份或固定预算不同")
    require([r["phase"] for r in rows]==list(LIMITS)[:len(rows)],"阶段乱序或重复")
    require((batch/"runtime-worker.py").read_bytes()==(MATERIALS/"runtime/worker.py").read_bytes(),"运行worker不是本周供给")
    complete=(len(rows)==3 and all(r["status"]==("cancelled" if r["phase"]=="C" else "stop") for r in rows)
              and len(rows[1]["attempts"])==2 and (rows[1]["attempts"][0].get("signal") or {}).get("signal")=="SIGKILL"
              and (rows[2]["attempts"][0].get("signal") or {}).get("signal")=="SIGINT")
    if not complete:
        counts={phase:len(list((batch/phase).glob("*/runs/*/turn-*/request.json"))) for phase in LIMITS}
        require(all(counts[p]<=LIMITS[p] for p in LIMITS) and sum(counts.values())<=20,"未完成批次超过预算")
        return {"coverage_complete":False,"coverage":"not_covered","actual_model_calls":None,
                "observed_model_calls":sum(counts.values()),"usage":usage_total([]),"semantic_verdict":"unevaluated",
                "phases":[{"phase":p,"observed_model_calls":counts[p],"status":next((r["status"] for r in rows if r["phase"]==p),"not_run")} for p in LIMITS]}
    outcomes=[];all_usages=[]
    for row in rows:
        phase=row["phase"];directory=batch/phase;before=snapshot(directory/"before");after=snapshot(directory/"after")
        require(before=={name:(batch/"inputs/sample_project"/name).read_bytes() for name in FILES},"独立阶段初始版本不同")
        entries=lines(directory/"session.jsonl");index=index_journal(entries,phase)
        require((directory/"after-session.jsonl").read_bytes()==(directory/"session.jsonl").read_bytes(),"末尾快照与Journal不同")
        require(entries[0]["workspace"]==str(directory/"workspace"),"工作区绑定不同")
        state=database(directory/"state.sqlite3");service_manifest=read(directory/"receiver/receiver.json")
        receiver=database(Path(service_manifest["database"]))
        require(state==read(directory/"state-export.json") and receiver==read(directory/"receiver-export.json"),"数据库导出与实际库不同")
        cleanup=read(directory/"receiver/controller-cleanup.json")
        require(cleanup["cleanup_confirmed"] and cleanup["pid"]==service_manifest["pid"] and cleanup["process_identity"]==service_manifest["process_identity"] and type(cleanup["returncode"]) is int,"接收服务未确认结束")
        require(len(state["budget_groups"])==1 and len(state["continuation_configs"])==1,"预算组不唯一")
        group=state["budget_groups"][0];config=json.loads(state["continuation_configs"][0]["config_json"])
        require(group["max_turns"]==LIMITS[phase] and group["max_actions"]==16 and group["summary_calls"]==0,"重启增加预算或独立任务发生非法摘要")
        require(config["runtime"]==str(batch/"runtime.json") and config["runtime_digest"]==hashlib.sha256((batch/"runtime.json").read_bytes()).hexdigest(),"原runtime绑定不同")
        require(config["mcp_config"] is None and config["receiver_id"]==service_manifest["receiver_id"] and config["read_concurrency"]==2,"原继续配置不同")
        expected_attempts=2 if phase=="B" else 1
        require(len(row["attempts"])==expected_attempts,"初次/恢复次数不同")
        usages=[];requests={};completed=[];shells=[];state_files=dict(before);all_events=[];read_results=[];worker_intervals=[]
        for attempt_number,attempt in enumerate(row["attempts"]):
            attempt_dir=directory/("initial" if attempt_number==0 else "recovery")
            require(Path(attempt["directory"])==attempt_dir,"采集引用其他目录")
            invocation=read(attempt_dir/"invocation.json");supervision=read(attempt_dir/"supervision.json")
            require(invocation["evidence_source"]==evidence_source and invocation["pid"]==attempt["pid"] and supervision["stop_reason"] is None,"采集进程或监督状态不同")
            require(int((attempt_dir/"exit-code.txt").read_text())==attempt["exit_code"],"退出码不同")
            result=None if phase=="B" and attempt_number==0 else read(attempt_dir/"stdout.json")
            if result is not None:
                require(result==attempt["result"] and result["evidence_source"]==evidence_source and result["exit_code"]==attempt["exit_code"],"CLI输出/来源不同")
            runs=list((attempt_dir/"runs").glob("run-*"));require(len(runs)==1,"运行目录数量不同")
            run=runs[0];events=lines(run/"events.jsonl");all_events+=events
            require([e["seq"] for e in events]==list(range(1,len(events)+1)) and events[0]["event"]=="run_started","事件前缀缺号")
            require(all(e["run_id"]==run.name and e["session_id"]==entries[0]["session_id"] and e["evidence_source"]==evidence_source for e in events),"事件身份来源不同")
            if result is None:
                require(attempt["exit_code"]==-9 and events[-1]["event"]!="run_finished","中断run伪造正常收尾")
                signal=read(attempt_dir/"signal.json");marker=read(directory/"receiver/after-effect-commit.json")
                require(signal==attempt["signal"] and signal["signal"]=="SIGKILL" and signal["pid"]==attempt["pid"] and signal["marker"]==marker and marker["committed"] is True,"效果后kill缺少实际命中")
                prefix=(directory/"before-recovery-session.jsonl").read_bytes()
                require((directory/"session.jsonl").read_bytes().startswith(prefix),"恢复改写旧Journal")
                prior_state=read(directory/"before-recovery-state.json")
                require(len(prior_state["operations"])==1 and prior_state["operations"][0]["status"]=="intent" and prior_state["operations"][0]["result_json"] is None,"kill未处于效果完成、本地结果未存窗口")
            else:
                require(events[-1]["event"]=="run_finished" and events[-1]["status"]==result["status"] and read(run/"result.json")==result,"事件/结果终态不同")
            worker_cleanup=read(attempt_dir/"controller-worker-cleanup.json")
            require({Path(item["source"]) for item in worker_cleanup}==set(attempt_dir.rglob("worker.json")),"HTTP worker控制器观察缺失")
            for item in worker_cleanup:
                worker=read(Path(item["source"]))
                require(item["cleanup_confirmed"] and item["identity_after"]!=item["process_identity"] and all(item[k]==worker[k] for k in ("pid","process_identity")),"HTTP worker清理未确认")
            read_cleanup=read(attempt_dir/"controller-read-cleanup.json")
            require({Path(item["source"]) for item in read_cleanup}==set(run.glob("reads-*/read-*/spawn.json")),"read worker控制器观察缺失")
            for item in read_cleanup:
                spawn=read(Path(item["source"]))
                require(item["cleanup_confirmed"] and not item["actions"] and item["identity_after"]!=item["process_identity"] and all(item[k]==spawn[k] for k in ("pid","process_identity")),"控制器未独立确认read worker已退出")
            requested=[e for e in events if e["event"]=="model_requested"]
            turns=sorted(run.glob("turn-*/request.json"))
            require(len(turns)==attempt["actual_model_calls"]==len(requested)==supervision["model_requests_observed"],"实际请求计数不同")
            assistants=iter(r for r in entries if r.get("run_id")==run.name and r["type"]=="message" and r["message"]["role"]=="assistant")
            for path in turns:
                number=int(path.parent.name.split("-")[1]);request=read(path);manifest=read(run/f"projection-{number:03d}.json")
                require(number not in requests and manifest["request_index"]==number and manifest["run_id"]==run.name and manifest["budget_group_id"]==group["budget_group_id"],"请求跨恢复重复编号或预算归属不同")
                require(request["max_tokens"]==1536 and request["stream"] is True and request["stream_options"]=={"include_usage":True} and request["thinking"]=={"type":"disabled"} and request["temperature"]==0,"实际请求参数不同")
                require({t["function"]["name"] for t in request["tools"]}=={"read","edit","write","shell","submit_result"},"工具目录扩展或缺失")
                budget=previous.budget_for(request,8192);require(budget["fits"] and manifest["budget"]==budget,"实际完整请求预算不符")
                rebuilt,receipt=previous.read_response(path.parent,stream,old)
                require(receipt["request_index"]==number and receipt["purpose"]=="task" and receipt["budget"]==budget,"回执用途预算不同")
                assistant=next(assistants,None)
                require(assistant is not None and assistant["message"]==rebuilt["message"] and assistant["parent_id"]==manifest["source_head_id"],"原始响应与Journal assistant不同")
                paired_feedback(entries,assistant,number,events,result is None)
                expected=previous.projected_messages(index,assistant["parent_id"],[],80,old,"")
                require(manifest["messages"]==request["messages"] and all(manifest.get(k)==v for k,v in expected.items()),"实际请求没有由原消息父链派生")
                if attempt_number==1:
                    recovery=next(r for r in entries if r["type"]=="recovery")
                    continuation=next(r for r in entries if r["type"]=="run_continue")
                    expected_chain=[{"entry_id":recovery["entry_id"],"interrupted_run_id":recovery["interrupted_run_id"],"continued_run_id":continuation["run_id"],"assistant_id":recovery["assistant_id"],"call_id":recovery["call_id"],"result_entry_id":recovery["result_entry_id"]}]
                    require(manifest["recovery_chain"]==expected_chain,"恢复来源关联被遗漏")
                requests[number]=(request,receipt);usages.append(receipt["usage"])
            require(next(assistants,None) is None,"Journal多出无模型响应的assistant")
            completed+=mutation_evidence(attempt_dir,run,events,state_files)
            for event in events:
                if event["event"]!="tool_finished":continue
                if event["tool_name"]=="shell":shells.append(w03.inspect_shell(event,run))
                if event["tool_name"]=="read" and event["result"].get("ok"):
                    data=event["result"];artifact=Path(data["artifact_dir"]);raw=(artifact/"input.bin").read_bytes()
                    require(hashlib.sha256(raw).hexdigest()==data["version"],"读取版本和实际快照不同")
                    source=old.source_lines(raw);offset,limit=data["offset"],data["limit"]
                    require(data["lines"]==[{"line":i+1,"text":source[i]} for i in range(offset-1,min(len(source),offset-1+limit))],"读取行号或内容不同")
                    read_results.append(read(artifact/"result.json"))
            for path in run.glob("reads-*/read-*/cleanup.json"):
                clean=read(path);spawn=read(path.parent/"spawn.json")
                require(clean["cleanup_confirmed"] and clean["pid"]==spawn["pid"] and clean["identity"]==spawn["identity"] and type(clean["returncode"]) is int,"read worker未确认退出")
                require(clean["finished_monotonic"]>=spawn["started_monotonic"],"read worker生命周期倒置")
                worker_intervals.append({"identity":spawn["identity"],"started":spawn["started_monotonic"],"finished":clean["finished_monotonic"]})
        require(sorted(requests)==list(range(1,len(requests)+1)) and len(requests)==group["model_calls"]==row["actual_model_calls"]<=LIMITS[phase],"恢复请求占用重置或漏计")
        reservations=state["request_reservations"]
        require([r["request_index"] for r in reservations]==list(requests),"SQL请求编号与实际请求不同")
        for reservation in reservations:
            _,receipt=requests[reservation["request_index"]]
            require(reservation["sent"]=="true" and json.loads(reservation["usage_json"])==receipt["usage"],"SQL发送/usage与原流回执不同")
        last_result=row["attempts"][-1]["result"]
        require(last_result["usage"]==usage_total(usages),"恢复前后累计用量不同")
        if phase in {"A","B"}:
            final=next(r for r in reversed(entries) if r["type"]=="message" and r["message"]["role"]=="assistant")
            require(last_result["text"]==final["message"].get("content"),"最终stdout回答与实际模型响应不同")
        require(state_files==after and after=={name:(directory/"workspace"/name).read_bytes() for name in FILES},"最终文件不对应批准的修改")
        effects=receiver["effects"];operations=state["operations"]
        require(len(receiver["requests"])==group["service_requests"]<=12,"服务发送数与持久预算不同")
        if phase in {"A","B"}:
            require(set(completed)==FILES and shells and shells[-1]["check_passed"] and shells[-1]["inputs"]==after,"两个文件修改及实际Docker检查未命中")
            require(len(effects)==len(operations)==1 and operations[0]["status"]=="journaled","效果数、本地状态不符")
            operation=operations[0];effect=effects[0];payload=json.loads(operation["payload_json"])
            require(operation["op_key"]=="op-"+digest([entries[0]["session_id"],operation["assistant_id"],operation["call_id"]]) and operation["resource"]=="budget-"+digest([entries[0]["session_id"],group["budget_group_id"]]),"稳定键没有绑定原身份")
            require(operation["payload_hash"]==effect["payload_hash"]==digest(payload) and operation["payload_json"]==effect["payload_json"] and operation["op_key"]==effect["op_key"] and operation["receiver_id"]==service_manifest["receiver_id"],"服务效果与本地意图绑定不同")
            require(payload["checked_files"]=={name:hashlib.sha256(data).hexdigest() for name,data in after.items()},"提交的检查版本不同于实际文件")
            result=json.loads(operation["result_json"]);receipt=result["receipt"]
            require(receipt["receipt"]==effect["receipt"] and receipt["payload_hash"]==effect["payload_hash"] and receipt["receiver_id"]==service_manifest["receiver_id"],"SQL回执不对应服务效果")
            tool=index[operation["result_entry_id"]]
            require(tool["message"]["tool_call_id"]==operation["call_id"] and json.loads(tool["message"]["content"])["receipt"]==receipt,"原tool回填没有使用确认回执")
            submits=[e for e in all_events if e["event"]=="approval_decided" and e.get("tool_name")=="submit_result"]
            require(len(submits)==1 and submits[0]["decision"]=="approve","提交缺少唯一逐笔批准")
            initial_run=directory/"initial/runs"/operation["origin_run_id"]
            proposal_dir=initial_run/("submission-"+submits[0]["proposal_id"])
            proposal=read(proposal_dir/"proposal.json");decision=read(proposal_dir/"decision.json")
            require(proposal["payload"]==payload and proposal["op_key"]==operation["op_key"] and proposal["payload_hash"]==operation["payload_hash"] and decision["payload_hash"]==operation["payload_hash"] and decision["decision"]=="approve","最终提交候选或批准被替换")
            observations=[r for r in lines(directory/"initial/driver.jsonl") if r["proposal"]["proposal_id"]==submits[0]["proposal_id"]]
            require(len(observations)==1 and observations[0]["decision"]=="approve" and observations[0]["source"]=="experiment-driver" and read(Path(observations[0]["observation_directory"])/"proposal.json")==proposal,"提交批准缺少控制器候选副本")
            routes=[r["route"] for r in receiver["requests"]]
            require(routes==(["/fence","/apply"] if phase=="A" else ["/fence","/apply","/fence","/lookup"]),"恢复重复apply或遗漏查询")
            service_evidence(directory,phase,service_manifest,operation,effect,receiver["requests"])
            if phase=="B":
                require(state["owner"][0]["epoch"]==2 and [r["epoch"] for r in receiver["requests"] if r["route"]=="/fence"]==[1,2],"新执行代没有在查询前安装fence")
                require(read(directory/"before-recovery-receiver.json")["effects"]==effects,"恢复增加或替换服务效果")
        else:
            require(before==after and not effects and not operations and not completed and not shells and not receiver["requests"],"取消只读阶段发生写入或服务请求")
            sig=read(directory/"initial/signal.json");require(sig["signal"]=="SIGINT" and len(sig["read_arrivals"])==2,"真实双读取取消未命中")
            for arrival in sig["read_arrivals"]:
                record=read(Path(arrival["path"]));require(record==arrival["record"] and record["monotonic"]<sig["sent_monotonic"],"SIGINT未在实际worker到达后")
            cleanups=[read(path) for path in (directory/"initial/runs").glob("*/reads-*/read-*/cleanup.json")]
            require(len(cleanups)==2 and all(c["cancel_requested"] and "kill" in c["actions"] for c in cleanups),"取消未覆盖两个不配合terminate的真实worker")
            require(len(requests)==1,"双read取消后出现后续模型请求")
        overlap=any(a["identity"]["assistant_id"]==b["identity"]["assistant_id"] and a["identity"]["call_id"]!=b["identity"]["call_id"] and max(a["started"],b["started"])<min(a["finished"],b["finished"]) for a in read_results for b in read_results)
        worker_overlap=any(a["identity"]["assistant_id"]==b["identity"]["assistant_id"] and a["identity"]["call_id"]!=b["identity"]["call_id"] and max(a["started"],b["started"])<min(a["finished"],b["finished"]) for a in worker_intervals for b in worker_intervals)
        outcomes.append({"phase":phase,"coverage":"hit","actual_model_calls":len(requests),"effects":len(effects),"service_requests":len(receiver["requests"]),"read_overlap":"hit" if overlap else "not_covered","worker_overlap":"hit" if worker_overlap else "not_covered","usage":usage_total(usages),"semantic_verdict":"unevaluated"})
        all_usages+=usages
    total=sum(r["actual_model_calls"] for r in outcomes);require(total<=20,"整组请求超预算")
    return {"evidence_source":evidence_source,"coverage_complete":True,"actual_model_calls":total,"usage":usage_total(all_usages),"phases":outcomes,"semantic_verdict":"requires-independent-review"}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--batch",type=Path,required=True)
    args=parser.parse_args();result=inspect(args.batch.resolve(strict=True));print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["coverage_complete"] else 1

if __name__=="__main__":raise SystemExit(main())
