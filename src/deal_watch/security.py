"""Windows DPAPI 凭据保护。非 Windows 不提供不安全的明文回退。"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import default_data_dir, validate_webhook_url


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 上可用")
    source, source_buffer = _blob(data)
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "DealWatch", None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


def unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI 仅在 Windows 上可用")
    source, source_buffer = _blob(data)
    target = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        del source_buffer


class CredentialStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.path = (data_dir or default_data_dir()) / "credentials.dpapi"

    def save(self, webhook: str, secret: str) -> None:
        if webhook:
            validate_webhook_url(webhook)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"webhook": webhook, "secret": secret}).encode("utf-8")
        encrypted = base64.b64encode(protect(raw))
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, self.path)

    def load(self) -> tuple[str, str]:
        if not self.path.exists():
            return "", ""
        raw = json.loads(unprotect(base64.b64decode(self.path.read_bytes())).decode("utf-8"))
        return raw.get("webhook", ""), raw.get("secret", "")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def destination_id(kind: str, *identity_parts: str) -> str:
    """Bind to an exact provider/account/recipient without publishing its address."""
    if not isinstance(kind, str) or not kind.isascii() or not kind.isidentifier():
        raise ValueError("渠道类型无效")
    if not identity_parts or any(not isinstance(part, str) or not part.strip() for part in identity_parts):
        raise ValueError("接收端身份不能为空")
    raw = json.dumps([kind, *identity_parts], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{kind}-{hashlib.sha256(raw).hexdigest()}"


class DestinationCredentialStore:
    """Atomic, DPAPI-protected channel configuration; legacy credentials remain readable."""

    def __init__(self, data_dir: Path | None = None):
        self.path = (data_dir or default_data_dir()) / "notification-destinations.dpapi"

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        value = json.loads(unprotect(self.path.read_bytes()).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("通知目标配置无效")
        return value

    def save(self, value: dict) -> None:
        if not isinstance(value, dict):
            raise ValueError("通知目标配置无效")
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        encrypted = protect(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=self.path.parent, prefix="destinations-", suffix=".tmp", delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            with temporary.open("wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            if unprotect(temporary.read_bytes()) != raw:
                raise OSError("通知目标配置校验失败")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
