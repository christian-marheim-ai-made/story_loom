from __future__ import annotations

import ctypes
import sys

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

SUPPORTED = sys.platform == "win32"


class JobObjectError(RuntimeError):
    pass


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel32():
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          wintypes.LPVOID, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


class KillOnCloseJob:

    def __init__(self) -> None:
        if not SUPPORTED:
            raise JobObjectError("job objects are a Windows mechanism; "
                                 f"this is {sys.platform}")
        self._k = _kernel32()
        self._handle = self._k.CreateJobObjectW(None, None)
        if not self._handle:
            raise JobObjectError(
                f"CreateJobObjectW failed, GetLastError={ctypes.get_last_error()}")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k.SetInformationJobObject(
            self._handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            err = ctypes.get_last_error()
            self._k.CloseHandle(self._handle)
            self._handle = None
            raise JobObjectError(
                f"SetInformationJobObject(KILL_ON_JOB_CLOSE) failed, "
                f"GetLastError={err}. A job without this flag holds the child "
                f"exactly as well as no job at all.")

    def add(self, pid: int) -> None:
        if self._handle is None:
            raise JobObjectError("the job is already closed")
        hproc = self._k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                                    False, int(pid))
        if not hproc:
            raise JobObjectError(f"OpenProcess({pid}) failed, "
                                 f"GetLastError={ctypes.get_last_error()}")
        try:
            if not self._k.AssignProcessToJobObject(self._handle, hproc):
                raise JobObjectError(
                    f"AssignProcessToJobObject(pid {pid}) failed, "
                    f"GetLastError={ctypes.get_last_error()}")
        finally:
            self._k.CloseHandle(hproc)

    def close(self) -> None:
        if self._handle is not None:
            self._k.CloseHandle(self._handle)
            self._handle = None

    @property
    def open(self) -> bool:
        return self._handle is not None
