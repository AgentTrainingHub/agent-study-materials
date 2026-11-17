"""在当前入口运行八项既有合同；受控模型、真实本地资源，零外部 API。"""
import argparse,json,subprocess,sys,uuid
from pathlib import Path
CASES=[(5,'test_ui_display_projection'),(5,'test_cancel_model_sigint'),(6,'test_summary_committed_before_continuation'),
       (7,'test_mcp_double_service_binding'),(8,'test_kill_after_effect'),(9,'test_question_wait_has_no_new_request'),
       (10,'test_citation_missing_fake_unseen_and_old'),(11,'test_G01_revoke_before_worker_zero_http')]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('entry','sdk-python','embedding-model','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();root=a.output_dir.absolute()/('regression-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
    course=Path(__file__).resolve().parents[3];reports=[]
    for week,case in CASES:
        directory=root/f'w{week:02d}-{case}';directory.mkdir()
        if week<=9:
            # 当前 W09 harness 继承旧断言，并接住当前 w09_shell 受控边界。
            code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('w12_previous',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);r=m.run_suite(Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),[sys.argv[5]]);raise SystemExit(0 if r['verdict']=='pass' else 1)"
            argv=[sys.executable,'-c',code,str(course/'week-09/practices/PRA-W09/check_cli.py'),str(a.entry.resolve()),str(a.sdk_python.absolute()),str(directory/'current-harness'),case]
        else:
            argv=[sys.executable,str(course/f'week-{week:02d}/practices/PRA-W{week:02d}/check_cli.py'),'--entry',str(a.entry.resolve()),'--output-dir',str(directory),'--case',case,'--embedding-model',str(a.embedding_model.resolve())]
        (directory/'invocation.json').write_text(json.dumps(argv,ensure_ascii=False,indent=2)+'\n')
        result=subprocess.run(argv,capture_output=True,timeout=360)
        (directory/'stdout.txt').write_bytes(result.stdout);(directory/'stderr.txt').write_bytes(result.stderr)
        reports.append({'week':week,'case':case,'passed':result.returncode==0,'exit_code':result.returncode,'directory':str(directory)})
        print(json.dumps(reports[-1],ensure_ascii=False),flush=True)
    report={'count':8,'passed':sum(r['passed'] for r in reports),'cases':reports,'external_model_requests':0}
    (root/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'directory':str(root),**report},ensure_ascii=False))
    return 0 if report['passed']==8 else 1
if __name__=='__main__':raise SystemExit(main())
