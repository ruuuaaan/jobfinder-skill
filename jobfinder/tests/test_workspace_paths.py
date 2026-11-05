"""工作区路径守卫：同一目录的别名写法不能被判成"项目之外"。

回归背景：GitHub Actions 的 Windows runner 把 %TEMP% 写成 `C:\\Users\\RUNNER~1\\...`，
`Path.resolve()` 会展开成 `runneradmin`；若只 resolve 待检路径、不 resolve 工作区根，
`is_relative_to` 就会把合法输出目录判成越界，领英浏览器检索的 5 个用例在 Windows 上全红。
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  —— 负责把 runtime 放进 sys.path
from jobfinder import service


def make_alias(alias: Path, target: Path) -> bool:
    """建一个指向 target 的目录别名：优先符号链接，Windows 上退回 junction（免管理员）。"""
    try:
        alias.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                              capture_output=True, text=True)
        return done.returncode == 0 and alias.exists()
    return False


def drop_alias(alias: Path) -> None:
    """junction / 符号链接都要按链接删除，不能跟进目标目录。"""
    try:
        os.rmdir(alias)          # junction 与目录符号链接都可直接 rmdir
    except OSError:
        try:
            alias.unlink()
        except OSError:
            pass


class WorkspaceAliasTests(unittest.TestCase):
    def test_workspace_alias_is_not_treated_as_outside(self):
        with tempfile.TemporaryDirectory(prefix="jobfinder-alias-") as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            alias = Path(tmp) / "alias"
            if not make_alias(alias, workspace):
                self.skipTest("当前环境既不能建符号链接也不能建 junction")

            original = service.ROOT
            service.ROOT = alias          # 未 resolve 的别名写法，等同 CI 上的 8.3 短名
            try:
                target = alias / "outputs" / "real" / "run-1"
                expected = (workspace / "outputs" / "real" / "run-1").resolve()
                self.assertEqual(service.local_path(target), expected)
                self.assertEqual(service.output_path(target, "real"), expected)
                # 越界仍然必须拒绝
                with self.assertRaises(ValueError):
                    service.local_path(Path(tmp) / "outside" / "x.json")
            finally:
                service.ROOT = original
                drop_alias(alias)


if __name__ == "__main__":
    unittest.main()
