"""采集组件打包约束的回归测试。

背景（2026-11-05 现场故障）：0.2.1 便携包点“圈选”报
``WindowCaptureError: ImportError ... DLL load failed while importing
_multiarray_umath``。根因是组件里混了两份 numpy——纯 Python 取自
``.capture-runtime``（2.4.6），C 扩展取自构建解释器的 site-packages
（2.2.6），ABI 不一致。下面三条断言锁住修复方式，避免再次混版：

1. 构建暂存目录必须排除所有 numpy 副本（只保留 cv2 / windows_capture）；
2. spec 必须从 ``DEALWATCH_CAPTURE_STAGE`` 取构建依赖目录；
3. 主程序打包前必须先跑组件依赖自检（“文件存在”不等于“能采集”）。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_build_module():
    """按路径加载 tools/build.py；它把重型导入都放在 main() 内，导入无副作用。"""
    spec = importlib.util.spec_from_file_location(
        "dealwatch_build_script", ROOT / "tools" / "build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureStageTests(unittest.TestCase):
    def test_stage_excludes_every_numpy_copy(self) -> None:
        build = load_build_module()
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / ".capture-runtime"
            for name in ("numpy", "numpy.libs", "numpy-2.2.6.dist-info", "cv2", "bin"):
                (runtime / name).mkdir(parents=True)
            (runtime / "numpy" / "_multiarray_umath.pyd").write_text("x", encoding="utf-8")
            (runtime / "cv2" / "cv2.pyd").write_text("x", encoding="utf-8")
            (runtime / "windows_capture-2.0.1.dist-info").mkdir()
            stage = Path(temporary) / "build" / "capture-stage"
            with mock.patch.object(build, "ROOT", Path(temporary)), mock.patch.object(build, "CAPTURE_RUNTIME", runtime), mock.patch.object(
                build, "CAPTURE_STAGE", stage
            ):
                produced = build.stage_capture_dependencies()
                names = sorted(item.name for item in produced.iterdir())

        self.assertIn("cv2", names)
        self.assertIn("bin", names)
        self.assertIn("windows_capture-2.0.1.dist-info", names)
        self.assertEqual([name for name in names if name.startswith("numpy")], [])

    def test_spec_reads_stage_environment_variable(self) -> None:
        text = (ROOT / "packaging" / "capture_worker.spec").read_text(encoding="utf-8")
        self.assertIn("DEALWATCH_CAPTURE_STAGE", text)
        self.assertIn("os.environ.get", text)

    def test_component_self_test_runs_before_main_app_packaging(self) -> None:
        text = (ROOT / "tools" / "build.py").read_text(encoding="utf-8")
        self.assertIn("verify_capture_component()", text)
        self.assertLess(
            text.index("verify_capture_component()"),
            text.index('"packaging/deal_watch.spec"'),
            "组件依赖自检必须在主程序打包之前执行",
        )

    def test_worker_exposes_import_self_test_flag(self) -> None:
        text = (ROOT / "src" / "deal_watch" / "capture_worker.py").read_text(encoding="utf-8")
        self.assertIn("--self-test", text)
        self.assertIn("def import_self_test", text)
        # 截断过短会把 numpy 的真实原因切掉，至少保留 600 字。
        self.assertNotIn('[:300]', text)


if __name__ == "__main__":
    unittest.main()
