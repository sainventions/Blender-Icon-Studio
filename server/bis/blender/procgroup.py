"""Tie Blender child processes to the server's lifetime (Windows Job Object, KILL_ON_JOB_CLOSE).

If the server dies without a clean shutdown (crash, `taskkill /F`, closed console, uvicorn --reload), the
kernel closes our job handle and terminates every Blender process assigned to it — no orphaned worker keeps
holding VRAM on the shared 8 GB GPU. The "Open in Blender" GUI process is deliberately NOT assigned.
On non-Windows platforms this is a no-op.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading

log = logging.getLogger("bis.blender.procgroup")

_lock = threading.Lock()
_job = None
_failed = False


def _create_job():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
    return kernel32, handle


def attach(proc: subprocess.Popen) -> bool:
    """Assign `proc` to the server's kill-on-close job. Returns True on success (never raises)."""
    global _job, _failed
    if sys.platform != "win32" or _failed:
        return False
    try:
        with _lock:
            if _job is None:
                _job = _create_job()
        kernel32, handle = _job
        ph = getattr(proc, "_handle", None)
        if ph is None:
            return False
        if not kernel32.AssignProcessToJobObject(handle, int(ph)):
            import ctypes

            log.debug("AssignProcessToJobObject failed: %s", ctypes.get_last_error())
            return False
        return True
    except Exception as e:  # pragma: no cover - platform quirks must not break rendering
        _failed = True
        log.warning("process job object unavailable (%s); Blender children may outlive a crashed server", e)
        return False
