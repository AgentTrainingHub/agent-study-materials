"""W11 连续工程的八组回归；模型受控，实际本地效果单独留证。"""
from pathlib import Path
import argparse,importlib.util,json,sys,unittest,uuid

MATERIALS=Path(__file__).resolve().parent
W10=MATERIALS.parents[2]/'week-10/practices/PRA-W10'
sys.path.insert(0,str(W10))
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
prior=module('w11_prior_regressions',W10/'check_regressions.py')
docs=module('w11_document_regressions',W10/'check_cli.py')

class Checks(prior.Checks):
    def test_normal_stop_resume(self):self.test_regression_resume_branch()
    def test_truncated_zero_dispatch(self):self.test_regression_truncated_stream()
    def test_changed_file_approval(self):self.test_regression_version_changed_during_approval()
    def test_compaction_atomicity(self):self.test_invalid_summaries_never_continue()
    def test_mcp_original_binding(self):self.test_regression_connection_replaced()
    def test_known_unknown_recovery(self):
        self.test_kill_after_effect();self.isolate('unknown-query');self.test_unknown_query_stops()
    def test_planned_wait_consumption(self):self.test_new_process_consumes_original_candidate()
    def test_document_cpu_staleness_citations(self):
        report=docs.run_suite(self.entry_point,self.embedding_model,self.directory/'docs',names=[
            'test_cpu_ingestion_and_original_ranges','test_same_revision_bytes_change_blocks',
            'test_citation_missing_fake_unseen_and_old'])
        self.assertEqual(report['failed'],0,report)

TESTS=[name for name in Checks.__dict__ if name.startswith('test_')]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('entry','embedding-model','sdk-python','runtime','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--case',choices=TESTS);a=p.parse_args()
    out=a.output_dir.absolute()/('check-'+uuid.uuid4().hex[:8]);out.mkdir(parents=True)
    class Bound(Checks):pass
    Bound.entry_point=a.entry.absolute();Bound.embedding_model=a.embedding_model.absolute()
    Bound.sdk_python=a.sdk_python.absolute();Bound.real_runtime=a.runtime.absolute();Bound.output_root=out
    names=[a.case] if a.case else TESTS
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Bound(name) for name in names))
    failures=[{'case':str(test),'traceback':trace} for test,trace in result.failures+result.errors]
    report={'evidence_source':'controlled-sse+real-local-effects','external_model_requests':0,
            'entry':str(Bound.entry_point),'groups':result.testsRun,'passed':result.testsRun-len(failures),
            'failed':len(failures),'failures':failures,'selected_cases':names}
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'directory':str(out),'groups':result.testsRun,'passed':report['passed'],'failed':report['failed']}))
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
