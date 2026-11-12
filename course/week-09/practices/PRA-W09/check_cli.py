"""W09 CLI 检查：受控 SSE，实际进程、SQLite、候选及文件；零模型 API。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid

MATERIALS = Path(__file__).resolve().parent
W08 = MATERIALS.parents[2] / "week-08/practices/PRA-W08"
spec = importlib.util.spec_from_file_location("w09_previous_checks", W08 / "check_cli.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
call, response, read_jsonl = previous.call, previous.response, previous.read_jsonl
frames = previous.previous.previous.previous.frames
# This opt-in fixture answers a real summary request wherever the student's
# budgeting requires it. Summary replies do not consume a business response.
_SELECT = r'''task_index = index
adaptive = bool(items and items[0].get("allow_compaction_response"))
def is_summary_request(request):
    if "tools" in request or len(request.get("messages", [])) != 2:
        return False
    try:
        payload = json.loads(request["messages"][-1]["content"])
    except (KeyError, TypeError, ValueError):
        return False
    return isinstance(payload, dict) and set(payload) == {"previous_summary", "new_records", "current_task"}
if adaptive:
    prior = [json.loads(line) for line in audit.read_text().splitlines()[:-1]]
    summaries = sum(is_summary_request(row["request"]) for row in prior)
    task_index = sum(not is_summary_request(row["request"]) for row in prior)
    if is_summary_request(incoming["request"]):
        if summaries >= 2:
            raise AssertionError("controlled logical task permits at most two summaries")
        item = {"summary_variant": "valid", "sse": []}
    else:
        if task_index >= len(items):
            raise RuntimeError("controlled responses exhausted: extra business request")
        item = items[task_index]
else:
    if index >= len(items):
        raise RuntimeError("controlled responses exhausted: extra model request")
    item = items[index]
# W09_SELECTED_ITEM
'''
WORKER = previous.WORKER.replace(
    'if index >= len(items):\n    raise RuntimeError("controlled responses exhausted: extra model request")\nitem = items[index]',
    _SELECT,
).replace('# W09_SELECTED_ITEM', r'''
# Placeholder IDs are selected only from the request actually received by this
# controlled worker. The real provider and student state logic are untouched.
contexts=[]
for message in incoming["request"]["messages"]:
    if message.get("role")=="user":
        try:
            projection=json.loads(message["content"])
        except (ValueError,TypeError):
            continue
        if isinstance(projection,dict):
            contexts.extend(projection.get("context",[]))
replacements={}
if item.get("source_from_journal"):
    target=item["source_from_journal"]
    records=[json.loads(line) for line in Path(target["path"]).read_text().splitlines()]
    matching=[row for row in records if row.get("message",{}).get("tool_call_id")==target["call_id"]]
    if len(matching)!=1:
        raise AssertionError("controlled fixture requires one actually persisted tool result")
    replacements["@@failed-source@@"]=matching[0]["entry_id"]
for context in contexts:
    if context.get("kind")=="baseline_contract":
        replacements["@@baseline@@"]=context["source"]
    if context.get("kind")=="workflow_state":
        for ident,fact in context["content"].get("facts",{}).items():
            if fact.get("valid") is not True:
                continue
            if fact["kind"]=="check_passed":replacements["@@check@@"]=ident
            if fact["kind"]=="answer_bound":replacements["@@answer@@"]=ident
            if fact["kind"]=="input_visible" and fact.get("path"):
                replacements["@@read:"+fact["path"]+"@@"]=ident
for old,new in replacements.items():
    item["sse"]=[block.replace(old,new) for block in item["sse"]]
''')

# These hooks stop at actual persistence boundaries. They neither decide whether
# an approval is valid nor replace the learner's file commit or database code.
HOOKS = r'''
import w09_shell
import hashlib
original_w09_check = run_check
def w09_check(workspace,image,evidence_dir,**kwargs):
    result = original_w09_check(workspace,image,evidence_dir,**kwargs)
    if settings.get("controlled_shell_failure"):
        result.update(ok=False,exit_code=10,fixed_checks_passed=False,new_tests_ran=False,
                      error={"class":"command_failed","message":"controlled fixed check failure"})
        (evidence_dir/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    contract = kwargs.get("workflow_contract")
    if contract is not None:
        directory = evidence_dir/"trusted-contract"
        directory.mkdir()
        path = directory/"contract.json"
        data = (json.dumps(contract,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
        path.write_bytes(data)
        result["workflow_contract"] = {"path":str(path),"sha256":hashlib.sha256(data).hexdigest(),
            **{key:contract[key] for key in ("separator","duplicates","requirement_revision")}}
        (evidence_dir/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    return result
if not settings.get("real_docker"):
    w09_shell.preflight = preflight
    w09_shell.run_check = w09_check
if settings.get("change_target_before_decision"):
    prior_decision=decision
    def decision(proposal,deadline,cancelled):
        target=Path(settings["change_target_before_decision"])
        target.write_bytes(target.read_bytes()+b"\n# EXTERNAL_DURING_APPROVAL\n")
        observe("external_during_approval",path=str(target),proposal_id=proposal.proposal_id)
        return prior_decision(proposal,deadline,cancelled)
if settings.get("workflow_fault") or settings.get("workflow_gate"):
    import decisions
    from w08_io import write_json
    def workflow_hold(stage, value):
        if settings.get("workflow_fault") != stage and settings.get("workflow_gate") != stage:
            return
        marker = Path(settings["workflow_marker"])
        write_json(marker,{"stage":stage,"pid":os.getpid(),"monotonic":time.monotonic(),
                           "status":value.get("status") if isinstance(value,dict) else None})
        observe("workflow_boundary",stage=stage)
        gate = settings.get("workflow_release")
        limit = time.monotonic()+20
        while not gate or not Path(gate).is_file():
            if time.monotonic()>limit:
                raise AssertionError("workflow observation gate was not released")
            time.sleep(0.01)
    for method in ("decide","consume","save_result"):
        original = getattr(decisions.DecisionStore,method)
        def wrapped(*args,_original=original,_method=method,**kwargs):
            if _method=="consume":
                workflow_hold("before_consume",None)
            value = _original(*args,**kwargs)
            if _method!="consume" or value.get("status")=="consumed":
                workflow_hold(_method,value)
            return value
        setattr(decisions.DecisionStore,method,wrapped)
    if settings.get("workflow_fault")=="wait_continue":
        import journal
        original_wait_continue=journal.Journal.begin_wait_continuation
        def wait_continue(*args,**kwargs):
            value=original_wait_continue(*args,**kwargs)
            workflow_hold("wait_continue",value)
            return value
        journal.Journal.begin_wait_continuation=wait_continue
'''
WRAPPER = previous.WRAPPER.replace('sys.argv = [str(entry), *settings["argv"]]',
                                   HOOKS + '\nsys.argv = [str(entry), *settings["argv"]]')


class CliChecks(previous.CliChecks):
    def setUp(self):
        super().setUp()
        sample = MATERIALS / "inputs/sample_project"
        if sample.is_dir():
            for path in sample.iterdir():
                if path.is_file():
                    shutil.copyfile(path,self.workspace/path.name)
        self.initial = self.workspace_bytes()

    def invoke(self,command,raws=(),*,workflow=False,extra=(),**settings):
        previous.WRAPPER,previous.WORKER = WRAPPER,WORKER
        args=list(extra)
        if workflow:
            args += ["--workflow","--state-db",str(self.state_db),"--max-tokens","1536"]
        if command in {"answer","decide","revise","cancel","continue"}:
            args += ["--state-db",str(self.state_db)]
        if command=="continue":
            directory=self.directory/f"cli-{len(self.invocations)+1:02d}-{command}"
            args += ["--output-dir",str(directory/"runs")]
        return super().invoke(command,raws,extra=args,**settings)

    def workflow_rows(self,table):
        self.assertIn(table,{"workflow_states","workflow_tasks","workflow_waits","workflow_commands","budget_groups","request_reservations"})
        with sqlite3.connect(self.state_db) as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM "+table)]

    def waits(self):
        return [{**json.loads(row["origin_json"]),**row} for row in self.workflow_rows("workflow_waits")]

    def wait(self):
        waits=self.waits()
        self.assertTrue(waits,"没有保存 workflow wait")
        return waits[-1]

    def feedback(self,ident):
        messages=[row["message"] for row in read_jsonl(self.session) if row["type"]=="message"]
        rows=[item for item in messages if item.get("tool_call_id")==ident]
        self.assertEqual(len(rows),1,"每笔接纳的 call 必须恰好配对一次")
        return json.loads(rows[0]["content"])

    def snapshot(self):
        observed=self.invoke("inspect",extra=["--state-db",str(self.state_db)])
        self.assertEqual(observed["exit_code"],0,observed["result"])
        self.assertFalse(observed["requests"])
        return observed["result"]["workflow"]["state"]

    def plan_steps(self,*,independent_read=False):
        baseline={"id":"baseline","title":"核对原单端口合同","depends_on":[],
                  "deliverable":"保留的单端口规则","input_refs":[{"kind":"baseline_contract"}]}
        contract={"id":"list-contract","title":"确定列表输入条件","depends_on":[],
                  "deliverable":"分隔符与重复项选择","input_refs":[{"kind":"requirement","key":"separator"},
                                                                   {"kind":"requirement","key":"duplicates"}]}
        if independent_read:
            contract={"id":"old-tests","title":"阅读既有测试","depends_on":[],"deliverable":"已有测试约束", "input_refs":[
                {"kind":"file","path":"test_validator.py","version":hashlib.sha256(self.initial["test_validator.py"]).hexdigest()}]}
        implementation={"id":"implement","title":"实现并测试列表解析","depends_on":["baseline",contract["id"]],
                        "deliverable":"两个文件的实际修改","input_refs":[{"kind":"requirement","key":"separator"},
                                                                         {"kind":"requirement","key":"duplicates"}]}
        check={"id":"check","title":"运行全部检查","depends_on":["implement"],"deliverable":"当前文件检查结果",
               "input_refs":[{"kind":"requirement","key":"separator"},{"kind":"requirement","key":"duplicates"}]}
        return [baseline,contract,implementation,check]

    def plan_call(self,*,steps=None,revision=0,action="create",invalidated=()):
        args={"action":action,"expected_revision":revision,"reason":"按输入与产物安排有限步骤。", "steps":steps or self.plan_steps()}
        if action=="revise":args["invalidated_steps"]=list(invalidated)
        return call("plan-"+uuid.uuid4().hex[:8],"plan",args)

    def question_call(self,ident="duplicates-question",*,revision=1):
        return call(ident,"question",{"expected_requirement_revision":revision,
            "text":"输入80,443,80时，重复项保留还是只保留第一次？",
            "options":[{"value":"keep","label":"保留重复项","result":[80,443,80]},
                       {"value":"unique","label":"只保留第一次","result":[80,443]}],
            "affected_step_ids":["implement","check"],"affected_paths":["validator.py"]})

    def start_question(self,*,additional=(),steps=None):
        read=call("observe-tests","read",{"path":"test_validator.py","offset":1,"limit":80})
        plan=self.plan_call(steps=steps or self.plan_steps(independent_read=True))
        observed=self.invoke("new",[response(calls=[read]),response(calls=[plan]),
                                   response(calls=[self.question_call(),*additional])],workflow=True)
        self.assertEqual(observed["result"]["status"],"awaiting_question",observed["result"])
        self.assertEqual(observed["exit_code"],0)
        self.assertEqual(len(observed["requests"]),3)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.assert_no_commit(observed)
        return observed,self.wait()

    def answer(self,wait=None,*,value="keep",command_id="answer-keep",revision=None,**settings):
        wait=wait or self.wait()
        return self.invoke("answer",extra=["--question-id",wait["question_id"],"--expected-revision",
            str(wait["requirement_revision"] if revision is None else revision),"--command-id",command_id,"--value",value],**settings)

    def record_call(self,step,evidence,*,revision,ident="record-facts"):
        return call(ident,"plan",{"action":"record","expected_revision":revision,
            "records":[{"step_id":step,"claim":"已核对该项来源与当前版本。","evidence_ids":list(evidence)}]})

    def revise_requirement(self,*,separator=";",revision=1,command_id=None):
        path=self.directory/("change-"+uuid.uuid4().hex+".json")
        path.write_text(json.dumps({"command_id":command_id or "change-"+uuid.uuid4().hex,
            "expected_requirement_revision":revision,"separator":separator,
            "reason":"仅改变列表分隔符，保留重复项选择和原单端口规则。"},ensure_ascii=False,indent=2)+"\n")
        return self.invoke("revise",extra=["--change",str(path)])

    def test_plan_four_steps(self):
        observed=self.invoke("new",[response(calls=[self.plan_call()]),response("计划已提出，尚未执行。")],workflow=True)
        self.assertEqual(observed["result"]["status"],"stop",observed["result"])
        plan=self.snapshot()["plan"]
        self.assertEqual(plan["revision"],1)
        self.assertEqual([item["id"] for item in plan["steps"]],["baseline","list-contract","implement","check"])
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.facts["plan"]=plan

    def test_invalid_plan_graphs(self):
        for bad in ("duplicate","unknown","cycle"):
            with self.subTest(kind=bad):
                self.isolate(bad)
                steps=self.plan_steps()
                if bad=="duplicate":steps[-1]["id"]="baseline"
                if bad=="unknown":steps[-1]["depends_on"]=["unlisted"]
                if bad=="cycle":steps[0]["depends_on"]=["check"]
                command=self.plan_call(steps=steps)
                observed=self.invoke("new",[response(calls=[command]),response(calls=[self.plan_call()]),
                    response("已纠正计划；文件尚未修改。")],workflow=True)
                self.assertEqual(observed["exit_code"],0,observed["result"])
                self.assertEqual(len(observed["requests"]),3)
                self.assertFalse(self.feedback(command["id"])["ok"])
                self.assertEqual(self.snapshot()["plan"]["revision"],1)
                self.assertEqual(self.workspace_bytes(),self.initial)

    def test_plan_stale_revision_atomic(self):
        revision=self.plan_call(revision=0,action="revise",invalidated=["implement"])
        observed=self.invoke("new",[response(calls=[self.plan_call()]),response(calls=[revision]),
            response("修订被拒，仍保留原计划。")],workflow=True)
        self.assertEqual(observed["exit_code"],0,observed["result"])
        self.assertFalse(self.feedback(revision["id"])["ok"])
        state=self.snapshot()
        self.assertEqual(state["plan"]["revision"],1)
        self.assertEqual([item["id"] for item in state["plan"]["steps"]],["baseline","list-contract","implement","check"])

    def test_model_done_does_not_create_checks(self):
        observed=self.invoke("new",[response(calls=[self.plan_call()]),response("全部步骤done，代码与测试都成功。")],workflow=True)
        self.assertEqual(observed["exit_code"],0,observed["result"])
        state=self.snapshot()
        for step in state["plan"]["steps"]:
            self.assertFalse(step["checks"])
        self.assertEqual(self.workspace_bytes(),self.initial)

    def test_invalid_evidence_cannot_complete_step(self):
        record=self.record_call("baseline",["not-a-visible-source"],revision=1)
        observed=self.invoke("new",[response(calls=[self.plan_call()]),response(calls=[record]),
            response("证据引用被拒，步骤尚未完成。")],workflow=True)
        self.assertEqual(observed["exit_code"],0,observed["result"])
        self.assertFalse(self.feedback(record["id"])["ok"])
        self.assertFalse(self.snapshot()["plan"]["steps"][0]["checks"])
        self.isolate("failed-result")
        self.start_question()
        self.assertEqual(self.answer()["exit_code"],0)
        failed_record=self.record_call("baseline",["@@failed-source@@"],revision=1)
        fixture={"sse":frames(response(calls=[failed_record])),
                 "source_from_journal":{"path":str(self.session),"call_id":"failed-check"}}
        failed=self.invoke("continue",[response(calls=[call("failed-check","shell",{"command":"check"})]),fixture,
            response("失败的检查不能证明步骤完成。")],controlled_shell_failure=True)
        self.assertEqual(failed["exit_code"],0,failed["result"])
        self.assertEqual(len(failed["requests"]),3)
        self.assertFalse(self.feedback("failed-check")["ok"])
        self.assertFalse(self.feedback(failed_record["id"])["ok"])
        self.isolate("off-branch-result")
        first=self.invoke("new",[response(calls=[self.plan_call()]),response("计划已保存。")],workflow=True)
        self.assertEqual(first["exit_code"],0,first["result"])
        branch_point=next(row["entry_id"] for row in read_jsonl(self.session) if row["type"]=="run_end" and row["status"]=="stop")
        second=self.invoke("resume",[response(calls=[call("later-read","read",{"path":"validator.py","offset":1,"limit":2})]),response("读取已完成。")],workflow=True)
        self.assertEqual(second["exit_code"],0,second["result"])
        off_branch=next(row["entry_id"] for row in read_jsonl(self.session) if row.get("message",{}).get("tool_call_id")=="later-read")
        self.assertEqual(self.invoke("branch",extra=["--from-entry",branch_point])["exit_code"],0)
        reference=self.record_call("baseline",[off_branch],revision=1)
        refused=self.invoke("resume",[response(calls=[reference]),response("其他分支的证据未被接纳。")],workflow=True)
        self.assertEqual(refused["exit_code"],0,refused["result"])
        self.assertFalse(self.feedback(reference["id"])["ok"])
        self.assertFalse(self.snapshot()["plan"]["steps"][0]["checks"])

    def test_requirement_invalidates_only_dependency_closure(self):
        record=self.record_call("baseline",["@@baseline@@"],revision=1)
        observed=self.invoke("new",[response(calls=[self.plan_call()]),response(calls=[record]),response("已核对固定合同。")],workflow=True)
        self.assertEqual(observed["result"]["status"],"stop",observed["result"])
        before=self.snapshot()
        self.assertIs(before["plan"]["steps"][0]["valid"],True)
        changed=self.revise_requirement()
        self.assertEqual(changed["exit_code"],0,changed["result"])
        state=self.snapshot()
        self.assertEqual(state["requirements"]["revision"],2)
        self.assertEqual(state["requirements"]["separator"],";")
        self.assertEqual(state["plan"]["steps"][0],before["plan"]["steps"][0])
        for step in state["plan"]["steps"][1:]:
            self.assertIs(step["valid"],False)
            self.assertEqual(step["invalid_reason"],"separator_changed")
        self.facts["retained_step"]=state["plan"]["steps"][0]

    def test_baseline_source_retained_across_requirement_change(self):
        first=self.invoke("new",[response(calls=[self.plan_call()]),response("计划已保存。")],workflow=True)
        self.assertEqual(first["exit_code"],0,first["result"])
        before=self.snapshot()
        self.assertEqual(self.revise_requirement()["exit_code"],0)
        continued=self.invoke("continue",[response("已读取新的要求。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        context=json.loads(continued["requests"][0]["request"]["messages"][1]["content"])["context"]
        baseline=next(item for item in context if item["kind"]=="baseline_contract")
        self.assertEqual(baseline["source"],before["baseline"]["source_id"])
        self.assertEqual(baseline["content"],before["baseline"]["raw_text"])
        self.assertNotEqual(first["result"]["budget_group_id"],continued["result"]["budget_group_id"])

    def test_old_check_kept_but_current_check_rerun(self):
        steps=self.plan_steps(independent_read=True)
        steps[-1]["depends_on"]=[]
        self.start_question(steps=steps)
        self.assertEqual(self.answer()["exit_code"],0)
        first=self.invoke("continue",[response(calls=[call("first-check","shell",{"command":"check"})]),
            response(calls=[self.record_call("check",["@@check@@"],revision=1)]),response("当前检查来源已保存。")])
        self.assertEqual(first["result"]["status"],"stop",first["result"])
        old=self.snapshot()
        checked=next(item for item in old["plan"]["steps"] if item["id"]=="check")
        self.assertIs(checked["valid"],True)
        self.assertEqual(self.revise_requirement()["exit_code"],0)
        state=self.snapshot()
        stale=next(item for item in state["plan"]["steps"] if item["id"]=="check")
        self.assertIs(stale["valid"],False)
        self.assertEqual(stale["evidence"],checked["evidence"])
        check_response={"sse":frames(response(calls=[call("current-check","shell",{"command":"check"})])),
                        "allow_compaction_response":True}
        current=self.invoke("continue",[check_response,
            response(calls=[self.record_call("check",["@@check@@"],revision=state["plan"]["revision"])]),response("新要求检查来源已保存。")])
        self.assertEqual(current["result"]["status"],"stop",current["result"])
        final=next(item for item in self.snapshot()["plan"]["steps"] if item["id"]=="check")
        self.assertIs(final["valid"],True)
        self.assertNotEqual(final["evidence"],checked["evidence"])
        for item in (first,current):
            self.assertEqual(sum(row["operation"]=="shell_fixture" for row in item["operations"]),1)
        self.facts["controlled_docker_boundary"]=True

    def test_question_wait_has_no_new_request(self):
        first,wait=self.start_question()
        again=self.invoke("continue")
        self.assertEqual(again["result"]["status"],"awaiting_question",again["result"])
        self.assertFalse(again["requests"])
        self.assert_no_commit(again)
        self.assertEqual(self.wait()["wait_id"],wait["wait_id"])
        self.assertEqual(self.workspace_bytes(),self.initial)

    def test_wait_independent_read_and_mixed_mutation(self):
        reads=[call("independent-read","read",{"path":"test_validator.py","offset":1,"limit":2}),
               call("affected-read","read",{"path":"validator.py","offset":1,"limit":2})]
        observed,_=self.start_question(additional=reads)
        self.assertTrue(self.feedback("independent-read")["ok"])
        affected=self.feedback("affected-read")
        self.assertFalse(affected["ok"])
        events=read_jsonl(Path(observed["result"]["output_directory"])/"events.jsonl")
        original=next(row for row in events if row["event"]=="tool_finished" and row.get("call_id")=="affected-read")
        self.assertIs(original["executed"],False)
        self.assertIs(original["result"]["control"]["blocks_run"],False)
        self.assertEqual(original["result"]["control"]["effect"],"none")
        records=[json.loads(path.read_text()) for path in Path(observed["result"]["output_directory"]).rglob("read-*/result.json")]
        self.assertFalse(any(row["identity"]["call_id"]=="affected-read" for row in records))
        independent=next(row for row in events if row["event"]=="tool_finished" and row.get("call_id")=="independent-read")
        self.assertIs(independent["executed"],True)
        self.assertTrue(any(row["operation"]=="read" and row.get("path")=="test_validator.py" for row in observed["operations"]))
        self.assertEqual([row["text"] for row in self.feedback("independent-read")["lines"]],self.initial["test_validator.py"].decode().splitlines()[:2])
        self.isolate("question-with-edit")
        before=self.initial["validator.py"].decode()
        batch=[self.question_call(),call("mixed-edit","edit",{"path":"validator.py","old_text":before,"new_text":before+"\n# forbidden mixed batch\n"})]
        plan=self.plan_call(steps=self.plan_steps(independent_read=True))
        mixed=self.invoke("new",[response(calls=[call("read-tests","read",{"path":"test_validator.py","offset":1,"limit":80})]),
            response(calls=[plan]),response(calls=batch)],workflow=True)
        self.assertEqual(mixed["exit_code"],1,mixed["result"])
        self.assertEqual(len(mixed["requests"]),3)
        self.assertFalse(self.waits())
        self.assert_no_commit(mixed)
        self.assertEqual(self.workspace_bytes(),self.initial)
        for item in batch:self.assertFalse(self.feedback(item["id"])["ok"])

    def test_answer_becomes_real_user(self):
        first,wait=self.start_question()
        answer=self.answer(wait,value="unique",command_id="actual-unique-answer")
        self.assertEqual(answer["exit_code"],0,answer["result"])
        self.assertFalse(answer["requests"])
        continued=self.invoke("continue",[response("已记录唯一值策略。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        messages=continued["requests"][0]["request"]["messages"]
        self.assertEqual(messages[-1],{"role":"user","content":"unique"})
        users=[row for row in read_jsonl(self.session) if row.get("message",{}).get("role")=="user"]
        self.assertEqual(users[-1]["message"]["content"],"unique")
        self.assertEqual(len(users),2)
        self.assertEqual(self.workspace_bytes(),self.initial)

    def test_stale_or_other_question_rejected(self):
        for kind in ("revision","superseded","ended","other_session"):
            with self.subTest(kind=kind):
                self.isolate(kind)
                _,wait=self.start_question()
                if kind=="superseded":
                    self.assertEqual(self.revise_requirement()["exit_code"],0)
                elif kind=="ended":
                    self.assertEqual(self.answer(wait)["exit_code"],0)
                    done=self.invoke("continue",[response("答案已记录。")])
                    self.assertEqual(done["result"]["status"],"stop",done["result"])
                elif kind=="other_session":
                    alternate=self.directory/"other-session.jsonl"
                    created=self.invoke("new",[response("另一会话。")],session=alternate)
                    self.assertEqual(created["exit_code"],0,created["result"])
                rejected=self.answer(wait,revision=0 if kind=="revision" else None,
                    command_id="new-invalid-answer",**({"session":alternate} if kind=="other_session" else {}))
                self.assertEqual(rejected["exit_code"],2,rejected["result"])
                self.assertFalse(rejected["requests"])
                self.assert_no_commit(rejected)
                self.assertEqual(self.workspace_bytes(),self.initial)

    def test_answer_does_not_approve_candidate(self):
        _,wait=self.start_question()
        self.assertEqual(self.answer(wait)["exit_code"],0)
        before=self.initial["validator.py"].decode()
        edit=call("after-answer-edit","edit",{"path":"validator.py","old_text":before,"new_text":before+"\n# distinct approval required\n"})
        continued=self.invoke("continue",[response(calls=[edit])])
        self.assertEqual(continued["result"]["status"],"awaiting_approval",continued["result"])
        current=self.wait()
        self.assertEqual(current["status"],"pending")
        self.assertIsNone(current["decision_json"])
        self.assertIsNone(current["permit_id"])
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.assert_no_commit(continued)

    def start_candidate(self,*,max_turns=8,deadline_seconds=90):
        before=self.initial["validator.py"].decode()
        self.candidate=before+"\n# W09 controlled frozen candidate\n"
        candidate=call("frozen-edit","edit",{"path":"validator.py","old_text":before,"new_text":self.candidate})
        observed=self.invoke("new",[response(calls=[candidate])],workflow=True,
                             max_turns=max_turns,deadline_seconds=deadline_seconds)
        self.assertEqual(observed["exit_code"],0,observed["result"])
        self.assertEqual(observed["result"]["status"],"awaiting_approval",observed["result"])
        self.assertEqual(len(observed["requests"]),1)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.assertFalse(any(item["operation"]=="commit_enter" for item in observed["operations"]))
        wait=self.wait()
        self.assertEqual(wait["status"],"pending")
        self.assertEqual(wait["kind"],"approval")
        return observed,wait

    def decide(self,wait=None,*,decision="approve",command_id=None,**settings):
        wait=wait or self.wait()
        return self.invoke("decide",extra=["--wait-id",wait["wait_id"],"--expected-revision",str(wait["requirement_revision"]),
                           "--command-id",command_id or "command-"+uuid.uuid4().hex,"--decision",decision],**settings)

    def assert_no_commit(self,observed):
        self.assertFalse(any(row["operation"]=="commit_enter" for row in observed["operations"]),observed)

    def isolate(self,name):
        """Use new files for a subcase; preserve every previous observation."""
        self.workspace=self.directory/("workspace-"+name)
        self.workspace.mkdir()
        for relative,data in self.initial.items():
            (self.workspace/relative).write_bytes(data)
        self.session=self.directory/("session-"+name+".jsonl")
        self.state_db=self.directory/("state-"+name+".sqlite3")

    def test_candidate_persisted_before_exit(self):
        first,wait=self.start_candidate()
        proposal=Path(wait["proposal_path"])
        saved=json.loads(proposal.read_text())
        self.assertEqual(saved["binding"],wait["candidate_binding"])
        self.assertEqual((proposal.parent/"before.bin").read_bytes(),self.initial["validator.py"])
        self.assertEqual((proposal.parent/"after.bin").read_text(),self.candidate)
        again=self.invoke("continue")
        self.assertEqual(again["result"]["status"],"awaiting_approval")
        self.assertFalse(again["requests"])
        self.assert_no_commit(again)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.facts.update(wait_id=wait["wait_id"],candidate_binding=wait["candidate_binding"],first_pid=first["pid"],second_pid=again["pid"])

    def test_new_process_consumes_original_candidate(self):
        first,wait=self.start_candidate()
        decision=self.decide(wait)
        self.assertEqual(decision["exit_code"],0,decision["result"])
        self.assertFalse(decision["requests"])
        self.assertEqual(self.workspace_bytes(),self.initial)
        continued=self.invoke("continue",[response("冻结候选已执行。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)
        commits=[row for row in continued["operations"] if row["operation"]=="commit_enter"]
        self.assertEqual([row["proposal_id"] for row in commits],[wait["proposal_id"]])
        self.assertEqual(len({first["pid"],decision["pid"],continued["pid"]}),3)
        self.assertEqual(self.feedback("frozen-edit")["control"]["effect"],"applied")
        self.facts.update(wait_id=wait["wait_id"],proposal_id=wait["proposal_id"],commits=commits)

    def test_candidate_or_target_changed(self):
        observations=[]
        for kind in ("file_object","workspace_object","candidate_bytes","permission"):
            with self.subTest(kind=kind):
                self.isolate(kind)
                _,wait=self.start_candidate()
                self.assertEqual(self.decide(wait)["exit_code"],0)
                target=self.workspace/"validator.py"
                extra=[]
                if kind=="file_object":
                    original=target.stat().st_ino
                    replacement=self.workspace/"replacement.tmp"
                    replacement.write_bytes(target.read_bytes())
                    os.replace(replacement,target)
                    self.assertNotEqual(target.stat().st_ino,original)
                elif kind=="workspace_object":
                    old=self.directory/"original-workspace-object"
                    self.workspace.rename(old)
                    self.workspace.mkdir()
                    for name,data in self.initial.items():(self.workspace/name).write_bytes(data)
                elif kind=="candidate_bytes":
                    frozen=Path(wait["proposal_path"]).parent/"after.bin"
                    frozen.chmod(0o600)
                    frozen.write_bytes(frozen.read_bytes()+b"# changed after approval\n")
                else:
                    extra=["--disable-mutations"]
                continued=self.invoke("continue",extra=extra)
                self.assertIn(continued["exit_code"],(1,2),continued["result"])
                self.assert_no_commit(continued)
                self.assertFalse(continued["requests"])
                self.assertEqual(self.workspace_bytes(),self.initial)
                observations.append({"changed":kind,"result":continued["result"]})
        self.facts["changes_rejected"]=observations

    def test_command_idempotence_and_conflict(self):
        _,wait=self.start_candidate()
        ident="decision-stable-command"
        first=self.decide(wait,command_id=ident)
        second=self.decide(wait,command_id=ident)
        self.assertEqual(first["exit_code"],0,first["result"])
        self.assertEqual(second["exit_code"],0,second["result"])
        self.assertEqual(first["result"],second["result"])
        commands=self.workflow_rows("workflow_commands")
        self.assertEqual(len([row for row in commands if row["command_id"]==ident]),1)
        saved=commands[0]["receipt_json"]
        conflict=self.decide(wait,command_id=ident,decision="reject")
        self.assertEqual(conflict["exit_code"],2,conflict["result"])
        self.assertEqual(self.workflow_rows("workflow_commands")[0]["receipt_json"],saved)
        for item in (first,second,conflict):
            self.assertFalse(item["requests"])
            self.assert_no_commit(item)
        self.assertEqual(self.workspace_bytes(),self.initial)

    def test_consumed_result_not_executed_again(self):
        self.start_candidate()
        self.decide()
        first=self.invoke("continue",[response("修改完成。")])
        self.assertEqual(first["result"]["status"],"stop",first["result"])
        second=self.invoke("continue")
        self.assertFalse(second["requests"])
        self.assert_no_commit(second)
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)
        self.assertEqual(len([row for row in first["operations"] if row["operation"]=="commit_enter"]),1)

    def test_reject_expiry_cancel_no_write(self):
        outcomes=[]
        for kind in ("reject","expired","cancelled"):
            with self.subTest(kind=kind):
                self.isolate(kind)
                _,wait=self.start_candidate(deadline_seconds=2 if kind=="expired" else 90)
                if kind=="cancelled":
                    decision=self.invoke("cancel",extra=["--command-id","cancel-before-permit"])
                else:
                    decision=self.decide(decision="reject" if kind=="reject" else "approve")
                self.assertEqual(decision["exit_code"],0,decision["result"])
                if kind=="expired":
                    remaining=wait["expires_at"]-time.time()+0.05
                    if remaining>0:time.sleep(remaining)
                continued=self.invoke("continue")
                self.assertEqual(continued["exit_code"],1,continued["result"])
                self.assertFalse(continued["requests"])
                self.assert_no_commit(continued)
                self.assertEqual(self.workspace_bytes(),self.initial)
                outcomes.append({"condition":kind,"result":continued["result"]})
        self.facts["outcomes"]=outcomes

    def test_approved_command_killed_then_continued(self):
        _,wait=self.start_candidate()
        marker=self.directory/"decision-committed.json"
        ident="decision-killed-after-commit"
        killed=self.decide(wait,command_id=ident,workflow_fault="decide",workflow_marker=str(marker),kill_marker=marker)
        self.assertTrue(killed["killed_after_marker"])
        self.assertEqual(self.wait()["status"],"approved")
        self.assertEqual(self.workspace_bytes(),self.initial)
        retried=self.decide(wait,command_id=ident)
        self.assertEqual(retried["exit_code"],0,retried["result"])
        continued=self.invoke("continue",[response("已按原候选完成。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        self.assertEqual([row["proposal_id"] for row in continued["operations"] if row["operation"]=="commit_enter"],[wait["proposal_id"]])
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)

    def test_saved_result_only_fills_journal(self):
        self.start_candidate()
        self.decide()
        marker=self.directory/"result-saved.json"
        killed=self.invoke("continue",workflow_fault="save_result",workflow_marker=str(marker),kill_marker=marker)
        self.assertTrue(killed["killed_after_marker"])
        self.assertEqual(self.wait()["status"],"result_saved")
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)
        continued=self.invoke("continue",[response("结果已从持久记录补齐。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        self.assert_no_commit(continued)
        self.assertEqual(self.feedback("frozen-edit")["control"]["effect"],"applied")

    def test_consumed_without_result_unknown(self):
        self.start_candidate()
        self.decide()
        marker=self.directory/"permit-consumed.json"
        killed=self.invoke("continue",workflow_fault="consume",workflow_marker=str(marker),kill_marker=marker)
        self.assertTrue(killed["killed_after_marker"])
        self.assertEqual(self.wait()["status"],"consumed")
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.assert_no_commit(killed)
        continued=self.invoke("continue")
        self.assertEqual(continued["exit_code"],1,continued["result"])
        self.assertEqual(continued["result"]["status"],"effect_unknown",continued["result"])
        self.assertFalse(continued["requests"])
        self.assert_no_commit(continued)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.facts.update(consumed_before_write=True,result=continued["result"])

    def test_cancel_and_permit_transaction_order(self):
        observations=[]
        for boundary in ("before_consume","consume"):
            with self.subTest(boundary=boundary):
                self.isolate(boundary)
                self.start_candidate()
                self.decide()
                marker=self.directory/(boundary+"-reached.json")
                release=self.directory/(boundary+"-release")
                cancellations=[]
                def control(process,directory):
                    if marker.is_file() and not cancellations:
                        cancelled=self.invoke("cancel",extra=["--command-id","cancel-at-"+boundary])
                        self.assertEqual(cancelled["exit_code"],0,cancelled["result"])
                        self.assertNotEqual(cancelled["pid"],process.pid)
                        cancellations.append(cancelled)
                        release.touch()
                continued=self.invoke("continue",workflow_gate=boundary,workflow_marker=str(marker),
                                      workflow_release=str(release),control=control)
                self.assertEqual(len(cancellations),1)
                self.assertEqual(continued["exit_code"],1,continued["result"])
                self.assertFalse(continued["requests"])
                commits=[row for row in continued["operations"] if row["operation"]=="commit_enter"]
                self.assertEqual(len(commits),int(boundary=="consume"))
                effect=self.feedback("frozen-edit")["control"]["effect"]
                self.assertIn(effect,("none","applied"))
                expected=self.candidate.encode() if effect=="applied" else self.initial["validator.py"]
                self.assertEqual((self.workspace/"validator.py").read_bytes(),expected)
                if boundary=="before_consume":self.assertEqual(effect,"none")
                self.assertEqual(continued["result"]["status"],"cancelled",continued["result"])
                observations.append({"boundary":boundary,"cancel_pid":cancellations[0]["pid"],
                                     "continue_pid":continued["pid"],"commits":len(commits),"effect":effect,"result":continued["result"]})
        self.facts["transaction_orders"]=observations

    def test_nonwait_and_partial_journal_stay_blocked(self):
        marker=self.directory/"ordinary-interrupted.json"
        broken=self.invoke("new",[response(calls=[call("ordinary-read","read",{"path":"validator.py"})])],
            interrupt_ordinary_call=True,fault_stage="ordinary_call",fault_marker=str(marker),kill_marker=marker)
        self.assertTrue(broken["killed_after_marker"])
        before=self.session.read_bytes()
        refused=self.invoke("resume")
        self.assertNotEqual(refused["exit_code"],0,refused["result"])
        self.assertFalse(refused["requests"])
        self.assertEqual(self.session.read_bytes(),before)
        self.isolate("partial-jsonl")
        self.start_candidate()
        with self.session.open("ab") as target:target.write(b'{"type":')
        partial=self.session.read_bytes()
        continued=self.invoke("continue")
        self.assertNotEqual(continued["exit_code"],0,continued["result"])
        self.assertFalse(continued["requests"])
        self.assertEqual(self.session.read_bytes(),partial)
        self.assert_no_commit(continued)

    def test_wait_continuations_preserve_pairing(self):
        first,_=self.start_candidate()
        self.decide()
        before=self.initial["test_validator.py"].decode()
        second_call=call("second-edit","edit",{"path":"test_validator.py","old_text":before,"new_text":before+"\n# second original candidate\n"})
        middle=self.invoke("continue",[response(calls=[second_call])])
        self.assertEqual(middle["result"]["status"],"awaiting_approval",middle["result"])
        second_wait=self.wait()
        self.assertEqual(second_wait["call_id"],"second-edit")
        self.assertEqual(self.decide(second_wait)["exit_code"],0)
        last=self.invoke("continue",[response("两份原候选均已执行。")])
        self.assertEqual(last["result"]["status"],"stop",last["result"])
        self.assertEqual(len({item["result"]["budget_group_id"] for item in (first,middle,last)}),1)
        rows=read_jsonl(self.session)
        messages=[row["message"] for row in rows if row["type"]=="message"]
        self.assertEqual(len([item for item in messages if item["role"]=="user"]),1)
        self.assertEqual(len([item for item in messages if item["role"]=="assistant"]),3)
        tools=[item for item in messages if item["role"]=="tool"]
        self.assertEqual([item["tool_call_id"] for item in tools],["frozen-edit","second-edit"])
        self.assertEqual(len([row for row in rows if row["type"]=="wait_continue"]),2)
        self.assertEqual(len([row for row in rows if row["type"]=="wait_resolution"]),2)
        self.assertEqual([item["tool_call_id"] for item in last["requests"][0]["request"]["messages"] if item["role"]=="tool"],["frozen-edit","second-edit"])
        projection=next(Path(last["result"]["output_directory"]).glob("projection-*.json"))
        omitted=json.loads(projection.read_text())["omitted"]
        first_result=next(row for row in rows if row.get("message",{}).get("tool_call_id")=="frozen-edit")
        self.assertFalse(any(item["source_id"]==first_result["entry_id"] for item in omitted))

    def test_crash_after_wait_continue_stays_blocked(self):
        self.start_candidate()
        self.decide()
        marker=self.directory/"continued-before-model.json"
        killed=self.invoke("continue",workflow_fault="wait_continue",workflow_marker=str(marker),kill_marker=marker)
        self.assertTrue(killed["killed_after_marker"])
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)
        self.assertFalse(killed["requests"])
        again=self.invoke("continue")
        self.assertNotEqual(again["exit_code"],0,again["result"])
        self.assertFalse(again["requests"])
        self.assert_no_commit(again)
        self.assertEqual((self.workspace/"validator.py").read_text(),self.candidate)

    def test_logical_budget_and_summary_survive_wait(self):
        first,wait=self.start_candidate(max_turns=2)
        original=self.workflow_rows("budget_groups")[0]
        self.decide()
        final=self.invoke("continue",[response("最后一次模型额度完成。")])
        self.assertEqual(final["result"]["status"],"stop",final["result"])
        current=self.workflow_rows("budget_groups")[0]
        self.assertEqual(current["budget_group_id"],original["budget_group_id"])
        self.assertEqual(current["model_calls"],2)
        self.assertEqual(current["action_count"],original["action_count"])
        self.assertEqual(current["deadline_unix"],original["deadline_unix"])
        self.isolate("exhausted")
        before=self.initial["validator.py"].decode()
        edit=call("no-budget-edit","edit",{"path":"validator.py","old_text":before,"new_text":before+"\n# must not be dispatched\n"})
        blocked=self.invoke("new",[response(calls=[edit])],workflow=True,max_turns=1)
        self.assertNotEqual(blocked["exit_code"],0,blocked["result"])
        self.assertEqual(blocked["result"]["status"],"turn_limit")
        self.assertEqual(len(blocked["requests"]),1)
        self.assertFalse(self.waits())
        self.assert_no_commit(blocked)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.isolate("summary-across-wait")
        self.seed(question="目标：只接受有效端口，保持函数接口与端点。资料条目："+"中文历史资料。"*650)
        before=self.initial["validator.py"].decode()
        edit=call("summary-edit","edit",{"path":"validator.py","old_text":before,"new_text":before+"\n# after one summary\n"})
        waiting=self.invoke("resume",[previous.previous.previous.summary(),response(calls=[edit])],workflow=True)
        self.assertEqual(waiting["result"]["status"],"awaiting_approval",waiting["result"])
        self.assertEqual(self.workflow_rows("budget_groups")[0]["summary_calls"],1)
        self.decide()
        continued=self.invoke("continue",[response("摘要和等待已接续。")])
        self.assertEqual(continued["result"]["status"],"stop",continued["result"])
        self.assertEqual(self.workflow_rows("budget_groups")[0]["summary_calls"],1)
        self.assertEqual(len([row for row in read_jsonl(self.session) if row["type"]=="compaction"]),1)

    def test_regression_real_docker_cleanup(self):
        self.assertIsNotNone(self.real_runtime,"完整检查需 --runtime 指向已准备的固定镜像配置")
        self.runtime.write_bytes(self.real_runtime.read_bytes())
        observed=self.invoke("new",[response(calls=[call("actual-check","shell",{"command":"check"})]),response("固定副本检查已结束。")],real_docker=True)
        self.assertEqual(observed["result"]["status"],"stop",observed["result"])
        feedback=self.feedback("actual-check")
        self.assertTrue(feedback["ok"],feedback)
        roots=list(Path(observed["result"]["output_directory"]).glob("shell-*/container.json"))
        self.assertEqual(len(roots),1)
        lifecycle=json.loads(roots[0].read_text())
        self.assertIs(lifecycle["removed_confirmed"],True)
        self.assertIs(lifecycle["stopped_confirmed"],True)
        self.assertEqual(self.workspace_bytes(),self.initial)
        self.facts.update(real_docker=True,container=lifecycle["name"],stopped=True,removed=True)

    def test_regression_version_changed_during_approval(self):
        original=self.initial["validator.py"].decode()
        edit=call("inline-edit","edit",{"path":"validator.py","old_text":original,"new_text":original+"\n# approved candidate\n"})
        observed=self.invoke("new",[response(calls=[edit])],change_target_before_decision=str(self.workspace/"validator.py"))
        self.assertEqual(observed["exit_code"],1,observed["result"])
        self.assertEqual(len(observed["requests"]),1)
        self.assertEqual(len(observed["approvals"]),1)
        self.assertEqual((self.workspace/"validator.py").read_bytes(),self.initial["validator.py"]+b"\n# EXTERNAL_DURING_APPROVAL\n")
        events=read_jsonl(Path(observed["result"]["output_directory"])/"events.jsonl")
        result=next(row["result"] for row in events if row["event"]=="tool_finished" and row.get("call_id")=="inline-edit")
        self.assertFalse(result["ok"])
        self.assertEqual(result["control"]["effect"],"none")
        self.assertTrue(any(row["operation"]=="external_during_approval" for row in observed["operations"]))

    def test_regression_receipt_recovery(self):self.test_kill_after_effect()
    def test_regression_read_cancel(self):self.test_real_sigint_group()
    def test_regression_source_and_skill(self):self.test_skill_resource_requires_body_in_issuing_request()
    def test_regression_connection_replaced(self):
        self.assertIsNotNone(self.sdk_python,"该项需要 --sdk-python 指向 W07 固定环境")
        self.test_mcp_replaced_connection_rejects_old_candidate()


TESTS=["test_plan_four_steps","test_invalid_plan_graphs","test_plan_stale_revision_atomic",
       "test_model_done_does_not_create_checks","test_invalid_evidence_cannot_complete_step",
       "test_requirement_invalidates_only_dependency_closure","test_baseline_source_retained_across_requirement_change",
       "test_old_check_kept_but_current_check_rerun","test_question_wait_has_no_new_request",
       "test_wait_independent_read_and_mixed_mutation","test_answer_becomes_real_user",
       "test_stale_or_other_question_rejected","test_answer_does_not_approve_candidate",
       "test_candidate_persisted_before_exit","test_new_process_consumes_original_candidate",
       "test_candidate_or_target_changed","test_command_idempotence_and_conflict",
       "test_consumed_result_not_executed_again","test_reject_expiry_cancel_no_write",
       "test_approved_command_killed_then_continued","test_saved_result_only_fills_journal",
       "test_consumed_without_result_unknown","test_cancel_and_permit_transaction_order",
       "test_nonwait_and_partial_journal_stay_blocked","test_wait_continuations_preserve_pairing",
       "test_crash_after_wait_continue_stays_blocked","test_logical_budget_and_summary_survive_wait",
       "test_regression_resume_branch","test_regression_truncated_stream","test_regression_version_changed_during_approval",
       "test_regression_real_docker_cleanup",
       "test_regression_source_and_skill","test_regression_connection_replaced","test_regression_receipt_recovery",
       "test_regression_read_cancel"]


def run_suite(entry,sdk_python,output_root,names=None,*,runtime=None):
    entry,output_root=entry.resolve(),output_root.absolute()
    output_root.mkdir(parents=True,exist_ok=False)
    class Bound(CliChecks):pass
    Bound.entry_point,Bound.sdk_python,Bound.output_root=entry,sdk_python.absolute() if sdk_python else None,output_root
    Bound.real_runtime=runtime.absolute() if runtime else None
    names=names or TESTS
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(name) for name in names))
    failed=len({case.id() for case,_ in result.failures+result.errors})
    report={"evidence_source":"controlled-local","external_model_requests":0,
            "docker":"real-container-in-test_regression_real_docker_cleanup; other-cases-controlled-boundary",
            "entry":str(entry),"selected_cases":names,"count":result.testsRun,"passed":result.testsRun-failed,
            "failed":failed,"failures":len(result.failures),"errors":len(result.errors),
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
    parser.add_argument("--runtime",type=Path,help="固定镜像配置；完整检查及 Docker 回归项必需")
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--case",choices=TESTS)
    args=parser.parse_args()
    report=run_suite(args.entry or args.student_dir/"main.py",args.sdk_python,args.output_dir/("check-"+uuid.uuid4().hex[:8]),[args.case] if args.case else TESTS,runtime=args.runtime)
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report["verdict"]=="pass" else 1


if __name__=="__main__":raise SystemExit(main())
