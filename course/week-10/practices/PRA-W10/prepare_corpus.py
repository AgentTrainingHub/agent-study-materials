"""复制固定语料到全新目录；update/withdraw 显式复核原版本再修改副本。"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
from uuid import uuid4
from w10_io import canonical,decode,snapshot,write_json,RetrievalError
MATERIALS=Path(__file__).resolve().parent


def prepare(target):
    target=Path(target).absolute()
    if target.exists() or target.is_symlink():raise FileExistsError('保留已有语料目录：'+str(target))
    source=MATERIALS/'inputs/corpus'
    data={path.name:snapshot(source,path.name,max_bytes=16384).data for path in source.iterdir()}
    target.mkdir(parents=True,exist_ok=False)
    for name,content in data.items():
        with (target/name).open('xb') as handle:handle.write(content)
    return {'status':'ok','corpus':str(target),'documents':12,'manifest_revision':'1'}


def change(target,action,evidence_dir):
    target=Path(target).absolute();directory=Path(evidence_dir).absolute()
    if target==MATERIALS/'inputs/corpus':raise ValueError('变化练习只操作复制的语料。')
    before=snapshot(target,'manifest.json',max_bytes=16384)
    manifest=decode(before.data);plan=decode((MATERIALS/f'inputs/changes/{action}.json').read_bytes())
    if manifest['revision']!=plan['from_manifest_revision']:raise ValueError('变化清单的原版本不匹配。')
    item=plan['changes'][0];selected=[row for row in manifest['sources'] if row['document_id']==item['document_id']]
    if len(selected)!=1 or selected[0]['revision']!=item['from_revision']:raise ValueError('来源原修订不匹配。')
    row=selected[0];old=snapshot(target,row['path'])
    expected=(MATERIALS/('inputs/corpus/support-guide.md' if action=='update' else 'inputs/changes/support-guide-v2.md')).read_bytes()
    if old.data!=expected:raise ValueError('来源已有其他改动，保留原文件。')
    replacement=(MATERIALS/'inputs/changes'/item['replacement']).read_bytes() if item['replacement'] else None
    manifest['revision']=plan['to_manifest_revision'];row.update(revision=item['revision'],status=item['status'])
    directory.mkdir(parents=True,exist_ok=False)
    (directory/'manifest-before.json').write_bytes(before.data);(directory/'source-before.md').write_bytes(old.data)
    if replacement is not None:
        temp=target/('.replacement-'+uuid4().hex)
        with temp.open('xb') as handle:handle.write(replacement)
        if snapshot(target,row['path']).object!=old.object:raise ValueError('替换前对象变化。')
        os.replace(temp,target/row['path'])
    temp=target/('.manifest-'+uuid4().hex)
    with temp.open('xb') as handle:handle.write(canonical(manifest)+b'\n')
    if snapshot(target,'manifest.json',max_bytes=16384).data!=before.data:raise ValueError('替换前清单变化。')
    os.replace(temp,target/'manifest.json')
    (directory/'manifest-after.json').write_bytes((target/'manifest.json').read_bytes())
    (directory/'source-after.md').write_bytes((target/row['path']).read_bytes())
    result={'status':'ok','action':action,'manifest_revision':manifest['revision'],'document_id':row['document_id'],
            'source_revision':row['revision'],'source_status':row['status'],'artifact_dir':str(directory)}
    write_json(directory/'result.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--target',type=Path,required=True)
    parser.add_argument('--change',choices=('update','withdraw'));parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    try:
        if args.change and args.output_dir is None:raise ValueError('change需要output-dir保存前后原文。')
        result=change(args.target,args.change,args.output_dir) if args.change else prepare(args.target)
    except (OSError,ValueError,RetrievalError) as error:parser.error(str(error))
    print(canonical(result).decode())


if __name__=='__main__':main()
