"""T1.5 采集请求预算与阶段埋点。

离线判定四件事，全部不接触真实窗口：

1. **预算真实存在**：子进程睡 6 秒时，父进程墙钟必须 ≤5.6 秒（5.0 预算 + 0.5 强杀宽限）；
2. **等帧超时**仍然 ≤4.0 秒（子进程 3.2 秒后自己报 ``WGC_FRAME_TIMEOUT``）；
3. **会话退出卡住**（``control.wait()`` 永不返回的等价场景）下，父进程仍能在预算内
   返回，并标记 ``orphan_suspected``，绝不无限 ``communicate()``；
4. **失败阶段可区分**，且阶段日志只有白名单字段，不含图像/聊天正文/窗口标题。

真实子进程用的是 ``python -c`` 脚本（与 ``capture_worker`` 走同一条 Popen + 管道
路径），因此测的是父进程的预算逻辑本身，不是替身。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from deal_watch.diagnostics import STAGE_FIELD_ALLOWLIST
from deal_watch.models import CaptureErrorCode
from deal_watch.window_capture import (
    CAPTURE_KILL_GRACE_SECONDS,
    DEFAULT_SESSION_EXIT_TIMEOUT_MS,
    MAX_LIVE_READERS,
    MAX_PENDING_STARTS,
    MAX_SESSION_EXIT_TIMEOUT_MS,
    PROCESS_REGISTRY,
    START_SUPERVISOR,
    CaptureTrace,
    WindowCaptureClient,
    WindowCaptureError,
    clamp_session_exit_timeout,
    last_stage,
    parse_stage_lines,
)
from stage_log_fixture import StageLogCapture

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# 子进程脚本：只用标准库，避免把 .capture-runtime 拉进测试。
SLEEP_SCRIPT = "import sys, time\ntime.sleep(6)\n"

FRAME_TIMEOUT_SCRIPT = """
import json, sys, time

def stage(name, t_ms):
    print(json.dumps({"stage": name, "t_ms": t_ms, "request_id": sys.argv[1] if len(sys.argv) > 1 else ""}),
          file=sys.stderr, flush=True)

stage("component_enter", 0.0)
stage("deps_init", 210.0)
time.sleep(3.2)
stage("session_exit", 3410.0)
print(json.dumps({"ok": False, "error": "TimeoutError: WGC_FRAME_TIMEOUT",
                  "stage_failed": "frame_wait"}), flush=True)
"""

SUCCESS_SCRIPT = """
import base64, io, json, sys

# 4x3 纯色 PNG，避免依赖 PIL 之外的东西；父进程用 PIL 解码。
import zlib, struct

def png(width, height, rgb):
    raw = b"".join(b"\\x00" + bytes(rgb) * width for _ in range(height))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

print(json.dumps({"stage": "component_enter", "t_ms": 0.0, "request_id": ""}),
      file=sys.stderr, flush=True)
print(json.dumps({"ok": True, "request_id": sys.argv[-1], "width": 4, "height": 3,
                  "frame_width": 4, "frame_height": 3, "roi": None,
                  "captured_at": 12.5, "backend": "wgc",
                  "image": base64.b64encode(png(4, 3, (20, 30, 40))).decode("ascii"),
                  "stages": {"component_enter_ms": 0.0, "total_ms": 12.0}}), flush=True)
