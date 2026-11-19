"""0.2.2：spec 交给 PyInstaller 的版本资源必须是**渲染产物**（离线，不构建）。

为什么要有这一层：``packaging/version_info.txt`` 现在是模板，里面只有
``@@FILEVERS@@`` / ``@@PRODVERS@@`` / ``@@VERSION@@``。把模板直接写进
``EXE(version=...)``，PyInstaller 要么解析失败，要么把版本资源静默丢掉 ——
后者更糟：构建“成功”，但用户右键 EXE 看不到版本号。

做法：在 **tmp 的 root** 里 ``exec`` 真实 spec（复制 ``pyproject.toml`` 与
``packaging/`` 三件套），把 ``Analysis`` / ``PYZ`` / ``EXE`` / ``COLLECT`` 换成记录
参数的替身，断言 ``EXE(version=...)`` 指向该 root 下的 ``build/version_info.txt``
且内容已渲染。**测试对真实 ``build/`` 零副作用**：谁在打包时并行跑测试，都不该
能让 PyInstaller 正在读的版本资源消失（那会伪装成“渲染链路没接通”）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_VERSION_INFO = REPO_ROOT / "build" / "version_info.txt"
TEMPLATE_NAMES = ("version_info.txt", "version_metadata.py")


def _stub_pyinstaller() -> dict[str, types.ModuleType]:
    """造出 spec 依赖的 PyInstaller 模块（只保留 spec 用到的那一个函数）。"""
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda name: ([], [], [])  # noqa: ARG005 - 只吞参数
    utils = types.ModuleType("PyInstaller.utils")
    utils.hooks = hooks
    package = types.ModuleType("PyInstaller")
    package.utils = utils
    return {
        "PyInstaller": package,
        "PyInstaller.utils": utils,
        "PyInstaller.utils.hooks": hooks,
    }


def make_temp_root(root: Path) -> None:
    """在 tmp root 里铺出 spec 需要的文件，让渲染产物落在 tmp 的 ``build/``。"""
    (root / "packaging").mkdir(parents=True)
    (root / "models").mkdir()
    shutil.copy2(REPO_ROOT / "models/manifest.json", root / "models/manifest.json")
    shutil.copy2(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    for name in TEMPLATE_NAMES:
        shutil.copy2(REPO_ROOT / "packaging" / name, root / "packaging" / name)


class SpecRun:
    """执行一次 spec，记录四个构建步骤收到的参数。"""

    def __init__(self, spec_path: Path) -> None:
        self.spec_path = spec_path
        self.analysis: dict = {}
        self.exe: dict = {}
        self.collect: dict = {}
        self.namespace: dict = {}

    def run(self) -> SpecRun:
        def recorder(step: str, result):
            def capture(*args, **kwargs):
                setattr(self, step, kwargs)
                return result

            return capture

        self.namespace = {
            "SPEC": str(self.spec_path),
            "Analysis": recorder(
                "analysis", types.SimpleNamespace(pure=[], scripts=[], datas=[], binaries=[])
            ),
            "PYZ": recorder("pyz", object()),
            "EXE": recorder("exe", "synthetic-exe"),
            "COLLECT": recorder("collect", "synthetic-collect"),
        }
        source = self.spec_path.read_text(encoding="utf-8")
        saved_path = list(sys.path)
        # 强制重新导入：证明 spec 自己在 sys.path 上找到了 version_metadata，而不是蹭缓存。
        sys.modules.pop("version_metadata", None)
        try:
            with patch.dict(sys.modules, _stub_pyinstaller()):
                exec(compile(source, str(self.spec_path), "exec"), self.namespace)  # noqa: S102
        finally:
            sys.path[:] = saved_path
            sys.modules.pop("version_metadata", None)
        return self

    @property
    def version_argument(self) -> str:
        return self.exe.get("version", "")


class SpecVersionResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        make_temp_root(self.root)
        self.build_version_info = self.root / "build" / "version_info.txt"
        # 真实 build/ 的快照：用例结束后必须一字不差。
        self.repo_before = (
            REPO_VERSION_INFO.read_bytes() if REPO_VERSION_INFO.exists() else None
        )

    def tearDown(self) -> None:
        repo_after = REPO_VERSION_INFO.read_bytes() if REPO_VERSION_INFO.exists() else None
        self.temp.cleanup()
        self.assertEqual(
            repo_after,
            self.repo_before,
            "用例改动了真实的 build/version_info.txt：打包期间并行跑测试会让 PyInstaller "
            "读不到版本资源，症状会伪装成“渲染链路没接通”",
        )

    def prepare_spec(self, name: str) -> Path:
        spec_path = self.root / "packaging" / name
        shutil.copy2(REPO_ROOT / "packaging" / name, spec_path)
        return spec_path

    def test_deal_watch_spec_renders_version_resource_itself(self) -> None:
        """单独执行 `pyinstaller packaging/deal_watch.spec` 也必须拿到渲染产物。"""
        run = SpecRun(self.prepare_spec("deal_watch.spec")).run()
        self.assertTrue(
            self.build_version_info.is_file(),
            "spec 执行后没有生成 build/version_info.txt，说明它没有走渲染函数",
        )
        self.assertEqual(Path(run.version_argument).resolve(), self.build_version_info.resolve())
        self.assertNotIn("@@", self.build_version_info.read_text(encoding="utf-8"))


    def test_product_name_follows_pyproject_version(self) -> None:
        """产物目录名与版本资源必须来自同一次读到的版本。"""
        sys.path.insert(0, str(REPO_ROOT / "packaging"))
        try:
            import version_metadata  # noqa: PLC0415 - 依赖 sys.path 调整
        finally:
            sys.path.remove(str(REPO_ROOT / "packaging"))
            sys.modules.pop("version_metadata", None)
        expected = version_metadata.project_version(REPO_ROOT)
        run = SpecRun(self.prepare_spec("deal_watch.spec")).run()
        self.assertEqual(run.collect.get("name"), f"CueScope-{expected}")
        self.assertEqual(run.exe.get("name"), "CueScope")
        resource = self.build_version_info.read_text(encoding="utf-8")
        self.assertIn(f"'FileVersion', '{expected}'", resource)
        self.assertIn(f"'ProductVersion', '{expected}'", resource)

    def test_icon_and_model_files_are_explicit_packaging_inputs(self) -> None:
        run = SpecRun(self.prepare_spec("deal_watch.spec")).run()
        self.assertEqual(Path(run.exe["icon"]).name, "cuescope.ico")
        data = run.analysis["datas"]
        model_inputs = [Path(source).name for source, target in data if target == "models"]
        self.assertIn("manifest.json", model_inputs)
        self.assertNotIn("models", model_inputs)
        self.assertEqual(len(model_inputs), 4)
        self.assertIn("deal_watch/assets/brand", [target for _, target in data])


class SpecSourceTests(unittest.TestCase):
    """源码级锁：不 exec，只读文本，任何环境都能跑。"""

    def test_version_argument_never_points_at_the_template(self) -> None:
        for name in ("deal_watch.spec", "capture_worker.spec"):
            with self.subTest(spec=name):
                text = (REPO_ROOT / "packaging" / name).read_text(encoding="utf-8")
                self.assertNotIn(
                    'version=str(ROOT / "packaging" / "version_info.txt")',
                    text,
                    f"{name} 把模板当版本资源交给 PyInstaller，EXE 属性会没有版本号",
                )

    def test_build_script_renders_before_pyinstaller(self) -> None:
        """双保险：spec 自己会渲染，build.py 也在调 PyInstaller 之前渲染一次。"""
        text = (REPO_ROOT / "tools" / "build.py").read_text(encoding="utf-8")
        self.assertIn("ensure_version_info(ROOT)", text)
        self.assertLess(
            text.index("ensure_version_info(ROOT)"),
            text.index('"packaging/capture_worker.spec"'),
            "渲染必须发生在任何一次 PyInstaller 调用之前",
        )


if __name__ == "__main__":
    unittest.main()
