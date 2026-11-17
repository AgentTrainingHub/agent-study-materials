"""独占备份 W11 工程并安装浏览器供给；保留核心实现任务。"""
from __future__ import annotations
import argparse,json,os,stat,subprocess
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent
SUPPLIES=('w12_io.py','w12_browser.py','w12_site.py')
REQUIRED=('main.py','session.py','journal.py','context.py','events.py','stream.py','memory.py','research.py','retrieval.py','docs_cli.py','w05_support.py')
def regular(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('需要普通单硬链接文件：'+str(path))
    return path.read_bytes()
def prepare(student_dir,browser_python):
    student=Path(student_dir).absolute();python=Path(browser_python).absolute()
    if student.is_symlink() or not student.is_dir():raise ValueError('需要已有 W11 工程。')
    if not python.is_file() or not os.access(python,os.X_OK):raise ValueError('browser-python 必须为可执行解释器。')
    for name in REQUIRED:regular(student/name)
    backup=student/'.artifacts/w12-before-upgrade';runtime=student/'supplies/w12'
    for path in (backup,runtime,*[student/n for n in SUPPLIES]):
        if path.exists() or path.is_symlink():raise FileExistsError('保留已有文件：'+str(path))
    env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}}
    code="import json,sys;from importlib.metadata import version;print(json.dumps({'python':list(sys.version_info[:2]),'dependencies':{k:version(k) for k in ['playwright','greenlet','pyee','typing-extensions']}}))"
    completed=subprocess.run([str(python),'-c',code],capture_output=True,check=True,timeout=20,env=env);versions=json.loads(completed.stdout)
    expected={'python':[3,12],'dependencies':{'playwright':'1.64.0','greenlet':'3.5.6','pyee':'14.0.0','typing-extensions':'4.16.0'}}
    if versions!=expected:raise ValueError('固定依赖版本不符：'+json.dumps(versions))
    sources=[]
    for path in student.rglob('*.py'):
        relative=path.relative_to(student)
        if not any(part.startswith('.') or part=='__pycache__' for part in relative.parts):sources.append((relative,regular(path)))
    new={n:regular(MATERIALS/n) for n in SUPPLIES};backup.mkdir(parents=True)
    for relative,data in sources:
        target=backup/relative;target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as f:f.write(data)
        target.chmod(0o444)
    runtime.mkdir(parents=True)
    with (runtime/'browser-runtime.json').open('x') as f:json.dump({'python':str(python),'versions':versions,'channel':'chrome'},f)
    for name,data in new.items():
        with (student/name).open('xb') as f:f.write(data)
    return {'student_dir':str(student),'backup':str(backup),'installed':list(SUPPLIES),'browser_python':str(python),'versions':versions,'browser_downloads':0}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--student-dir',type=Path,required=True);p.add_argument('--browser-python',type=Path,required=True);a=p.parse_args()
    try:result=prepare(a.student_dir,a.browser_python)
    except (OSError,ValueError,subprocess.SubprocessError) as e:p.error(str(e))
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
