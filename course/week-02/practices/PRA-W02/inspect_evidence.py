import json
import os
from pathlib import Path

materials = Path(os.environ["MATERIALS"])
student = Path(os.environ["STUDENT_DIR"])
batch = Path(os.environ["W02_BATCH_DIR"])
marker = "[read status: ok, file content omitted]"

def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def check_pairs(messages):
    pending, seen = set(), set()
    for message in messages:
        role = message["role"]
        if pending:
            assert role == "tool", "工具结果尚未配齐就出现了其他角色"
        if role == "assistant":
            for call in message.get("tool_calls", []):
                call_id = call["id"]
                assert isinstance(call_id, str) and call_id.strip() and call_id not in seen
                seen.add(call_id)
                pending.add(call_id)
        elif role == "tool":
            assert message["tool_call_id"] in pending, "孤立或重复的 tool 消息"
            pending.remove(message["tool_call_id"])
    assert not pending, "请求结束时工具结果仍未配齐"

reports, initial_requests = [], []
for name in ("baseline", "omitted", "changed"):
    stdout = batch / f"{name}-stdout.json"
    exit_path = batch / f"{name}-exit-code.txt"
    if not stdout.exists():
        assert not exit_path.exists(), f"{name} 已记录退出状态却没有 stdout 文件：{exit_path}"
        reports.append({"group": name, "execution": "not_run", "coverage": "not_covered"})
        continue
    assert stdout.read_text(encoding="utf-8").strip(), f"{name} stdout 为空；请先检查 {exit_path} 与终端 stderr"
    result = read_json(stdout)
    recorded_exit = int(exit_path.read_text(encoding="utf-8").strip())
    assert recorded_exit == result["exit_code"], f"{name} 实际进程退出状态与结果字段不一致"
    run = Path(result["output_directory"])
    assert result == read_json(run / "result.json"), "stdout 与 result.json 不一致"
    events = [json.loads(line) for line in (run / "events.jsonl").read_bytes().splitlines() if line.strip()]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["run_id"] == result["run_id"] and event["evidence_source"] == result["evidence_source"] for event in events)
    finished = {(event["turn"], event["call_id"]): event for event in events if event["event"] == "tool_finished"}
    projected = [event for event in events if event["event"] == "feedback_projected"]
    for event in projected:
        actual = finished[(event["turn"], event["call_id"])]["result"]
        message = event["message"]
        assert message["role"] == "tool" and message["tool_call_id"] == event["call_id"]
        expected = dict(actual)
        if event["omitted"]:
            assert actual["ok"] is True
            expected.pop("lines")
            expected.update(content_omitted=True, content=marker)
        assert json.loads(message["content"]) == expected, "投影正文与实际结果不一致"

    turns = sorted(path for path in run.glob("turn-*") if path.is_dir())
    assert len(turns) == result["model_calls"], "轮次记录数与 model_calls 不一致"
    usages, confirmed_events = [], set()
    for number, turn in enumerate(turns, 1):
        assert turn.name == f"turn-{number:03d}"
        request = read_json(turn / "request.json")
        receipt = read_json(turn / "receipt.json")
        check_pairs(request["messages"])
        prior = [event for event in projected if event["turn"] < number]
        actual_tools = [message for message in request["messages"] if message["role"] == "tool"]
        assert actual_tools == [event["message"] for event in prior], "工具投影没有原样进入后续请求"
        if number == 1:
            initial_requests.append(request)
            assert "def parse_port" not in json.dumps(request["messages"])
            assert "return port" not in json.dumps(request["messages"])
        usages.append(receipt["usage"])
        error = receipt["error"]
        valid_response = (receipt["request_delivery"] == "response_received"
                          and receipt["http_status"] == 200
                          and bool(receipt["actual_model"])
                          and receipt["finish_reason"] in {"stop", "tool_calls", "length"}
                          and (error is None or error.get("class") == "output_truncated"))
        if valid_response:
            assert (turn / "raw-response.json").exists()
            confirmed_events.update(event["seq"] for event in prior)

    visible = sum(event["seq"] in confirmed_events and not event["omitted"]
                  and finished[(event["turn"], event["call_id"])]["result"].get("ok") is True for event in projected)
    assert visible == result["visible_read_successes"], "可见读取计数与已确认的后续请求不一致"
    known = bool(usages) and all(usage["usage_status"] == "known" for usage in usages)
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    expected_usage = {"usage_status": "known" if known else "usage_unknown",
                      **{field: sum(usage[field] for usage in usages) if known else None for field in fields}}
    assert result["usage"] == expected_usage, "逐轮 usage 与运行聚合不一致"
    omitted_observed = any(event["omitted"] and event["seq"] in confirmed_events for event in projected)
    changed_fact_seen = any(
        event["seq"] in confirmed_events and not event["omitted"]
        and any(line["line"] == 5 and line["text"] == "    if not 1 <= port <= 10240:"
                for line in finished[(event["turn"], event["call_id"])]["result"].get("lines", []))
        for event in projected)
    reports.append({"group": name, "evidence_source": result["evidence_source"],
                    "status": result["status"], "exit_code": result["exit_code"],
                    "execution": result["execution"], "coverage": result["coverage"],
                    "model_calls": result["model_calls"], "usage": result["usage"],
                    "visible_read_successes": visible, "task_verdict": result["task_verdict"],
                    "pairing_and_projection": "checked" if turns else "not_covered",
                    "omitted_condition": "observed" if omitted_observed else "not_covered",
                    "changed_fact": "observed" if changed_fact_seen else "not_covered",
                    "output_directory": str(run)})

if len(initial_requests) == 3:
    assert initial_requests[0] == initial_requests[1] == initial_requests[2], "三组首轮请求或配置发生了额外变化"
    comparison = "checked"
else:
    comparison = "not_covered"
for name in ("inputs/sample_project/validator.py", "inputs/changed_project/validator.py", "inputs/question.txt"):
    assert (materials / name).read_bytes() == (student / ".artifacts/w02-before" / name).read_bytes(), f"输入文件发生变化：{name}"
all_known = len(reports) == 3 and all(report.get("usage", {}).get("usage_status") == "known" for report in reports)
print(json.dumps({"groups": reports, "initial_request_comparison": comparison,
                  "input_bytes_unchanged": True,
                  "actual_model_calls": sum(report.get("model_calls", 0) for report in reports),
                  "total_tokens": sum(report["usage"]["total_tokens"] for report in reports) if all_known else None,
                  "semantic_verdict": "unevaluated"}, ensure_ascii=False, indent=2))
