"""独立核对 W09 原始请求、持久决定、条件来源、文件与 Docker 证据。"""
from __future__ import annotations
import argparse
import difflib
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

FILES = ("validator.py", "test_validator.py")
WAIT_KEYS = ("wait_id", "kind", "origin_run_id", "assistant_id", "call_id",
             "requirement_revision", "plan_revision", "candidate_binding", "question_id", "budget_group_id")
BOUND_KEYS = ("proposal_id", "run_id", "call_id", "tool_name", "args", "path", "workspace_identity",
              "before_identity", "before_sha256", "after_sha256", "diff", "proposal_dir")


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("nonfinite JSON: " + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _read(path):
    return _decode(Path(path).read_bytes())


def _lines(path):
    raw = Path(path).read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("JSONL must be a nonempty complete prefix: " + str(path))
    return [_decode(line) for line in raw.splitlines()]


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _one(rows, message):
    _require(len(rows) == 1, message + ": count=" + str(len(rows)))
    return rows[0]


def _chain(entries):
    _require(bool(entries), "missing Journal")
    indexed = {row["entry_id"]: row for row in entries}
    _require(len(indexed) == len(entries), "duplicate Journal entry_id")
    result, seen, head = [], set(), entries[-1]["entry_id"]
    while head is not None:
        _require(head not in seen and head in indexed, "broken or cyclic Journal parent chain")
        seen.add(head)
        row = indexed[head]
        result.append(row)
        head = row["parent_id"]
    result.reverse()
    _require(result[0]["type"] == "header", "parent chain has no header")
    return result


def _states(export, chain):
    positions = {row["entry_id"]: index for index, row in enumerate(chain)}
    rows = []
    for index, row in enumerate(export["workflow_states"]):
        if row["anchor_id"] in positions:
            state = _decode(row["state_json"])
            _require(all(state[key] == row[key] for key in ("state_id", "revision", "session_id", "branch_id")),
                     "workflow state identity mismatch")
            rows.append((positions[row["anchor_id"]], index, state))
    _require(bool(rows), "missing current-chain workflow state")
    return [row[2] for row in sorted(rows)]


def _argument(argv, flag):
    _require(argv.count(flag) == 1, "missing or duplicate CLI argument " + flag)
    return argv[argv.index(flag) + 1]


def audit_workflow(phase_dir, entries, exported_state, attempts, before_files, after_files):
    """Return mechanical facts and missing coverage; semantic review remains required.

    entries and exported_state are immutable end-of-phase snapshots. In particular,
    phase A is never compared with its live database after phase C has appended to it.
    """
    root = Path(phase_dir)
    report = {"facts": {}, "errors": [], "coverage": {}, "semantic_review": "required"}
    facts, coverage = report["facts"], report["coverage"]

    def check(name, action):
        try:
            value = action()
            coverage[name] = True
            return value
        except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
            coverage[name] = False
            report["errors"].append({"check": name, "class": type(error).__name__, "message": str(error)})
            return None

    def foundation():
        config = _read(root / "phase-config.json")
        _require(config["phase"] in {"A", "B", "C"} and bool(attempts), "missing phase or attempts")
        chain = _chain(entries)
        _require(chain[0]["workspace"] == config["workspace"], "workspace differs from original Journal")
        states = _states(exported_state, chain)
        _require(states[-1]["session_id"] == chain[0]["session_id"], "SQL session differs from Journal")
        baseline = states[-1]["baseline"]
        version = _sha(baseline["raw_text"].encode())
        _require(baseline["version"] == version and baseline["source_id"] == "baseline:" + version
                 and _decode(baseline["raw_text"]) == baseline["content"], "baseline provenance mismatch")
        _require(all(state["baseline"] == baseline for state in states), "baseline mutated in history")
        ev, invocations, pids = [], {}, {}
        for attempt in attempts:
            directory = Path(attempt["directory"])
            invocation = _read(directory / "invocation.json")
            _require(invocation["pid"] == attempt["pid"], "attempt PID differs from invocation")
            invocations[str(directory)] = invocation
            for path in sorted((directory / "runs").glob("*/events.jsonl")):
                rows = _lines(path)
                ev.extend(rows)
                for row in rows:
                    if row["event"] == "run_started":
                        _require(row["run_id"] not in pids, "duplicate run_started")
                        pids[row["run_id"]] = attempt["pid"]
        _require(bool(pids), "no actual run evidence")
        waits = []
        for row in exported_state["workflow_waits"]:
            origin = _decode(row["origin_json"])
            _require(all(origin[key] == row[key] for key in ("wait_id", "session_id", "branch_id", "budget_group_id", "kind")),
                     "SQL wait identity mismatch")
            if origin["origin_run_id"] in pids:
                waits.append({**origin, **{key: row[key] for key in ("status", "permit_id", "permit_epoch", "result_entry_id")},
                    **{key: _decode(row[key + "_json"]) if row[key + "_json"] is not None else None
                       for key in ("decision", "result", "resolution")}})
        position = {row["entry_id"]: index for index, row in enumerate(chain)}
        waits.sort(key=lambda wait: position[wait["assistant_id"]])
        _require(bool(waits), "no phase waits observed")
        facts.update(phase=config["phase"], session_id=chain[0]["session_id"], wait_count=len(waits),
                     baseline_source_id=baseline["source_id"], requirement=states[-1]["requirements"])
        return config, chain, states, ev, invocations, pids, waits

    base = check("snapshot_and_sources", foundation)
    if base is None:
        report["coverage_complete"] = False
        return report
    config, chain, states, events, invocations, pids, waits = base
    by_id = {row["entry_id"]: row for row in chain}
    phase = config["phase"]
    applied = []

    def message_pairing():
        pending, seen, active = {}, set(), None
        for row in chain:
            kind = row["type"]
            if kind in {"run_start", "run_continue", "wait_continue"}:
                _require(not pending and active is None, "new run overlaps an unclosed original run")
                active = row["run_id"]
            elif kind == "message":
                _require(row["run_id"] == active, "message escaped its active run")
                message = row["message"]
                if message["role"] == "assistant":
                    _require(not pending, "next assistant arrived before tool pairing")
                    for call in message.get("tool_calls", []):
                        ident = (active, call["id"])
                        _require(ident not in seen, "duplicate tool call ID inside run")
                        seen.add(ident)
                        pending[call["id"]] = row["entry_id"]
                elif message["role"] == "tool":
                    _require(message["tool_call_id"] in pending, "tool result missing original call or repeated")
                    del pending[message["tool_call_id"]]
                elif message["role"] == "user":
                    _require(not pending, "user arrived before all pending tool results")
            elif kind == "run_end":
                _require(row["run_id"] == active and not pending, "run ended with missing tool result")
                active = None
        _require(active is None and not pending, "unfinished original run")
        _require(chain[-1]["type"] == "run_end" and chain[-1]["status"] == "stop", "phase did not reach final stop")
        facts["paired_tool_calls_in_session_chain"] = len(seen)
    check("full_message_pairing_and_stop", message_pairing)

    def command_record(command, kind):
        row = _one([r for r in exported_state["workflow_commands"] if r["command_id"] == command["command_id"]], "command receipt")
        _require(row["kind"] == kind and row["command_hash"] == _digest(command), "command canonical identity differs")
        return _decode(row["receipt_json"])

    def audit_wait(wait):
        saved = _one([r for r in chain if r["type"] == "wait" and r["wait_id"] == wait["wait_id"]], "Journal wait")
        _require(all(saved[key] == wait[key] for key in WAIT_KEYS), "SQL and Journal wait bindings differ")
        assistant = by_id[wait["assistant_id"]]
        _require(assistant["run_id"] == wait["origin_run_id"] and assistant["message"]["role"] == "assistant", "original assistant differs")
        calls = assistant["message"]["tool_calls"]
        call = _one([c for c in calls if c["id"] == wait["call_id"]], "original model call")
        _require(call["function"]["name"] == (wait["tool_name"] if wait["kind"] == "approval" else "question"), "tool name differs")
        args = _decode(call["function"]["arguments"])
        _require(args == wait["args" if wait["kind"] == "approval" else "question"], "frozen arguments differ from original model call")
        result = by_id[wait["result_entry_id"]]
        _require(result["run_id"] == wait["origin_run_id"] and result["message"]["role"] == "tool"
                 and result["message"]["tool_call_id"] == wait["call_id"], "tool result does not pair with original call")
        resolution = _one([r for r in chain if r["type"] == "wait_resolution" and r["wait_id"] == wait["wait_id"]], "Journal resolution")
        _require(all(resolution[key] == value for key, value in wait["resolution"].items()), "SQL and Journal resolution differ")
        _require(resolution["result_entry_id"] == result["entry_id"], "resolution references other result")
        end = by_id[resolution["parent_id"]]
        _require(end["type"] == "run_end" and end["status"] == "waiting" and end["run_id"] == wait["origin_run_id"], "old run has no paired waiting end")
        continuation = _one([r for r in chain if r["type"] == "wait_continue" and r["resolution_id"] == resolution["resolution_id"]], "continuation")
        _require(continuation["parent_id"] == resolution["entry_id"] and continuation["previous_run_id"] == wait["origin_run_id"]
                 and continuation["budget_group_id"] == wait["budget_group_id"], "continuation binding differs")
        _require(pids[continuation["run_id"]] != pids[wait["origin_run_id"]], "continuation did not enter a new process")
        children = [r for r in chain if r["parent_id"] == continuation["entry_id"]]
        following = _one(children, "continuation first record")
        if wait["kind"] == "question":
            value = config["question_value"]
            _require(resolution["outcome"] == "answered" and continuation["input_kind"] == "answer", "question not resolved by actual answer")
            _require(following.get("message") == {"role": "user", "content": value}, "answer is not an exact real user message")
            answer = _one([a for a in states[-1]["answers"] if a["wait_id"] == wait["wait_id"]], "answer source")
            _require(answer["user_entry_id"] == following["entry_id"] and answer["requirement_revision"] == wait["requirement_revision"], "answer source identity differs")
            command = {"command_id": answer["command_id"], "session_id": facts["session_id"], "question_id": wait["question_id"],
                       "expected_revision": wait["requirement_revision"], "value": value}
            receipt = command_record(command, "answer")
            actual = _one([a for a in attempts if a["command"] == "answer" and (a.get("result") or {}).get("command_id") == answer["command_id"]], "actual answer process")
            _require(actual["result"] == receipt and _argument(invocations[actual["directory"]]["argv"], "--value") == value, "answer receipt or CLI value differs")
        else:
            _require(len(calls) == 1 and resolution["outcome"] == "approved_applied" and continuation["input_kind"] == "none", "approval is not exclusive and applied")
            _require(following.get("message", {}).get("role") != "user", "approval fabricated a user input")
            _require(_decode(result["message"]["content"]) == wait["result"], "SQL result and original tool differ")
        return True

    for wait in waits:
        check("wait:" + wait["wait_id"], lambda wait=wait: audit_wait(wait))

    def audit_approval(wait):
        directory = root / "approval-observations" / wait["wait_id"]
        observed = _read(directory / "wait.json")
        _require(observed["status"] == "pending" and all(observed[key] == wait[key] for key in WAIT_KEYS), "original pending observation differs")
        proposal = _read(directory / "proposal.json")
        binding = _sha(json.dumps({key: proposal[key] for key in BOUND_KEYS}, ensure_ascii=False, sort_keys=True).encode())
        _require(binding == proposal["binding"] == wait["candidate_binding"], "candidate binding differs")
        _require(proposal["run_id"] == wait["origin_run_id"] and proposal["call_id"] == wait["call_id"] and proposal["args"] == wait["args"], "proposal call identity differs")
        _require(proposal["proposal_id"] == wait["proposal_id"] and str(Path(proposal["proposal_dir"]) / "proposal.json") == wait["proposal_path"], "proposal path differs")
        before, after = (directory / "before.bin").read_bytes(), (directory / "after.bin").read_bytes()
        _require(proposal["before_sha256"] == (_sha(before) if proposal["before_exists"] else None)
                 and proposal["after_sha256"] == _sha(after) and (directory / "diff.patch").read_text() == proposal["diff"], "frozen bytes or diff mismatch")
        path, args = proposal["path"], proposal["args"]
        _require(path in FILES and path == args["path"], "candidate escaped allowed files")
        if proposal["tool_name"] == "edit":
            old, new = args["old_text"].encode(), args["new_text"].encode()
            starts = [i for i in range(len(before)) if old and before.startswith(old, i)]
            index = _one(starts, "edit match")
            _require(after == before[:index] + new + before[index + len(old):], "candidate does not apply original edit")
        else:
            _require(proposal["tool_name"] == "write" and not proposal["before_exists"] and after == args["content"].encode(), "candidate does not apply original write")
        result, decision = wait["result"], wait["decision"]
        _require(decision["status"] == "approved" and decision["candidate_binding"] == binding and decision["executed"] is False, "approval differs from candidate")
        _require(bool(wait["permit_id"]) and type(wait["permit_epoch"]) is int, "no consumed permit")
        _require(result["ok"] is True and result["commit_attempted"] is True and result["control"]["effect"] == "applied"
                 and result["version"] == _sha(after), "no confirmed exact write result")
        command = {"command_id": decision["command_id"], "session_id": facts["session_id"], "wait_id": wait["wait_id"],
                   "expected_revision": wait["requirement_revision"], "decision": "approve"}
        receipt = command_record(command, "decide")
        _require(receipt == {**decision, "exit_code": 0}, "stored decide receipt differs")
        selected = [e for e in events if e.get("call_id") == wait["call_id"] and e.get("previous_run_id") == wait["origin_run_id"]]
        start = _one([e for e in selected if e["event"] == "commit_started"], "actual commit start")
        finish = _one([e for e in selected if e["event"] == "tool_finished"], "actual write finish")
        _require(start["proposal_id"] == proposal["proposal_id"] and finish["executed"] is True and finish["result"] == result, "commit events differ from persisted result")
        applied.append((wait, path, before, after, directory))

    approvals = [wait for wait in waits if wait["kind"] == "approval"]
    for wait in approvals:
        check("candidate:" + wait["wait_id"], lambda wait=wait: audit_approval(wait))

    def file_chain():
        _require(bool(approvals) and len(applied) == len(approvals), "missing approved mutation evidence")
        current = dict(before_files)
        for wait, path, before, after, directory in applied:
            _require(current[path] == before, "candidate did not use preceding actual file bytes")
            _require(all((directory / "workspace-before" / name).read_bytes() == current[name] for name in FILES), "preapproval workspace changed without prior confirmed candidate")
            current[path] = after
        _require(current == after_files and set(current) == set(FILES), "final files do not match ordered confirmed candidates")
        _require(all(before_files[name] != after_files[name] for name in FILES), "both implementation and tests were not changed")
        facts["confirmed_candidates"] = [{"wait_id": w["wait_id"], "path": p, "permit_id": w["permit_id"]} for w, p, *_ in applied]
    check("ordered_file_effects", file_chain)

    def budget():
        groups = {wait["budget_group_id"] for wait in waits}
        _require(len(groups) == 1, "planned waits changed logical budget")
        group = _one([g for g in exported_state["budget_groups"] if g["budget_group_id"] in groups], "logical budget")
        reservations = [r for r in exported_state["request_reservations"] if r["budget_group_id"] in groups]
        _require(len(reservations) == group["model_calls"] <= group["max_turns"] == config["maximum_model_requests"], "request budget differs")
        _require(sum(r["purpose"] == "compaction" for r in reservations) == group["summary_calls"] <= 2, "summary budget reset")
        _require(0 <= group["action_count"] <= group["max_actions"] and all(w["expires_at"] == group["deadline_unix"] for w in waits), "action budget or absolute deadline differs")
        facts["budget"] = group
    check("persistent_budget", budget)

    def interrupted_decision():
        attempt = _one([a for a in attempts if a["command"] == "decide" and a.get("signal")], "postdecision kill")
        signal = attempt["signal"]
        marker = signal["marker"]
        _require(attempt["exit_code"] == -9 and signal["signal"] == "SIGKILL" and signal["pid"] == marker["pid"] == attempt["pid"]
                 and marker["boundary"] == "after_decision_commit" and marker["committed"] is True, "actual committed decision kill not observed")
        wait = _one([w for w in approvals if w["wait_id"] == marker["wait_id"]], "killed approval")
        directory = root / "approval-observations" / wait["wait_id"]
        state = _read(directory / "after-kill-state.json")
        row = _one([r for r in state["workflow_waits"] if r["wait_id"] == wait["wait_id"]], "postkill SQL approval")
        _require(row["status"] == "approved" and row["permit_id"] is None and row["result_json"] is None
                 and _decode(row["decision_json"])["decision_id"] == marker["decision_id"], "postkill approval already consumed or lost")
        journal = _lines(directory / "after-kill-session.jsonl")
        _require(journal[-1]["type"] == "wait" and journal[-1]["wait_id"] == wait["wait_id"], "postkill Journal changed beyond original wait")
        _require(all((directory / "workspace-before" / name).read_bytes() == (directory / "workspace-after-kill" / name).read_bytes() for name in FILES), "decide caused file mutation")
        replay = _one([a for a in attempts if a.get("control_kind") == "approval_replay"], "same-command replay")
        receipt = _decode(_one([r for r in state["workflow_commands"] if r["command_id"] == marker["command_id"]], "committed command")["receipt_json"])
        _require(replay["pid"] != attempt["pid"] and replay["result"] == receipt and replay["exit_code"] == 0, "replay did not return original committed receipt")
        _require(_argument(invocations[replay["directory"]]["argv"], "--command-id") == marker["command_id"], "replay command identity changed")
    if phase in {"A", "B"}:
        check("committed_decision_survives_kill", interrupted_decision)
        check("one_real_answer", lambda: _require(len([w for w in waits if w["kind"] == "question"]) == 1, "exactly one actual question required"))

    def change_chain():
        before_entries = _lines(root / "before-session.jsonl")
        _require(entries[:len(before_entries)] == before_entries and before_entries[-1]["type"] == "run_end" and before_entries[-1]["status"] == "stop", "C does not preserve successful A Journal prefix")
        old_export = _read(root / "before-state.json")
        original = _states(old_export, _chain(before_entries))[-1]
        for row in old_export["workflow_states"]:
            _require(_one([r for r in exported_state["workflow_states"] if r["state_id"] == row["state_id"]], "historical state") == row, "C rewrote old state")
        change_text = (root / "change.json").read_text()
        change = _decode(change_text)
        _require(change["separator"] == ";" and change["expected_requirement_revision"] == original["requirements"]["revision"], "change targets wrong revision")
        revised = next(s for s in states if s["requirements"]["revision"] == original["requirements"]["revision"] + 1)
        steps = original["plan"]["steps"]
        affected = {s["id"] for s in steps if {"kind": "requirement", "key": "separator"} in s["input_refs"]}
        while True:
            updated = affected | {s["id"] for s in steps if set(s["depends_on"]) & affected}
            if updated == affected:
                break
            affected = updated
        _require(bool(affected), "no separator dependency to invalidate")
        changed_steps = {s["id"]: s for s in revised["plan"]["steps"]}
        for step in steps:
            expected = {**step, "valid": False, "invalid_reason": "separator_changed"} if step["id"] in affected else step
            _require(changed_steps[step["id"]] == expected, "requirement change invalidated wrong step")
        _require(states[-1]["answers"] == original["answers"] and revised["baseline"] == original["baseline"], "C changed original answer or baseline")
        input_row = _one([r for r in entries[len(before_entries):] if r.get("message") == {"role": "user", "content": change_text}], "actual change user input")
        _require(by_id[input_row["parent_id"]]["type"] == "run_start", "fresh requirement input is not a fresh task")
        actual = _one([a for a in attempts if a["command"] == "revise"], "actual revise process")
        _require(actual["result"] == command_record({**change, "input_text": change_text}, "revise"), "revise receipt differs")
        old_groups = {g["budget_group_id"] for g in old_export["budget_groups"]}
        new_groups = {g["budget_group_id"] for g in exported_state["budget_groups"]} - old_groups
        _require(len(new_groups) == 1 and {w["budget_group_id"] for w in waits} == new_groups, "fresh C task has wrong budget group")
        _require(not any(w["kind"] == "question" for w in waits), "C asked duplicate choice again")
        facts["invalidated_steps"] = sorted(affected)
    if phase == "C":
        check("real_requirement_change", change_chain)
    report["coverage_complete"] = bool(coverage) and all(coverage.values()) and not report["errors"]
    return report


MATERIALS=Path(__file__).resolve().parent
LIMITS={"A":10,"B":10,"C":8}
FILES={"validator.py","test_validator.py"}
NORMAL_MAX_TOKENS=1536


def require(condition,message):
    if not condition:raise AssertionError(message)


def read(path):
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,"JSON重复字段");result[key]=value
        return result
    def invalid(value):raise ValueError("JSON非有限数值："+value)
    return json.loads(path.read_text(),object_pairs_hook=pairs,parse_constant=invalid)


