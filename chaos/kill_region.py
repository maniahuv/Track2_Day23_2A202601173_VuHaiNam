"""Chaos script: giết 1 region. [CÓ SẴN — đọc kỹ phần AN TOÀN trước khi chạy]

Hai chế độ hỏng, cố ý khác nhau vì chúng cho RTO khác nhau (§6 Chaos Engineering):
  --mode stop      : process/container chết -> ConnectError ngay (fail nhanh, dễ phát hiện)
  --mode netblock  : cổng bị DROP -> request TREO tới timeout (fail chậm, health check
                     interval + timeout cộng thẳng vào RTO)

Hai backend:
  --backend bare   : uvicorn chạy trực tiếp, PID trong run/region-<r>.pid  (mặc định khi --mock)
  --backend docker : docker compose stop / iptables DROP trong container

--mock: pin mọi tham số thời gian (không phụ thuộc máy nhanh/chậm) -> chấm điểm reproducible.

AN TOÀN (đọc §6 "Nguyên tắc an toàn"):
  * Script TỪ CHỐI giết region nếu region còn lại không healthy -> không bao giờ tự
    tay tạo double-region outage rồi ngồi đo một con số vô nghĩa.
  * `--i-really-want-both` bỏ chặn đó, nhưng ghi cờ vào chaos-events.jsonl và
    tools/measure_rto.py sẽ đánh dấu drill là INVALID.
  * `restore` là kill switch: luôn chạy được, không cần điều kiện gì.

    python chaos/kill_region.py --region a --mode netblock --mock
    python chaos/kill_region.py restore --region a --backend bare

LƯU Ý: `restore` không có cờ `--mock` để tự suy ra backend như `kill` -- ở bare mode
PHẢI truyền `--backend bare` tường minh, nếu không nó mặc định `docker` và sẽ báo lỗi
trên máy không có Docker daemon (`docker compose ... start` thất bại).
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import time

import httpx

EVENTS = pathlib.Path("chaos/chaos-events.jsonl")
PID_DIR = pathlib.Path("run")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
PORT = {"a": 8001, "b": 8002}


def event(**kw):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with EVENTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("CHAOS", json.dumps(rec))
    return rec


def is_ready(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=timeout).status_code == 200
    except Exception:
        return False


def is_alive(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def _win_kernel32():
    # PHAI khai bao restype=HANDLE (64-bit) cho OpenProcess -- mac dinh ctypes
    # coi no tra ve c_int (32-bit) va cat cut handle tren Windows 64-bit, khien
    # OpenProcess tuong nhu that bai voi MOI pid (verify: process ranh ranh con
    # song van bi bao "chet").
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return kernel32


def _win_ntdll():
    import ctypes
    from ctypes import wintypes
    ntdll = ctypes.windll.ntdll
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    return ntdll


def _pid_alive(pid: int) -> bool:
    if os.name == 'nt':
        # os.kill(pid, 0) tren Windows KHONG phai no-op -- no goi TerminateProcess
        # that su (da verify: process chet ngay). Phai dung OpenProcess query-only.
        kernel32 = _win_kernel32()
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_of(region: str) -> int | None:
    f = PID_DIR / f"region-{region}.pid"
    if not f.exists():
        return None
    pid = int(f.read_text().strip())
    return pid if _pid_alive(pid) else None


def _win_pid_listening_on(port: int) -> int | None:
    """run/region-<r>.pid ghi PID cua `$!` trong bash, nhung tren may nay khong
    dang tin: `.venv/Scripts/python.exe -m uvicorn` co the tach thanh 1 process
    con rieng (khong co execve() thay image nhu POSIX), va process cha bash thay
    duoc doi khi da THOAT hen truoc do -- pid trong file co the la mo coi, khong
    con lien he cha/con voi process dang thuc su lang nghe. Cach dang tin duy nhat
    la hoi he dieu hanh: ai dang LISTEN tren cong nay. Dung `netstat -ano` thay vi
    tu dung GetExtendedTcpTable de tranh phu thuoc layout struct theo phien ban Win.
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                              timeout=5).stdout
    except Exception:
        return None
    needle = f"127.0.0.1:{port} "
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[1].startswith(needle.strip()) \
                and parts[3] == "LISTENING":
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def _win_target_pid(region: str) -> int | None:
    live = _win_pid_listening_on(PORT[region])
    if live is not None:
        return live
    return pid_of(region)  # fallback: file PID (co the la stub, con hon khong co gi)


