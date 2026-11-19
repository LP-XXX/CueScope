"""Real child processes exercise the pipe protocol without touching the desktop."""

import sys
import time

import pytest

from deal_watch.models import CaptureErrorCode, Rect
from deal_watch.window_capture import PROCESS_REGISTRY, WindowCaptureClient, WindowCaptureError

SCRIPT = r'''
import base64,json,os,struct,sys,time,zlib
def png():
 def chunk(tag,data):
  return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',4,3,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+bytes((20,30,40))*4)*3))+chunk(b'IEND',b'')
print(json.dumps({'type':'ready','protocol_version':2,'ok':True,'pid':os.getpid()}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r.get('type')=='shutdown':break
 if r['hwnd']==99:
  print(json.dumps({'stage':'wgc_construct_started','t_ms':0,'request_id':r['request_id']}),file=sys.stderr,flush=True)
  time.sleep(20)
 if r['hwnd']==98:
  r['generation']+=1
 if r['hwnd']==97:
  print('not-json',flush=True);continue
 p={**r,'type':'result','ok':True,'worker_reusable':True,'width':4,'height':3,
 'frame_width':4,'frame_height':3,'captured_at':time.time(),'image':base64.b64encode(png()).decode(),'stages':{}}
 print(json.dumps(p),flush=True)
 if r['hwnd']==96:print(json.dumps(p),flush=True)
'''


class ScriptClient(WindowCaptureClient):
    def __init__(self, script=SCRIPT, **kwargs):
        super().__init__(persistent=True, **kwargs)
        self.script = script

    def _command(self):
        return [sys.executable, "-u", "-c", self.script]


@pytest.fixture
def clients():
    made = []
    yield made
    for client in made:
        client.close()
    deadline = time.monotonic() + 6
    while PROCESS_REGISTRY.active and time.monotonic() < deadline:
        time.sleep(0.025)
    assert PROCESS_REGISTRY.active == 0


def client_for(clients, **kwargs):
    client = ScriptClient(**kwargs)
    clients.append(client)
    return client


def test_many_regions_reuse_only_two_processes(clients):
    client = client_for(clients)
    client.prepare()
    pids = set()
    for index in range(12):
        frame = client.capture(index + 1, region_id=f"r{index}", generation=index,
                               roi=Rect(0, 0, 4, 3), reference_frame_width=4, reference_frame_height=3)
        assert frame.image.shape == (3, 4, 3)
        assert frame.trace.generation == index
        pids.add(frame.trace.worker_id)
        assert PROCESS_REGISTRY.active <= 2
    assert len(pids) <= 2
    assert client._pool._starts <= 2


def test_wrong_generation_retires_process_and_next_request_recovers(clients):
    client = client_for(clients)
    first = client.capture(1)
    with pytest.raises(WindowCaptureError) as failure:
        client.capture(98, generation=2)
    assert failure.value.trace.stage_failed == "protocol"
    second = client.capture(2)
    assert first.trace.worker_id != second.trace.worker_id
    assert PROCESS_REGISTRY.active <= 2


def test_malformed_output_is_not_a_success(clients):
    client = client_for(clients)
    with pytest.raises(WindowCaptureError):
        client.capture(97)
    assert client.capture(2).image.size == 36


def test_native_start_stall_is_limited_by_parent_wgc_budget(clients):
    client = client_for(clients)
    client.capture(1)
    started = time.monotonic()
    with pytest.raises(WindowCaptureError) as failure:
        client.capture(99)
    assert time.monotonic() - started < 3.8
    assert failure.value.code is CaptureErrorCode.CAPTURE_TIMEOUT
    assert failure.value.trace.stage_failed == "wgc_construct_started"
    assert client.capture(2).image.size == 36


def test_duplicate_response_cannot_be_used_for_next_region(clients):
    client = client_for(clients)
    client.capture(96, region_id="first")
    # Either the reader has quarantined the surplus response already, or the next
    # request detects its mismatching identity. It can never return the stale frame.
    try:
        result = client.capture(2, region_id="second", generation=4)
    except WindowCaptureError as error:
        assert error.trace.stage_failed in {"protocol", "transport"}
    else:
        assert result.trace.region_id == "second"
        assert result.trace.generation == 4


