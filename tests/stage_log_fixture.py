"""阶段日志（capture-stages.log）的测试替身：把落盘内容收进列表再断言。

单独成模块的原因：T1.5/T1.6 的四个测试文件都要断言阶段行形状，
各写一份 ``_ListHandler`` 只会让断言漂移。
"""

from __future__ import annotations

import logging

from deal_watch.diagnostics import CAPTURE_STAGE_LOGGER_NAME


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(record.getMessage())
        except Exception:  # noqa: BLE001 - 格式化失败必须让断言看见，不能静默
            self.sink.append("<format-error>")


class StageLogCapture:
    """上下文管理器：临时把阶段日志的内容收进 ``messages``。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._handler = _ListHandler(self.messages)
        self._logger = logging.getLogger(CAPTURE_STAGE_LOGGER_NAME)
        self._level = self._logger.level
        self._propagate = self._logger.propagate

    def __enter__(self) -> list[str]:
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        return self.messages

    def __exit__(self, *exc_info) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        self._logger.propagate = self._propagate


def parse_line(message: str) -> dict[str, str]:
    """把一行 ``k=v k=v`` 拆成字典，便于逐字段断言。"""
    fields: dict[str, str] = {}
    for token in message.split(" "):
        key, _, value = token.partition("=")
        if key:
            fields[key] = value
    return fields


def find_lines(messages: list[str], stage: str) -> list[dict[str, str]]:
    """取出某个阶段名对应的全部字段字典。"""
    return [
        parse_line(message)
        for message in messages
        if parse_line(message).get("stage") == stage
    ]