def _win_process_tree(root_pid: int) -> list[int]:
    """PID trong run/*.pid la process bash spawn (`$!`), nhung tren Windows
    `.venv/Scripts/python.exe -m uvicorn` tu re-exec ra 1 process con moi that su
    bind cong va tra response (khong co execve() thay the image nhu POSIX).
    Suspend/kill dung mỗi root_pid la vo tac dung -- phai di het ca cay con.
    Duyet toan bo process list bang Toolhelp32Snapshot (khong can psutil)."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return [root_pid]

    children_of: dict[int, list[int]] = {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        found = kernel32.Process32First(snap, ctypes.byref(entry))
        while found:
            children_of.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            found = kernel32.Process32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)

    tree, stack = [], [root_pid]
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children_of.get(pid, []))
    return tree


def kill(region: str, mode: str, backend: str, force_both: bool, mock: bool):
    other = "b" if region == "a" else "a"
    other_alive = is_alive(other)
    if not other_alive and not force_both:
        event(action="refused", region=region, mode=mode,
              reason=f"region-{other} khong phan hoi /healthz -> giet region-{region} nua "
                     f"la double outage, RTO do duoc se vo nghia")
        raise SystemExit(
            f"CHAN LAI: region-{other} dang khong sống. Chạy `restore --region {other}` trước.\n"
            f"(Muốn ép: --i-really-want-both, nhưng drill sẽ bị đánh dấu INVALID.)")

    ev = event(action="kill", region=region, mode=mode, backend=backend, mock=mock,
               other_region=other, other_alive=other_alive, forced_both=force_both,
               note="t_outage_start — moc 0 cua RTO clock")
    if backend == "bare":
        pid = _win_target_pid(region) if os.name == 'nt' else pid_of(region)
        if pid is None:
            raise SystemExit(f"khong tim thay PID cua region-{region} trong {PID_DIR}")
        # Xử lý tương thích đa nền tảng
        if os.name == 'nt':
            kernel32 = _win_kernel32()
            ntdll = _win_ntdll()
            PROCESS_SUSPEND_RESUME = 0x0800
            PROCESS_TERMINATE = 0x0001

            for p in _win_process_tree(pid):
                if mode == "netblock":
                    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, p)
                    if handle:
                        ntdll.NtSuspendProcess(handle)
                        kernel32.CloseHandle(handle)
                else:
                    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, p)
                    if handle:
                        kernel32.TerminateProcess(handle, 1)
                        kernel32.CloseHandle(handle)
        else:
            # netblock: SIGSTOP -> TCP handshake vẫn xong nhưng không ai trả lời => request TREO
            #           (đúng hành vi của iptables DROP ở tầng app)
            # stop    : SIGKILL -> cổng đóng => ConnectError ngay
            os.kill(pid, signal.SIGSTOP if mode == "netblock" else signal.SIGKILL)
    else:
        svc = f"serving-{region}"
        if mode == "stop":
            subprocess.run(["docker", "compose", "stop", svc], check=True)
        else:
            subprocess.run(["docker", "exec", "--privileged", svc, "iptables", "-A", "INPUT",
                            "-p", "tcp", "--dport", "8000", "-j", "DROP"], check=True)
    return ev


def restore(region: str, backend: str):
    if backend == "bare":
        pid = _win_target_pid(region) if os.name == 'nt' else pid_of(region)
        if pid:
            if os.name == 'nt':
                kernel32 = _win_kernel32()
                ntdll = _win_ntdll()
                PROCESS_SUSPEND_RESUME = 0x0800
                for p in _win_process_tree(pid):
                    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, p)
                    if handle:
                        ntdll.NtResumeProcess(handle)
                        kernel32.CloseHandle(handle)
                return event(action="restore", region=region, method="NtResumeProcess", pid=pid)
            else:
                os.kill(pid, signal.SIGCONT)
                return event(action="restore", region=region, method="SIGCONT", pid=pid)
        return event(action="restore", region=region, method="need_manual_start",
                     note="process da bi SIGKILL, chay `make up-bare` lai")
    subprocess.run(["docker", "compose", "start", f"serving-{region}"], check=False)
    subprocess.run(["docker", "exec", "--privileged", f"serving-{region}", "iptables", "-F",
                    "INPUT"], check=False)
    return event(action="restore", region=region, method="docker_start+iptables_flush")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="kill", choices=["kill", "restore", "status"])
    p.add_argument("--region", default="a", choices=["a", "b"])
    p.add_argument("--mode", default="netblock", choices=["stop", "netblock"])
    p.add_argument("--backend", default=None, choices=["bare", "docker"])
    p.add_argument("--mock", action="store_true",
                   help="pin tham so thoi gian -> cham diem reproducible; ham y --backend bare")
    p.add_argument("--i-really-want-both", action="store_true")
    a = p.parse_args()
    backend = a.backend or ("bare" if a.mock else "docker")
    if a.cmd == "status":
        print(json.dumps({r: {"alive": is_alive(r), "ready": is_ready(r)} for r in "ab"}, indent=2))
    elif a.cmd == "restore":
        restore(a.region, backend)
    else:
        kill(a.region, a.mode, backend, a.i_really_want_both, a.mock)
