"""独占备份 W09 工程，并安装本地检索供给和显式编码解释器。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

MATERIALS=Path(__file__).resolve().parent
SUPPLIES=('w10_io.py','bm25.py','embedding_worker.py','w10_model_manifest.json')
REQUIRED=('main.py','session.py','journal.py','context.py','events.py','stream.py','workflow.py','workflow_run.py','decisions.py',
          'w01_support.py','w04_support.py','w05_support.py','w09_io.py','w09_shell.py')


def regular(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('需要普通单硬链接文件：'+str(path))
    return path.read_bytes()


def prepare(student_dir,embedding_python):
    student=Path(student_dir).absolute()
    if student.is_symlink() or not student.is_dir():raise ValueError('需要已有 W09 工程。')
    python=Path(embedding_python).absolute()
    if not python.is_file() or not os.access(python,os.X_OK):raise ValueError('embedding-python 不是可执行解释器。')
    for name in REQUIRED:regular(student/name)
    for path in (student/'.artifacts',student/'supplies'):
        if path.is_symlink() or path.exists() and not path.is_dir():raise ValueError('需要普通目录：'+str(path))
    backup=student/'.artifacts/w10-before-upgrade';runtime=student/'supplies/w10'
    for path in (backup,runtime,*[student/name for name in SUPPLIES]):
        if path.exists() or path.is_symlink():raise FileExistsError('保留已有文件：'+str(path))
    new={name:regular(MATERIALS/name) for name in SUPPLIES}
    env={k:v for k,v in os.environ.items() if k in {'PATH','LANG','LC_ALL','LC_CTYPE'}}
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1')
    code="import json,sys;from importlib.metadata import version;print(json.dumps({'python':list(sys.version_info[:2]),'dependencies':{k:version(k) for k in ['onnxruntime','tokenizers','numpy']}}))"
    checked=subprocess.run([str(python),'-c',code],env=env,capture_output=True,check=True,timeout=20)
    observed=json.loads(checked.stdout)
    if observed!={'python':[3,12],'dependencies':{'onnxruntime':'1.23.2','tokenizers':'0.22.2','numpy':'2.2.6'}}:
        raise ValueError('固定 Python/依赖版本不符：'+json.dumps(observed))
    rows=[]
    for path in student.rglob('*.py'):
        rel=path.relative_to(student)
        if not any(part.startswith('.') or part=='__pycache__' for part in rel.parts):rows.append((rel,regular(path)))
    backup.mkdir(parents=True,exist_ok=False)
    for rel,data in rows:
        path=backup/rel;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as handle:handle.write(data)
        path.chmod(0o444)
    runtime.mkdir(parents=True,exist_ok=False)
    with (runtime/'embedding-runtime.json').open('x') as handle:json.dump({'python':str(python)},handle,ensure_ascii=False)
    for name,data in new.items():
        with (student/name).open('xb') as handle:handle.write(data)
    return {'student_dir':str(student),'backup':str(backup),'installed':list(SUPPLIES),
            'embedding_python':str(python),'embedding_dependencies':observed,'model_downloads':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--student-dir',type=Path,required=True);parser.add_argument('--embedding-python',type=Path,required=True)
    args=parser.parse_args()
    try:result=prepare(args.student_dir,args.embedding_python)
    except (OSError,ValueError,subprocess.SubprocessError) as error:parser.error(str(error))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
