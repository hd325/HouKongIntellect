"""Fail-closed OS resource limits, installed before importing document libraries."""
from __future__ import annotations

import os

WORKER_MEMORY_BYTES = 768 * 1024**2


def limit_worker():
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        return None
    import ctypes
    from ctypes import wintypes as w
    size = ctypes.c_size_t
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", w.DWORD), ("min_ws", size), ("max_ws", size),
                    ("processes", w.DWORD), ("affinity", size),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", Counters), ("process_memory", size),
                    ("job_memory", size), ("peak_process", size), ("peak_job", size)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100 | 0x200 | 0x2000  # process/job memory; kill descendants on exit
    info.process_memory = info.job_memory = WORKER_MEMORY_BYTES
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        if job: kernel.CloseHandle(job)
        raise RuntimeError("Cannot enforce document worker memory limit")
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        kernel.CloseHandle(job)
        raise RuntimeError("Cannot bind document worker memory limit")
    # Retain for process lifetime. OS closes the handle on exit, killing children.
    return job