def test_close_cancels_request_without_waiting_native_call(clients):
    import threading
    client = client_for(clients)
    client.capture(1)
    outcomes = []
    def capture():
        try:
            client.capture(99)
        except WindowCaptureError as error:
            outcomes.append(error)
    thread = threading.Thread(target=capture)
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    client.close()
    thread.join(1)
    assert time.monotonic() - started < 1
    assert outcomes and not thread.is_alive()


def test_old_protocol_fails_without_silent_one_shot_fallback(clients):
    script = "import json,time;print(json.dumps({'type':'ready','ok':True,'protocol_version':1}),flush=True);time.sleep(20)"
    client = client_for(clients, script=script, timeout=0.5)
    with pytest.raises(WindowCaptureError) as failure:
        client.capture(1)
    assert failure.value.trace.stage_failed == "component_version"


def test_dependency_initialization_stall_keeps_startup_quota_bounded(clients):
    client = client_for(clients, script="import time;time.sleep(20)", timeout=0.3)
    for _ in range(3):
        started = time.monotonic()
        with pytest.raises(WindowCaptureError):
            client.capture(1)
        assert time.monotonic() - started < 0.8
        assert PROCESS_REGISTRY.active <= 2


def test_preview_component_path_is_honored_in_source_mode(tmp_path):
    component = tmp_path / "component.exe"
    component.touch()
    assert WindowCaptureClient(component_path=component)._command() == [str(component)]


