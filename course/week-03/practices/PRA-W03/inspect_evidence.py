"""核对四组运行的证据一致性；代码正确性与原理解释仍需人工判断。"""
from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

FILES = {"validator.py", "test_validator.py"}
CASES = {"normal", "denied", "stale", "timeout"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_lines(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def file_bytes(path, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f"证据须为普通单链接文件：{path}")
    return path.read_bytes()


def unique_events(events, name, key):
    selected = [event for event in events if event["event"] == name]
    indexed = {key(event): event for event in selected}
    require(len(indexed) == len(selected), f"{name} 记录重复")
    return indexed


def check_pairs(messages):
    pending, seen = set(), set()
    for item in messages:
        if pending:
            require(item["role"] == "tool", "工具批次未配齐就发送下一消息")
        if item["role"] == "assistant":
            for call in item.get("tool_calls", []):
                identity = call["id"]
                require(identity and identity not in seen, "调用 ID 重复或缺失")
                seen.add(identity)
                pending.add(identity)
        elif item["role"] == "tool":
            require(item["tool_call_id"] in pending, "孤立或重复 tool 反馈")
            pending.remove(item["tool_call_id"])
    require(not pending, "请求结束时仍有待配对调用")


def inspect_requests(run, result, events, finished):
    projected = [event for event in events if event["event"] == "feedback_projected"]
    visible, usages = set(), []
    requests = sorted(run.glob("turn-*/request.json"))
    require(len(requests) == result["model_calls"], "请求文件数与模型计数不同")
    requested = [event["turn"] for event in events if event["event"] == "model_requested"]
    require(requested == list(range(1, len(requests) + 1)), "请求事件不连续或与文件不同")
    for turn, request_path in enumerate(requests, 1):
        require(request_path.parent.name == f"turn-{turn:03d}", "请求目录序号不连续")
        request, receipt = read_json(request_path), read_json(request_path.parent / "receipt.json")
        check_pairs(request["messages"])
        prior = [event for event in projected if event["turn"] < turn]
        require([message for message in request["messages"] if message["role"] == "tool"]
                == [event["message"] for event in prior], "下一轮请求缺少或替换了工具观察")
        if turn == 1:
            require("def parse_port" not in json.dumps(request["messages"]), "初始消息预装了源码")
        for event in prior:
            content = json.loads(event["message"]["content"])
            require("control" not in content, "宿主控制字段被直接当作模型观察")
            actual = finished[event["turn"], event["call_id"]]
            require(actual["seq"] < event["seq"] and actual["tool_name"] == event["tool_name"],
                    "观察没有对应的已结束工具调用")
        valid = (receipt["request_delivery"] == "response_received" and receipt["http_status"] == 200
                 and receipt["actual_model"] and receipt["finish_reason"] in {"stop", "tool_calls", "length"}
                 and (receipt["error"] is None or receipt["error"].get("class") == "output_truncated"))
        if valid:
            raw = read_json(request_path.parent / "raw-response.json")
            require(raw["model"] == receipt["actual_model"], "原始响应与回执模型不同")
            for event in prior:
                actual = finished[event["turn"], event["call_id"]]
                if event["tool_name"] == "read" and actual["result"].get("ok") is True:
                    visible.add(event["call_id"])
        usages.append(receipt["usage"])
    require(len(visible) == result["visible_read_successes"], "读取计数与有效后续请求不同")
    known = bool(usages) and all(item["usage_status"] == "known" for item in usages)
    require(result["usage"]["usage_status"] == ("known" if known else "usage_unknown"), "usage 状态不同")
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        require(result["usage"][field] == (sum(item[field] for item in usages) if known else None),
                f"{field} 与回执合计不同")
    return visible, len(requests)


def inspect_proposals(directory, run, events, finished, initial):
    key = lambda event: event["proposal_id"]
    proposals = unique_events(events, "mutation_proposed", key)
    requested = unique_events(events, "approval_requested", key)
    decided = unique_events(events, "approval_decided", key)
    started = unique_events(events, "commit_started", key)
    committed = unique_events(events, "commit_finished", key)
    tool_started = unique_events(events, "tool_started", lambda event: (event["turn"], event["call_id"]))
    driver = read_lines(directory / "driver.jsonl") if (directory / "driver.jsonl").exists() else []
    observations = {item["proposal"]["proposal_id"]: item for item in driver}
    require(len(observations) == len(driver), "驱动重复回答同一提案")
    require(set(observations) == set(decided) == set(requested) == set(proposals), "提案与审批证据未一一对应")
    require(set(committed) <= set(started) <= set(proposals), "提交事件没有对应提案")
    state, completed = dict(initial), []
    for identity, event in proposals.items():
        proposal_dir = Path(event["proposal_dir"]).resolve(strict=True)
        require(proposal_dir == run / "proposals" / identity, "提案引用了其他运行的目录")
        metadata = read_json(proposal_dir / "proposal.json")
        for field in ("proposal_id", "run_id", "call_id", "tool_name", "path", "proposal_dir"):
            require(metadata[field] == event[field], f"提案 {field} 与事件不同")
        name = metadata["path"]
        require(name in FILES, "提案目标超出练习范围")
        before = file_bytes(proposal_dir / "before.bin", optional=True)
        after = file_bytes(proposal_dir / "after.bin")
        require(metadata["before_exists"] == (before is not None), "提案存在性与 before 文件不同")
        require(before == state[name], "提案前版本与先前已提交结果不同")
        require(file_bytes(proposal_dir / "diff.patch").decode("utf-8") == metadata["diff"], "完整 diff 被替换")
        args = metadata["args"]
        actual_start = tool_started[event["turn"], event["call_id"]]
        require(actual_start["arguments"] == args and args["path"] == name, "工具参数与提案不同")
        if metadata["tool_name"] == "edit":
            old, new = args["old_text"].encode("utf-8"), args["new_text"].encode("utf-8")
            index = before.find(old) if before is not None and old else -1
            require(index >= 0 and before.find(old, index + 1) < 0, "edit 缺少唯一非空匹配")
            expected = before[:index] + new + before[index + len(old):]
        else:
            require(metadata["tool_name"] == "write" and before is None, "write 覆盖了已有文件")
            expected = args["content"].encode("utf-8")
        require(after == expected, "候选字节与本笔参数的结果不同")
        observation = observations[identity]
        observed_dir = Path(observation["observation_directory"]).resolve(strict=True)
        require(observed_dir == directory / "driver-observations" / identity, "审批观测来自其他目录")
        require(observation["source"] == "experiment-driver", "审批来源不明确")
        for filename in ("proposal.json", "before.bin", "after.bin", "diff.patch"):
            require(file_bytes(proposal_dir / filename, optional=filename == "before.bin")
                    == file_bytes(observed_dir / filename, optional=filename == "before.bin"), "审批后候选证据改变")
        decision = read_json(proposal_dir / "decision.json")
        require(decision["proposal_id"] == identity and decision["binding"] == metadata["binding"]
                and decision["decision"] == observation["decision"] == decided[identity]["decision"]
                and decision["input"] == f"{decision['decision']} {identity}", "审批未绑定本笔提案")
        require(event["seq"] < requested[identity]["seq"] < decided[identity]["seq"], "审批事件顺序无效")
        if identity in started:
            require(decision["decision"] == "approve" and decided[identity]["seq"] < started[identity]["seq"],
                    "提交先于审批或未获批准")
        if identity in committed:
            done = committed[identity]
            actual = finished[event["turn"], event["call_id"]]
            require(started[identity]["seq"] < done["seq"] < actual["seq"], "提交完成顺序无效")
            require(done["path"] == name and done["bytes_written"] == len(after), "提交目标或字节数不同")
            require(actual["executed"] and actual["result"].get("ok") is True
                    and actual["result"].get("proposal_id") == identity
                    and actual["result"]["control"]["effect"] == "applied", "提交结果未确认效果")
            state[name] = after
            completed.append({"seq": done["seq"], "path": name, "tool_name": metadata["tool_name"], "after": after})
    return started, completed, observations


def inspect_shell(event, run):
    item = event["result"]
    artifact = Path(item["artifact_dir"]).resolve(strict=True)
    require(artifact == run / f"shell-{event['action_count']:03d}", "容器证据来自其他调用")
    require(item == read_json(artifact / "result.json"), "工具反馈与容器结果不同")
    lifecycle, manifest = read_json(artifact / "container.json"), read_json(artifact / "manifest.json")
    require(manifest["main_workspace_published"] is False and manifest["effect_scope"] == "container_copy",
            "检查副本错误地声明发布到主目录")
    inputs = {}
    for name, path in item["input_files"].items():
        require(name in FILES and Path(path).resolve() == artifact / "input" / name, "检查输入路径无效")
        inputs[name] = file_bytes(Path(path))
    require(set(manifest["input_files"]) == set(inputs), "输入清单与保存的输入不同")
    require(set(manifest["output_files"]) <= FILES | {"marker.txt"}, "导出目标超出练习范围")
    outputs = {name: file_bytes(artifact / "output" / name) for name in manifest["output_files"]}
    changed = sorted(name for name in set(inputs) | set(outputs) if inputs.get(name) != outputs.get(name))
    require(changed == manifest["changed_files"], "副本变化与逐字节比较不同")
    version_unchanged = all(inputs.get(name) == outputs.get(name) for name in FILES)
    require(item["source_version_unchanged"] == version_unchanged, "检查期间源码或测试版本声明与字节不同")
    for stream in ("stdout", "stderr"):
        raw = file_bytes(artifact / f"{stream}.bin")
        require(len(raw) == item[f"{stream}_bytes"] and item["observed_bytes"][stream] >= len(raw),
                "原始输出长度与结果不同")
    require(lifecycle["created"]["Id"] == item["container_id"], "容器 ID 不同")
    stopped, absence = lifecycle.get("stopped_state", {}), lifecycle.get("absence", {})
    cleanup = (stopped.get("Running") is False and stopped.get("Pid") == 0
               and lifecycle.get("remove", {}).get("exit_code") == 0
               and absence.get("exit_code") != 0 and "no such object" in absence.get("stderr", "").lower())
    require(item["cleanup_confirmed"] == cleanup, "清理声明缺少停止、删除或再次查询证据")
    require(item["exit_code"] == stopped.get("ExitCode"), "退出码与 Docker 停止记录不同")
    normal = lifecycle["profile"] == "normal" and item["original_status"] is None and version_unchanged
    fixed_passed = normal and item["exit_code"] in {0, 11, 12, 13}
    tests_ran = normal and item["exit_code"] in {0, 12} and "test_validator.py" in inputs
    require(item["fixed_checks_passed"] == fixed_passed and item["new_tests_ran"] == tests_ran,
            "固定检查或新增测试声明与可信 runner 退出约定不同")
    passed = normal and item["exit_code"] == 0 and cleanup and item["capture_complete"] and tests_ran
    timed_out = (item["original_status"] == lifecycle["status"] == "timeout" and item["timed_out"]
                 and lifecycle["profile"] == "slow" and bool(outputs.get("marker.txt")))
    return {"seq": event["seq"], "inputs": inputs, "check_passed": passed,
            "fixed_checks_passed": fixed_passed, "new_tests_ran": tests_ran,
            "container_id": item["container_id"], "cleanup_confirmed": cleanup,
            "timed_out": timed_out, "exit_code": item["exit_code"], "changed_files": changed}


def inspect_case(row):
    directory, workspace = Path(row["directory"]).resolve(strict=True), Path(row["workspace"]).resolve(strict=True)
    require(workspace == directory / "workspace", "工作区来自其他实验组")
    result = read_json(directory / "stdout.json")
    require(result["exit_code"] == int((directory / "exit-code.txt").read_text()), "stdout 与实际退出码不同")
    run = Path(result["output_directory"]).resolve(strict=True)
    require(run == directory / "runs" / result["run_id"], "结果引用了其他实验组或旧运行")
    require(result == read_json(run / "result.json"), "最终 JSON 与保存结果不一致")
    for name in ("run_id", "output_directory", "status", "exit_code", "model_calls", "usage"):
        require(row[name] == result[name], f"采集摘要 {name} 与本次 CLI 输出不同")
    events = read_lines(run / "events.jsonl")
    require([event["seq"] for event in events] == list(range(1, len(events) + 1)), "事件序号不连续")
    require(all(event["run_id"] == result["run_id"] for event in events), "事件混入其他运行")
    require(result["evidence_source"] == "live-external-api"
            and all(event["evidence_source"] == result["evidence_source"] for event in events), "此入口只核验真实调用证据")
    finished = unique_events(events, "tool_finished", lambda event: (event["turn"], event["call_id"]))
    visible, model_calls = inspect_requests(run, result, events, finished)
    before = file_bytes(directory / "before-validator.py")
    initial = {"validator.py": before, "test_validator.py": None}
    final = {name: file_bytes(workspace / name, optional=name == "test_validator.py") for name in FILES}
    require(final["validator.py"] == file_bytes(directory / "after-validator.py"), "运行后工作区又发生变化")
    commits, completed, approvals = inspect_proposals(directory, run, events, finished, initial)
    require(result["mutation_successes"] == len(completed), "修改计数缺少已提交候选证据")
    require(row["approval_count"] == len(approvals), "审批计数缺少驱动观测证据")
    shells = [inspect_shell(event, run) for event in finished.values()
              if event["tool_name"] == "shell" and event["executed"]]
    last = shells[-1] if shells else None
    checked_matches = bool(last and set(last["inputs"]) == FILES
                           and all(last["inputs"][name] == final[name] for name in FILES))
    derived_gates = {"visible_read": bool(visible), "confirmed_mutation": bool(completed),
                     "last_check_succeeded": bool(last and last["check_passed"]),
                     "new_tests_ran": bool(last and last["new_tests_ran"]),
                     "fixed_checks_passed": bool(last and last["fixed_checks_passed"]),
                     "checked_version_matches": checked_matches}
    require(result["process_gates"] == derived_gates, "CLI 过程声明与独立证据核对不同")
    case, coverage, case_pass = row["case"], "not_covered", None
    require(case in CASES, "未知实验组")
    prior_preserved, prior_coverage, preserved_paths = None, "not_applicable", []
    latest = {item["path"]: item["after"] for item in completed}
    if result["status"] == "blocked":
        coverage = "blocked"
    elif case == "normal" and result["status"] == "stop":
        coverage = "hit"
        required_writes = {("edit", "validator.py"), ("write", "test_validator.py")}
        case_pass = (result["exit_code"] == 0 and all(derived_gates.values())
                     and required_writes <= {(item["tool_name"], item["path"]) for item in completed}
                     and final["validator.py"] != before
                     and all(final[name] == data for name, data in latest.items()))
    elif case == "denied" and any(item["decision"] == "deny" for item in approvals.values()):
        coverage = "hit"
        case_pass = (result["status"] == "approval_denied" and result["exit_code"] == 1
                     and not commits and final == initial)
    elif case == "stale" and any(item["external_change_before_response"] for item in approvals.values()):
        coverage = "hit"
        require(file_bytes(directory / "external-before.py") == before, "外部变化前版本不同")
        external = file_bytes(directory / "external-after.py")
        case_pass = (result["status"] == "target_changed" and result["exit_code"] == 1 and not commits
                     and final["validator.py"] == external and external != before and final["test_validator.py"] is None)
    elif case == "timeout" and any(item["timed_out"] for item in shells):
        coverage = "hit"
        timeout = next(item for item in shells if item["timed_out"])
        prior = {item["path"]: item["after"] for item in completed if item["seq"] < timeout["seq"]}
        prior_coverage = "hit" if prior else "not_covered"
        prior_preserved = all(final[name] == data for name, data in prior.items()) if prior else None
        preserved_paths = sorted(prior)
        case_pass = (result["status"] == "timeout" and result["exit_code"] == 1
                     and all(item["cleanup_confirmed"] for item in shells) and prior_preserved is not False)
    if case in {"denied", "stale", "timeout"} and coverage == "hit":
        blocked = [event for event in finished.values()
                   if event["result"].get("control", {}).get("blocks_run") and event.get("action_count")]
        require(blocked, "指定条件命中后没有工具停止记录")
        first = blocked[0]["seq"]
        require(not any(event["seq"] > first and event["event"] in {"model_requested", "commit_started", "tool_started"}
                        for event in events), "停止条件之后仍调用模型或执行工具")
    containers = [{key: value for key, value in item.items() if key not in {"inputs", "seq"}} for item in shells]
    return {"case": case, "coverage": coverage, "boundary_checks_passed": case_pass,
            "status": result["status"], "model_calls": model_calls, "usage": result["usage"],
            "commit_attempts": len(commits), "confirmed_mutations": len(completed), "visible_read_successes": len(visible),
            "derived_process_gates": derived_gates, "containers": containers, "task_verdict": result["task_verdict"],
            "prior_mutation_coverage": prior_coverage, "prior_mutation_preserved": prior_preserved,
            "preserved_paths": preserved_paths, "semantic_verdict": "unevaluated", "output_directory": str(run)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    rows = read_json(args.batch / "summary.json")
    reports = []
    for row in rows:
        try:
            reports.append(inspect_case(row))
        except (ValueError, KeyError, OSError, TypeError, RecursionError) as error:
            reports.append({"case": row.get("case"), "coverage": "invalid_evidence",
                            "boundary_checks_passed": False, "error": str(error), "semantic_verdict": "unevaluated"})
    valid = all("model_calls" in report for report in reports)
    known = bool(reports) and valid and all(row["usage"]["usage_status"] == "known" for row in reports)
    coverage_complete = ({row["case"] for row in reports} == CASES and len(reports) == len(CASES)
                         and all(row["coverage"] == "hit" for row in reports)
                         and all(row.get("prior_mutation_coverage") != "not_covered" for row in reports))
    print(json.dumps({"groups": reports, "actual_model_calls": sum(row["model_calls"] for row in reports) if valid else None,
                      "total_tokens": sum(row["usage"]["total_tokens"] for row in reports) if known else None,
                      "coverage_complete": coverage_complete, "semantic_verdict": "unevaluated"}, ensure_ascii=False, indent=2))
    return 0 if coverage_complete and all(row["boundary_checks_passed"] is True for row in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
