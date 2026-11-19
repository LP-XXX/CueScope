"""渲染 Windows 版本资源：版本号只有一个来源（``pyproject.toml``）。

``packaging/version_info.txt`` 是**模板**，本身不含任何版本数字；构建时由
:func:`ensure_version_info` 渲染出 ``build/version_info.txt`` 再交给 PyInstaller。

为什么不让版本资源自己读版本：PyInstaller 需要的是一个静态文件路径，运行时
没有机会读 ``__version__``。0.2.1 之前该文件把 ``0.2.1`` 写死在
``FileVersion`` / ``ProductVersion`` / ``filevers`` / ``prodvers`` 四处，升版
漏改就会让用户在 EXE 属性里看到旧版本 —— 与 ``self_check.py`` 写死版本号是同
一类缺陷。现在改版本只需动 ``pyproject.toml``。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

#: 模板位置（仓库内固定）。
TEMPLATE_NAME = "version_info.txt"

#: 模板里的占位标记；用标记替换而非 ``str.format``，避免模板结构里的括号被当成格式符。
VERSION_TOKEN = "@@VERSION@@"
FILEVERS_TOKEN = "@@FILEVERS@@"
PRODVERS_TOKEN = "@@PRODVERS@@"


def project_version(root: Path) -> str:
    """读取 ``pyproject.toml`` 的 ``project.version``：版本的唯一真值源。"""
    with (Path(root) / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """``"1.2.3"`` → ``(1, 2, 3, 0)``：Windows 版本资源要四段数字。"""
    parts = [int(item) for item in version.split(".")]
    if len(parts) != 3 or any(item < 0 for item in parts):
        raise ValueError(f"版本号必须是 主.次.修订 三段数字：{version!r}")
    return (parts[0], parts[1], parts[2], 0)


def render_version_info(template_text: str, version: str) -> str:
    """把模板渲染成真正的 ``VSVersionInfo`` 文本；占位缺失或残缺一律报错。

    两条防线：
    1. 模板里必须三个占位齐全 —— 少一个（比如漏了 ``@@PRODVERS@@``）会产出缺字段的
       版本资源，PyInstaller 不报错但 EXE 属性里那一项就是空的；
    2. 渲染后不允许残留 ``@@`` —— 占位拼错（如 ``@@PRODVERS@``）不会匹配任何已知
       标记，漏到 PyInstaller 就是“版本资源被静默丢弃”。
    """
    numbers = version_tuple(version)
    tuple_text = f"({numbers[0]}, {numbers[1]}, {numbers[2]}, {numbers[3]})"
    for token in (FILEVERS_TOKEN, PRODVERS_TOKEN, VERSION_TOKEN):
        if token not in template_text:
            raise ValueError(f"版本资源模板缺少占位：{token}")
    rendered = (
        template_text.replace(FILEVERS_TOKEN, tuple_text)
        .replace(PRODVERS_TOKEN, tuple_text)
        .replace(VERSION_TOKEN, version)
    )
    if "@@" in rendered:
        raise ValueError("版本资源模板里存在无法识别的占位（@@ 残留），请检查占位拼写")
    return rendered


def ensure_version_info(root: Path, *, component: bool = False) -> Path:
    """渲染并写出 ``build/version_info.txt``，返回给 PyInstaller 的路径。"""
    root = Path(root)
    template = (root / "packaging" / TEMPLATE_NAME).read_text(encoding="utf-8")
    if component:
        template = template.replace("'CueScope.exe'", "'DealWatchCapture.exe'").replace(
            "'CueScope 窗口文字监测与提醒'", "'CueScope 采集组件'")
    target = root / "build" / ("capture_version_info.txt" if component else TEMPLATE_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_version_info(template, project_version(root)), encoding="utf-8", newline="\n"
    )
    return target