def lines(path):
    raw=path.read_bytes();require(not raw or raw.endswith(b"\n"),"JSONL尾部残缺")
    return [json.loads(line) for line in raw.split(b"\n") if line]


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()

def load_previous():
    path=MATERIALS.parents[2]/"week-08/practices/PRA-W08/inspect_evidence.py"
    spec=importlib.util.spec_from_file_location("w09_previous_inspector",path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def check_pairs(messages):
    require(messages and messages[0]["role"]=="system","缺少system")
    state="user";pending=[]
    for message in messages[1:]:
        role=message["role"]
        if role=="user":
            require(not pending and state in {"user","assistant"},"user打断未配对工具")
            state="assistant"
        elif role=="assistant":
            require(state=="assistant" and not pending,"assistant位置错误")
            calls=message.get("tool_calls",[])
            if calls:
                pending=[call["id"] for call in calls]
                require(len(set(pending))==len(pending) and all(isinstance(i,str) and i.strip() for i in pending),"调用身份重复")
                state="tool"
            else:
                require(isinstance(message.get("content"),str) and message["content"].strip(),"最终文本为空")
                state="user"
        else:
            require(role=="tool" and state=="tool" and pending and message["tool_call_id"]==pending[0],"工具身份或配对顺序不同")
            pending.pop(0)
            if not pending:state="assistant"
    require(state=="assistant" and not pending,"请求未停在可继续位置")


def source_context(messages,index,state,head,files,baseline):
    wrapped=json.loads(messages[1]["content"])
    resources=[item for item in wrapped["context"] if item["kind"]!="summary"]
    require([item["kind"] for item in resources]==["baseline_contract","workflow_state"],"上下文来源扩展或遗漏")
    contract,workflow=resources
    raw=baseline.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    require(contract["content"]==raw.decode() and contract["source"]=="baseline:"+sha and contract["version"]==sha,"基线合同内容或身份不同")
    rows=[row for row in state["workflow_states"] if row["state_id"]==workflow["source"]]
    require(len(rows)==1,"当前请求没有唯一持久计划来源")
    saved=json.loads(rows[0]["state_json"])
    require(workflow["version"]==str(saved["revision"]) and rows[0]["anchor_id"] in index,"请求计划来源版本或锚点不同")
    ancestors=set();at=head
    while at is not None:
        ancestors.add(at);at=index[at]["parent_id"]
    require(rows[0]["anchor_id"] in ancestors,"计划来源跨分支或来自未来")
    content=workflow["content"]
    expected_plan=deepcopy(saved["plan"])
    if expected_plan:
        for step in expected_plan["steps"]:step.pop("checks",None)
    expected_question=saved["question"] if saved["question"] and saved["question"]["status"]=="pending" else None
    expected_answers=[{key:answer[key] for key in ("question_id","value","requirement_revision","user_entry_id","depends_on")}
        for answer in saved["answers"] if answer.get("user_entry_id") in content["facts"] and content["facts"][answer["user_entry_id"]]["kind"]=="answer_bound"]
    expected_changes=[]
    for change in saved["changes"]:
        if change["requirement_revision"]!=saved["requirements"]["revision"]:continue
        matching=[ident for ident in ancestors if index[ident].get("message")=={"role":"user","content":change["input_text"]}]
        require(len(matching)==1,"当前要求修改没有唯一真实user来源")
        expected_changes.append({**{key:change[key] for key in ("decision_id","previous_requirement_revision","requirement_revision","separator","invalidated_steps")},"user_entry_id":matching[0]})
    expected={"original_input":saved["original_input"],"requirements":saved["requirements"],"plan":expected_plan,
        "question":expected_question,"answers":expected_answers,"changes":expected_changes}
    if isinstance(content.get("original_input"),dict):
        source=content["original_input"].get("source_id")
        require(set(content["original_input"])=={"source_id"} and source in ancestors and index[source].get("message")=={"role":"user","content":saved["original_input"]},"原输入引用不符")
        visible=[wrapped["current_user_content"],*[m["content"] for m in messages[2:] if m["role"]=="user"]]
        require(saved["original_input"] in visible,"原输入已不可见却省略正文")
        expected["original_input"]={"source_id":source}
    require(set(content)==set(expected)|{"control","facts"},"workflow_state投影字段变化")
    for key,value in expected.items():require(content[key]==value,"请求中的计划、答案或要求与持久来源不同："+key)
    require(content["control"]["budget_group_id"]==saved["budget_group_id"] and content["control"]["cancelled"] is False,"当前控制范围或取消状态不同")
    require(contract["path"]==saved["baseline"]["path"] and saved["baseline"]["version"]==sha,"基线安装来源不同")
    for ident,fact in content["facts"].items():
        require(fact.get("valid") is True,"当前事实目录含失效来源")
        if ident==contract["source"]:
            require(fact=={"kind":"input_visible","source_kind":"baseline_contract","version":sha,"valid":True},"基线事实被替换")
            continue
        require(ident in index and ident in ancestors,"事实来源不在当前父链")
        entry=index[ident];message=entry.get("message",{})
        if fact["kind"]=="answer_bound":
            require(message=={"role":"user","content":fact["value"]} and fact["value"]==content["requirements"]["duplicates"],"答案来源不是原始用户输入")
            require(any(a.get("user_entry_id")==ident and a["value"]==fact["value"] for a in saved["answers"]),"答案未关联原问题")
        else:
            require(ident in saved["observations"] and message.get("role")=="tool","事实不是宿主持久观察")
            original=saved["observations"][ident]
            require({k:v for k,v in fact.items() if k!="valid"}==original and digest(json.loads(message["content"]))==fact["result_digest"],"事实内容与原tool不同")
            if fact["valid"]:
                require(fact["requirement_revision"]==content["requirements"]["revision"],"旧需求事实冒充当前事实")
                if fact["kind"] in {"input_visible","file_applied"}:
                    require(fact["path"] in files and hashlib.sha256(files[fact["path"]]).hexdigest()==fact["version"],"当前文件事实版本已经失效")
                elif fact["kind"]=="check_passed":
                    require(fact["versions"]=={k:hashlib.sha256(v).hexdigest() for k,v in files.items()},"检查事实对应旧文件")
                    result=read(Path(fact["artifact_dir"])/"result.json")
                    require(all(result.get(k) is True for k in ("ok","fixed_checks_passed","new_tests_ran","cleanup_confirmed")),"未通过或未收尾的检查被列为有效")
    return resources


def workflow_projector(previous, version="workflow-tool-view-v1"):
    original_projector=previous.projected_messages
    def project(index,head,resources,cap,old,source_rules):
        result=original_projector(index,head,resources,cap,old,source_rules)
        if not any(r["kind"]=="workflow_state" for r in resources):return result
        if version in {"workflow-tool-view-v3","workflow-tool-view-v4","workflow-tool-view-v5","workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"}:
            envelope=old.decode(result["messages"][1]["content"])
            if version=="workflow-tool-view-v9" and any(item["kind"]=="workflow_state" and {"original_input","requirements","plan","facts","answers","changes"}<=set(item["content"]) for item in envelope["context"]):
                envelope["context"]=[item for item in envelope["context"] if item["kind"]!="summary"]
                result["context_sources"]=[item for item in result["context_sources"] if item["kind"]!="summary"]
                if result["compaction_id"]:
                    result["summary_view"]={"source_id":result["compaction_id"],"omitted":True,"reason":"current_workflow_state_retained"}
            for item in envelope["context"]:
                if item["kind"]!="summary":continue
                summary=item["content"];roles={}
                for key in ("goal","constraints","pending","unknowns","observations"):
                    for row in summary.get(key,[]):
                        for source in row.pop("sources"):roles[source["entry_id"]]=source["role"]
                for row in summary["completed"]:
                    roles[row["source_id"]]="tool"
                    for key in ("run_id","tool_call_id","observed_at"):row.pop(key)
                summary["source_roles"]=roles
            result["messages"][1]["content"]=previous.encoded(envelope)
            selected=result["source_ids"];entries=[index[ident] for ident in selected[1:]]
            latest=max((i for i,row in enumerate(entries) if row.get("message",{}).get("role")=="assistant"),default=-1)
            omitted=[];omitted_plans=[];omitted_questions=[];omitted_reads=[];latest_mutations={}
            answered={a["question_id"] for r in resources if r["kind"]=="workflow_state" for a in r["content"].get("answers",[])}
            if version in {"workflow-tool-view-v5","workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"}:
                for i,row in enumerate(entries):
                    batch=row.get("message",{}).get("tool_calls",[])
                    if any(c["function"]["name"] in {"edit","write"} for c in batch):
                        for result_entry in entries[i+1:i+1+len(batch)]:
                            value=old.decode(result_entry["message"]["content"])
                            if value.get("ok") is True and value.get("control",{}).get("effect")=="applied":latest_mutations[value.get("path")]=i
            for i,row in enumerate(entries):
                batch=row.get("message",{}).get("tool_calls",[])
                if version in {"workflow-tool-view-v4","workflow-tool-view-v5","workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"} and i<latest and batch and all(c["function"]["name"]=="plan" for c in batch):
                    group=entries[i:i+1+len(batch)]
                    if len(group)==len(batch)+1 and all(old.decode(r["message"]["content"]).get("ok") is True for r in group[1:]):
                        omitted_plans.extend(r["entry_id"] for r in group)
                if version in {"workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"} and i<latest and batch and any(c["function"]["name"]=="question" for c in batch) and all(c["function"]["name"] in {"plan","question"} for c in batch):
                    group=entries[i:i+1+len(batch)]
                    values=[old.decode(r["message"]["content"]) for r in group[1:]]
                    if len(group)==len(batch)+1 and all(v.get("ok") is True and (c["function"]["name"]!="question" or v.get("question_id") in answered) for c,v in zip(batch,values)):
                        omitted_questions.extend(r["entry_id"] for r in group)
                if version in {"workflow-tool-view-v8","workflow-tool-view-v9"} and i<latest and batch and all(c["function"]["name"]=="read" for c in batch):
                    group=entries[i:i+1+len(batch)];values=[old.decode(r["message"]["content"]) for r in group[1:]]
                    if len(group)==len(batch)+1 and all(v.get("ok") is True and latest_mutations.get(v.get("path"),-1)>i for v in values):omitted_reads.extend(r["entry_id"] for r in group)
                if i>=latest or i in latest_mutations.values() or not batch or any(c["function"]["name"] not in {"edit","write"} for c in batch):continue
                group=entries[i:i+1+len(batch)]
                if len(group)!=len(batch)+1:continue
                results=[old.decode(r["message"]["content"]) for r in group[1:]]
                if all(r.get("ok") is True and r.get("commit_attempted") is True and r.get("control",{}).get("effect")=="applied" for r in results):
                    omitted.extend(r["entry_id"] for r in group)
            if version in {"workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"}:
                candidates=[i for i,ident in enumerate(selected) if ident not in omitted+omitted_plans+omitted_questions+omitted_reads]
                for left,right in zip(candidates,candidates[1:]):
                    if result["messages"][left]["role"]==result["messages"][right]["role"]=="user":
                        boundary=max(i for i in range(left+1,right) if result["messages"][i]["role"]=="assistant")
                        retained=set(selected[boundary:right])
                        omitted=[i for i in omitted if i not in retained];omitted_plans=[i for i in omitted_plans if i not in retained];omitted_questions=[i for i in omitted_questions if i not in retained];omitted_reads=[i for i in omitted_reads if i not in retained]
            positions=[i for i,ident in enumerate(selected) if ident not in omitted and ident not in omitted_plans and ident not in omitted_questions and ident not in omitted_reads];remap={old:new for new,old in enumerate(positions)}
            result["messages"]=[result["messages"][i] for i in positions]
            result["source_ids"]=[selected[i] for i in positions]
            result["derived"]=[{**r,"index":remap[r["index"]]} for r in result["derived"] if r["index"] in remap]
            result["omitted"].extend({"source_id":ident,"reason":"superseded_read_group"} for ident in omitted_reads)
            result["omitted"].extend({"source_id":ident,"reason":"resolved_question_group"} for ident in omitted_questions)
            result["omitted"].extend({"source_id":ident,"reason":"completed_plan_group"} for ident in omitted_plans)
            result["omitted"].extend({"source_id":ident,"reason":"completed_mutation_group"} for ident in omitted)
        calls=previous.tool_sources(old.path_entries(index,head))
        # Match the original projection order: all transformations for one message,
        # then the system and user context envelopes.
        original_derived=result["derived"];derived=[]
        for position,(message,ident) in enumerate(zip(result["messages"],result["source_ids"])):
            if ident not in calls:continue
            derived.extend(r for r in original_derived if r["index"]==position and r["kind"] not in {"source_rules","user_context"})
            payload=old.decode(message["content"])
            if not isinstance(payload,dict):continue
            name=calls[ident]["function"]["name"];omitted=[]
            if (name in {"edit","write"} and payload.get("ok") is True and payload.get("commit_attempted") is True
                    and payload.get("control",{}).get("effect")=="applied" and "diff" in payload):
                del payload["diff"];omitted.append("diff")
            if name=="plan" and payload.get("ok") is True and isinstance(payload.get("steps"),list):
                for step in payload["steps"]:step.pop("checks",None)
                omitted.append("steps.checks")
            if (name=="shell" and payload.get("exit_code")==0 and payload.get("cancelled") is False
                    and payload.get("timed_out") is False and all(payload.get(k) is True for k in
                    ("ok","fixed_checks_passed","new_tests_ran","cleanup_confirmed"))):
                for key in ("stdout","stderr"):
                    if key in payload:del payload[key];omitted.append(key)
                if "path" in payload.get("workflow_contract",{}):
                    del payload["workflow_contract"]["path"];omitted.append("workflow_contract.path")
            pairs=version in {"workflow-tool-view-v2","workflow-tool-view-v3","workflow-tool-view-v4","workflow-tool-view-v5","workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"} and name=="read" and payload.get("ok") is True and isinstance(payload.get("lines"),list)
            if pairs:payload["lines"]=[[row["line"],row["text"]] for row in payload["lines"]]
            if omitted or pairs:
                payload["view"]={"source_id":ident,"omitted":omitted}
                if pairs:payload["view"]["line_format"]="line-text-pairs"
            encoded=previous.encoded(payload)
            if encoded!=message["content"]:
                message["content"]=encoded
                derived.append({"index":position,"source_id":ident,"kind":"workflow_tool_view" if omitted or pairs else "json_encoding"})
        derived.extend(r for r in original_derived if r["kind"] in {"source_rules","user_context"})
        result["derived"]=derived
        return result
    return project


def inspect(batch,*,evidence_source="live-external-api"):
    prior=load_previous();previous=prior.load_previous();stream=previous.load_previous();old=stream.load_previous();w03=old.load_w03_inspector();old.check_pairs=check_pairs
    experiment=read(batch/"experiment.json");rows=read(batch/"summary.json")
    projection_version=experiment.get("request_projection")
    require((projection_version,experiment["normal_max_tokens"]) in {(None,768),("workflow-tool-view-v1",NORMAL_MAX_TOKENS),("workflow-tool-view-v2",NORMAL_MAX_TOKENS),("workflow-tool-view-v3",NORMAL_MAX_TOKENS),("workflow-tool-view-v4",NORMAL_MAX_TOKENS),("workflow-tool-view-v5",NORMAL_MAX_TOKENS),("workflow-tool-view-v6",NORMAL_MAX_TOKENS),("workflow-tool-view-v7",NORMAL_MAX_TOKENS),("workflow-tool-view-v8",NORMAL_MAX_TOKENS),("workflow-tool-view-v9",NORMAL_MAX_TOKENS)},"未知请求投影或输出配置")
    normal_max_tokens=experiment["normal_max_tokens"]
    if projection_version is not None:previous.projected_messages=workflow_projector(previous,projection_version)
    limits=experiment["limits"]
    require(isinstance(limits,dict) and set(limits)==set(LIMITS)
            and all(type(limits[p]) is int and 1<=limits[p]<=LIMITS[p] for p in LIMITS),"阶段预算无效或超过规格上限")
    maximum=sum(limits.values())
    require(experiment["evidence_source"]==evidence_source and type(experiment["maximum_model_requests"]) is int and experiment["maximum_model_requests"]==maximum and maximum<=28 and experiment["context_tokens"]==8192 and experiment["automatic_retries"]==0,"采集来源或固定预算不同")
    require([r["phase"] for r in rows]==list(LIMITS)[:len(rows)],"阶段乱序或重复")
    require((batch/"inputs/question-a.md").read_bytes()==(batch/"inputs/question-b.md").read_bytes(),"A/B初始输入不同")
    for name in ("worker","runner"):
        require((batch/f"runtime-{name}.py").read_bytes()==(MATERIALS/f"runtime/{name}.py").read_bytes(),"Docker供给版本不同")
    if len(rows)!=3 or any(row["status"]!="stop" for row in rows):
        counts={phase:len(list((batch/phase).glob("attempt-*/runs/*/turn-*/request.json"))) for phase in LIMITS}
        require(all(counts[p]<=limits[p] for p in LIMITS) and sum(counts.values())<=maximum,"未完成批次也必须遵守预算")
        return {"coverage_complete":False,"coverage":"not_covered","actual_model_calls":None,"observed_model_calls":sum(counts.values()),"usage":prior.usage_total([]),"semantic_verdict":"unevaluated"}
    outcomes=[];all_usages=[];a_after=None;source_rules=None
    for row in rows:
        phase=row["phase"];directory=batch/phase;before=prior.snapshot(directory/"before");after=prior.snapshot(directory/"after")
        config=read(directory/"phase-config.json");entries=lines(directory/"after-session.jsonl");index={r["entry_id"]:r for r in entries}
        require(len(index)==len(entries) and entries[0]["type"]=="header","Journal重复或缺少header")
        for position,entry in enumerate(entries):require(entry["parent_id"]==(entries[position-1]["entry_id"] if position else None),"Journal父链不连续")
        require(config["phase"]==phase and config["workspace"]==entries[0]["workspace"],"阶段或工作区绑定不同")
        if phase!="C":
            require(before=={n:(batch/"inputs/sample_project"/n).read_bytes() for n in FILES},"A/B初始文件不同")
            first_user=next(r["message"] for r in entries if r.get("message",{}).get("role")=="user")
            require(first_user["content"]==(batch/"inputs"/f"question-{phase.lower()}.md").read_text(),"初始任务没有使用实际输入原文")
        else:
            require(before==a_after and config["reuse_from"]=="A","C没有继续A的实际文件")
            require((directory/"before-session.jsonl").read_bytes()==(batch/"A/after-session.jsonl").read_bytes(),"C之前的Journal不等于A结束状态")
            hints=read(directory/"change-line-hints.json")
            expected_ranges={name:[[j1+1,j2] for tag,i1,i2,j1,j2 in difflib.SequenceMatcher(None,(batch/"A/before"/name).read_text().splitlines(),before[name].decode().splitlines()).get_opcodes() if tag!="equal" and j2>j1] for name in FILES}
            require(hints=={"source":"A-before-and-current-file-diff","ranges":expected_ranges},"C行号提示没有来自A实际文件差异")
        state=read(directory/"state-export.json")
        if phase in {"B","C"}:
            require(prior.database(Path(config["state_db"]))==state,"真实数据库与阶段导出不同")
            require(Path(config["session"]).read_bytes()==(directory/"after-session.jsonl").read_bytes(),"真实Journal与末尾快照不同")
            require(after=={name:(Path(config["workspace"])/name).read_bytes() for name in FILES},"末尾文件快照不同于实际工作区")
        if phase=="C":require(read(directory/"before-state.json")==read(batch/"A/state-export.json"),"C进入前数据库不等于A末尾状态")
        files=dict(before);usages=[];requests={};shells=[];compactions=[];all_events=[]
        attempt_results=[]
        for number,attempt in enumerate(row["attempts"],1):
            attempt_dir=directory/f"attempt-{number:03d}"
            require(Path(attempt["directory"])==attempt_dir,"CLI证据引用其他目录")
            invocation=read(attempt_dir/"invocation.json");supervision=read(attempt_dir/"supervision.json")
            require(invocation["evidence_source"]==evidence_source and invocation["pid"]==attempt["pid"] and supervision["stop_reason"] is None,"采集身份或监督状态不同")
            require(int((attempt_dir/"exit-code.txt").read_text())==attempt["exit_code"],"实际退出码与摘要不同")
            result=None if attempt["signal"] else read(attempt_dir/"stdout.json")
            require(result==attempt["result"],"stdout与采集摘要不同")
            if attempt["signal"]:
                signal=read(attempt_dir/"signal.json")
                require(signal==attempt["signal"]==supervision["signal"] and signal["pid"]==attempt["pid"] and signal["process_identity"]==invocation["process_identity"],"kill记录身份或来源不同")
                require(read(Path(signal["marker_path"]))==signal["marker"],"kill没有对应实际门闩原记录")
            for cleanup_file,pattern in (("controller-worker-cleanup.json","**/worker.json"),("controller-read-cleanup.json","runs/*/reads-*/read-*/spawn.json")):
                cleanups=read(attempt_dir/cleanup_file)
                require({Path(c["source"]) for c in cleanups}==set(attempt_dir.glob(pattern)),"worker清理观察缺失")
                for cleanup in cleanups:
                    metadata=read(Path(cleanup["source"]))
                    require(cleanup["cleanup_confirmed"] and cleanup["identity_after"]!=cleanup["process_identity"] and all(cleanup[k]==metadata[k] for k in ("pid","process_identity")),"worker身份或收尾不同")
            runs=list((attempt_dir/"runs").glob("run-*"));count=0
            require(len(runs)==(1 if attempt["command"] in {"new","continue"} else 0),"控制命令运行了模型或任务命令缺少日志")
            for run in runs:
                events=lines(run/"events.jsonl");all_events+=events
                require([e["seq"] for e in events]==list(range(1,len(events)+1)) and events[0]["event"]=="run_started","事件前缀缺号")
                require(all(e["run_id"]==run.name and e["session_id"]==entries[0]["session_id"] and e["evidence_source"]==evidence_source for e in events),"事件身份来源不同")
                require(result is not None and result["evidence_source"]==evidence_source and read(run/"result.json")==result and events[-1]["event"]=="run_finished" and events[-1]["status"]==result["status"],"运行终态与实际stdout不同")
                assistants=iter(r for r in entries if r.get("run_id")==run.name and r["type"]=="message" and r["message"]["role"]=="assistant")
                for event in events:
                    if event["event"]=="tool_finished" and event.get("tool_name") in {"edit","write"} and event["result"].get("ok") and event["result"].get("control",{}).get("effect")=="applied":
                        event={**event,**event["result"]}
                        origins=list(directory.glob("approval-observations/*/proposal.json"));origins=[p for p in origins if read(p)["proposal_id"]==event["proposal_id"]]
                        require(len(origins)==1,"实际提交没有唯一审批观察")
                        proposal=read(origins[0]);data=(origins[0].parent/"after.bin").read_bytes()
                        require(files[proposal["path"]]==(origins[0].parent/"before.bin").read_bytes() and event["bytes_written"]==len(data),"提交前字节或实际字节数不同")
                        files[proposal["path"]]=data
                    if event["event"]=="tool_finished" and event.get("tool_name")=="read" and event["result"].get("ok"):
                        observed=event["result"];name=observed["path"]
                        require(name in files and hashlib.sha256(files[name]).hexdigest()==observed["version"],"read版本不对应当前实际文件")
                        source=old.source_lines(files[name]);offset=observed["offset"];limit=observed["limit"]
                        require(observed["lines"]==[{"line":i+1,"text":source[i]} for i in range(offset-1,min(len(source),offset-1+limit))],"read行号或正文不同于原文件")
                        if observed.get("artifact_dir"):
                            artifact=Path(observed["artifact_dir"])
                            require((artifact/"input.bin").read_bytes()==files[name],"read worker实际快照与来源不同")
                            clean=read(artifact/"cleanup.json");spawn=read(artifact/"spawn.json")
                            require(clean["cleanup_confirmed"] and clean["identity"]==spawn["identity"] and clean["pid"]==spawn["pid"] and type(clean["returncode"]) is int,"read worker没有确认收尾")
                    if event["event"]!="model_requested":continue
                    count+=1;turn=event["turn"];path=run/f"turn-{turn:03d}";request=read(path/"request.json");manifest=read(run/f"projection-{turn:03d}.json")
                    require(turn not in requests and manifest["request_index"]==turn and manifest["run_id"]==run.name and manifest["session_id"]==entries[0]["session_id"],"请求编号或归属不同")
                    purpose=manifest["purpose"];require(purpose==event["purpose"] and purpose in {"task","compaction"},"请求用途不同")
                    require(request["stream"] is True and request["stream_options"]=={"include_usage":True} and request["thinking"]=={"type":"disabled"} and request["temperature"]==0,"实际模型参数不同")
                    budget=previous.budget_for(request,32768 if purpose=="compaction" else 8192)
                    require(budget["fits"] and budget==manifest["budget"] and manifest["messages"]==request["messages"],"完整请求超预算或投影不同")
                    rebuilt,receipt=previous.read_response(path,stream,old)
                    require(receipt["purpose"]==purpose and receipt["request_index"]==turn and receipt["budget"]==budget,"流回执用途或预算不同")
                    requests[turn]=(request,receipt,manifest);usages.append(receipt["usage"])
                    if purpose=="task":
                        assistant=next(assistants,None)
                        require(assistant is not None and assistant["message"]==rebuilt["message"] and assistant["parent_id"]==manifest["source_head_id"],"模型原响应与Journal不同")
                        resources=source_context(request["messages"],index,state,manifest["source_head_id"],files,batch/"inputs/baseline-contract.json")
                        original=entries[0]["system"]["content"];actual=request["messages"][0]["content"]
                        require(actual.startswith(original),"宿主规则被替换")
                        suffix=actual[len(original):]
                        if source_rules is None:source_rules=suffix
                        require(suffix==source_rules and bool(suffix),"来源规则发生变化")
                        expected=previous.projected_messages(index,manifest["source_head_id"],resources,80,old,source_rules)
                        require(all(manifest.get(k)==v for k,v in expected.items()),"实际请求不是原消息父链派生")
                        old.check_pairs(request["messages"])
                        require(request["max_tokens"]==normal_max_tokens,"普通输出预算不同")
                        view=resources[1]["content"]
                        if "remaining_model_requests_including_this" in view["control"]:
                            require(view["control"]["remaining_model_requests_including_this"]==limits[phase]-turn+1,"剩余模型额度与原账本不符")
                        expected_tools={"read","edit","shell","plan"}
                        if projection_version is None:
                            expected_tools|={"write","question"}
                        else:
                            if view["requirements"]["duplicates"] is None:expected_tools.add("question")
                            if any(name not in files for name in FILES):expected_tools.add("write")
                        facts=[fact for fact in view["facts"].values() if fact["valid"]]
                        report_ready=(view["plan"] and all(step["valid"] for step in view["plan"]["steps"])
                            and {f.get("path") for f in facts if f["kind"]=="file_applied"}==set(FILES)
                            and any(f["kind"]=="check_passed" for f in facts))
                        if projection_version in {"workflow-tool-view-v6","workflow-tool-view-v7","workflow-tool-view-v8","workflow-tool-view-v9"} and report_ready:expected_tools=set()
                        names=[t["function"]["name"] for t in request.get("tools",[])]
                        require(len(names)==len(expected_tools) and set(names)==expected_tools,"工具范围不同")
                        require(request.get("tool_choice")==("auto" if names else None),"工具选择模式不同")
                        for tool in request.get("tools",[]):
                            props=tool["function"]["parameters"]["properties"]
                            if tool["function"]["name"] in {"read","edit","write"}:require(set(props["path"]["enum"])==FILES,"文件范围扩大")
                            if tool["function"]["name"]=="shell":require(props["command"]["enum"]==["check"],"命令范围扩大")
                    else:
                        overflow=read(run/f"context-candidate-{turn:03d}.json")
                        resources=source_context(overflow["messages"],index,state,overflow["source_head_id"],files,batch/"inputs/baseline-contract.json")
                        compactions.append(previous.verify_summary(path,manifest,index,run,events,rebuilt,old,resources,source_rules,8192))
                require(next(assistants,None) is None,"存在没有原响应的assistant")
                require(len(list(run.glob("turn-*/request.json")))==count,"原请求数与事件不同")
                for event in events:
                    if event["event"]=="tool_finished" and event["tool_name"]=="shell":
                        shell=w03.inspect_shell(event,run);result_check=event["result"];contract=result_check["workflow_contract"]
                        visible=sum(len(result_check[k].encode()) for k in ("stdout","stderr"))
                        require(visible<=768,"模型可见检查日志超过768字节")
                        for output in ("stdout","stderr"):
                            raw_output=(Path(result_check["artifact_dir"])/(output+".bin")).read_bytes()
                            require(raw_output.decode("utf-8",errors="replace").startswith(result_check[output]),"模型可见日志不是实际原始输出前缀")
                        if sum(result_check["observed_bytes"].values())>visible:require(result_check["truncated"] is True,"日志截断没有明确标记")
                        trusted=read(Path(contract["path"]))
                        require(hashlib.sha256(Path(contract["path"]).read_bytes()).hexdigest()==contract["sha256"] and trusted["baseline_contract"]==read(batch/"inputs/baseline-contract.json"),"检查合同字节或基线不同")
                        require(trusted["separator"]==(";" if phase=="C" else ",") and trusted["duplicates"]==("unique" if phase=="B" else "keep") and trusted["requirement_revision"]==(2 if phase=="C" else 1),"Docker没有使用当前人类条件")
                        container=read(Path(result_check["artifact_dir"])/"container.json")
                        require(container["stopped_confirmed"] is True and container["removed_confirmed"] is True,"Docker未确认停止并删除")
                        require(any(m["Destination"]=="/contract" and not m["RW"] for m in container["created"]["Mounts"]),"合同未只读挂载")
                        shells.append(shell)
            require(count==attempt["actual_model_calls"]==supervision["model_requests_observed"],"每次进程请求数不同")
        require(files==after and shells and shells[-1]["check_passed"] and shells[-1]["inputs"]==after,"最终文件、真实检查与批准效果不同")
        group_ids={m["budget_group_id"] for _,_,m in requests.values()};require(len(group_ids)==1,"计划等待重置逻辑预算")
        group_id=next(iter(group_ids));groups=[g for g in state["budget_groups"] if g["budget_group_id"]==group_id];require(len(groups)==1,"逻辑预算记录不唯一")
        group=groups[0];require(sorted(requests)==list(range(1,len(requests)+1)) and len(requests)==group["model_calls"]==row["actual_model_calls"]<=limits[phase],"请求占用重置或漏计")
        require(group["max_turns"]==limits[phase] and group["max_actions"]==16 and group["summary_calls"]==len(compactions)<=2,"等待增加预算或摘要次数")
        reservations=[r for r in state["request_reservations"] if r["budget_group_id"]==group_id]
        require([r["request_index"] for r in reservations]==list(requests),"SQL请求计数不同")
        for reservation in reservations:
            receipt=requests[reservation["request_index"]][1]
            require(reservation["sent"]=="true" and json.loads(reservation["usage_json"])==receipt["usage"],"SQL用量/发送与原流不同")
        last=row["attempts"][-1]["result"];require(last["usage"]==prior.usage_total(usages),"逻辑累计用量不同")
        final=next(r for r in reversed(entries) if r.get("message",{}).get("role")=="assistant")
        require(last["text"]==final["message"].get("content"),"最终答复没有来自实际响应")
        audit=audit_workflow(directory,entries,state,row["attempts"],before,after)
        require(not audit["errors"],"持久工作流核对失败："+str(audit["errors"]))
        outcomes.append({"phase":phase,"coverage":"hit" if audit["coverage_complete"] else "not_covered","actual_model_calls":len(requests),"summary_calls":len(compactions),"usage":prior.usage_total(usages),"workflow":audit,"semantic_verdict":"requires-independent-review"})
        all_usages+=usages
        if phase=="A":a_after=after
    total=sum(o["actual_model_calls"] for o in outcomes);require(total<=maximum,"整批请求超过预算")
    complete=all(o["coverage"]=="hit" for o in outcomes)
    return {"evidence_source":evidence_source,"coverage_complete":complete,"actual_model_calls":total,"usage":prior.usage_total(all_usages),"phases":outcomes,"semantic_verdict":"requires-independent-review"}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--batch",type=Path,required=True)
    args=parser.parse_args();result=inspect(args.batch.resolve(strict=True));print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["coverage_complete"] else 1

if __name__=="__main__":raise SystemExit(main())
