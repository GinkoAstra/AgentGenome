"""Bounded JSONL bridge. Only the injected trusted host can execute capabilities."""
import os
import selectors
import signal
import subprocess
import time

from .common import SopError, canonical

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_TOOL_REQUESTS = 1000


def invoke_interactive(command, request, execute, strict_loads, *, timeout, env):
    if not callable(execute):
        raise SopError('invalid_backend_config', 'Interactive Pi requires a trusted capability callback')
    process = None
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    seen = set()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, cwd='/tmp', env=env,
                                   start_new_session=True, bufsize=0)
        payload = (canonical(dict(request, interactive=True)) + '\n').encode()
        if len(payload) > MAX_FRAME_BYTES:
            raise SopError('backend_protocol', 'Pi request exceeds the bounded frame size')
        # A nonblocking stdin also bounds a worker that never reads its input.
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(value):
            data = (canonical(value) + '\n').encode()
            if len(data) > MAX_FRAME_BYTES:
                raise SopError('backend_protocol', 'Pi host reply exceeds the bounded frame size')
            offset = 0
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SopError('backend_timeout', 'Pi interactive worker exceeded its bounded deadline')
                try:
                    written = os.write(process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    with selectors.DefaultSelector() as writable:
                        writable.register(process.stdin, selectors.EVENT_WRITE)
                        if not writable.select(remaining):
                            raise SopError('backend_timeout', 'Pi interactive worker exceeded its bounded deadline')
                    continue
                if written == 0:
                    raise SopError('backend_protocol', 'Pi worker closed its request channel')
                offset += written

        send(dict(request, interactive=True))
        buffer = b''
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SopError('backend_timeout', 'Pi interactive worker exceeded its bounded deadline')
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise SopError('backend_protocol', 'Pi worker ended before a final response')
            buffer += chunk
            if len(buffer) > MAX_FRAME_BYTES:
                raise SopError('backend_protocol', 'Pi worker output exceeds the bounded frame size')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    frame = strict_loads(line.decode('utf-8'))
                except (ValueError, UnicodeError, SopError) as exc:
                    raise SopError('backend_protocol', 'Pi worker emitted an invalid strict JSON frame') from exc
                if not isinstance(frame, dict):
                    raise SopError('backend_protocol', 'Pi worker frame must be an object')
                if frame.get('type') == 'execute':
                    if set(frame) != {'type', 'request_id', 'capability', 'inputs_json'}:
                        raise SopError('backend_protocol', 'Pi execute frame has unknown or missing fields')
                    request_id = frame['request_id']
                    if (not isinstance(request_id, str) or not request_id or len(request_id) > 128 or
                            request_id in seen or len(seen) >= MAX_TOOL_REQUESTS):
                        raise SopError('backend_protocol', 'Pi execute request identity is invalid or repeated')
                    if not isinstance(frame['capability'], str) or not isinstance(frame['inputs_json'], str):
                        raise SopError('backend_protocol', 'Pi execute capability and inputs_json must be strings')
                    try:
                        inputs = strict_loads(frame['inputs_json'])
                    except (ValueError, SopError) as exc:
                        raise SopError('backend_protocol', 'Pi tool inputs must be strict JSON') from exc
                    if not isinstance(inputs, dict):
                        raise SopError('backend_protocol', 'Pi tool inputs must be an object')
                    if deadline <= time.monotonic():
                        raise SopError('backend_timeout', 'Pi interactive worker exceeded its bounded deadline')
                    seen.add(request_id)
                    # Do not convert host failures into a tool result the model may retry.
                    result = execute(frame['capability'], inputs)
                    if (not isinstance(result, dict) or set(result) != {'operation', 'outputs', 'reused'} or
                            not isinstance(result['operation'], str) or not isinstance(result['outputs'], dict) or
                            type(result['reused']) is not bool):
                        raise SopError('backend_protocol', 'Host capability callback returned an invalid receipt')
                    send({'type': 'tool_result', 'request_id': request_id, 'ok': True, 'result': result})
                    continue
                if 'type' in frame or type(frame.get('ok')) is not bool:
                    raise SopError('backend_protocol', 'Pi worker emitted an unknown frame type')
                if buffer.strip():
                    raise SopError('backend_protocol', 'Pi worker emitted frames after its final response')
                return frame
    except OSError as exc:
        raise SopError('backend_unavailable' if process is None else 'backend_protocol',
                       'Cannot start or communicate with the Pi interactive worker') from exc
    finally:
        selector.close()
        if process is not None:
            # Worker descendants cannot retain authority after the bridge exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdin.close()
            process.stdout.close()