def test_simultaneous_prepare_has_one_owned_pool(clients):
    import threading
    client = client_for(clients)
    barrier = threading.Barrier(8)
    pools = []
    def prepare():
        barrier.wait()
        client.prepare()
        pools.append(client._pool)
    threads = [threading.Thread(target=prepare) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert len(pools) == 8 and len({id(pool) for pool in pools}) == 1
    assert client.capture(1).image.size == 36
    assert PROCESS_REGISTRY.active <= 2


def test_correlated_failed_response_counts_as_failure(clients):
    script = SCRIPT.replace("'ok':True,'worker_reusable':True", "'ok':False,'error':'BLACK_FRAME','worker_reusable':True")
    client = client_for(clients, script=script)
    with pytest.raises(WindowCaptureError) as error:
        client.capture(1)
    assert error.value.code is CaptureErrorCode.BLACK_FRAME
    assert client._pool._failures == 1


def test_stop_then_explicit_prepare_restarts_cleanly(clients):
    client = client_for(clients)
    first = client.capture(1)
    client.close()
    assert client.wait_closed(3)
    with pytest.raises(WindowCaptureError):
        client.capture(1)
    client.prepare()
    assert client.capture(1).trace.worker_id != first.trace.worker_id


def test_ready_gate_measures_startup_separately_from_capture(clients):
    script = SCRIPT.replace("print(json.dumps({'type':'ready'", "time.sleep(0.5)\nprint(json.dumps({'type':'ready'")
    client = client_for(clients, script=script, timeout=2)
    client.prepare()
    started = time.monotonic()
    assert client.wait_ready(2)
    assert time.monotonic() - started >= 0.4
    frame = client.capture(1)
    assert frame.trace.total_ms < 400


def test_idle_worker_exits_through_shutdown_protocol(clients):
    client = client_for(clients)
    client.capture(1)
    worker = client._pool._workers[0]
    client.close()
    assert client.wait_closed(3)
    assert worker.process.poll() == 0


def test_shared_preview_cannot_reuse_worker_before_decode_failure_quarantines_it(clients):
    import threading
    script = SCRIPT.replace("print(json.dumps(p),flush=True)",
                            "if r['hwnd']==95:p['image']='invalid-png'\n print(json.dumps(p),flush=True)", 1)
    client = client_for(clients, script=script)
    assert client.wait_ready(5)
    decode_entered = threading.Event()
    release_decode = threading.Event()
    second_decode = threading.Event()
    original = client._decode_payload
    outcomes = {}
    def decode(payload, *args):
        if payload['hwnd'] == 95:
            decode_entered.set()
            assert release_decode.wait(3)
        else:
            second_decode.set()
        return original(payload, *args)
    client._decode_payload = decode
    def capture(key, hwnd):
        try:
            outcomes[key] = client.capture(hwnd)
        except WindowCaptureError as error:
            outcomes[key] = error
    first = threading.Thread(target=capture, args=('first', 95))
    second = threading.Thread(target=capture, args=('second', 1))
    first.start()
    assert decode_entered.wait(2)
    second.start()
    try:
        assert not second_decode.wait(.15)
    finally:
        release_decode.set()
        first.join(4)
        second.join(4)
    assert isinstance(outcomes['first'], WindowCaptureError)
    assert not isinstance(outcomes['second'], WindowCaptureError)
    assert outcomes['first'].trace.worker_id != outcomes['second'].trace.worker_id


@pytest.mark.parametrize('delayed_stage', ['children', 'request_total'])
def test_slow_diagnostic_output_cannot_deliver_frame_after_deadline(monkeypatch, delayed_stage):
    import base64
    from io import BytesIO

    from PIL import Image

    from deal_watch.window_capture import CaptureTrace
    encoded = BytesIO()
    Image.new('RGB', (4, 3), (20, 30, 40)).save(encoded, format='PNG')
    payload = {'ok': True, 'width': 4, 'height': 3, 'frame_width': 4, 'frame_height': 3,
               'roi': None, 'backend': 'wgc', 'captured_at': time.time(),
               'image': base64.b64encode(encoded.getvalue()).decode()}
    client = WindowCaptureClient(timeout=.05)
    def record(_trace, stage, *_args):
        if delayed_stage == stage:
            time.sleep(.08)
    monkeypatch.setattr(client, '_record', record)
    monkeypatch.setattr(client, '_record_child_stages', lambda trace: record(trace, 'children'))
    started = time.monotonic()
    with pytest.raises(WindowCaptureError) as failure:
        client._decode_payload(payload, CaptureTrace(), None, 'wgc', started, started + .05)
    assert failure.value.code is CaptureErrorCode.CAPTURE_TIMEOUT
    assert failure.value.trace.stage_failed == 'diagnostics'
    assert failure.value.trace.total_ms >= 70
    assert failure.value.trace.deadline_overrun_ms > 0


def test_incompatible_component_reports_specific_error_without_respawn_loop(clients):
    client = client_for(clients, script="import json,time;print(json.dumps({'type':'ready','ok':True,'protocol_version':1}),flush=True);time.sleep(20)")
    with pytest.raises(WindowCaptureError) as failure:
        client.wait_ready(5)
    assert failure.value.code is CaptureErrorCode.CAPTURE_FAILED
    assert failure.value.trace.stage_failed == 'component_version'
    assert '版本不匹配' in str(failure.value)
    for _ in range(3):
        with pytest.raises(WindowCaptureError, match='版本不匹配'):
            client.capture(1)
    assert client._pool._starts == 1


def test_target_identity_is_checked_immediately_before_pooled_dispatch(clients):
    from deal_watch.capture import CaptureError
    from deal_watch.window_capture import WindowFrameCapturer
    from test_window_capture import FakeWindows, region
    windows = FakeWindows()
    get_window = windows.window_from_hwnd
    calls = []
    def changing_window(hwnd):
        calls.append(hwnd)
        if len(calls) > 1:
            return None
        return get_window(hwnd)
    windows.window_from_hwnd = changing_window
    client = client_for(clients)
    cap = WindowFrameCapturer(client=client, windows=windows)
    with pytest.raises(CaptureError) as failure:
        cap.capture(region())
    assert failure.value.code is CaptureErrorCode.WINDOW_NOT_FOUND
    assert len(calls) == 2
    assert client._pool._failures == 0
    assert client.last_trace.stage_failed == 'target_validation'
    assert client.capture(1).image.size == 36


def test_window_disappearing_during_capture_cannot_publish_a_frame():
    from deal_watch.capture import CaptureError
    from deal_watch.window_capture import WindowFrameCapturer
    from test_window_capture import FakeClient, FakeWindows, region
    windows, client = FakeWindows(), FakeClient()
    capture = client.capture
    def close_while_capturing(*args, **kwargs):
        frame = capture(*args, **kwargs)
        windows.window_from_hwnd = lambda hwnd: None
        return frame
    client.capture = close_while_capturing
    with pytest.raises(CaptureError) as failure:
        WindowFrameCapturer(client=client, windows=windows).capture(region())
    assert failure.value.code is CaptureErrorCode.WINDOW_NOT_FOUND
