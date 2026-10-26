"""Subprocess execution with explicit POSIX descriptors or Windows handles."""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager


_spawn_lock = threading.Lock()


@contextmanager
def _windows_handles(descriptors: tuple[int, ...]):
    import msvcrt

    handles = list(dict.fromkeys(msvcrt.get_osfhandle(item) for item in descriptors))
    # Restore inheritability immediately after CreateProcess, including failures.
    with _spawn_lock:
        previous = {handle: os.get_handle_inheritable(handle) for handle in handles}
        try:
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            yield handles
        finally:
            for handle, inheritable in previous.items():
                os.set_handle_inheritable(handle, inheritable)


def run(arguments, *, pass_fds: tuple[int, ...] = (), **kwargs) -> subprocess.CompletedProcess:
    if os.name != "nt":
        return subprocess.run(arguments, pass_fds=pass_fds, **kwargs)
    if not pass_fds:
        return subprocess.run(arguments, **kwargs)

    # subprocess.run does not expose the spawn boundary. Wait outside the
    # inheritability guard so another Git operation can start while this runs.
    input_value = kwargs.pop("input", None)
    check = kwargs.pop("check", False)
    timeout = kwargs.pop("timeout", None)
    if input_value is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    startup = subprocess.STARTUPINFO()
    with _windows_handles(pass_fds) as handles:
        startup.lpAttributeList = {"handle_list": handles}
        process = subprocess.Popen(arguments, startupinfo=startup, close_fds=True, **kwargs)
    with process:
        try:
            stdout, stderr = process.communicate(input_value, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            error.stdout, error.stderr = process.communicate()
            raise
        except BaseException:
            process.kill()
            process.wait()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
