"""0.2.2：Windows 版本资源由构建脚本按 ``pyproject.toml`` 渲染（离线，无 Qt）。

0.2.1 的 ``packaging/version_info.txt`` 把版本号写死在 ``filevers`` / ``prodvers`` /
``FileVersion`` / ``ProductVersion`` 四处，升版漏改就让用户在 EXE 属性里看到旧版本，
与 ``self_check.py`` 写死版本号是同一类“版本字面量”缺陷。现在该文件只是模板，
渲染结果才交给 PyInstaller。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 打包器读版本资源用的就是它：把渲染结果交给真解析器，才算证明“能被嵌进 EXE”。
try:
    from PyInstaller.utils.win32.versioninfo import (
        load_version_info_from_text_file as _load_version_info,
    )
except Exception:  # pragma: no cover - 未装 PyInstaller（非 Windows / 非 dev 环境）
    _load_version_info = None


def load_version_metadata():
    """按文件路径加载 ``packaging/version_metadata.py``（它不在包内，不能直接 import）。"""
    path = REPO_ROOT / "packaging" / "version_metadata.py"
    spec = importlib.util.spec_from_file_location("version_metadata_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_version_metadata()
        self.template = (REPO_ROOT / "packaging" / self.module.TEMPLATE_NAME).read_text(
            encoding="utf-8"
        )

    def test_template_holds_no_version_number(self) -> None:
        """模板里一旦出现版本数字，升版就会漏改并让 EXE 属性显示旧版本。"""
        self.assertNotRegex(self.template, r"\d+\.\d+\.\d+")
        for token in (
            self.module.VERSION_TOKEN,
            self.module.FILEVERS_TOKEN,
            self.module.PRODVERS_TOKEN,
        ):
            self.assertIn(token, self.template, f"模板缺少占位 {token}")

    def test_render_replaces_all_four_version_sites(self) -> None:
        rendered = self.module.render_version_info(self.template, "9.8.7")
        self.assertIn("filevers=(9, 8, 7, 0)", rendered)
        self.assertIn("prodvers=(9, 8, 7, 0)", rendered)
        self.assertIn("'FileVersion', '9.8.7'", rendered)
        self.assertIn("'ProductVersion', '9.8.7'", rendered)
        for token in (
            self.module.VERSION_TOKEN,
            self.module.FILEVERS_TOKEN,
            self.module.PRODVERS_TOKEN,
        ):
            self.assertNotIn(token, rendered, f"渲染后仍残留占位 {token}")
        # 结构必须仍是可被 PyInstaller eval 的 VSVersionInfo，而不是残片。
        self.assertTrue(rendered.lstrip().startswith("# UTF-8"))
        self.assertIn("VSVersionInfo(", rendered)
        self.assertIn("VarFileInfo([VarStruct('Translation', [2052, 1200])])", rendered)

    def test_packaging_marker_is_first_line(self) -> None:
        """PyInstaller 靠首行的编码标记读版本资源；中文 FileDescription 依赖它。"""
        rendered = self.module.render_version_info(self.template, "1.0.0")
        self.assertEqual(rendered.splitlines()[0], "# UTF-8")

    def test_unreplaced_placeholder_is_rejected(self) -> None:
        """占位拼错（少一个 @）必须报错，而不是把残缺标记喂给 PyInstaller。"""
        broken = self.template.replace(self.module.PRODVERS_TOKEN, "@@PRODVERS@", 1)
        with self.assertRaises(ValueError):
            self.module.render_version_info(broken, "1.0.0")

    def test_missing_placeholder_is_rejected(self) -> None:
        """模板少一个占位（漏替换）必须报错：否则产出缺字段的版本资源，静默丢版本号。"""
        for token in (
            self.module.FILEVERS_TOKEN,
            self.module.PRODVERS_TOKEN,
            self.module.VERSION_TOKEN,
        ):
            with self.subTest(token=token):
                broken = self.template.replace(token, "")
                with self.assertRaises(ValueError):
                    self.module.render_version_info(broken, "1.0.0")

    def test_malformed_version_is_rejected(self) -> None:
        for bad in ("1.0", "1.0.0.0", "1.0.x"):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    self.module.version_tuple(bad)


class EnsureVersionInfoTests(unittest.TestCase):
    def test_capture_component_keeps_its_compatible_executable_identity(self) -> None:
        module = load_version_metadata()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "packaging").mkdir()
            (root / "packaging" / module.TEMPLATE_NAME).write_bytes(
                (REPO_ROOT / "packaging" / module.TEMPLATE_NAME).read_bytes())
            (root / "pyproject.toml").write_text(
                '[project]\nversion = "3.2.1"\n', encoding="utf-8")
            target = module.ensure_version_info(root, component=True)
            text = target.read_text(encoding="utf-8")
            self.assertIn("'OriginalFilename', 'DealWatchCapture.exe'", text)
            self.assertIn("'ProductName', 'CueScope'", text)
            self.assertEqual(target.name, "capture_version_info.txt")

    def test_renders_into_build_directory_with_lf(self) -> None:
        module = load_version_metadata()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "packaging").mkdir()
            (root / "packaging" / module.TEMPLATE_NAME).write_bytes(
                (REPO_ROOT / "packaging" / module.TEMPLATE_NAME).read_bytes()
            )
            (root / "pyproject.toml").write_text(
                '[project]\nname = "synthetic"\nversion = "3.2.1"\n', encoding="utf-8"
            )
            target = module.ensure_version_info(root)
            self.assertEqual(target, root / "build" / module.TEMPLATE_NAME)
            self.assertTrue(target.is_file())
            raw = target.read_bytes()
            self.assertNotIn(bytes([13]), raw, "渲染结果必须是 LF，避免打包机行尾差异")
            text = raw.decode("utf-8")
            self.assertIn("'FileVersion', '3.2.1'", text)
            self.assertIn("filevers=(3, 2, 1, 0)", text)

    def test_repository_version_matches_pyproject(self) -> None:
        """渲染链路必须读 ``pyproject.toml``，而不是自带一份版本号。"""
        module = load_version_metadata()
        expected = module.project_version(REPO_ROOT)
        rendered = module.render_version_info(
            (REPO_ROOT / "packaging" / module.TEMPLATE_NAME).read_text(encoding="utf-8"),
            expected,
        )
        self.assertIn(f"'FileVersion', '{expected}'", rendered)


@unittest.skipUnless(_load_version_info, "未安装 PyInstaller，跳过打包器解析验证")
class PyInstallerParseTests(unittest.TestCase):
    """把渲染结果交给**打包器自己的解析器**，证明它能被读成 0.2.2。

    手工右键 EXE 看属性只能验一次；这里守的是“文件一旦写不出来就会被发现”：
    编码标记（中文 FileDescription 依赖它）、四段版本元组的形状、字符串表结构，
    任何一处不对都会在这里失败，而不是等到发版后用户看不到版本号。
    """

    def parse(self, rendered: str):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "version_info.txt"
            path.write_text(rendered, encoding="utf-8", newline="\n")
            return _load_version_info(str(path))

    def test_rendered_resource_parses_to_pyproject_version(self) -> None:
        module = load_version_metadata()
        expected = module.project_version(REPO_ROOT)
        rendered = module.render_version_info(
            (REPO_ROOT / "packaging" / module.TEMPLATE_NAME).read_text(encoding="utf-8"),
            expected,
        )
        info = self.parse(rendered)

        major, minor, patch = (int(part) for part in expected.split("."))
        self.assertEqual(info.ffi.fileVersionMS, (major << 16) | minor)
        self.assertEqual(info.ffi.fileVersionLS, patch << 16)
        self.assertEqual(info.ffi.productVersionMS, (major << 16) | minor)
        self.assertEqual(info.ffi.productVersionLS, patch << 16)

        strings = {entry.name: entry.val for entry in info.kids[0].kids[0].kids}
        self.assertEqual(strings.get("FileVersion"), expected)
        self.assertEqual(strings.get("ProductVersion"), expected)
        # 中文描述能读出来，才说明 `# UTF-8` 首行标记生效（否则打包时是乱码或异常）。
        self.assertEqual(strings.get("FileDescription"), "CueScope 窗口文字监测与提醒")
        self.assertEqual(strings.get("OriginalFilename"), "CueScope.exe")


if __name__ == "__main__":
    unittest.main()
