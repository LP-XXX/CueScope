"""测试基础设施：让"意外弹出的模态框"立即失败，而不是把整轮测试挂死。

背景（G0 取证，2026-11-05）：`MainWindow.start_monitoring` 与 `config_slot` 在兜住
异常后会调用 `QMessageBox.critical`。`QT_QPA_PLATFORM=offscreen` 下模态框的事件循环
照常运行，但没有人能点"确定"，于是用例永久阻塞——`pytest` 不会超时，只能靠外层
`taskkill` 收场，整轮回归停在 8%/90% 不动，也看不到真正的异常。

这里把 4 个**会阻塞**的静态入口换成"记录 + 抛 AssertionError"：
- 断言失败立刻可见，异常文本里带着弹框标题与正文（正文通常就是被吞掉的原因）；
- 只替换静态方法，不碰实例 API；用例自己 `patch(...)` 时仍然优先，行为不变；
- 不改变任何用例的判定标准，只把"无限等待"换成"明确失败"。
"""

from __future__ import annotations

import os

# 必须在导入 Qt 之前定下来：测试不允许占用真实屏幕，也不允许落到 windows 平台插件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ModalDialogCalled(AssertionError):
    """用例执行路径上出现了本不该有的模态对话框。"""


def _install_dialog_guard() -> None:
    try:
        from PySide6.QtWidgets import QMessageBox
    except Exception:  # pragma: no cover - 没有 Qt 的环境照常跑非 UI 用例
        return

    def make(name: str):
        def guard(parent=None, title="", text="", *args, **kwargs):
            raise ModalDialogCalled(
                f"执行路径上弹出了模态对话框 QMessageBox.{name}："
                f"title={title!r} text={text!r}"
                "（离线平台下它会永久阻塞，请先用 patch 替换该对话框）"
            )

        guard.__name__ = f"guarded_{name}"
        return staticmethod(guard)

    for name in ("critical", "warning", "information", "question"):
        setattr(QMessageBox, name, make(name))


_install_dialog_guard()
