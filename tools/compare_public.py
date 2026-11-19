"""Print a read-only migration inventory relative to a public CueScope checkout."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def compare(development, public, additions):
    if not (public / ".git").exists() or development.resolve() == public.resolve():
        raise ValueError("请选择不同的公开 Git 工作区和日常开发目录")
    tracked = subprocess.check_output(["git", "-C", str(public), "ls-files", "-z"]).decode().split("\0")
    for name in sorted(set(filter(None, [*tracked, *additions]))):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("仅支持工作区内的项目相对路径")
        source = development / relative
        target = public / relative
        if not source.is_file():
            kind = "D" if target.is_file() else "?"
        elif not target.is_file():
            kind = "A"
        else:
            a, b = source.read_bytes(), target.read_bytes()
            if a == b:
                continue
            kind = "EOL" if b"\0" not in a + b and a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n") else "M"
        yield kind, relative.as_posix()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("public", type=Path)
    parser.add_argument("--development", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--add", action="append", default=[], help="Explicit new public path to compare")
    args = parser.parse_args()
    for status, path in compare(args.development, args.public, args.add):
        print(f"{status}\t{path}")
