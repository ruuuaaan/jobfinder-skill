"""Regenerate MANIFEST.sha256.json for the release allowlist (no user data, no git history)."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.idea', '.vscode'}
SKIP_FILES = {'MANIFEST.sha256.json'}
SKIP_SUFFIXES = {'.pyc', '.pyo', '.tmp'}


def digest(path):
    """按 LF 归一化后取哈希：同一份内容在 Windows/Linux 检出下必须得到同一摘要。"""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def release_files():
    files = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
            continue
        files.append(rel)
    return files


def main():
    files = release_files()
    check_path = ROOT / 'RELEASE-CHECK.json'
    if check_path.exists():
        check = json.loads(check_path.read_text(encoding='utf-8'))
        check['file_count'] = len(files) + 1  # 清单本身也属于发布包
        check_path.write_text(json.dumps(check, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    manifest = {rel: digest(ROOT / rel) for rel in files}
    (ROOT / 'MANIFEST.sha256.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(f'manifest updated: {len(manifest)} files')


if __name__ == '__main__':
    main()
