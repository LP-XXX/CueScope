"""0.2.2：SHA256 校验清单必须是 **LF** 行尾，否则用户的自校验命令整屏报错。

现场症状（0.2.0 / 0.2.1 的包都有，不是本轮引入）：

    $ sha256sum -c SHA256SUMS.txt
    sha256sum: '_internal/_asyncio.pyd'$'\\r': No such file or directory
    _internal/_asyncio.pyd: FAILED open or read

原因：Windows 上 ``Path.write_text`` 不传 ``newline`` 时把 ``\\n`` 翻成 ``\\r\\n``，
``sha256sum`` 按 ``f"{digest}  {name}"`` 取文件名，那个 ``\\r`` 就成了文件名的一部分。

这里分两层守：单测盯 ``tools/checksums.py`` 的写盘行为（任何环境都跑）；产物用例对
真实 ``dist/DealWatch-<version>/`` 跑，包括**真的执行一次 `sha256sum -c`**。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from checksums import (  # noqa: E402 - 依赖上面的 sys.path 调整
    MANIFEST_NAME,
    file_hash,
    manifest_lines,
    write_manifest,
    write_sidecar,
)

#: ``{64 位小写 hex}␠␠{相对路径}``：sha256sum 认的就是这个格式（两个空格）。
MANIFEST_LINE = re.compile(r"^[0-9a-f]{64}  \S.*$")


def project_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


class ManifestWriterTests(unittest.TestCase):
    """单元层：生成的字节必须是 LF，格式必须被 sha256sum 认识。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sub").mkdir()
        (self.root / "a.txt").write_bytes(b"a")
        (self.root / "sub" / "b.bin").write_bytes(b"b" * 4096)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_manifest_bytes_contain_no_carriage_return(self) -> None:
        raw = write_manifest(self.root).read_bytes()
        self.assertNotIn(
            bytes([13]),
            raw,
            "校验清单里出现 CR：sha256sum -c 会把 \\r 当成文件名的一部分，逐条 FAILED",
        )

    def test_lines_are_sorted_posix_and_well_formed(self) -> None:
        lines = write_manifest(self.root).read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("  ", 1)[1] for line in lines], ["a.txt", "sub/b.bin"])
        for line in lines:
            self.assertRegex(line, MANIFEST_LINE)

    def test_manifest_excludes_itself_and_is_idempotent(self) -> None:
        first = write_manifest(self.root).read_bytes()
        second = write_manifest(self.root).read_bytes()
        self.assertEqual(first, second, "重复生成必须得到相同结果（清单不能把自己算进去）")
        self.assertNotIn(MANIFEST_NAME, first.decode("utf-8"))

    def test_hash_values_match_file_contents(self) -> None:
        expected = {
            line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in write_manifest(self.root).read_text(encoding="utf-8").splitlines()
        }
        self.assertEqual(expected["a.txt"], file_hash(self.root / "a.txt"))
        self.assertEqual(
            expected["sub/b.bin"], file_hash(self.root / "sub" / "b.bin")
        )
        self.assertEqual(len(manifest_lines(self.root)), 2)

    def test_sidecar_is_lf(self) -> None:
        target = self.root / "DealWatch-x-win-x64.zip.sha256"
        write_sidecar(target, "0" * 64, "DealWatch-x-win-x64.zip")
        raw = target.read_bytes()
        self.assertNotIn(bytes([13]), raw, "ZIP 旁挂校验值同样必须 LF")
        self.assertEqual(raw.decode("utf-8"), "0" * 64 + "  DealWatch-x-win-x64.zip\n")

    @unittest.skipUnless(shutil.which("sha256sum"), "没有 sha256sum（非 Git Bash / 非 WSL）")
    def test_generated_manifest_is_accepted_by_sha256sum(self) -> None:
        """不依赖构建产物也能验格式：让真工具读一遍我们写的清单。"""
        (self.root / "中文 名称.txt").write_bytes(b"cjk")
        write_manifest(self.root)
        result = subprocess.run(
            ["sha256sum", "-c", MANIFEST_NAME],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        self.assertNotIn("FAILED", result.stdout)


class BuiltArtifactTests(unittest.TestCase):
    """产物层：对真实 ``dist/DealWatch-<version>/`` 跑。没有构建产物时跳过。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dist = Path(os.environ.get("CUESCOPE_ARTIFACT_DIR", str(REPO_ROOT / "dist" / f"CueScope-{project_version()}")))
        cls.manifest = cls.dist / MANIFEST_NAME
        if not cls.manifest.is_file():
            raise unittest.SkipTest(f"没有构建产物：{cls.manifest}")

    def test_artifact_manifest_is_lf(self) -> None:
        raw = self.manifest.read_bytes()
        first_cr = raw.find(bytes([13]))
        self.assertEqual(
            first_cr,
            -1,
            f"{self.manifest} 含 CR（首个在偏移 {first_cr}）；用户按交付说明执行 "
            "sha256sum -c 会看到整屏 FAILED",
        )

    def test_artifact_entries_cover_every_file_except_itself(self) -> None:
        lines = self.manifest.read_text(encoding="utf-8").splitlines()
        files = [item for item in self.dist.rglob("*") if item.is_file()]
        self.assertEqual(len(lines), len(files) - 1, "清单条目数应等于包内文件数减一（不含清单自身）")
        for line in lines:
            self.assertRegex(line, MANIFEST_LINE)
        listed = {line.split("  ", 1)[1] for line in lines}
        actual = {
            item.relative_to(self.dist).as_posix() for item in files if item != self.manifest
        }
        self.assertEqual(listed, actual, "清单条目与包内实际文件不一致")

    def test_artifact_hashes_all_match(self) -> None:
        for line in self.manifest.read_text(encoding="utf-8").splitlines():
            digest, relative = line.split("  ", 1)
            with self.subTest(path=relative):
                self.assertEqual(file_hash(self.dist / relative), digest)

    @unittest.skipUnless(shutil.which("sha256sum"), "没有 sha256sum（非 Git Bash / 非 WSL）")
    def test_sha256sum_check_passes(self) -> None:
        """用户真的会敲的那条命令：任何一行坏掉都会让这一条红。"""
        result = subprocess.run(
            ["sha256sum", "-c", MANIFEST_NAME],
            cwd=self.dist,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertNotIn("FAILED", result.stdout)


class ToolSourceTests(unittest.TestCase):
    """源码级锁：两个入口都必须走共享的 LF 写入，不许再各自 write_text。"""

    def test_both_tools_use_shared_writer(self) -> None:
        build = (REPO_ROOT / "tools" / "build.py").read_text(encoding="utf-8")
        finalize = (REPO_ROOT / "tools" / "finalize_package.py").read_text(encoding="utf-8")
        self.assertIn("write_manifest(dist)", build)
        self.assertIn("write_manifest(DIST)", finalize)
        self.assertIn("write_sidecar(", finalize)
        for name, text in (("build.py", build), ("finalize_package.py", finalize)):
            with self.subTest(tool=name):
                self.assertNotIn(
                    'write_text("\\n".join(lines)',
                    text,
                    f"{name} 又自己拼清单写盘了：Windows 上会落成 CRLF",
                )


if __name__ == "__main__":
    unittest.main()
