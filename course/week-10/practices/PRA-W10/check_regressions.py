"""对当前 my-pi 运行八组既有机制回归；模型受控，Docker/MCP/接收器为真实本地进程。"""
from __future__ import annotations
import argparse,importlib.util,json,sys,unittest,uuid
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('w10_prior_checks',MATERIALS.parents[2]/'week-09/practices/PRA-W09/check_cli.py')
prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)

class Checks(prior.CliChecks):
    def test_stream(self):self.test_regression_truncated_stream()
    def test_pairing(self):
        calls=[prior.call('read-a','read',{'path':'validator.py','offset':1,'limit':8}),
               prior.call('read-b','read',{'path':'test_validator.py','offset':1,'limit':8})]
        observed=self.invoke('new',[prior.response(calls=calls),prior.response('已取得两个实际读取结果。')])
        self.assertEqual(observed['exit_code'],0,observed['result'])
        messages=observed['requests'][1]['request']['messages']
        tools=[message for message in messages if message['role']=='tool']
        self.assertEqual([message['tool_call_id'] for message in tools],['read-a','read-b'])
        for call in calls:self.assertTrue(self.feedback(call['id'])['ok'])
    def test_paths_and_stale_approval(self):
        self.test_regression_version_changed_during_approval();self.isolate('path-refusal');self.test_resource_paths_and_links_refused()
    def test_docker_exit_and_timeout(self):
        self.test_regression_real_docker_cleanup()
        self.session=self.directory/'timeout-session.jsonl'
        observed=self.invoke('new',[prior.response(calls=[prior.call('slow-check','shell',{'command':'check'})])],real_docker=True,extra=['--check-profile','slow','--command-timeout','0.2'])
        self.assertEqual(observed['exit_code'],1,observed['result'])
        result=json.loads(next(Path(observed['result']['output_directory']).glob('shell-*/result.json')).read_text())
        self.assertFalse(result['ok']);self.assertTrue(result['cleanup_confirmed'])
        roots=list(Path(observed['result']['output_directory']).glob('shell-*/container.json'));self.assertEqual(len(roots),1)
        lifecycle=json.loads(roots[0].read_text());self.assertTrue(lifecycle['stopped_confirmed']);self.assertTrue(lifecycle['removed_confirmed'])
    def test_journal_and_branch(self):self.test_regression_resume_branch()
    def test_summary_failure(self):self.test_invalid_summaries_never_continue()
    def test_mcp_binding(self):self.test_regression_connection_replaced()
    def test_receipt_and_decision_continuations(self):
        self.test_regression_receipt_recovery();self.isolate('decision-continuation');self.test_new_process_consumes_original_candidate()
TESTS=[name for name in Checks.__dict__ if name.startswith('test_')]

def run(entry,sdk_python,runtime,output,names=None):
    output.mkdir(parents=True,exist_ok=False)
    class Bound(Checks):pass
    Bound.entry_point=entry;Bound.sdk_python=sdk_python;Bound.real_runtime=runtime;Bound.output_root=output
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(n) for n in (names or TESTS)))
    failed=len({str(c) for c,_ in result.failures+result.errors})
    value={'evidence_source':'controlled-sse+real-local-effects','external_model_requests':0,'count':result.testsRun,'passed':result.testsRun-failed,
        'failed':failed,'verdict':'pass' if result.wasSuccessful() else 'fail','failure_details':[{'case':str(c),'traceback':e} for c,e in result.failures+result.errors]}
    (output/'summary.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');return value

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('entry','sdk-python','runtime','output-dir'):p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--case',choices=TESTS);a=p.parse_args()
    value=run(a.entry.absolute(),a.sdk_python.absolute(),a.runtime.absolute(),a.output_dir.absolute()/('check-'+uuid.uuid4().hex[:8]),[a.case] if a.case else None)
    print(json.dumps(value,ensure_ascii=False));return 0 if value['verdict']=='pass' else 1
if __name__=='__main__':raise SystemExit(main())
