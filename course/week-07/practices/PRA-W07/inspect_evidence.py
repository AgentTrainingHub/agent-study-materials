"""交叉核对真实模型、MCP wire、批准与容器；回答语义另行检查。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W06 = MATERIALS.parents[2] / "week-06/practices/PRA-W06"
LIMITS = {"A": 8, "B": 2, "C": 2}
NORMAL_MAX_TOKENS = 1024
CONTEXT_TOKENS = 8192


def load_previous():
    spec = importlib.util.spec_from_file_location("w07_previous_inspector", W06 / "inspect_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def journal_index(entries, expected_status, require):
    index, head, active = {}, None, None
    for position, entry in enumerate(entries):
        ident, parent, kind = entry["entry_id"], entry["parent_id"], entry["type"]
        require(ident not in index and entry["schema_version"] == 1, "Journal 身份或版本无效")
        require(parent == head, "独立会话的父链断裂")
        if position == 0:
            require(kind == "header", "缺少会话 header")
        elif kind == "run_start":
            require(active is None, "运行重叠")
            active = entry["run_id"]
        elif kind in {"message", "run_end"}:
            require(entry["run_id"] == active, "消息不属于当前运行")
            if kind == "run_end":
                require(entry["status"] == expected_status, "Journal 终态与实际退出不同")
                active = None
        else:
            require(False, "本周独立会话出现未预期记录")
        index[ident], head = entry, ident
    require(active is None and bool(index), "Journal 未结束")
    return index


def inspect_exchange(exchange, wire, old):
    require=old.require
    require(exchange["sent"] in (True,False,"unknown"), "exchange派发状态无效")
    require(sum(exchange[name] is not None for name in ("result","protocol_error","transport_error"))==1,
            "exchange结果或错误应互斥且明确")
    if exchange["sent"] is True:
        matches=[row for row in wire if row["direction"]=="send" and row.get("sent") is True
                 and row.get("message",{}).get("id")==exchange["rpc_id"]]
        require(len(matches)==1 and matches[0]["connection_id"]==exchange["connection_id"]
                and matches[0]["message"]["method"]==exchange["method"], "exchange未对应实际发送")
        received=[row["message"] for row in wire if row["direction"]=="receive"
                  and row.get("message",{}).get("id")==exchange["rpc_id"]]
        if exchange["result"] is not None:
            require(len(received)==1 and received[0].get("result")==exchange["result"], "exchange结果不同于原始响应")
        elif exchange["protocol_error"] is not None:
            require(len(received)==1 and received[0].get("error")==exchange["protocol_error"], "exchange协议错误不同于原始响应")
        else:
            require(exchange["transport_error"].get("class") and exchange["result"] is None,
                    "传输失败不应被改写为业务成功")
    elif exchange["sent"] is False:
        require(exchange["rpc_id"] is None, "零派发却具有实际RPC ID")


def inspect_wire(host, config, result, old):
    require = old.require
    wires, business = {}, {}
    totals = {"mcp_requests": 0, "mcp_calls": 0, "mcp_resource_reads": 0}
    host_cleanup = old.read_json(host / "cleanup.json")
    require(host_cleanup == result["mcp_cleanup"] and host_cleanup["cleanup_confirmed"], "Host 清理未确认")
    require(set(host_cleanup["connections"])=={server["id"] for server in config["servers"]},
            "Host清理连接清单不完整")
    for server in config["servers"]:
        ident = server["id"]
        directory = host / ident
        initialization = old.read_json(directory / "initialization.json")
        require(initialization["sdk_version"]=="1.30.0" and initialization["protocol_version"]=="2025-11-25"
                and initialization["python_version"].startswith("3.12."), "实际SDK或Python版本不同")
        cleanup = old.read_json(directory / "cleanup.json")
        wire = old.lines(directory / "wire.jsonl")
        require(wire and [r["seq"] for r in wire] == list(range(1,len(wire)+1)), "wire 序号缺失")
        require(all(r["connection_id"] == initialization["connection_id"] for r in wire), "wire 连接身份变化")
        for row in wire:
            require(old.decode(base64.b64decode(row["raw_base64"],validate=True).decode()) == row["message"],
                    "wire 原始字节与解码对象不同")
        sent = [r for r in wire if r["direction"] == "send" and r.get("sent") is True]
        requests = [r for r in sent if "id" in r["message"]]
        require(len({r["message"]["id"] for r in requests}) == len(requests), "连接内 RPC ID 重复")
        init = next(r for r in requests if r["message"].get("method") == "initialize")
        ready = next(r for r in sent if r["message"].get("method") == "notifications/initialized")
        init_response = next(r for r in wire if r["direction"] == "receive" and r["message"].get("id") == init["message"]["id"])
        require(init["seq"] < init_response["seq"] < ready["seq"]
                and init_response["message"]["result"]["protocolVersion"] == "2025-11-25", "握手版本或顺序不同")
        business[ident] = [r for r in requests if r["message"].get("method") in {"tools/call","resources/read"}]
        require(all(r["seq"] > ready["seq"] for r in business[ident]), "未就绪就派发业务")
        totals["mcp_requests"] += len(requests)
        totals["mcp_calls"] += sum(r["message"]["method"] == "tools/call" for r in business[ident])
        totals["mcp_resource_reads"] += sum(r["message"]["method"] == "resources/read" for r in business[ident])
        require(cleanup["cleanup_confirmed"] and cleanup["connection_id"] == initialization["connection_id"]
                and type(cleanup["worker_returncode"]) is int and type(cleanup["server_returncode"]) is int
                and all(cleanup[k] == initialization[k] for k in ("worker_pid","server_pid")), "清理记录缺少进程退出事实")
        require(host_cleanup["connections"][ident] == cleanup, "Host与逐连接清理事实不同")
        wires[ident] = wire
    require(set(host_cleanup["connections"])==set(wires) and host_cleanup["stats"]==totals,
            "Host清理漏连接或协议统计不同")
    recorded_rpc=set()
    for path in sorted(host.glob("rpc-*.json")):
        record=old.read_json(path)
        exchange=record["exchange"]
        inspect_exchange(exchange,wires[record["config_id"]],old)
        if exchange["sent"] is True:
            identity=record["config_id"],exchange["rpc_id"]
            require(identity not in recorded_rpc,"RPC记录重复")
            recorded_rpc.add(identity)
            sent=next(r for r in wires[record["config_id"]] if r["direction"]=="send"
                      and r.get("message",{}).get("id")==exchange["rpc_id"])
            require(sent["message"].get("params",{})==record["params"], "RPC记录参数与wire不同")
    expected_rpc={(ident,row["message"]["id"]) for ident,wire in wires.items() for row in wire
                  if row["direction"]=="send" and row.get("sent") is True and "id" in row["message"]
                  and row["message"].get("method")!="initialize"}
    require(recorded_rpc==expected_rpc,"Host逐RPC记录缺失或多出")
    require(all(result[k] == v for k,v in totals.items()), "MCP 实际 wire 计数与汇总不同")
    return wires, business, totals


def inspect_disclosed(requests, manifests, config, wires, config_digest, previous, old):
    """从原始目录响应重建本组固定服务的披露，避开目标 Host 实现。"""
    require=old.require
    expected_tools, expected_bindings={},{}
    resource_schema={"type":"object","properties":{"uri":{"type":"string"}},
                     "required":["uri"],"additionalProperties":False}
    digest=lambda value:hashlib.sha256(previous.encoded(value).encode()).hexdigest()
    for server in config["servers"]:
        ident=server["id"]; wire=wires[ident]
        methods={row["message"]["id"]:row["message"]["method"] for row in wire
                 if row["direction"]=="send" and "id" in row["message"]}
        remote_tools, resources={},{}
        for row in wire:
            message=row["message"]
            if row["direction"]!="receive" or "result" not in message: continue
            method=methods.get(message.get("id"))
            field="tools" if method=="tools/list" else "resources" if method=="resources/list" else None
            if field is None: continue
            for item in message["result"][field]:
                key=item["name"] if field=="tools" else item["uri"]
                values=remote_tools if field=="tools" else resources
                require(key not in values or values[key]==item,"固定真实服务目录在运行中变化")
                values[key]=item
        connection_id=wire[0]["connection_id"]
        for name,permit in server["allowed_tools"].items():
            require(name in remote_tools,"许可工具缺少实际列举结果")
            remote=remote_tools[name]; alias=f"mcp__{ident}__{name}"
            expected_tools[alias]={"type":"function","function":{"name":alias,
                "description":f"[{ident}] "+remote.get("description","")+" 允许 document_id："+", ".join(permit["document_ids"]),
                "parameters":remote["inputSchema"]}}
            contract={key:remote.get(key) for key in ("name","description","inputSchema","outputSchema","annotations")}
            expected_bindings[alias]={"config_id":ident,"connection_id":connection_id,"remote_name":name,
                "kind":"tool","contract_digest":digest(contract),"config_digest":config_digest}
        if server["resource_uris"]:
            require(set(server["resource_uris"])<=set(resources),"许可资源缺少实际列举结果")
            alias=f"mcp__{ident}__read_resource"
            expected_tools[alias]={"type":"function","function":{"name":alias,
                "description":f"读取 {ident} 服务的文本资源；允许 URI："+", ".join(server["resource_uris"]),
                "parameters":resource_schema}}
            expected_bindings[alias]={"config_id":ident,"connection_id":connection_id,"remote_name":"read_resource",
                "kind":"resource","contract_digest":digest({"inputSchema":resource_schema,"resources":list(resources.values())}),
                "config_digest":config_digest}
    for number,request in requests.items():
        actual={tool["function"]["name"]:tool for tool in request["tools"] if tool["function"]["name"].startswith("mcp__")}
        require(actual==expected_tools and manifests[number]["mcp_bindings"]==expected_bindings,
                "实际工具定义或绑定不同于原始服务目录与可信配置")


def inspect(batch, *, evidence_source="live-external-api"):
    """默认只核对真实批次；受控检查需显式传入其证据标签。"""
    previous = load_previous()
    stream = previous.load_previous()
    old = stream.load_previous()
    w03 = old.load_w03_inspector()
    require = old.require
    experiment, rows = old.read_json(batch / "experiment.json"), old.read_json(batch / "summary.json")
    require(experiment.get("limits") == LIMITS and experiment.get("maximum_model_requests") == 12
            and experiment.get("normal_max_tokens") == NORMAL_MAX_TOKENS
            and experiment.get("context_tokens") == CONTEXT_TOKENS
            and experiment.get("automatic_retries") == 0
            and experiment.get("evidence_source") == evidence_source, "采集身份或预算不同")
    require([r["phase"] for r in rows] == list(LIMITS)[:len(rows)], "阶段重复或乱序")
    reports, usage_rows = [], []
    total_calls, count_complete, usage_complete = 0, True, True
    for phase in LIMITS:
        row = next((r for r in rows if r["phase"] == phase), None)
        if row is None:
            count_complete = usage_complete = False
            reports.append({"phase":phase,"coverage":"not_covered","reason":"stage_missing"})
            continue
        directory = batch / phase
        result = old.read_json(directory / "stdout.json")
        calls = result["model_calls"]
        if calls is None:
            count_complete = False
        else:
            require(type(calls) is int and 0 <= calls <= LIMITS[phase], "模型计数无效")
            total_calls += calls
        require(all(row.get(k) == v for k,v in result.items()), "采集摘要替换了 CLI 事实")
        require(result["exit_code"] == int((directory / "exit-code.txt").read_text()), "实际退出码不同")
        require(result.get("evidence_source", evidence_source) == evidence_source, "CLI证据标签不同")
        if result["status"] in {"blocked","persistence_error","cleanup_unconfirmed","length","context_overflow",
                                "recent_exceeds_budget","deadline_exceeded"} or row["supervisor_stop_reason"]:
            usage_complete = False
            reports.append({"phase":phase,"coverage":"blocked","status":result["status"],"error":result.get("error")})
            continue
        require(type(calls) is int and calls > 0, "完整阶段缺少可核对的模型调用")
        invocation = old.read_json(directory / "invocation.json")
        args = invocation["argv"]
        for flag, value in (("--max-turns",LIMITS[phase]),("--max-tokens",NORMAL_MAX_TOKENS),("--context-tokens",CONTEXT_TOKENS)):
            require(args.count(flag)==1 and args[args.index(flag)+1]==str(value), "实际 CLI 参数与预算不同")
        require(invocation["pid"] == result["pid"] == row["pid"] and invocation["phase"]==phase, "进程或阶段身份不同")
        run = directory / "runs" / result["run_id"]
        require(Path(result["output_directory"]).resolve()==run and old.read_json(run/"result.json")==result,
                "输出目录或最终结果不同")
        events = old.lines(run/"events.jsonl")
        require([e["seq"] for e in events]==list(range(1,len(events)+1)), "事件序号不连续")
        require(all(e["run_id"]==result["run_id"] and e["session_id"]==result["session_id"]
                    and e["evidence_source"]==evidence_source for e in events), "事件归属不同")
        require(events[0]["event"]=="run_started" and events[-1]["event"]=="run_finished"
                and events[-1]["status"]==result["status"], "事件起止不匹配")
        entries = old.lines(directory/"after-session.jsonl")
        require((directory/"after-session.jsonl").read_bytes()==(batch/f"session-{phase}.jsonl").read_bytes(), "会话采集后发生变化")
        index=journal_index(entries,result["status"],require)
        manifests, requests, proposed, actual_usage = {}, {}, {}, []
        turns=sorted(run.glob("turn-*/request.json"))
        require(len(turns)==calls==len([e for e in events if e["event"]=="model_requested"]), "请求计数不符")
        assistants=iter(r for r in entries if r.get("message",{}).get("role")=="assistant")
        for request_path in turns:
            number=int(request_path.parent.name.split("-")[-1])
            request=old.read_json(request_path)
            manifest=old.read_json(run/f"projection-{number:03d}.json")
            expected=previous.projected_messages(index,manifest["source_head_id"],[],80,old,"")
            require(all(manifest[k]==v for k,v in expected.items()) and request["messages"]==expected["messages"], "请求未由真实父链投影")
            old.check_pairs(request["messages"])
            budget=previous.budget_for(request,CONTEXT_TOKENS)
            require(manifest["budget"]==budget and budget["fits"] and request["max_tokens"]==NORMAL_MAX_TOKENS, "请求预算不同")
            rebuilt,receipt=previous.read_response(request_path.parent,stream,old)
            assistant=next(assistants,None)
            require(receipt["purpose"]=="task" and assistant is not None
                    and assistant["message"]==rebuilt["message"]
                    and assistant["parent_id"]==manifest["source_head_id"]
                    and manifest["request_index"]==number and manifest["run_id"]==result["run_id"]
                    and manifest["session_id"]==result["session_id"], "原始响应、来源head或请求归属与Journal不同")
            actual_usage.append(receipt["usage"])
            aliases={t["function"]["name"] for t in request["tools"] if t["function"]["name"].startswith("mcp__")}
            require(set(manifest["mcp_bindings"])==aliases
                    and {"mcp__rules__lookup","mcp__checks__lookup"}<=aliases, "模型能力与绑定清单不同")
            for call in rebuilt["message"].get("tool_calls",[]):
                proposed[number,call["id"]]=call
            requests[number],manifests[number]=request,manifest
        require(next(assistants,None) is None and result["summary_calls"]==0 and result["task_calls"]==calls, "多出assistant或摘要计数")
        known=bool(actual_usage) and all(r["usage_status"]=="known" for r in actual_usage)
        usage={"usage_status":"known" if known else "usage_unknown",**{k:sum(r[k] for r in actual_usage) if known else None
               for k in ("prompt_tokens","completion_tokens","total_tokens")}}
        require(result["usage"]==usage,"usage汇总不同")
        usage_rows.extend(actual_usage)
        started=w03.unique_events(events,"tool_started",lambda e:(e["turn"],e["call_id"]))
        finished=w03.unique_events(events,"tool_finished",lambda e:(e["turn"],e["call_id"]))
        feedback=w03.unique_events(events,"feedback_projected",lambda e:(e["turn"],e["call_id"]))
        require(set(finished)==set(proposed) and set(started)<=set(finished)
                and set(feedback)==set(started),"工具提议、派发或配对结果缺失")
        for key in set(finished)-set(started):
            stopped = finished[key]
            require(stopped["executed"] is False and stopped["local_message"]["tool_call_id"]==key[1]
                    and stopped["result"]["control"]["blocks_run"] is True,
                    "未执行候选缺少本地停止记录")
        returned={e["turn"]:e["seq"] for e in events if e["event"]=="model_returned"}
        for key,event in started.items():
            call=proposed[key]
            require(event["tool_name"]==call["function"]["name"] and returned[key[0]]<event["seq"]<finished[key]["seq"],"工具名称或执行时序不同")
            arguments=old.decode(event["arguments_raw"]) if "arguments_raw" in event else event["arguments"]
            expected=old.decode(call["function"]["arguments"]) if event["tool_name"].startswith("mcp__") else previous.dispatched_arguments(call,old.decode)
            require(arguments==expected,"执行参数与模型提议不同")
            public={k:v for k,v in finished[key]["result"].items() if k not in {"control","artifact_dir","input_files","proposal_dir"}}
            require(old.decode(feedback[key]["message"]["content"])==public,"反馈改变了工具事实")
        journal_tools=[r["message"] for r in entries if r.get("message",{}).get("role")=="tool"]
        require(journal_tools==[e["message"] for e in events if e["event"]=="feedback_projected"],"Journal反馈不同")
        host=Path(result["mcp_evidence_directory"]).resolve()
        require(host.parent==directory/"runs" and host.name.startswith("mcp-preflight-"),"MCP证据来自其他运行")
        config=old.read_json(batch/("slow-config.json" if phase=="C" else "normal-config.json"))
        digest=hashlib.sha256(previous.encoded(config).encode()).hexdigest()
        wire,business,counts=inspect_wire(host,config,result,old)
        inspect_disclosed(requests,manifests,config,wire,digest,previous,old)
        observations=old.read_json(directory/"process-observations.json")
        expected_processes = {(str(host / server["id"] / "initialization.json"), field,
                               old.read_json(host / server["id"] / "initialization.json")[field])
                              for server in config["servers"] for field in ("worker_pid", "server_pid")}
        require(len(observations)==len(expected_processes)
                and {(o["source"],o["field"],o["pid"]) for o in observations}==expected_processes
                and all(o["alive"] is False for o in observations),"采集器未观察到对应自有进程退出")
        seen,business_ids=set(),set()
        for path in sorted(host.glob("call-*.json")):
            record=old.read_json(path); association=record["association"]
            key=association["turn"],association["call_id"]
            require(key in finished and key not in seen and association["run_id"]==result["run_id"]
                    and association["alias"]==proposed[key]["function"]["name"]
                    and record["result"]==finished[key]["result"],"MCP调用归属或结果不同")
            seen.add(key)
            binding=manifests[key[0]]["mcp_bindings"][association["alias"]]
            require(record["binding"]==binding and binding["config_digest"]==digest,"产生时绑定或配置版本不同")
            source=record["result"]["source"]
            require(source["config_id"]==binding["config_id"] and source["connection_id"]==binding["connection_id"]
                    and source["remote_name"]==binding["remote_name"],"来源与绑定不同")
            exchange=record.get("exchange")
            if exchange is not None:
                inspect_exchange(exchange, wire[binding["config_id"]], old)
                require(record["result"]["mcp"] == {k:exchange[k] for k in
                        ("method","rpc_id","sent","protocol_error","transport_error")}, "工具协议事实与exchange不同")
            if exchange is not None and exchange["sent"] is True:
                business_id=binding["config_id"],exchange["rpc_id"]
                require(business_id not in business_ids,"一个业务RPC被重复关联")
                business_ids.add(business_id)
                sent=next(r for r in business[binding["config_id"]] if r["message"]["id"]==exchange["rpc_id"])
                arguments=old.decode(proposed[key]["function"]["arguments"])
                expected_params = ({"name":binding["remote_name"],"arguments":arguments}
                                   if binding["kind"]=="tool" else arguments)
                require(sent["connection_id"]==binding["connection_id"]
                        and sent["message"]["params"]==expected_params,"实际业务参数或连接不同")
                if record["result"].get("ok") and binding["kind"]=="tool":
                    response=next(r for r in wire[binding["config_id"]] if r["direction"]=="receive" and r["message"].get("id")==exchange["rpc_id"])
                    require(record["result"]["data"]==response["message"]["result"]["structuredContent"],"模型资料不同于远端正文")
                    require(any(any(m.get("role")=="tool" and m.get("tool_call_id")==key[1]
                                    and old.decode(m["content"]).get("data")==record["result"]["data"]
                                    for m in req["messages"]) for n,req in requests.items() if n>key[0]),"资料没有进入后续真实请求")
        require(seen=={k for k,e in started.items() if e["tool_name"].startswith("mcp__")},"MCP调用记录缺失")
        require(business_ids=={(ident,row["message"]["id"]) for ident,values in business.items() for row in values},
                "原始业务RPC缺少对应模型候选")
        before=previous.snapshot(batch/f"before-{phase}",old,w03)
        after=previous.snapshot(batch/f"after-{phase}",old,w03)
        commits,completed,approvals=w03.inspect_proposals(directory,run,events,finished,before)
        if phase=="A":
            shells=[w03.inspect_shell(e,run) for e in finished.values() if e["tool_name"]=="shell" and e["executed"]]
            covered=(result["status"]=="stop" and {e["path"] for e in completed}==previous.FILES and shells
                     and shells[-1]["check_passed"] and shells[-1]["inputs"]==after
                     and all(any(r["message"]["method"]=="tools/call" for r in business[name]) for name in ("rules","checks")))
        else:
            require(before==after and not commits and not completed and not approvals,"只读阶段发生修改")
            if phase=="B":
                covered=(counts["mcp_calls"]==0 and counts["mcp_resource_reads"]==0 and result["exit_code"]==1
                         and any(e["result"].get("error",{}).get("class")=="mcp_object_denied" for e in finished.values()))
            else:
                cancel_path=directory/"cancellation.json"
                covered=cancel_path.is_file() and result["status"]=="cancelled" and result["exit_code"]==1
                if covered:
                    cancellation=old.read_json(cancel_path)
                    diagnostics=[]
                    for line in (host/"rules/stderr.txt").read_text().splitlines():
                        try:
                            item=json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(item,dict): diagnostics.append(item)
                    require(cancellation["signal"]=="SIGINT" and cancellation["pid"]==result["pid"]
                            and cancellation["arrival"]["record"] in diagnostics
                            and cancellation["heartbeat"]["record"] in diagnostics,
                            "取消依据未对应实际服务记录或CLI进程")
                    require(cancellation["arrival"]["record"]["monotonic"]<=cancellation["sent_monotonic"]
                            and cancellation["heartbeat"]["record"]["monotonic"]<=cancellation["sent_monotonic"],"到达与取消次序错误")
                    require(counts["mcp_calls"]==1 and counts["mcp_resource_reads"]==0
                            and all(r["monotonic"]<=cancellation["sent_monotonic"] for r in business["rules"]),"取消后新增业务派发")
                    require(calls==1,"取消阶段新增模型请求")
        reports.append({"phase":phase,"coverage":"hit" if covered else "not_covered","status":result["status"],
                        "model_calls":calls,"mcp_counts":counts,"usage":usage,"semantic_verdict":"unevaluated"})
    require(total_calls<=12,"超过整组模型预算")
    known=usage_complete and bool(usage_rows) and all(r["usage_status"]=="known" for r in usage_rows)
    return {"evidence_source":evidence_source,
            "coverage_complete":all(r["coverage"]=="hit" for r in reports),
            "actual_model_calls":total_calls if count_complete else None, "observed_model_calls":total_calls,
            "phases":reports,"usage":{"usage_status":"known" if known else "usage_unknown",
            **{k:sum(r[k] for r in usage_rows) if known else None for k in ("prompt_tokens","completion_tokens","total_tokens")}},
            "semantic_verdict":"requires-independent-review"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch",type=Path,required=True)
    args=parser.parse_args()
    result=inspect(args.batch.resolve(strict=True))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["coverage_complete"] else 1


if __name__=="__main__":
    raise SystemExit(main())
