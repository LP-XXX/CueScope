"""Read-only DISPLAY1 capture audit, alternating A/B, cadence and eight-hour soak.

This measures the capture subsystem, not MonitorService/OCR/notification acceptance.
Only configured, preview-confirmed HWNDs are used. No window manipulation, screen
images, chat text, aliases, credentials or user database writes are performed.
Raw request records and atomic checkpoints are restricted to ignored build/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "src"), str(ROOT / "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import real_env  # noqa: E402

UNVERIFIED = [
    "production_service_scheduler", "ocr_and_keyword_rules", "movement",
    "resize", "minimize_and_resume", "occluded_new_message",
    "real_feishu", "mobile_delivery",
]
SAFE_WORD = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TRACE_STAGES = frozenset({
    "component_enter", "worker_started", "dpi_aware", "request_received",
    "deps_init_started", "deps_init", "wgc_construct_started", "wgc_construct_finished",
    "wgc_start_requested", "wgc_start", "frame_callback_enter", "first_valid_frame",
    "session_stop", "session_exit", "print_window", "encode", "total", "worker_complete",
})


class ValidationFailure(RuntimeError):
    """Only fixed, non-sensitive codes are emitted by this tool."""


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def safe_word(value: Any) -> str:
    return value if isinstance(value, str) and SAFE_WORD.fullmatch(value) else "unknown"


def output_path(value: str | Path) -> Path:
    result = Path(value).resolve()
    if not result.is_relative_to((ROOT / "build").resolve()):
        raise ValidationFailure("output_must_be_inside_build")
    return result


def checkpoint(path: Path, report: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": ordered[int((len(ordered) - 1) * .5)] if ordered else None,
        "p95_ms": ordered[int((len(ordered) - 1) * .95)] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }


def audit_regions(regions, windows, device, bounds, visible) -> list[dict]:
    """All enabled regions must pass; never silently choose another window."""
    from deal_watch.readiness import region_readiness_problems

    result = []
    for region in regions:
        problems = []
        if region_readiness_problems(region, windows):
            problems.append("region_not_ready")
        window = windows.window_from_hwnd(region.window_hwnd)
        if window is None:
            problems.append("window_missing")
        else:
            if not window.process_path.casefold().endswith("\\weixin.exe"):
                problems.append("not_weixin")
            if window.minimized:
                problems.append("window_minimized")
            if device(region.window_hwnd).upper() != real_env.DISPLAY1.upper():
                problems.append("not_display1")
            rect = visible(region.window_hwnd)
            if rect is None or not real_env.fully_inside(rect, bounds):
                problems.append("not_fully_inside_display1")
        if region.capture_backend != "wgc":
            problems.append("not_wgc")
        result.append({
            "region": fingerprint(region.id), "window": fingerprint(str(region.window_hwnd)),
            "previewed": bool(region.previewed), "ready": not problems,
            "reasons": problems,
            "reference_size": [region.reference_frame_width, region.reference_frame_height],
            "roi_size": [region.rect.width, region.rect.height],
        })
    return result


def resource_snapshot(client) -> dict:
    """Drop all unrecognized fields; protocol diagnostics must not become a leak."""
    method = getattr(client, "worker_stats", None)
    if not callable(method):
        return {"available": False}
    raw = method()
    result: dict[str, Any] = {"available": True}
    for key in ("process_starts", "failures", "active_processes", "orphan_processes"):
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = value
    result["state"] = safe_word(raw.get("state"))
    result["processes"] = []
    for process in raw.get("processes", [])[:2]:
        clean = {}
        for key in ("pid", "private_bytes", "working_set_bytes", "handles", "threads"):
            value = process.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                clean[key] = value
        result["processes"].append(clean)
    return result


def trace_snapshot(trace) -> dict:
    """Keep stage offsets and readiness wait even when no frame is returned."""
    fields_method = getattr(trace, "as_fields", None)
    if not callable(fields_method):
        return {}
    fields = fields_method()
    if not isinstance(fields, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("popen_ms", "wait_ms", "kill_ms", "decode_ms", "total_ms", "queue_wait_ms",
                "deadline_overrun_ms", "generation", "worker_id"):
        value = fields.get(key)
        if (isinstance(value, int | float) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            result[key] = value
    for key in ("request_id", "stage_failed", "child_error"):
        if key in fields:
            result[key] = safe_word(fields[key])
    if type(fields.get("exit_code")) is int:
        result["exit_code"] = fields["exit_code"]
    if type(fields.get("orphan_suspected")) is bool:
        result["orphan_suspected"] = fields["orphan_suspected"]
    stages = fields.get("stages")
    if isinstance(stages, dict):
        result["stage_offsets_ms"] = {
            key: value for key, value in stages.items()
            if key in TRACE_STAGES and isinstance(value, int | float)
            and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        }
    return result


def capture_sample(capturer, region, *, previous_sequence=0, clock=time) -> dict:
    """No image or text leaves this function. Identical pixels are valid new frames."""
    started_wall = clock.time()
    started = clock.monotonic()
    record = {"region": fingerprint(region.id), "ok": False, "started_at": started_wall}
    try:
        frame = capturer.capture(region, force=True)
        if frame is None:
            raise ValidationFailure("missing_fresh_frame")
        if frame.region_id != region.id or frame.sequence <= previous_sequence:
            raise ValidationFailure("stale_or_wrong_region_frame")
        if not started_wall - .1 <= frame.captured_at <= clock.time() + .1:
            raise ValidationFailure("stale_frame_timestamp")
        if frame.rect != region.rect or frame.image.shape[:2] != (region.rect.height, region.rect.width):
            raise ValidationFailure("unexpected_roi")
        record.update(ok=True, sequence=int(frame.sequence))
        del frame
    except Exception as error:
        record["error_code"] = safe_word(
            str(error) if isinstance(error, ValidationFailure) else str(getattr(error, "code", "capture_exception"))
        )
        record["exception_type"] = safe_word(type(error).__name__)
    record["elapsed_ms"] = round((clock.monotonic() - started) * 1000, 3)
    trace = getattr(capturer, "last_trace", None)
    if trace:
        record["request_id"] = safe_word(getattr(trace, "request_id", ""))
        record["stage_failed"] = safe_word(getattr(trace, "stage_failed", ""))
        record["deadline_overrun_ms"] = max(0.0, record["elapsed_ms"] - 5000.0)
        record["trace"] = trace_snapshot(trace)
    if record["ok"] and record["elapsed_ms"] > 5000:
        record.update(ok=False, error_code="capture_budget_exceeded")
    return record


def round_deadline(start: float, index: int, interval: float, now: float) -> tuple[float, float]:
    """Fixed monotonic targets expose overload instead of shifting the target."""
    target = start + index * interval
    return max(0.0, target - now), max(0.0, now - target) * 1000


def round_count(mode: str, rounds: int, hours: float, interval: float) -> int:
    if not math.isfinite(interval) or not 10 <= interval <= 300:
        raise ValidationFailure("interval_must_be_10_to_300_seconds")
    if mode == "soak":
        if not math.isfinite(hours) or not 8 <= hours <= 72:
            raise ValidationFailure("soak_hours_must_be_8_to_72")
        # Last round starts at or after the requested elapsed duration.
        return math.ceil(hours * 3600 / interval) + 1
    if not 10 <= rounds <= 10000:
        raise ValidationFailure("rounds_must_be_10_to_10000")
    return rounds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("audit", "ab", "schedule", "soak"), default="audit")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--hours", type=float, default=8)
    parser.add_argument("--expected-windows", type=int, default=5)
    parser.add_argument("--baseline-component", type=Path,
                        default=ROOT / "dist/DealWatch-0.2.4/capture-component/DealWatchCapture.exe")
    parser.add_argument("--candidate-component", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def close_client(client, *, clock=time) -> None:
    from deal_watch.window_capture import PROCESS_REGISTRY

    close = getattr(client, "close", None)
    if callable(close):
        close()
    deadline = clock.monotonic() + 5.0
    while PROCESS_REGISTRY.active and clock.monotonic() < deadline:
        clock.sleep(.05)
    if PROCESS_REGISTRY.active:
        raise ValidationFailure("worker_cleanup_pending")


def prepare_candidate(client, *, clock=time) -> dict:
    """Separate bounded startup from sampling, retaining its actual cost and failures."""
    started = clock.monotonic()
    result = {"startup_ready": False, "startup_timeout_ms": 5000}
    try:
        client.prepare()
        result["startup_ready"] = client.wait_ready(5.0) is True
        if not result["startup_ready"]:
            result["error_code"] = "candidate_startup_not_ready"
    except Exception as error:
        result.update(error_code="candidate_startup_error",
                      exception_type=safe_word(type(error).__name__))
    result["startup_elapsed_ms"] = round((clock.monotonic() - started) * 1000, 3)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    real_env.require_windows()
    real_env.enable_dpi_awareness()
    real_env.ensure_subprocess_pythonpath()
    from deal_watch import __version__
    from deal_watch.capture import WindowsWindows
    from deal_watch.config import ConfigStore
    from deal_watch.diagnostics import configure_capture_stage_log
    from deal_watch.window_capture import WindowCaptureClient, WindowFrameCapturer

    path = output_path(args.output or ROOT / "build/validation" /
                       f"capture-{args.mode}-{__version__}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    if path.exists() or path.with_suffix(".requests.jsonl").exists():
        raise ValidationFailure("output_already_exists_no_resume")
    configure_capture_stage_log(path.parent / "capture-diagnostics")
    records_path = path.with_suffix(".requests.jsonl")
    report: dict[str, Any] = {
        "kind": "real_configured_wechat_capture_validation", "version": __version__,
        "measurement_scope": "capture_subsystem_not_production_service_or_ocr",
        "mode": args.mode, "started_at": time.time(), "status": "preflight", "passed": False,
        "full_acceptance_passed": False, "unverified": UNVERIFIED,
        "user_config_written": False, "credentials_read": False,
        "real_notifications_sent": False, "window_manipulation_performed": False,
        "screenshots_saved": False, "monitor_device": real_env.DISPLAY1,
        "completed_rounds": 0, "request_records": records_path.name,
        "startup_attempts": [], "timings_exclude_explicit_startup": True,
        "resource_samples": [], "rounds": [],
        "capture_timeout_seconds": 5, "wgc_timeout_seconds": 3,
        "interval_seconds": args.interval_seconds,
        "production_params": args.interval_seconds == 30,
        "baseline_kind": "frozen_0.2.4_component" if args.mode == "ab" else None,
        "candidate_kind": "frozen_component" if args.candidate_component else "source",
    }
    checkpoint(path, report)
    current_client = None
    latencies: dict[str, list[float]] = {"baseline": [], "candidate": []}
    warm_latencies: dict[str, list[float]] = {"baseline": [], "candidate": []}
    failures: Counter = Counter()
    try:
        count = round_count(args.mode, args.rounds, args.hours, args.interval_seconds)
        if args.expected_windows < 5:
            raise ValidationFailure("at_least_five_distinct_windows_required")
        loaded = ConfigStore().load()  # no DPAPI credential loading or saves
        regions = [region for region in loaded.regions if region.enabled]
        windows = WindowsWindows()
        bounds = real_env.monitor_rect()

        def audit():
            return audit_regions(regions, windows, real_env.monitor_device, bounds, real_env.visible_rect)

        report["regions"] = audit()
        report["distinct_windows"] = len({region.window_hwnd for region in regions})
        if report["distinct_windows"] < args.expected_windows:
            raise ValidationFailure("insufficient_distinct_configured_windows")
        if not all(item["ready"] for item in report["regions"]):
            raise ValidationFailure("configured_window_audit_failed")
        report["source_fingerprint"] = {
            rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
            for rel in ("tools/validate_capture_025.py", "src/deal_watch/window_capture.py", "src/deal_watch/capture_pool.py",
                        "src/deal_watch/capture_worker.py")
        }
        for name, component in (("baseline", args.baseline_component),
                                ("candidate", args.candidate_component)):
            if component is None or (name == "baseline" and args.mode != "ab"):
                continue
            if not component.is_file():
                raise ValidationFailure(f"{name}_component_missing")
            report[f"{name}_component_sha256"] = hashlib.sha256(component.read_bytes()).hexdigest()
        if args.mode == "audit":
            report.update(status="complete", passed=True)
            return 0
        report.update(status="running", planned_rounds=count,
                      requested_duration_seconds=args.hours * 3600 if args.mode == "soak" else None)
        checkpoint(path, report)
        started = time.monotonic()
        sequences: dict[tuple[str, str], int] = {}
        sampler = None
        with records_path.open("x", encoding="utf-8", newline="\n") as records:
            for index in range(count):
                wait, _ = round_deadline(started, index, args.interval_seconds, time.monotonic())
                while wait > 0:
                    time.sleep(min(wait, 1.0))
                    wait, _ = round_deadline(started, index, args.interval_seconds, time.monotonic())
                _, late = round_deadline(started, index, args.interval_seconds, time.monotonic())
                order = ["candidate"] if args.mode != "ab" else (
                    ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"]
                )
                round_record = {"index": index + 1, "schedule_delay_ms": round(late, 3),
                                "order": order, "attempts": 0, "failures": 0}
                for mode in order:
                    if current_client is None:
                        current_client = WindowCaptureClient(
                            component_path=(args.baseline_component if mode == "baseline"
                                            else args.candidate_component),
                            timeout=5.0, persistent=mode == "candidate",
                        )
                        sampler = WindowFrameCapturer(client=current_client, windows=windows)
                        sequences.clear()
                        first_request = True
                        startup = {"startup_elapsed_ms": 0.0}
                        if mode == "candidate":
                            startup = prepare_candidate(current_client, clock=time)
                            report["startup_attempts"].append({
                                **startup, "round": index + 1, "implementation": mode,
                                "resources": resource_snapshot(current_client),
                            })
                            if not startup["startup_ready"]:
                                failures[startup["error_code"]] += 1
                                report["failures"] = dict(failures)
                                checkpoint(path, report)
                                raise ValidationFailure(startup["error_code"])
                    for region in regions:
                        # Every request is rechecked before touching pixels, not just at launch.
                        current_audit = audit_regions([region], windows, real_env.monitor_device,
                                                      bounds, real_env.visible_rect)
                        if not current_audit[0]["ready"]:
                            report["last_audit_failure"] = current_audit
                            raise ValidationFailure("configured_window_changed_during_run")
                        row = capture_sample(sampler, region,
                                             previous_sequence=sequences.get((mode, region.id), 0),
                                             clock=time)
                        row.update(round=index + 1, implementation=mode,
                                   cold_request=mode == "baseline" or first_request)
                        if first_request:
                            row["startup_elapsed_ms"] = startup["startup_elapsed_ms"]
                            row["startup_ready"] = startup.get("startup_ready")
                            row["first_capture_total_ms"] = round(
                                startup["startup_elapsed_ms"] + row["elapsed_ms"], 3,
                            )
                        if row["ok"]:
                            sequences[(mode, region.id)] = row["sequence"]
                            latencies[mode].append(row["elapsed_ms"])
                            if not first_request or mode == "baseline":
                                warm_latencies[mode].append(row["elapsed_ms"])
                        else:
                            failures[row["error_code"]] += 1
                            round_record["failures"] += 1
                        first_request = False
                        round_record["attempts"] += 1
                        records.write(json.dumps(row, ensure_ascii=False) + "\n")
                        records.flush()
                    resources = resource_snapshot(current_client)
                    resources.update(round=index + 1, implementation=mode)
                    report["resource_samples"].append(resources)
                    if resources.get("active_processes", 0) > 2:
                        raise ValidationFailure("owned_capture_process_limit_exceeded")
                    if args.mode == "ab":
                        close_client(current_client)
                        current_client = None
                report["rounds"].append(round_record)
                report.update(completed_rounds=index + 1, elapsed_seconds=round(time.monotonic() - started, 3),
                              failures=dict(failures), timings={key: summary(value) for key, value in latencies.items()},
                              steady_timings={key: summary(value) for key, value in warm_latencies.items()})
                checkpoint(path, report)
                print(json.dumps({"round": index + 1, "total": count,
                                  "failures": sum(failures.values())}), flush=True)
        if current_client is not None:
            close_client(current_client)
            current_client = None
            report["owned_workers_cleaned_up"] = True
        report.update(status="complete", passed=not failures)
        if args.mode == "soak" and report["elapsed_seconds"] < args.hours * 3600:
            raise ValidationFailure("soak_duration_incomplete")
        return 0 if report["passed"] else 1
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                      passed=False, error_code=safe_word(str(error)) if isinstance(error, ValidationFailure)
                      else safe_word(type(error).__name__))
        return 2
    finally:
        if current_client is not None:
            try:
                close_client(current_client)
                report["owned_workers_cleaned_up"] = True
            except Exception:
                report.update(owned_workers_cleaned_up=False, passed=False, status="cleanup_failed")
        report["finished_at"] = time.time()
        checkpoint(path, report)
        print(json.dumps({"report": str(path), "status": report["status"],
                          "passed": report["passed"], "full_acceptance_passed": False}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
