"""独占保存 W10 工程备份并安装 W11 供给；保留状态模块实现位置。"""
import argparse,json,shutil,stat,os
from pathlib import Path
MATERIALS=Path(__file__).resolve().parent

def prepare(student):
    student=Path(student).absolute()
    if student.is_symlink() or not student.is_dir():raise ValueError('需要已有普通 my-pi 目录。')
    required=('main.py','docs_cli.py','retrieval.py','sources.py','context.py','journal.py','session.py','events.py','w05_support.py','embedding_worker.py','w10_io.py')
    for name in required:
        p=student/name
        if not p.is_file() or p.is_symlink() or p.stat().st_nlink!=1:raise ValueError('缺少普通 W10 文件：'+name)
    runtime=student/'supplies/w10/embedding-runtime.json'
    if not runtime.is_file() or runtime.is_symlink():raise ValueError('请先完成 W10 embedding-python 准备。')
    backup=student/'w10-before-upgrade'
    targets=[student/name for name in ('w11_io.py','w11_stream.py')]
    if backup.exists() or any(os.path.lexists(p) for p in targets):raise FileExistsError('W11 备份或供给已存在；不覆盖。')
    sources=list(student.glob('*.py'))
    for path in [*sources,*(student/'supplies').rglob('*')]:
        if path.is_symlink() or path.is_file() and path.stat().st_nlink!=1:raise ValueError('源文件身份不安全。')
    backup.mkdir()
    for path in sources:
        shutil.copy2(path,backup/path.name);(backup/path.name).chmod(stat.S_IRUSR|stat.S_IRGRP|stat.S_IROTH)
    shutil.copytree(student/'supplies',backup/'supplies')
    for path in (backup/'supplies').rglob('*'):
        if path.is_file():path.chmod(stat.S_IRUSR|stat.S_IRGRP|stat.S_IROTH)
    for target in targets:
        with target.open('xb') as handle:handle.write((MATERIALS/target.name).read_bytes())
    return {'status':'prepared','student_dir':str(student),'backup':str(backup),'installed':[p.name for p in targets],
            'student_modules':['memory.py','research.py','memory_cli.py'],'external_model_requests':0}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--student-dir',type=Path,required=True);a=p.parse_args()
    print(json.dumps(prepare(a.student_dir),ensure_ascii=False,indent=2))
