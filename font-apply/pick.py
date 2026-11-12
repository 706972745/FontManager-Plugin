# -*- coding: utf-8 -*-
"""「点一下要应用到的窗口」：pick 模式下由插件自己等下一次点击。

全部在插件进程内完成，**不经过主程序**：主程序只负责把双击转成一次插件调用，
不碰任何窗口轮询逻辑（用户口径：主程序尽量只开放端口）。

为什么轮询前台窗口而不是装全局鼠标钩子：
- 只在待命的那十几秒里工作，80ms 一次的 GetForegroundWindow 成本可忽略；
- 全局钩子会被安全软件当成键盘记录器，也有 LowLevelHooksTimeout 被系统摘钩的风险；
- 取消只需要一个循环条件，没有钩子生命周期与消息循环要管。

只读窗口归属，不读剪贴板、不读窗口内容、不记录任何东西。
"""

import os
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# exe → 目标标识。与主程序 procs.rs / adobe.rs 同一口径：
# 大小写不敏感（Windows 文件名本来就不区分），CorelDRAW 少数汉化版多套了一层壳。
TARGETS = {
    "photoshop.exe": "photoshop",
    "illustrator.exe": "illustrator",
    "coreldrw.exe": "coreldraw",
    "coreldrw.exe.exe": "coreldraw",
}

# 任务栏 / 桌面 / 开始菜单都属于它。它**不是**目标，只是「切换途中」的路过窗口。
EXPLORER_EXE = "explorer.exe"

POLL_SECONDS = 0.08
TIMEOUT_SECONDS = 15.0
EXPLORER_GRACE_SECONDS = 1.5

MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002

_READY = False
_kernel32 = None
_user32 = None
_PROCESSENTRY32W = None

if IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        class _PROCESSENTRY32W(ctypes.Structure):  # noqa: N801 - 跟着 Win32 的名字写
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                # ULONG_PTR：32 位是 4 字节、64 位是 8 字节，必须用 c_size_t 才对得上
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * MAX_PATH),
            ]

        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _user32 = ctypes.WinDLL("user32", use_last_error=True)

        _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.Process32FirstW.restype = wintypes.BOOL
        _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.Process32NextW.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL

        _user32.GetForegroundWindow.argtypes = []
        _user32.GetForegroundWindow.restype = wintypes.HWND
        _user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        _READY = True
    except Exception:  # noqa: BLE001 - 拿不到 API 就当不支持，不能让 import 崩
        _READY = False


def _exe_of_pid(pid):
    """按 pid 反查 exe 文件名（小写）。用 Toolhelp 快照而不是 OpenProcess：

    目标软件常以管理员权限运行，普通权限拿不到它的进程句柄，
    但进程快照是系统级枚举，不需要对目标进程有任何权限 —— 我们只要一个文件名。
    """
    import ctypes
    import ctypes.wintypes as wintypes

    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == -1:
        return ""
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            return ""
        while True:
            if entry.th32ProcessID == pid:
                return (entry.szExeFile or "").strip().lower()
            if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                return ""
    finally:
        _kernel32.CloseHandle(snap)


def wait_for_target(timeout=TIMEOUT_SECONDS):
    """等用户点一个窗口。返回 `(app, exe, reason)`：

    - `("photoshop", "photoshop.exe", "hit")` —— 命中目标；
    - `(None, exe, "not-target")` —— 点到了别的软件（或任务栏宽限到期）；
    - `(None, "", "timeout")` —— 15 秒内没点任何别的窗口。

    非 Windows 返回 `None`（插件声明 platforms=Windows，但别让调用方踩到 ImportError）。
    """
    if not _READY:
        return None

    import ctypes
    import ctypes.wintypes as wintypes

    self_pid = os.getpid()
    baseline = _user32.GetForegroundWindow()
    # pid → exe 缓存：同一个软件常有多个顶层窗口，pid 不变就不必再拍快照
    cache = {}
    started = time.monotonic()
    explorer_since = None

    while True:
        time.sleep(POLL_SECONDS)
        if time.monotonic() - started >= timeout:
            return (None, "", "timeout")

        fg = _user32.GetForegroundWindow()
        if not fg or fg == baseline:
            # 又回到我们自己（FontManager）：中途那次绕路结束，清掉任务栏计时
            explorer_since = None
            continue

        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if not pid.value or pid.value == self_pid:
            explorer_since = None
            continue

        exe = cache.get(pid.value)
        if exe is None:
            exe = _exe_of_pid(pid.value)
            cache[pid.value] = exe

        if exe == EXPLORER_EXE:
            # 点任务栏切窗口时前台会先变成 explorer.exe，真正的目标窗口要等系统激活，
            # 通常晚 100–300ms。这段时间不能结算，否则「点任务栏切到 PS」会被误判。
            if explorer_since is None:
                explorer_since = time.monotonic()
            if time.monotonic() - explorer_since < EXPLORER_GRACE_SECONDS:
                continue
            # 宽限到期还是任务栏：用户大概就是想点桌面取消，别让他干等 15 秒
            return (None, exe, "not-target")

        app = TARGETS.get(exe)
        if app:
            return (app, exe, "hit")
        return (None, exe, "not-target")
