# -*- coding: utf-8 -*-
"""零依赖的进程资源采样器（Windows API / ctypes）。
不依赖 psutil，避免向 conda base 环境安装任何包。
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_psapi = ctypes.WinDLL("psapi.dll")
_k32 = ctypes.WinDLL("kernel32.dll")
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
_psapi.GetProcessMemoryInfo.restype = wt.BOOL
_k32.GetCurrentProcess.restype = wt.HANDLE
_k32.GetProcessTimes.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
    ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
]
_k32.GetProcessTimes.restype = wt.BOOL


def _mem_mb():
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    if not _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return None
    return pmc.WorkingSetSize / 1024.0 / 1024.0


def _cpu_seconds():
    """返回本进程累计 CPU 时间（秒），含内核态。"""
    creation, exit_, kernel, user = (wt.FILETIME() for _ in range(4))
    ok = _k32.GetProcessTimes(
        _k32.GetCurrentProcess(),
        ctypes.byref(creation), ctypes.byref(exit_),
        ctypes.byref(kernel), ctypes.byref(user),
    )
    if not ok:
        return None
    def to_s(ft):
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7
    return to_s(kernel) + to_s(user)


class ResourceSampler:
    """后台线程采样：峰值工作集、进程 CPU 时间增量。"""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.peak_mem_mb = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._cpu0 = None
        self._t0 = None

    def _loop(self):
        while not self._stop.is_set():
            m = _mem_mb()
            if m and m > self.peak_mem_mb:
                self.peak_mem_mb = m
            self._stop.wait(self.interval)

    def start(self):
        self._cpu0 = _cpu_seconds()
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        wall = time.perf_counter() - self._t0
        cpu1 = _cpu_seconds()
        cpu_used = (cpu1 - self._cpu0) if (cpu1 is not None and self._cpu0 is not None) else None
        return {
            "wall_s": round(wall, 3),
            "peak_mem_mb": round(self.peak_mem_mb, 1),
            "cpu_s": round(cpu_used, 3) if cpu_used is not None else None,
            "cpu_pct_of_one_core": round(cpu_used / wall * 100, 1) if cpu_used and wall > 0 else None,
        }


if __name__ == "__main__":
    s = ResourceSampler().start()
    acc = 0
    for i in range(3_000_000):
        acc += i * i
    print(ResourceSampler.stop(s) if False else s.stop())
