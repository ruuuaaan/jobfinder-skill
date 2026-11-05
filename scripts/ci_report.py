"""把 unittest 日志里的失败块转成 GitHub Actions 注解。

失败时日志只有 "Process completed with exit code 1"，看具体用例还得开日志页；
这个脚本把 `FAIL:` / `ERROR:` 块打成 `::error::` 注解，直接出现在提交页与 API 里。

用法：python scripts/ci_report.py unit-tests.log
"""
import re
import sys
from pathlib import Path

MAX_CHARS = 3500
_ESCAPES = (('%', '%25'), ('\r', '%0D'), ('\n', '%0A'))


def _escape(text: str) -> str:
    for raw, encoded in _ESCAPES:
        text = text.replace(raw, encoded)
    return text


def failure_blocks(text: str) -> list[str]:
    """按 unittest 的段落切分，只保留 FAIL/ERROR 开头的块。"""
    parts = re.split(r'\n(?=(?:FAIL|ERROR): )', text)
    return [part.strip() for part in parts if part.lstrip().startswith(('FAIL: ', 'ERROR: '))]


def main(path: str) -> int:
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    blocks = failure_blocks(text)
    if not blocks:
        print('::error title=unit tests failed::日志里没有 FAIL/ERROR 块，请查看完整日志')
        return 0
    for block in blocks:
        lines = block.splitlines()
        title = _escape(lines[0][:120])
        detail = _escape('\n'.join(lines[:40])[:MAX_CHARS])
        print(f'::error title={title}::{detail}')
    print(f'{len(blocks)} 个失败块已转成注解')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'unit-tests.log'))
