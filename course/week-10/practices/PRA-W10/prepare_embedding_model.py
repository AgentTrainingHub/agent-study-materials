"""准备固定版本 E5 文件；仅显式准备时下载，推理仍完全离线。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

MATERIALS = Path(__file__).resolve().parent
REMOTE_FILES = {
    'model.onnx': 'onnx/model.onnx',
    'tokenizer.json': 'onnx/tokenizer.json',
    'config.json': 'config.json',
    'tokenizer_config.json': 'tokenizer_config.json',
}


def verify(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected['bytes']:
        raise ValueError('模型文件类型或大小不符：' + str(path))
    with path.open('rb') as stream:
        observed = hashlib.file_digest(stream, 'sha256').hexdigest()
    if observed != expected['sha256']:
        raise ValueError('模型文件与固定清单不符：' + str(path))


def prepare(directory, source_dir=None):
    directory = Path(directory).absolute()
    if '..' in directory.parts or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('目标须为不经过符号链接的物理绝对路径。')
    manifest = json.loads((MATERIALS / 'w10_model_manifest.json').read_text())
    # 独占目录保留既有模型；下载失败保留 .part 与已完成文件供排查。
    directory.mkdir(parents=True, exist_ok=False)
    for name, expected in manifest['files'].items():
        partial = directory / (name + '.part')
        if name == 'representation.json':
            source = MATERIALS / 'inputs/embedding-representation.json'
            with source.open('rb') as reader, partial.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
        elif source_dir is not None:
            source = Path(source_dir) / name
            if source.is_symlink():
                raise ValueError('本地模型源文件不能是符号链接：' + str(source))
            with source.open('rb') as reader, partial.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
        else:
            url = (f"https://huggingface.co/{manifest['model']}/resolve/"
                   f"{manifest['revision']}/{REMOTE_FILES[name]}")
            with urllib.request.urlopen(url, timeout=60) as reader, partial.open('xb') as writer:
                shutil.copyfileobj(reader, writer)
        verify(partial, expected)
        partial.rename(directory / name)
    return {'directory': str(directory), 'revision': manifest['revision'],
            'files': list(manifest['files']), 'external_model_requests': 0,
            'source': 'local-copy' if source_dir is not None else 'official-fixed-revision'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--source-dir', type=Path, help='可选：从已取得的固定模型目录复制四份上游文件')
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory, args.source_dir), ensure_ascii=False))


if __name__ == '__main__':
    main()
