"""独立核对 W04 三段证据；不导入工程投影，也不替代答案语义和原理解释。"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W03 = MATERIALS.parents[2] / "week-03/practices/PRA-W03"
FILES = {"validator.py", "test_validator.py"}
LIMITS = {"A": 4, "B": 8, "C": 4}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_limits(limits):
    require(isinstance(limits, dict) and set(limits) == set(LIMITS), "采集预算必须恰好包含 A、B、C")
    require(all(type(limits[phase]) is int and 1 <= limits[phase] <= maximum
                for phase, maximum in LIMITS.items()), "阶段预算须为正整数，且不得超过 A4/B8/C4")


def validate_calls(phase, count, limits, *, minimum=0):
    require(phase in LIMITS and type(count) is int and minimum <= count <= limits[phase] <= LIMITS[phase],
            "实际请求数超过记录预算或规格上限，或计数类型无效")


def read_json(path):
    return decode(Path(path).read_text(encoding="utf-8"))


def decode(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"JSON 字段重复：{key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"JSON 含非有限数值：{value}")

    return json.loads(text, object_pairs_hook=unique, parse_constant=constant)


def lines(path):
    data = Path(path).read_bytes()
    require(data and data.endswith(b"\n"), f"记录缺少完整换行：{path}")
    return [decode(line.decode("utf-8")) for line in data.split(b"\n")[:-1]]


def source_lines(data):
    return [line.rstrip("\r\n") for line in io.StringIO(data.decode("utf-8"), newline="").readlines()]


def load_w03_inspector():
    spec = importlib.util.spec_from_file_location("w03_evidence_inspector", W03 / "inspect_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(directory, w03):
    names = read_json(directory / "files.json")
    require(isinstance(names, list) and set(names) <= FILES and len(names) == len(set(names)), "快照文件清单无效")
    contents = {name: w03.file_bytes(directory / name, optional=name not in names) for name in FILES}
    require({name for name, value in contents.items() if value is not None} == set(names), "快照存在性与清单不同")
    return contents


def path_entries(index, head):
    chain = []
    while head is not None:
        require(head in index and head not in {item["entry_id"] for item in chain}, "父链缺失或成环")
        chain.append(index[head])
        head = index[head]["parent_id"]
    return list(reversed(chain))


def project(index, head):
    selected = [item for item in path_entries(index, head) if item["type"] in {"header", "message"}]
    return ([item["system"] if item["type"] == "header" else item["message"] for item in selected],
            [item["entry_id"] for item in selected])


def journal_index(entries):
    index, head, active = {}, None, None
    for position, entry in enumerate(entries):
        identity, kind, parent = entry["entry_id"], entry["type"], entry["parent_id"]
        require(identity not in index and type(entry["schema_version"]) is int and entry["schema_version"] == 1,
                "entry_id 重复或版本不同")
        if position == 0:
            require(kind == "header" and parent is None, "首条必须为 header")
            head = identity
        else:
            require(parent in index, "父节点没有提前落盘")
            if kind == "run_start":
                require(active is None and parent == head, "运行没有接在当前 head 后")
                active, head = entry["run_id"], identity
            elif kind in {"message", "run_end"}:
                require(entry["run_id"] == active and parent == head, "运行消息与实际父链不一致")
                head = identity
                if kind == "run_end":
                    require(entry["status"] == "stop", "本组存在非正常结束记录")
                    active = None
            elif kind == "branch":
                require(active is None and index[parent]["type"] == "run_end"
                        and index[parent]["status"] == "stop", "分支没有指向完整轮次")
                head = identity
            else:
                require(kind == "ui_note" and active is None and parent == head, "UI 注记改变了路径或类型未知")
        index[identity] = entry
    require(active is None, "会话有未结束运行")
    return index, head


def check_pairs(messages):
    require(messages and messages[0]["role"] == "system", "请求缺少 system")
    state, pending = "user", set()
    for message in messages[1:]:
        role = message["role"]
        if role == "user":
            require(state == "user", "user 出现在未完成轮次中")
            state = "assistant"
        elif role == "assistant":
            require(state == "assistant", "assistant 位置无效")
            calls = message.get("tool_calls")
            if calls:
                ids = [call["id"] for call in calls]
                require(all(isinstance(item, str) and item.strip() for item in ids)
                        and len(set(ids)) == len(ids), "同批调用 ID 缺失或重复")
                pending, state = set(ids), "tool"
            else:
                require(isinstance(message.get("content"), str) and message["content"].strip(), "最终回答为空")
                state = "user"
        else:
            require(role == "tool" and state == "tool" and message["tool_call_id"] in pending, "工具结果失配")
            pending.remove(message["tool_call_id"])
            if not pending:
                state = "assistant"
    require(state == "assistant" and not pending, "请求没有停在可调用模型的位置")


def inspect_requests(run, result, events, index, ui_marker):
    current = [item for item in path_entries(index, result["head_id"])
               if item["type"] == "message" and item["run_id"] == result["run_id"]]
    assistants = [item for item in current if item["message"]["role"] == "assistant"]
    requests = sorted(run.glob("turn-*/request.json"))
    require(len(requests) == result["model_calls"] == len(assistants), "请求数、计数与已保存响应数不同")
    require([event["turn"] for event in events if event["event"] == "model_requested"]
            == list(range(1, len(requests) + 1)), "请求事件不连续")
    finished = {(event["turn"], event["call_id"]): event for event in events if event["event"] == "tool_finished"}
    projected = [event for event in events if event["event"] == "feedback_projected"]
    require([item["message"] for item in current if item["message"]["role"] == "tool"]
            == [event["message"] for event in projected], "持久工具结果与实际回填记录不同")
    visible, usages, source_sets = set(), [], []
    for turn, (path, assistant) in enumerate(zip(requests, assistants), 1):
        require(path.parent.name == f"turn-{turn:03d}", "请求目录序号不连续")
        request = read_json(path)
        projection = read_json(run / f"projection-{turn:03d}.json")
        expected, sources = project(index, assistant["parent_id"])
        require(projection["messages"] == request["messages"] == expected
                and projection["source_ids"] == sources, "实际请求不等于独立父链投影")
        require(projection["session_id"] == result["session_id"] and projection["run_id"] == result["run_id"],
                "投影证据属于其他运行")
        require(ui_marker not in json.dumps(request, ensure_ascii=False), "UI 注记泄漏到模型请求")
        check_pairs(request["messages"])
        receipt = read_json(path.parent / "receipt.json")
        raw = read_json(path.parent / "raw-response.json")
        require(receipt["request_delivery"] == "response_received" and receipt["http_status"] == 200
                and receipt["error"] is None and raw["model"] == receipt["actual_model"]
                and receipt["finish_reason"] in {"stop", "tool_calls"}, "真实请求未得到有效响应")
        response = raw["choices"][0]["message"]
        saved = {"role": "assistant", "content": response.get("content")}
        if receipt["finish_reason"] == "tool_calls":
            # Provider 的 index 是响应位置元数据，持久消息保存规范调用字段。
            saved["tool_calls"] = [{"id": call["id"], "type": call["type"],
                                    "function": {"name": call["function"]["name"],
                                                 "arguments": call["function"]["arguments"]}}
                                   for call in response["tool_calls"]]
        require(saved == assistant["message"], "持久 assistant 与实际响应不同")
        for event in projected:
            if event["turn"] >= turn:
                continue
            key = (event["turn"], event["call_id"])
            actual = finished[key]
            require(event["message"] in expected and actual["seq"] < event["seq"], "实际反馈没有进入后续请求")
            if event["tool_name"] == "read" and actual["result"].get("ok") is True:
                visible.add(key)
        usages.append(receipt["usage"])
        source_sets.append(set(sources))
    require(result["visible_read_successes"] == len(visible), "可见读取计数与实际后续请求不同")
    known = bool(usages) and all(item["usage_status"] == "known" for item in usages)
    require(result["usage"]["usage_status"] == ("known" if known else "usage_unknown"), "usage 状态不同")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        require(result["usage"][field] == (sum(item[field] for item in usages) if known else None), "usage 合计不同")
    require(current and current[0]["message"]["role"] == "user"
            and assistants[-1]["message"]["content"] == result["text"], "最终回答或新 user 未正确保存")
    return finished, visible, source_sets


def inspect_phase(batch, row, expected, index, w03):
    phase = row["phase"]
    directory = batch / phase
    result = read_json(directory / "stdout.json")
    invocation = read_json(directory / "invocation.json")
    require(Path(row["directory"]).resolve() == directory and result["exit_code"] == 0
            and int((directory / "exit-code.txt").read_text()) == 0, "阶段路径或实际退出码不同")
    require(result["pid"] == invocation["pid"] == expected["phases"][phase]["pid"], "进程身份不同")
    require(all(row.get(name) == result.get(name) for name in result), "采集摘要与 CLI 原始输出不同")
    require(all(expected["phases"][phase][name] == result[name] for name in ("run_id", "head_id", "session_id")),
            "宿主记录的会话身份与 CLI 输出不同")
    run = Path(result["output_directory"]).resolve(strict=True)
    require(run == directory / "runs" / result["run_id"] and result == read_json(run / "result.json"), "运行结果来自其他目录")
    require(result["evidence_source"] == "live-external-api" and result["task_verdict"] == "unevaluated", "证据来源或任务判定边界错误")
    validate_calls(phase, result["model_calls"], expected["limits"], minimum=1)
    require(type(invocation["model_request_limit"]) is int
            and invocation["model_request_limit"] == expected["limits"][phase], "启动预算与宿主记录不同")
    args = invocation["argv"]
    require(args.count("--max-turns") == 1
            and args[args.index("--max-turns") + 1] == str(expected["limits"][phase]), "实际 CLI 请求预算与记录不同")
    end = index[result["head_id"]]
    require(end["type"] == "run_end" and end["run_id"] == result["run_id"]
            and end["status"] == "stop", "阶段没有对应的正常 run_end")
    events = w03.read_lines(run / "events.jsonl")
    require([event["seq"] for event in events] == list(range(1, len(events) + 1))
            and all(event["run_id"] == result["run_id"] and event["evidence_source"] == "live-external-api"
                    for event in events), "事件混入其他运行或来源")
    finished, visible, sources = inspect_requests(run, result, events, index, expected["ui_marker"])
    before, after = snapshot(batch / f"before-{phase}", w03), snapshot(batch / f"after-{phase}", w03)
    started, completed, approvals = w03.inspect_proposals(directory, run, events, finished, before)
    shells = [w03.inspect_shell(event, run) for event in finished.values()
              if event["tool_name"] == "shell" and event["executed"]]
    last = shells[-1] if shells else None
    gates = {"visible_read": bool(visible), "confirmed_mutation": bool(completed),
             "last_check_succeeded": bool(last and last["check_passed"]),
             "new_tests_ran": bool(last and last["new_tests_ran"]),
             "fixed_checks_passed": bool(last and last["fixed_checks_passed"]),
             "checked_version_matches": bool(last and set(last["inputs"]) == FILES
                                              and all(last["inputs"][name] == after[name] for name in FILES))}
    require(result["process_gates"] == gates and result["mutation_successes"] == len(completed), "过程计数与独立证据不同")
    if phase == "B":
        latest = {item["path"]: item["after"] for item in completed}
        hit = (all(gates.values()) and {(item["tool_name"], item["path"]) for item in completed}
               >= {("edit", "validator.py"), ("write", "test_validator.py")}
               and before["validator.py"] != after["validator.py"]
               and all(after[name] == data for name, data in latest.items()))
    else:
        require(before == after and not approvals and not started
                and all(event["tool_name"] == "read" for event in finished.values()), "只读阶段发生了写入或其他工具调用")
        observed = []
        for key in visible:
            event = finished[key]
            value = event["result"]
            if value.get("path") != "validator.py":
                continue
            actual_lines = source_lines(before["validator.py"])
            for line in value["lines"]:
                require(1 <= line["line"] <= len(actual_lines)
                        and line["text"] == actual_lines[line["line"] - 1], "read 结果与当前磁盘快照不同")
                observed.append(line["text"])
        hit = bool(observed)
        if phase == "C":
            old_lines = source_lines(snapshot(batch / "initial", w03)["validator.py"])
            changed_code = {line for line in source_lines(before["validator.py"])
                            if line not in old_lines and line.strip() and not line.lstrip().startswith("#")}
            hit = (hit and bool(changed_code) and changed_code <= set(observed)
                   and expected["external_comment"] in observed and expected["external_comment"] in result["text"])
        else:
            hit = hit and any("1 < port < 65535" in line for line in observed)
    return {"phase": phase, "coverage": "hit" if hit else "not_covered", "boundary_checks_passed": bool(hit),
            "model_calls": result["model_calls"], "usage": result["usage"], "derived_process_gates": gates,
            "semantic_verdict": "unevaluated"}, sources


def inspect_batch(batch):
    w03 = load_w03_inspector()
    expected, rows = read_json(batch / "expected-state.json"), read_json(batch / "summary.json")
    validate_limits(expected["limits"])
    for row in rows:
        if row.get("model_calls") is not None:
            validate_calls(row["phase"], row["model_calls"], expected["limits"])
    if [row["phase"] for row in rows] != list(LIMITS) or any(row.get("status") != "stop" for row in rows):
        calls = [row.get("model_calls") for row in rows]
        return {"coverage_complete": False, "coverage": "blocked" if any(row.get("status") == "blocked" for row in rows)
                else "not_covered", "actual_model_calls": sum(calls) if all(type(item) is int for item in calls) else None,
                "phases": [{"phase": row["phase"], "status": row.get("status"), "error": row.get("error")} for row in rows],
                "semantic_verdict": "unevaluated"}
    workspace, session = Path(expected["workspace"]).resolve(), Path(expected["session"]).resolve()
    require(workspace == batch / "workspace" and session == batch / "session.jsonl", "证据引用其他批次")
    entries = lines(session)
    index, head = journal_index(entries)
    require(entries[0]["workspace"] == str(workspace), "会话工作区不同")
    require(len({row["run_id"] for row in rows}) == 3 and len({row["pid"] for row in rows}) == 3,
            "三个阶段未使用独立运行与进程")
    require(all(row["session_id"] == entries[0]["session_id"] for row in rows), "会话身份未保持稳定")
    notes = [item for item in entries if item["type"] == "ui_note"]
    branches = [item for item in entries if item["type"] == "branch"]
    require(len(notes) == 1 and notes[0]["text"] == expected["ui_marker"]
            and notes[0]["parent_id"] == rows[0]["head_id"], "UI 注记位置或标记不同")
    require(len(branches) == 1 and branches[0]["parent_id"] == rows[0]["head_id"], "分支没有选择 A 完成点")
    for name, control in expected["controls"].items():
        directory = batch / name
        require(Path(control["directory"]).resolve() == directory, "会话命令证据路径不同")
        output = read_json(directory / "stdout.json")
        require(output["exit_code"] == int((directory / "exit-code.txt").read_text()) == 0
                and output["model_calls"] == output["action_count"] == 0, "本地会话命令执行了模型或工具")
        before, after = lines(directory / "before-session.jsonl"), lines(directory / "after-session.jsonl")
        if name.startswith("inspect"):
            require(before == after, "inspect 改写了会话")
        else:
            require(after[:-1] == before and after[-1]["type"] == ("ui_note" if name == "note" else "branch"),
                    "会话命令没有只追加对应记录")
        view_index, view_head = journal_index(after)
        messages, sources = project(view_index, view_head)
        visible_entries = [item for item in after if name != "inspect-hidden" or item["type"] != "ui_note"]
        require(output["entries"] == visible_entries and output["head"] == view_head
                and output["messages"] == messages and output["source_ids"] == sources
                and output["blocked_reason"] is None, "inspect 输出与独立重建结果不同")
    require(set(expected["controls"]) == {"note", "inspect-visible", "inspect-hidden", "branch", "inspect-final"}, "本地对照命令未完成")
    reports, source_sets = [], {}
    for row in rows:
        report, sources = inspect_phase(batch, row, expected, index, w03)
        reports.append(report)
        source_sets[row["phase"]] = sources
    a_messages = {item["entry_id"] for item in entries if item["type"] == "message" and item["run_id"] == rows[0]["run_id"]}
    b_messages = {item["entry_id"] for item in entries if item["type"] == "message" and item["run_id"] == rows[1]["run_id"]}
    require(a_messages <= source_sets["B"][0] and a_messages <= source_sets["C"][0], "续聊或分支丢失 A 的完整消息")
    require(all(not sources & b_messages for sources in source_sets["C"]), "C 请求混入 B 分支消息")
    initial = snapshot(batch / "initial", w03)
    require(initial == snapshot(batch / "before-A", w03) == snapshot(batch / "after-A", w03)
            == snapshot(batch / "before-B", w03), "A 或注记命令改变了文件")
    b_after, c_before = snapshot(batch / "after-B", w03), snapshot(batch / "before-C", w03)
    require(b_after == snapshot(batch / "before-external", w03), "branch 改变了工作区")
    require(c_before == snapshot(batch / "after-external", w03) == snapshot(batch / "after-C", w03), "C 改变了当前文件")
    require(c_before["validator.py"] == b_after["validator.py"] + ("\n" + expected["external_comment"] + "\n").encode()
            and c_before["test_validator.py"] == b_after["test_validator.py"], "外部注释未保留 B 修改或改变了其他文件")
    require(all(w03.file_bytes(workspace / name, optional=c_before[name] is None) == c_before[name] for name in FILES), "采集后工作区再次变化")
    require(head == rows[-1]["head_id"], "最终 head 与 C 完成点不同")
    known = all(report["usage"]["usage_status"] == "known" for report in reports)
    return {"coverage_complete": all(report["coverage"] == "hit" for report in reports), "phases": reports,
            "actual_model_calls": sum(report["model_calls"] for report in reports),
            "total_tokens": sum(report["usage"]["total_tokens"] for report in reports) if known else None,
            "semantic_verdict": "unevaluated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inspect_batch(args.batch.resolve(strict=True))
    except (ValueError, KeyError, OSError, TypeError, IndexError, RecursionError) as error:
        report = {"coverage_complete": False, "coverage": "invalid_evidence", "error": str(error),
                  "semantic_verdict": "unevaluated"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["coverage_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
