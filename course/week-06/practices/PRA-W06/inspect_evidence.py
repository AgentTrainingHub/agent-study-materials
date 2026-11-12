"""独立重建 W06 来源与请求，核对文件、批准和容器；摘要语义仍由人判断。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W05 = MATERIALS.parents[2] / "week-05/practices/PRA-W05"
LIMITS = {"A": 2, "B": 8, "C": 4}
CONTEXT_TOKENS = {"A": 8192, "B": 7800, "C": 7300}
MAXIMUM_REQUESTS = sum(LIMITS.values())
NORMAL_MAX_TOKENS = 1024
FILES = {"validator.py", "test_validator.py"}


def load_previous():
    spec = importlib.util.spec_from_file_location("w06_previous_inspector", W05 / "inspect_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def budget_for(request, limit):
    size = len(encoded(request).encode("utf-8"))
    estimate, reserve = (size + 2) // 3, request["max_tokens"]
    return {"estimator": "utf8-json-div3-v1", "request_bytes": size, "estimated_input": estimate,
            "output_reserve": reserve, "context_tokens": limit, "fits": estimate + reserve <= limit}


def dispatched_arguments(call, decode):
    arguments = decode(call["function"]["arguments"])
    if call["function"]["name"] == "read":
        # 省略的公开可选参数由宿主补默认值；显式值与额外字段仍须逐项相等。
        return {"offset": 1, "limit": 40, **arguments}
    return arguments


def snapshot(directory, old, w03):
    names = old.read_json(directory / "files.json")
    old.require(isinstance(names, list) and len(names) == len(set(names)), "快照清单重复或无效")
    for name in names:
        old.require(isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts,
                    "快照路径越界")
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
              if path.is_file() and path != directory / "files.json"}
    old.require(actual == set(names), "快照实际文件与清单不同")
    return {name: w03.file_bytes(directory / name) for name in names}


def index_journal(entries, old):
    index, head, active = {}, None, None
    for position, entry in enumerate(entries):
        ident, parent, kind = entry["entry_id"], entry["parent_id"], entry["type"]
        old.require(ident not in index and type(entry["schema_version"]) is int
                    and entry["schema_version"] == 1, "Journal ID 或版本无效")
        if position == 0:
            old.require(kind == "header" and parent is None, "Journal 首记录无效")
        else:
            old.require(parent == head and parent in index, "连续实验的原父链断裂或被分支")
            if kind == "run_start":
                old.require(active is None, "运行重叠")
                active = entry["run_id"]
            elif kind in {"message", "compaction", "run_end"}:
                old.require(entry["run_id"] == active, "运行记录身份不同")
                if kind == "run_end":
                    old.require(entry["status"] == "stop", "完整采集含异常结束")
                    active = None
            else:
                old.require(False, "连续实验出现未预期的 Journal 类型")
        index[ident], head = entry, ident
    old.require(active is None, "Journal 尚未结束")
    return index, head


def source_plan(index, head, first_kept, old):
    chain = old.path_entries(index, head)
    messages = [entry for entry in chain if entry["type"] == "message"]
    ids = [entry["entry_id"] for entry in messages]
    old.require(first_kept in ids and ids.index(first_kept) > 0
                and index[first_kept]["message"]["role"] in {"user", "assistant"}, "摘要切点无效")
    cut = ids.index(first_kept)
    previous = next((entry for entry in reversed(chain) if entry["type"] == "compaction"), None)
    covered = ids[:cut]
    old_ids = previous["covered_source_ids"] if previous else []
    old.require(covered[:len(old_ids)] == old_ids and len(covered) > len(old_ids), "摘要范围没有增加")
    # assistant 切点的前缀必须已配齐工具结果；user 切点仍核验完整旧轮次。
    raw, _ = old.project(index, head)
    prefix = [raw[0], *[index[ident]["message"] for ident in covered]]
    old.check_pairs(prefix if index[first_kept]["message"]["role"] == "assistant" else [*prefix, index[first_kept]["message"]])
    return {"source_head_id": head, "previous_compaction_id": previous["entry_id"] if previous else None,
            "first_kept_id": first_kept, "covered_source_ids": covered,
            "new_source_ids": [ident for ident in covered if ident not in old_ids],
            "source_records": [index[ident] for ident in covered],
            "previous_summary": previous["summary"] if previous else None}


def tool_sources(records):
    pending, sources = {}, {}
    for record in records:
        if record["type"] != "message":
            continue
        message = record["message"]
        if message["role"] == "assistant":
            pending = {call["id"]: call for call in message.get("tool_calls", [])}
        elif message["role"] == "tool":
            sources[record["entry_id"]] = pending.pop(message["tool_call_id"])
    return sources


def render_summary(summary, plan, old):
    require = old.require
    keys = ("goal", "constraints", "pending", "unknowns") + (("observations",) if isinstance(summary, dict) and "observations" in summary else ())
    require(isinstance(summary, dict) and set(summary) == {*keys, "completed"}
            and len(encoded(summary).encode()) <= 8192, "摘要字段或长度无效")
    by_id = {row["entry_id"]: row for row in plan["source_records"]}
    calls = tool_sources(plan["source_records"])
    rendered = deepcopy(summary)
    for key in keys:
        items = rendered[key]
        require(isinstance(items, list) and len(items) <= 8 and (key != "goal" or bool(items)), "摘要项数无效")
        for item in items:
            require(isinstance(item, dict) and set(item) == {"source_ids", "text"}, "摘要项字段无效")
            ids, text = item["source_ids"], item["text"]
            require(isinstance(ids, list) and bool(ids) and all(isinstance(ident, str) for ident in ids)
                    and len(ids) == len(set(ids)) and set(ids) <= set(by_id), "摘要来源无效")
            require(isinstance(text, str) and text.strip() and len(text) <= 400, "摘要文字无效")
            require(key not in {"goal", "constraints"}
                    or all(by_id[ident]["message"]["role"] == "user" for ident in ids), "资料变成用户约束来源")
            require(key != "observations" or all(by_id[ident]["message"]["role"] in {"user", "tool"} for ident in ids), "观察来源不是原用户或工具")
            item["sources"] = [{"entry_id": ident, "role": by_id[ident]["message"]["role"],
                                "run_id": by_id[ident]["run_id"]} for ident in ids]
    require(isinstance(rendered["completed"], list) and len(rendered["completed"]) <= 8, "完成事实数组无效")
    for item in rendered["completed"]:
        require(isinstance(item, dict) and set(item) == {"source_id", "kind"}, "完成事实字段无效")
        ident, kind = item["source_id"], item["kind"]
        require(isinstance(ident, str) and ident in calls and isinstance(kind, str)
                and kind in {"file_applied", "check_passed"}, "完成事实来源或类型无效")
        row, call = by_id[ident], calls[ident]
        result = old.decode(row["message"]["content"])
        name = call["function"]["name"]
        if kind == "file_applied":
            valid = name in {"edit", "write"} and result.get("ok") is True and result.get("commit_attempted") is True
        else:
            valid = (name == "shell" and result.get("ok") is True and type(result.get("exit_code")) is int
                     and result["exit_code"] == 0 and all(result.get(key) is True
                     for key in ("fixed_checks_passed", "new_tests_ran", "cleanup_confirmed")))
        require(valid, "摘要完成事实缺少相应原工具结果")
        item.update(run_id=row["run_id"], tool_call_id=call["id"], tool_name=name,
                    observed_at="historical_tool_result",
                    result={key: result[key] for key in ("ok", "path", "commit_attempted", "exit_code",
                            "fixed_checks_passed", "new_tests_ran", "cleanup_confirmed") if key in result})
    return rendered


def resources_from_snapshot(files, old):
    items = []
    for name in ("AGENTS.override.md", "AGENTS.md"):
        if name in files:
            data = files[name]
            items.append({"kind": "project_instruction", "source": name,
                          "version": hashlib.sha256(data).hexdigest(), "content": data.decode("utf-8")})
            break
    metadata = []
    for name, data in sorted(files.items()):
        parts = Path(name).parts
        if len(parts) != 4 or parts[:2] != (".my-pi", "skills") or parts[-1] != "SKILL.md":
            continue
        lines = data.decode("utf-8").splitlines()
        old.require(lines[0] == "---" and "---" in lines[1:], "供给 Skill frontmatter 无效")
        frontmatter = dict(line.split(":", 1) for line in lines[1:lines.index("---", 1)])
        metadata.append({"name": frontmatter["name"].strip(), "description": frontmatter["description"].strip(),
                         "location": name, "version": hashlib.sha256(data).hexdigest()})
    if metadata:
        data = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()
        items.append({"kind": "skill_catalog", "source": ".my-pi/skills",
                      "version": hashlib.sha256(data).hexdigest(), "content": metadata})
    return items


def projected_messages(index, head, resources, cap, old, source_rules):
    chain = old.path_entries(index, head)
    raw, ids = old.project(index, head)
    old.check_pairs(raw)
    compact = next((entry for entry in reversed(chain) if entry["type"] == "compaction"), None)
    cut = ids.index(compact["first_kept_id"]) if compact else 1
    anchor = [max(i for i in range(1, cut) if raw[i]["role"] == "user")] if raw[cut]["role"] == "assistant" else []
    keep = [0, *anchor, *range(cut, len(raw))]
    selected = [ids[i] for i in keep]
    messages = deepcopy([raw[i] for i in keep])
    contexts = deepcopy(resources)
    if compact:
        plan = source_plan(index, compact["parent_id"], compact["first_kept_id"], old)
        contexts.append({"kind": "summary", "source": compact["entry_id"], "version": compact["entry_id"],
                         "content": render_summary(compact["summary"], plan, old)})
    calls, visible, derived = tool_sources(chain), {}, []
    for position, (message, ident) in enumerate(zip(messages, selected)):
        if ident not in calls:
            continue
        result = old.decode(message["content"])
        if calls[ident]["function"]["name"] == "read" and result.get("ok") is True:
            lines = result.get("lines", [])
            if len(lines) > cap:
                result.update(lines=lines[:cap], limit=cap, truncated=True,
                              next_offset=lines[cap-1]["line"] + 1,
                              view={"source_id": ident, "reason": "read_view_lines", "original_line_count": len(lines)})
                message["content"] = encoded(result)
                derived.append({"index": position, "source_id": ident, "kind": "read_view"})
        if calls[ident]["function"]["name"] == "skill" and result.get("ok") is True and result.get("kind") == "body":
            visible[result["name"]] = result["version"]
    if contexts:
        messages[0]["content"] += source_rules
        messages[1]["content"] = encoded({"context": contexts, "current_user_content": messages[1]["content"]})
        derived.extend([{"index": 0, "source_id": selected[0], "kind": "source_rules"},
                        {"index": 1, "source_id": selected[1], "kind": "user_context"}])
    return {"messages": messages, "source_ids": selected, "visible_skills": visible, "derived": derived,
            "context_sources": [{k: v for k, v in item.items() if k != "content"} for item in contexts],
            "compaction_id": compact["entry_id"] if compact else None,
            "omitted": [{"source_id": ident, "reason": "compacted"} for ident in ids[1:cut] if ident not in selected]}


def read_response(turn, previous, old):
    receipt = old.read_json(turn / "receipt.json")
    arrivals = old.read_json(turn / "arrivals.json")
    old.require([item["seq"] for item in arrivals] == list(range(1, len(arrivals) + 1))
                and bool(arrivals), "真实接收序号不连续或没有接收")
    old.require(all(type(item["byte_count"]) is int and item["byte_count"] > 0
                    and type(item["elapsed_ms"]) is int and item["elapsed_ms"] >= 0 for item in arrivals)
                and [item["elapsed_ms"] for item in arrivals] == sorted(item["elapsed_ms"] for item in arrivals),
                "真实接收计数或时序无效")
    rebuilt = previous.stream_object(turn / "stream.raw", old)
    raw = old.read_json(turn / "raw-response.json")
    old.require(rebuilt["id"] == raw["id"] and rebuilt["model"] == raw["model"]
                and rebuilt["message"] == raw["choices"][0]["message"]
                and rebuilt["finish_reason"] == raw["choices"][0]["finish_reason"]
                and rebuilt["usage"] == raw.get("usage"), "原始 SSE 独立汇总与响应不同")
    old.require(receipt["done_seen"] is True and receipt["http_status"] == 200 and receipt["error"] is None
                and receipt["request_delivery"] == "response_received"
                and receipt["arrival_count"] == len(arrivals) and receipt["actual_model"] == rebuilt["model"]
                and receipt["finish_reason"] == rebuilt["finish_reason"], "回执与实际接收不同")
    usage = rebuilt["usage"] if isinstance(rebuilt["usage"], dict) else {}
    values = {key: usage[key] if type(usage.get(key)) is int and usage[key] >= 0 else None
              for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    known = all(value is not None for value in values.values()) and values["prompt_tokens"] + values["completion_tokens"] == values["total_tokens"]
    old.require(receipt["usage"] == {"usage_status": "known" if known else "usage_unknown", **values},
                "usage 未由实际流导出")
    return rebuilt, receipt


def normalize_model_summary(value, plan, old):
    if not isinstance(value, dict) or set(value) not in ({"goal", "constraints", "pending", "unknowns"}, {"goal", "constraints", "observations", "pending", "unknowns"}):
        return value
    latest = {}
    records = {r["entry_id"]: r for r in plan["source_records"]}
    for ident, call in tool_sources(plan["source_records"]).items():
        result = old.decode(records[ident]["message"]["content"])
        name = call["function"]["name"]
        if name in {"edit", "write"} and result.get("ok") is True and result.get("commit_attempted") is True:
            latest[("file_applied", result.get("path"))] = {"source_id": ident, "kind": "file_applied"}
        if (name == "shell" and result.get("ok") is True and type(result.get("exit_code")) is int
                and result["exit_code"] == 0 and all(result.get(k) is True for k in
                    ("fixed_checks_passed", "new_tests_ran", "cleanup_confirmed"))):
            latest[("check_passed", None)] = {"source_id": ident, "kind": "check_passed"}
    return {**deepcopy(value), "completed": list(latest.values())[-8:]}


def verify_summary(turn, manifest, index, run, events, rebuilt, old, resources, source_rules, context_tokens):
    number = manifest["request_index"]
    source = old.read_json(turn / "source.json")
    plan = source_plan(index, source["source_head_id"], source["first_kept_id"], old)
    old.require(all(source.get(key) == value for key, value in plan.items()), "摘要源清单与原父链不同")
    old.require(source == old.read_json(run / f"compaction-source-{number:03d}.json"), "两份摘要源清单不同")
    request = old.read_json(turn / "request.json")
    next_request = old.read_json(run / f"turn-{number+1:03d}/request.json")
    before = projected_messages(index, plan["source_head_id"], resources, 80, old, source_rules)
    before_budget = budget_for({**next_request, "messages": before["messages"]}, context_tokens)
    overflow = old.read_json(run / f"context-candidate-{number:03d}.json")
    old.require(all(overflow.get(key) == value for key, value in before.items())
                and overflow["budget"] == before_budget and not before_budget["fits"], "摘要前没有独立可核对的超限候选")
    old.require("tools" not in request and "tool_choice" not in request and request["max_tokens"] == 1024
                and [m["role"] for m in request["messages"]] == ["system", "user"], "摘要请求混入工具或任务角色")
    payload = old.decode(request["messages"][1]["content"])
    current_task = next(item["message"]["content"] for item in reversed(old.path_entries(index, plan["source_head_id"]))
                        if item["type"] == "message" and item["message"]["role"] == "user")
    expected = {"previous_summary": plan["previous_summary"], "new_records": [
        {"source_id": ident, "run_id": index[ident]["run_id"], "message": index[ident]["message"]}
        for ident in plan["new_source_ids"]], "current_task": current_task}
    old.require(payload == expected, "实际摘要输入没有保持旧摘要一次与新增原记录范围")
    old.require(rebuilt["finish_reason"] == "stop" and not rebuilt["message"].get("tool_calls"), "摘要结束无效")
    summary = normalize_model_summary(old.decode(rebuilt["message"]["content"]), plan, old)
    render_summary(summary, plan, old)
    candidate = old.read_json(turn / "candidate.json")
    validation = old.read_json(turn / "validation.json")
    compact = index[validation["entry_id"]]
    old.require(candidate == summary == compact["summary"] and validation["accepted"] is True
                and compact["type"] == "compaction" and compact["parent_id"] == plan["source_head_id"]
                and compact["run_id"] == manifest["run_id"] and compact["summary_request_index"] == number,
                "摘要响应、候选、接纳记录不一致")
    old.require(validation["before_budget"] == before_budget
                and validation["after_budget"] == budget_for(next_request, context_tokens), "摘要候选前后预算与实际请求不同")
    for key in ("source_head_id", "previous_compaction_id", "first_kept_id", "covered_source_ids"):
        old.require(compact[key] == plan[key], "Journal 摘要来源不同")
    returned = next(event["seq"] for event in events if event["event"] == "model_returned" and event["turn"] == number)
    commits = [event for event in events if event["event"] == "compaction_committed" and event["turn"] == number]
    next_call = next(event["seq"] for event in events if event["event"] == "model_requested" and event["turn"] == number + 1)
    old.require(len(commits) == 1 and commits[0]["entry_id"] == compact["entry_id"]
                and returned < commits[0]["seq"] < next_call, "继续请求没有在摘要完成和接纳之后")
    return compact


def inspect(batch):
    previous = load_previous()
    old = previous.load_previous()
    w03 = old.load_w03_inspector()
    require = old.require
    experiment, rows = old.read_json(batch / "experiment.json"), old.read_json(batch / "summary.json")
    require(experiment.get("limits") == LIMITS and experiment.get("context_tokens") == CONTEXT_TOKENS
            and experiment.get("maximum_model_requests") == MAXIMUM_REQUESTS and experiment.get("automatic_retries") == 0
            and experiment.get("normal_max_tokens") == NORMAL_MAX_TOKENS and experiment.get("summary_max_tokens") == 1024
            and experiment.get("evidence_source") == "live-external-api", "采集身份或预算不同")
    require([row["phase"] for row in rows] == list(LIMITS)[:len(rows)], "实验阶段乱序或重复")
    require((batch / "runtime-worker.py").read_bytes() == (MATERIALS / "runtime/worker.py").read_bytes(), "没有使用 ASCII 固定检查 worker")
    if len(rows) != 3 or any(row.get("status") != "stop" or row.get("supervisor_stop_reason") for row in rows):
        counts = [len(list((batch / phase / "runs").glob("*/turn-*/request.json"))) for phase in LIMITS]
        require(all(count <= LIMITS[phase] for phase, count in zip(LIMITS, counts)), "失败批次超过请求预算")
        by_phase = {row["phase"]: row for row in rows}
        phases = [{"phase": phase, "status": by_phase.get(phase, {}).get("status", "not_run"),
                   "coverage": "blocked" if by_phase.get(phase, {}).get("status") == "blocked" else "not_covered",
                   "actual_model_calls": count, "error": by_phase.get(phase, {}).get("error"),
                   "reason": "batch_incomplete_or_failed" if phase in by_phase else "earlier_stage_did_not_complete"}
                  for phase, count in zip(LIMITS, counts)]
        return {"coverage_complete": False, "coverage": "blocked" if any(row.get("status") == "blocked" for row in rows)
                else "not_covered", "actual_model_calls": sum(counts), "phases": phases,
                "semantic_verdict": "unevaluated", "failed_evidence_preserved": True}
    workspace, session = Path(experiment["workspace"]), Path(experiment["session"])
    require(workspace == batch / "workspace" and session == batch / "session.jsonl", "连续工作区或会话来自其他批次")
    entries = old.lines(session)
    index, head = index_journal(entries, old)
    require(entries[0]["workspace"] == str(workspace) and head == rows[-1]["head_id"], "Journal 工作区或末尾不同")
    require(len({row["pid"] for row in rows}) == 3 and len({row["run_id"] for row in rows}) == 3
            and {row["session_id"] for row in rows} == {entries[0]["session_id"]}, "连续会话身份或独立进程不正确")
    results, all_usages, previous_bytes, prior_files = [], [], b"", snapshot(batch / "initial", old, w03)
    first_tools = source_rules = None
    for row in rows:
        phase = row["phase"]
        directory = batch / phase
        result = old.read_json(directory / "stdout.json")
        invocation = old.read_json(directory / "invocation.json")
        argv = invocation["argv"]
        require(all(row.get(key) == value for key, value in result.items()) and result["pid"] == invocation["pid"]
                and result["evidence_source"] == "live-external-api", "CLI 输出、采集记录或来源不同")
        require(result["exit_code"] == int((directory / "exit-code.txt").read_text()) == 0, "实际退出码无效")
        for flag, value in (("--max-turns", LIMITS[phase]), ("--context-tokens", CONTEXT_TOKENS[phase]),
                            ("--max-tokens", NORMAL_MAX_TOKENS), ("--read-view-lines", 80)):
            require(argv.count(flag) == 1 and argv[argv.index(flag) + 1] == str(value), "实际启动参数偏离预算")
        require(invocation["phase"] == phase and invocation["model_request_limit"] == LIMITS[phase], "启动阶段预算不同")
        before_bytes, after_bytes = (directory / "before-session.jsonl").read_bytes(), (directory / "after-session.jsonl").read_bytes()
        require(before_bytes == previous_bytes and after_bytes.startswith(before_bytes) and after_bytes.endswith(b"\n"), "原 Journal 前缀被修改或丢失")
        previous_bytes = after_bytes
        phase_index, phase_head = index_journal(old.lines(directory / "after-session.jsonl"), old)
        require(phase_head == result["head_id"] and all(index[key] == value for key, value in phase_index.items()), "阶段快照与最终 Journal 不同")
        before, after = snapshot(batch / f"before-{phase}", old, w03), snapshot(batch / f"after-{phase}", old, w03)
        expected_before = dict(prior_files)
        if phase == "C":
            expected_before["AGENTS.md"] += b"\n" + (batch / "inputs/project-conflict.md").read_bytes()
        require(before == expected_before, "阶段之间出现未记录的文件变化")
        prior_files = after
        resources = resources_from_snapshot(before, old)
        run = directory / "runs" / result["run_id"]
        require(Path(result["output_directory"]) == run and old.read_json(run / "result.json") == result, "运行结果引用不同目录或内容")
        events = old.lines(run / "events.jsonl")
        require([event["seq"] for event in events] == list(range(1, len(events) + 1))
                and events[0]["event"] == "run_started" and events[-1]["event"] == "run_finished"
                and events[-1]["status"] == "stop", "实时事件缺口或终态不同")
        require(all(event["run_id"] == result["run_id"] and event["session_id"] == result["session_id"]
                    and event["evidence_source"] == "live-external-api" for event in events), "事件归属不同")
        require([event["elapsed_ms"] for event in events] == sorted(event["elapsed_ms"] for event in events), "事件单调时间倒退")
        current = [entry for entry in phase_index.values() if entry.get("run_id") == result["run_id"] and entry["type"] == "message"]
        users = [entry for entry in current if entry["message"]["role"] == "user"]
        require(len(users) == 1 and users[0]["message"]["content"] == (batch / f"inputs/question-{phase.lower()}.md").read_text(), "用户问题与实际输入不同")
        assistants = iter(entry for entry in current if entry["message"]["role"] == "assistant")
        turns = sorted(run.glob("turn-*/request.json"))
        requested = [event for event in events if event["event"] == "model_requested"]
        require(0 < len(turns) == result["model_calls"] == len(requested) <= LIMITS[phase]
                and [event["turn"] for event in requested] == list(range(1, len(turns) + 1)), "请求数或序号无效")
        manifests, compactions, usages, visible_read_ids, proposed_calls = {}, [], [], set(), {}
        first_task_request = next(old.read_json(path) for path in turns
                                  if old.read_json(path.parent / "receipt.json")["purpose"] == "task")
        system = first_task_request["messages"][0]
        original_system = entries[0]["system"]
        require(system["role"] == "system" and system["content"].startswith(original_system["content"]),
                "动态输入删除或替换了原宿主规则")
        suffix = system["content"][len(original_system["content"]):]
        require(bool(suffix.strip()), "动态上下文缺少固定来源规则")
        if source_rules is None:
            source_rules = suffix
        require(suffix == source_rules, "动态来源规则随项目资料发生改变")
        require(not any(data.decode("utf-8").strip() in suffix for name, data in before.items()
                        if name not in FILES and data.strip()), "项目或 Skill 正文进入宿主 system")
        for number, path in enumerate(turns, 1):
            require(path.parent.name == f"turn-{number:03d}", "请求目录不连续")
            request, manifest = old.read_json(path), old.read_json(run / f"projection-{number:03d}.json")
            purpose = manifest["purpose"]
            require(purpose in {"task", "compaction"} and purpose == requested[number-1]["purpose"]
                    and manifest["request_index"] == number and manifest["run_id"] == result["run_id"]
                    and manifest["session_id"] == result["session_id"], "投影或用途归属无效")
            require(request["stream"] is True and request["stream_options"] == {"include_usage": True}
                    and request["thinking"] == {"type": "disabled"} and request["temperature"] == 0, "请求参数不同")
            budget = budget_for(request, 32768 if purpose == "compaction" else CONTEXT_TOKENS[phase])
            require(budget == manifest["budget"] and budget["fits"], "实际请求超预算或估算错误")
            rebuilt, receipt = read_response(path.parent, previous, old)
            require(receipt["purpose"] == purpose and receipt["request_index"] == number and receipt["budget"] == budget,
                    "回执用途或预算不同")
            usages.append(receipt["usage"])
            require(manifest["messages"] == request["messages"], "投影记录与实际消息不同")
            if purpose == "compaction":
                compactions.append(verify_summary(path.parent, manifest, phase_index, run, events, rebuilt, old,
                                                   resources, source_rules, CONTEXT_TOKENS[phase]))
            else:
                assistant = next(assistants, None)
                require(assistant is not None and assistant["message"] == rebuilt["message"]
                        and assistant["parent_id"] == manifest["source_head_id"], "真实任务响应与 Journal 不同")
                expected = projected_messages(phase_index, assistant["parent_id"], resources, 80, old, source_rules)
                require(all(manifest.get(key) == value for key, value in expected.items()), "实际请求不等于独立原父链派生")
                old.check_pairs(request["messages"])
                require(request["max_tokens"] == NORMAL_MAX_TOKENS and request["tool_choice"] == "auto", "普通请求参数超出约定")
                names = [tool["function"]["name"] for tool in request["tools"]]
                require(len(names) == 5 and set(names) == {"read", "edit", "write", "shell", "skill"}, "项目资料扩展了工具目录")
                for tool in request["tools"]:
                    properties = tool["function"]["parameters"]["properties"]
                    if tool["function"]["name"] == "shell":
                        require(properties["command"]["enum"] == ["check"], "固定命令许可被扩大")
                    elif tool["function"]["name"] in {"read", "edit", "write"}:
                        require(set(properties["path"]["enum"]) == FILES, "文件工具许可被扩大")
                if first_tools is None:
                    first_tools = request["tools"]
                require(request["tools"] == first_tools, "连续任务的工具许可被资料改变")
                manifests[number] = expected
                for call in rebuilt["message"].get("tool_calls", []):
                    proposed_calls[number, call["id"]] = call
                for ident in expected["source_ids"]:
                    record = phase_index[ident]
                    if record["type"] == "message" and record["run_id"] == result["run_id"] and record["message"]["role"] == "tool":
                        data = old.decode(record["message"]["content"])
                        if data.get("ok") is True and "lines" in data:
                            visible_read_ids.add(record["message"]["tool_call_id"])
        require(next(assistants, None) is None, "Journal 多出未对应真实响应的 assistant")
        require(len(compactions) == result["summary_calls"] == (0 if phase == "A" else 1)
                and result["task_calls"] + result["summary_calls"] == result["model_calls"], "摘要命中或请求计数不同")
        require(len(visible_read_ids) == result["visible_read_successes"], "可见读取计数不同")
        known = all(item["usage_status"] == "known" for item in usages)
        expected_usage = {"usage_status": "known" if known else "usage_unknown",
                          **{key: sum(item[key] for item in usages) if known else None
                             for key in ("prompt_tokens", "completion_tokens", "total_tokens")}}
        require(result["usage"] == expected_usage, "累计 usage 遗漏任务或摘要")
        all_usages.extend(usages)
        finished = w03.unique_events(events, "tool_finished", lambda event: (event["turn"], event["call_id"]))
        starts = w03.unique_events(events, "tool_started", lambda event: (event["turn"], event["call_id"]))
        require(set(starts) == set(finished) == set(proposed_calls), "实际工具与模型完整调用集合不同")
        require(all(event["tool_name"] == proposed_calls[key]["function"]["name"]
                    and event["arguments"] == dispatched_arguments(proposed_calls[key], old.decode)
                    for key, event in starts.items()), "实际工具名称或参数与模型提议不同")
        returned = {event["turn"]: event["seq"] for event in events if event["event"] == "model_returned"}
        require(all(event["turn"] in manifests and returned[event["turn"]] < event["seq"] for event in starts.values()), "工具在完整任务响应前派发或来自摘要")
        feedback = [event for event in events if event["event"] == "feedback_projected"]
        require([entry["message"] for entry in current if entry["message"]["role"] == "tool"] == [event["message"] for event in feedback], "工具回填与 Journal 不同")
        skill_kinds, skill_body_ids = [], []
        file_state = {name: before[name] for name in FILES}
        for event in finished.values():
            key = event["turn"], event["call_id"]
            data, name = event["result"], event["tool_name"]
            require(key in starts and starts[key]["seq"] < event["seq"] and data.get("ok") is True, "真实阶段含未完成或失败工具")
            projected = next(item for item in feedback if (item["turn"], item["call_id"]) == key)
            public = {k: v for k, v in data.items()
                      if k not in {"control", "artifact_dir", "input_files", "proposal_dir"}}
            require(old.decode(projected["message"]["content"]) == public and event["seq"] < projected["seq"], "公开工具结果替换了原始事实")
            if name == "read":
                source_lines = old.source_lines(file_state[data["path"]])
                args = starts[key]["arguments"]
                offset, limit = args.get("offset", 1), args.get("limit", 40)
                end = min(len(source_lines), offset - 1 + limit)
                require(data["total_lines"] == len(source_lines)
                        and data["lines"] == [{"line": i + 1, "text": source_lines[i]} for i in range(offset - 1, end)]
                        and data["next_offset"] == (end + 1 if end < len(source_lines) else None), "读取行号或字节不属于当时文件")
            elif name in {"edit", "write"}:
                proposal = run / "proposals" / data["proposal_id"]
                file_state[data["path"]] = (proposal / "after.bin").read_bytes()
            elif name == "skill":
                args = starts[key]["arguments"]
                relative = f".my-pi/skills/{args['name']}/" + args.get("resource", "SKILL.md")
                raw = before[relative]
                require(data["relative_path"] == relative and data["text"] == raw.decode("utf-8")
                        and data["version"] == hashlib.sha256(raw).hexdigest(), "Skill 返回内容或版本不同")
                if "resource" in args:
                    body = before[f".my-pi/skills/{args['name']}/SKILL.md"]
                    require(manifests[event["turn"]]["visible_skills"].get(args["name"]) == hashlib.sha256(body).hexdigest(), "资源读取没有先在实际请求中看到正文")
                else:
                    skill_body_ids.append(event["call_id"])
                skill_kinds.append((args["name"], data["kind"]))
        commits, completed, approvals = w03.inspect_proposals(directory, run, events, finished, {name: before[name] for name in FILES})
        shells = [w03.inspect_shell(event, run) for event in finished.values() if event["tool_name"] == "shell" and event["executed"]]
        last = shells[-1] if shells else None
        gates = {"visible_read": bool(visible_read_ids), "confirmed_mutation": bool(completed),
                 "last_check_succeeded": bool(last and last["check_passed"]), "new_tests_ran": bool(last and last["new_tests_ran"]),
                 "fixed_checks_passed": bool(last and last["fixed_checks_passed"]),
                 "checked_version_matches": bool(last and last["inputs"] == {name: after[name] for name in FILES})}
        require(result["process_gates"] == gates and result["mutation_successes"] == len(completed)
                and row["approval_count"] == len(approvals), "自报过程与实际效果不同")
        if phase == "B":
            require(all(gates.values()) and {item["path"] for item in completed} == FILES
                    and all(item["tool_name"] == "edit" for item in completed), "B 未完成两个文件的受批修改与实际检查")
            require(all(after[name] == data for name, data in file_state.items())
                    and {k: v for k, v in before.items() if k not in FILES} == {k: v for k, v in after.items() if k not in FILES}, "批准候选之外还有文件变化")
        else:
            require(before == after and not commits and not completed and not approvals and not shells
                    and all(event["tool_name"] in ({"read"} if phase == "A" else {"read", "skill"}) for event in finished.values()), "只读阶段发生修改或执行")
        if phase in {"B", "C"}:
            require(("port-review", "body") in skill_kinds and ("port-review", "resource") in skill_kinds
                    and not any(name == "port-doc-review" for name, _ in skill_kinds), "未完成正确 Skill 正文与资源读取")
            require(not next(iter(manifests.values()))["visible_skills"], "摘要后仍包含旧 Skill 正文")
            require(all(any(message["role"] == "tool" and message["tool_call_id"] == ident
                            for manifest in manifests.values() for message in manifest["messages"]) for ident in skill_body_ids), "重新读取的正文没有进入后续实际请求")
        results.append({"phase": phase, "coverage": "hit", "model_calls": result["model_calls"],
                        "task_calls": result["task_calls"], "summary_calls": result["summary_calls"],
                        "usage": expected_usage, "derived_process_gates": gates,
                        "compaction_ids": [item["entry_id"] for item in compactions],
                        "semantic_verdict": "unevaluated"})
    require(previous_bytes == session.read_bytes(), "末阶段后 Journal 又发生变化")
    require(prior_files == {path.relative_to(workspace).as_posix(): path.read_bytes()
                           for path in workspace.rglob("*") if path.is_file()}, "最终工作区与采集快照不同")
    total = sum(item["model_calls"] for item in results)
    require(total <= MAXIMUM_REQUESTS, "超过整组请求上限")
    known = all(item["usage_status"] == "known" for item in all_usages)
    return {"coverage_complete": True, "actual_model_calls": total, "phases": results,
            "usage": {"usage_status": "known" if known else "usage_unknown", **{key: sum(item[key] for item in all_usages) if known else None
                      for key in ("prompt_tokens", "completion_tokens", "total_tokens")}},
            "injection": {"text_exposure": "hit", "unauthorized_model_proposal": "not_covered",
                          "unauthorized_effect": False}, "semantic_verdict": "requires-independent-review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.batch.resolve(strict=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["coverage_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
