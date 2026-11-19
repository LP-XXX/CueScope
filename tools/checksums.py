"""SHA256 校验清单：行尾固定 LF，且只在这里生成。

为什么单独成模块：清单原先在 ``tools/build.py`` 与 ``tools/finalize_package.py``
各写一遍。Windows 上 ``Path.write_text`` 不传 ``newline`` 时会把 ``\\n`` 翻成
``os.linesep``（CRLF），于是清单里每行末尾多一个 ``\\r``。``sha256sum -c`` 逐行按
``f"{digest}  {name}"`` 解析文件名，那个 ``\\r`` 会被当成文件名的一部分：

    sha256sum: '_internal/_asyncio.pyd'$'\\r': No such file or directory
    _internal/_asyncio.pyd: FAILED open or read

用户照着交付说明自校验时会看到一整屏 FAILED —— 这种“看着像安全告警、其实是我们
自己的格式问题”最容易把信任打掉，比一个显式报错更糟。所以：

* 清单与 ``.sha256`` 旁挂文件一律 ``newline="\\n"``；
* 生成逻辑收敛到本模块，避免两份实现再次漂移。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: 清单文件名（包内根目录）。
MANIFEST_NAME = "SHA256SUMS.txt"


def file_hash(path: Path) -> str:
    """流式计算 SHA256：不把整文件读进内存（包内单文件可能上百 MB）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_lines(directory: Path) -> list[str]:
    """``{sha256}␠␠{posix 相对路径}``，按路径排序，**不含清单自身**。"""
    directory = Path(directory)
    manifest = directory / MANIFEST_NAME
    return [
        f"{file_hash(path)}  {path.relative_to(directory).as_posix()}"
        for path in sorted(
            item for item in directory.rglob("*") if item.is_file() and item != manifest
        )
    ]


def write_manifest(directory: Path) -> Path:
    """写 ``SHA256SUMS.txt``（LF），返回该文件路径。"""
    directory = Path(directory)
    manifest = directory / MANIFEST_NAME
    manifest.write_text(
        "\n".join(manifest_lines(directory)) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def write_sidecar(target: Path, digest: str, name: str | None = None) -> Path:
    """写单条哈希旁挂文件（``*.zip.sha256``），同样是 LF。

    ``name`` 默认取 ``target.name``；ZIP 旁挂文件记录的是 ZIP 自己的名字。
    """
    target = Path(target)
    target.write_text(
        f"{digest}  {name or target.name}\n", encoding="utf-8", newline="\n"
    )
    return target