"""


class FakeProcess:
    """``subprocess.Popen`` 替身：可控超时、可控强杀，用于构造现场难复现的形态。"""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        *,
        stall_first_wait: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        import io

        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False
        self.waits = 0
        self._stall_first_wait = stall_first_wait
        self._ignore_kill = ignore_kill
        self._exited = not (stall_first_wait or ignore_kill)

    def wait(self, timeout=None):
        self.waits += 1
        if self._stall_first_wait and self.waits == 1:
            raise subprocess.TimeoutExpired(cmd=["worker"], timeout=timeout or 0)
        if self._ignore_kill and self.killed:
            raise subprocess.TimeoutExpired(cmd=["worker"], timeout=timeout or 0)
        return self.returncode

    def poll(self):
        return self.returncode if self._exited else None

    def kill(self) -> None:
        self.killed = True
        if not self._ignore_kill:
            self.returncode = -9
            self._exited = True

    def force_exit(self) -> None:
        """Explicitly release a deliberately unreapable test child."""
        self.returncode = -9
        self._exited = True


class ScriptedClient(WindowCaptureClient):
    """把采集组件换成一段真实 python 脚本，走同一条 Popen + 读管道 + 预算路径。"""

    def __init__(self, script: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.script = script

    def _command(self) -> list[str]:
        return [sys.executable, "-c", self.script]


class BudgetWallClockTests(unittest.TestCase):
    def test_six_second_child_is_abandoned_within_budget_plus_grace(self):
        """子进程睡 6 秒：父进程墙钟必须 ≤5.0 总预算（含收尾）。"""
        client = ScriptedClient(SLEEP_SCRIPT, timeout=5.0)
        started = time.monotonic()
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(1)
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 5.6, f"父进程等了 {elapsed:.2f} 秒，超出预算")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        trace = context.exception.trace
        self.assertIsInstance(trace, CaptureTrace)
        self.assertFalse(trace.orphan_suspected)
        # 没有阶段通道时退回本地判定的阶段名，绝不空着。
        self.assertEqual(trace.stage_failed, "wait_exit")
        # 5.0 秒预算里给收尾预留 0.5 秒，所以等待段约 4.5 秒。
        self.assertGreaterEqual(trace.wait_ms, 4400.0)
        self.assertLessEqual(trace.total_ms, 5600.0)

    def test_frame_timeout_child_finishes_within_four_seconds(self):
        """子进程 3.2 秒后自报等帧超时：总耗时 ≤4.0 秒，且阶段来自阶段通道。"""
        client = ScriptedClient(FRAME_TIMEOUT_SCRIPT, timeout=5.0)
        started = time.monotonic()
        with self.assertRaises(WindowCaptureError) as context:
            client.capture(1, request_id="3f9c1d2a")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, 4.0, f"等帧超时用了 {elapsed:.2f} 秒")
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        trace = context.exception.trace
        self.assertEqual(trace.request_id, "3f9c1d2a")
        self.assertEqual(trace.child_error, "WGC_FRAME_TIMEOUT")
        self.assertEqual(trace.stage_failed, "frame_wait")
        self.assertIn("component_enter", trace.stages)
        self.assertIn("session_exit", trace.stages)

    def test_popen_consuming_whole_budget_reports_start_timeout(self):
        """CreateProcess 自己吃掉预算（W1）：不能假装是“等帧太短”。"""
        client = WindowCaptureClient(timeout=0.2)
        made: list[FakeProcess] = []

        def slow_spawn(*args, **kwargs):
            time.sleep(0.3)
            process = FakeProcess(stdout="")
            made.append(process)
            return process

        started = time.monotonic()
        with patch("deal_watch.window_capture.spawn", side_effect=slow_spawn) as spawn:
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(1)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.25, f"调用方被启动阻塞了 {elapsed:.2f} 秒")
            self.assertTrue(START_SUPERVISOR.wait_idle(3.0), "监督任务没有收尾")
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_POPEN_TIMEOUT)
        self.assertEqual(context.exception.trace.stage_failed, "component_start")
        # 启动段被自己的预算截断（0.2 - 0.02 预留），不是傻等 CreateProcess 返回。
        self.assertGreaterEqual(context.exception.trace.popen_ms, 100.0)
        self.assertLess(context.exception.trace.popen_ms, 250.0)
        self.assertTrue(made and made[0].killed, "迟到启动的子进程没有被监督任务回收")
        self.assertGreaterEqual(made[0].waits, 1, "回收路径没有 wait 子进程")

    def test_repeated_start_timeouts_do_not_accumulate_supervisors_or_orphans(self):
        """连续超时：启动任务槽位固定，迟到的进程全部被 kill，线程数不增长。"""
        client = WindowCaptureClient(timeout=0.3)
        made: list[FakeProcess] = []

        def slow_spawn(*args, **kwargs):
            time.sleep(0.6)
            process = FakeProcess(stdout="")
            made.append(process)
            return process

        with patch("deal_watch.window_capture.spawn", side_effect=slow_spawn):
            for _ in range(5):
                with self.assertRaises(WindowCaptureError):
                    client.capture(1)
                self.assertLessEqual(START_SUPERVISOR.pending, MAX_PENDING_STARTS)
                self.assertLessEqual(START_SUPERVISOR.workers, MAX_PENDING_STARTS)
            self.assertTrue(START_SUPERVISOR.wait_idle(5.0), "启动任务没有收尾")
            self.assertLessEqual(
                sum(1 for item in threading.enumerate() if item.name == "capture-start"),
                MAX_PENDING_STARTS,
            )
        self.assertEqual(START_SUPERVISOR.pending, 0)
        self.assertTrue(made, "监督线程根本没有执行 spawn")
        self.assertTrue(all(process.killed for process in made), "存在没有回收的子进程")

    def test_repeated_timeouts_do_not_accumulate_pipe_reader_threads(self):
        """连续超时：读管道线程在 kill 后退出，不会一轮一轮堆积。"""
        client = ScriptedClient(SLEEP_SCRIPT, timeout=0.3)
        for _ in range(5):
            with self.assertRaises(WindowCaptureError):
                client.capture(1)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            alive = [t for t in threading.enumerate() if t.name in {"capture-stdout", "capture-stderr"}]
            if not alive:
                break
            time.sleep(0.02)
        alive = [t for t in threading.enumerate() if t.name in {"capture-stdout", "capture-stderr"}]
        self.assertLessEqual(len(alive), MAX_LIVE_READERS, f"读线程堆积：{[t.name for t in alive]}")

    def test_orphan_is_recorded_instead_of_waiting_forever(self):
        """kill 之后仍不退出：记 orphan_suspected 立刻返回，绝不无限 communicate()。"""
        stalling = FakeProcess(
            stderr='{"stage": "wgc_start", "t_ms": 18.0, "request_id": "ab12cd34"}\n',
            stall_first_wait=True,
            ignore_kill=True,
        )
        client = WindowCaptureClient(timeout=0.05)
        started = time.monotonic()
        try:
            with patch("deal_watch.window_capture.spawn", return_value=stalling):
                with self.assertRaises(WindowCaptureError) as context:
                    client.capture(1, request_id="ab12cd34")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, "孤儿判定不该再等第二遍管道")
            self.assertTrue(stalling.killed)
            self.assertEqual(context.exception.code, CaptureErrorCode.CAPTURE_ORPHAN)
            trace = context.exception.trace
            self.assertTrue(trace.orphan_suspected)
            # 被强杀时 stdout 的 payload 不存在，但 stderr 里最后完成的阶段还在。
            self.assertEqual(trace.stage_failed, "wgc_start")
            self.assertEqual(trace.stages, {"wgc_start": 18.0})
            self.assertGreaterEqual(trace.kill_ms, 0.0)
        finally:
            stalling.force_exit()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with PROCESS_REGISTRY._lock:
                    still_tracked = any(
                        process is stalling
                        for process, _attempts in PROCESS_REGISTRY._orphans.values()
                    )
                if not still_tracked:
                    break
                time.sleep(0.02)
            self.assertFalse(still_tracked, "测试用孤儿进程未从生命周期注册表回收")

    def test_kill_grace_is_half_a_second(self):
        self.assertAlmostEqual(CAPTURE_KILL_GRACE_SECONDS, 0.5)


class StageFailureClassificationTests(unittest.TestCase):
    def _fail_with(self, payload: dict, stderr: str = "") -> WindowCaptureError:
        process = FakeProcess(
            stdout=json.dumps(payload), stderr=stderr, returncode=1
        )
        client = WindowCaptureClient()
        with patch("deal_watch.window_capture.spawn", return_value=process):
            with self.assertRaises(WindowCaptureError) as context:
                client.capture(1, request_id=payload.get("request_id", ""))
        return context.exception

    def test_session_exit_hang_is_its_own_code_and_stage(self):
        error = self._fail_with(
            {
                "ok": False,
                "error": "RuntimeError: WGC_SESSION_EXIT_TIMEOUT",
                "stage_failed": "session_exit",
            }
        )
        self.assertEqual(error.code, CaptureErrorCode.CAPTURE_SESSION_HUNG)
        self.assertEqual(error.trace.stage_failed, "session_exit")
        self.assertEqual(error.trace.child_error, "WGC_SESSION_EXIT_TIMEOUT")

    def test_frame_timeout_and_encode_failure_are_distinguishable(self):
        frame = self._fail_with(
            {"ok": False, "error": "TimeoutError: WGC_FRAME_TIMEOUT",
             "stage_failed": "frame_wait"}
        )
        encode = self._fail_with(
            {"ok": False, "error": "RuntimeError: PNG_ENCODE_FAILED",
             "stage_failed": "encode"}
        )
        start = self._fail_with(
            {"ok": False, "error": "RuntimeError: COMPONENT_START_FAILED: 拒绝访问",
             "stage_failed": "component_start"}
        )
        self.assertEqual(
            [frame.trace.stage_failed, encode.trace.stage_failed, start.trace.stage_failed],
            ["frame_wait", "encode", "component_start"],
        )
        self.assertEqual(frame.code, CaptureErrorCode.CAPTURE_TIMEOUT)
        self.assertEqual(encode.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertEqual(start.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertEqual(len({frame.code, encode.code, start.code}), 2)

    def test_black_frame_keeps_its_021_semantics(self):
        error = self._fail_with(
            {"ok": False, "error": "RuntimeError: BLACK_FRAME", "stage_failed": "black_check"}
        )
        self.assertEqual(error.code, CaptureErrorCode.BLACK_FRAME)

    def test_printwindow_blank_frame_stays_a_failure(self):
        """PrintWindow 空白帧仍然必须显式失败，不能当成“采到了内容”。"""
        error = self._fail_with(
            {
                "ok": False,
                "error": "CapturePayloadError: PRINTWINDOW_BLANK_FRAME: PrintWindow 无法取得当前微信内容（空白帧），请改用 WGC",
                "stage_failed": "print_window",
            }
        )
        self.assertEqual(error.code, CaptureErrorCode.CAPTURE_FAILED)
        self.assertEqual(error.trace.stage_failed, "print_window")
        self.assertEqual(error.trace.child_error, "PRINTWINDOW_BLANK_FRAME")

    def test_stage_lines_are_parsed_and_bounded(self):
        lines = [
            '{"stage": "component_enter", "t_ms": 0.0, "request_id": "x"}',
            "",
            "不是 JSON",
            '{"stage": "", "t_ms": 1.0}',
            '{"stage": "wgc_start", "t_ms": 18.2, "request_id": "x"}',
            '{"stage": "first_valid_frame", "t_ms": 140.9}',
        ]
        stages = parse_stage_lines(lines)
        self.assertEqual(
            stages, {"component_enter": 0.0, "wgc_start": 18.2, "first_valid_frame": 140.9}
        )
        self.assertEqual(last_stage(stages), "first_valid_frame")
        many = [json.dumps({"stage": f"s{index}", "t_ms": index}) for index in range(40)]
        self.assertLessEqual(len(parse_stage_lines(many)), 16)


class StageLogContentTests(unittest.TestCase):
    def test_stage_log_has_only_allowlisted_fields(self):
        """阶段日志只能出现白名单字段；图像、聊天正文、窗口标题一律不得落盘。"""
        stderr = "\n".join(
            [
                json.dumps({"stage": "component_enter", "t_ms": 0.0, "request_id": "3f9c1d2a"}),
                # 故意塞进聊天正文与标题：阶段名里的中文必须被白名单清掉。
                json.dumps({"stage": "手机 4999 元 张三说", "t_ms": 12.0, "request_id": "x"}),
            ]
        )
        process = FakeProcess(
            stdout=json.dumps(
                {"ok": False, "error": "RuntimeError: WGC_SESSION_EXIT_TIMEOUT",
                 "stage_failed": "session_exit", "image": "iVBORw0KGgoAAAA"}
            ),
            stderr=stderr,
            returncode=1,
        )
        client = WindowCaptureClient()
        with StageLogCapture() as messages:
            with patch("deal_watch.window_capture.spawn", return_value=process):
                with self.assertRaises(WindowCaptureError):
                    client.capture(1, request_id="3f9c1d2a", region_id="r1")

        self.assertTrue(messages, "阶段日志一行都没写")
        keys = set(STAGE_FIELD_ALLOWLIST)
        for message in messages:
            for token in message.split(" "):
                key = token.split("=", 1)[0]
                self.assertIn(key, keys, f"阶段日志出现非白名单字段：{message}")
            for forbidden in ("手机", "4999", "张三", "iVBORw0KGgoAAAA"):
                self.assertNotIn(forbidden, message, f"阶段日志泄露了内容：{message}")

    def test_stage_log_carries_request_id_region_and_stage_failed(self):
        process = FakeProcess(
            stdout=json.dumps({"ok": False, "error": "TimeoutError: WGC_FRAME_TIMEOUT",
                               "stage_failed": "frame_wait"}),
            stderr=json.dumps({"stage": "wgc_start", "t_ms": 18.2, "request_id": "3f9c1d2a"}),
            returncode=1,
        )
        client = WindowCaptureClient()
        with StageLogCapture() as messages:
            with patch("deal_watch.window_capture.spawn", return_value=process):
                with self.assertRaises(WindowCaptureError):
                    client.capture(1, request_id="3f9c1d2a", region_id="r1")
        joined = "\n".join(messages)
        self.assertIn("request_id=3f9c1d2a", joined)
        self.assertIn("region_id=r1", joined)
        self.assertIn("child.wgc_start", joined)
        self.assertIn("error_code=capture_timeout", joined)
        self.assertIn("request_total", joined)

    def test_success_path_records_every_segment(self):
        client = ScriptedClient(SUCCESS_SCRIPT, timeout=5.0)
        with StageLogCapture() as messages:
            frame = client.capture(1, request_id="3f9c1d2a", region_id="r9")
        self.assertEqual((frame.width, frame.height), (4, 3))
        self.assertEqual(frame.frame_width, 4)
        self.assertIsNone(frame.roi)
        trace = frame.trace
        self.assertIsNotNone(trace)
        # ``popen_ms`` 是 ``CreateProcess`` + 建管道本身的耗时，按 0.1ms 取整；
        # 本机起子进程通常 <0.05ms 就返回，取整后可以是 0.0（实测约 1/12 轮会命中），
        # 断言 ``> 0.0`` 会随机失败。这里只要求“有记录、不为负”，把“这段耗时被量到了”
        # 交给下面的阶段行断言（component_start 必须出现在日志里）。
        self.assertGreaterEqual(trace.popen_ms, 0.0)
        self.assertLess(trace.popen_ms, client.timeout * 1000.0)
        self.assertGreater(trace.total_ms, 0.0)
        self.assertIn("component_enter", trace.stages)
        joined = "\n".join(messages)
        for stage in ("request_total", "decode", "wait_exit", "component_start"):
            self.assertIn(stage, joined)


class SessionExitTimeoutParameterTests(unittest.TestCase):
    def test_default_and_clamp(self):
        self.assertEqual(WindowCaptureClient().session_exit_timeout_ms, DEFAULT_SESSION_EXIT_TIMEOUT_MS)
        self.assertEqual(clamp_session_exit_timeout(0), DEFAULT_SESSION_EXIT_TIMEOUT_MS)
        self.assertEqual(clamp_session_exit_timeout(-5), DEFAULT_SESSION_EXIT_TIMEOUT_MS)
        self.assertEqual(clamp_session_exit_timeout(9999), MAX_SESSION_EXIT_TIMEOUT_MS)
        self.assertEqual(clamp_session_exit_timeout(1000), 1000)
        self.assertEqual(clamp_session_exit_timeout("750"), 750)

    def test_cli_receives_request_id_and_session_exit_timeout(self):
        process = FakeProcess(stdout=json.dumps({"ok": False, "error": "RuntimeError: X"}))
        client = WindowCaptureClient(timeout=5.0, session_exit_timeout_ms=700)
        with patch("deal_watch.window_capture.spawn", return_value=process) as spawn:
            with self.assertRaises(WindowCaptureError):
                client.capture(4321, "wgc", request_id="3f9c1d2a")
        command = spawn.call_args.args[0]
        self.assertIn("--request-id", command)
        self.assertEqual(command[command.index("--request-id") + 1], "3f9c1d2a")
        self.assertIn("--session-exit-timeout-ms", command)
        self.assertEqual(command[command.index("--session-exit-timeout-ms") + 1], "700")
        # 0.2.1 的既有参数一个都不能少。
        self.assertEqual(command[command.index("--hwnd") + 1], "4321")
        self.assertEqual(command[command.index("--backend") + 1], "wgc")


class CaptureWorkerCliTests(unittest.TestCase):
    """真实子进程：``capture_worker`` 的新参数必须可选，旧行为一个不变。"""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "deal_watch.capture_worker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=str(ROOT),
            env=self._env(),
            check=False,
        )

    @staticmethod
    def _env() -> dict:
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        return env

    def test_self_test_keeps_stdout_only_and_writes_no_stage_lines(self):
        result = self._run("--self-test")
        self.assertTrue(result.stdout.strip(), "自检必须仍然输出 JSON 到 stdout")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn("ok", payload)
        # --self-test 的约定：不采集、不碰屏幕，阶段通道也保持安静。
        self.assertEqual(result.stderr.strip(), "")

    def test_hwnd_less_request_reports_new_fields(self):
        result = self._run("--hwnd", "0", "--request-id", "3f9c1d2a",
                           "--session-exit-timeout-ms", "700")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["request_id"], "3f9c1d2a")
        self.assertEqual(payload["stage_failed"], "component_enter")
        self.assertFalse(payload["session_exit_timeout"])
        # 0.2.1 的字段一个都没少。
        for key in ("ok", "backend", "error", "dpi_awareness", "dpi_request"):
            self.assertIn(key, payload)

    def test_new_cli_parameters_are_optional(self):
        """不带新参数的调用方式必须继续可用（老父进程 / tools 兼容）。"""
        result = self._run("--hwnd", "0")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["error"], "RuntimeError: INVALID_HWND")
        self.assertEqual(payload["request_id"], "")


if __name__ == "__main__":
    unittest.main()
