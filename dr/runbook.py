"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    evt = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "step": f"{n}_{name}"}
    evt.update(kw)
    with open(LOG, "a") as f:
        f.write(json.dumps(evt) + "\n")
    print(json.dumps(evt))


def confirm(auto: bool, msg: str) -> bool:
    if auto: return True
    return input(f"{msg} [y/N]: ").strip().lower() == "y"


def _confirm_outage(primary: str, interval: float, threshold: int, max_wait: float) -> dict:
    """Dung 1 lan fail khong phai la outage (chinh la ly do 3a co threshold chong
    flap) -- neu goi failover() ngay tai day thi t_cutover < t_detect, bi
    measure_rto.py gan warning va test_drill2_hop_le truot cung. Uu tien doc
    reports/health-events.jsonl (health_checker.py dang chay song song trong drill
    that) de dam bao cutover luon xay ra SAU khi detect that; neu chua thay dong
    nao trong max_wait giay (vd goi runbook doc lap, khong co health_checker.py
    chay cung) thi tu probe lay bang chinh ham hc.probe() cua 3a."""
    start = time.time()
    seen = 0
    while time.time() - start < max_wait:
        if HEALTH_LOG.exists():
            lines = HEALTH_LOG.read_text().splitlines()
            for line in lines[seen:]:
                evt = json.loads(line)
                if evt.get("region") == primary and evt.get("to") == "UNHEALTHY":
                    return evt
            seen = len(lines)
        time.sleep(0.5)

    fails = 0
    while fails < threshold:
        ready, reason = hc.probe(primary, timeout=2.0)
        fails = 0 if ready else fails + 1
        if fails < threshold:
            time.sleep(interval)
    return {"region": primary, "to": "UNHEALTHY", "reason": "self_probe_fallback",
            "interval_s": interval, "threshold": threshold}


def run(primary: str, target: str, backend: str, auto: bool,
        interval: float = 5.0, threshold: int = 3, max_wait: float = 60.0) -> dict:
    t0 = time.time()

    confirmed = _confirm_outage(primary, interval, threshold, max_wait)
    step(1, "xac_nhan_outage", **confirmed)
    if not confirm(auto, f"Xác nhận outage ở region {primary}. Bạn có muốn chuyển đổi sang {target}?"):
        return {"status": "aborted"}

    step(2, "thong_bao_incident")
    
    fo_res = fo.failover(target, backend, 60)
    step(3, "scale_gpu_pool", ok=bool(fo_res))
    
    if not fo_res:
        return {"status": "failover_aborted"}

    try:
        st = httpx.get(URL[target] + "/v1/state").json()
        step(4, "verify_state_replica", count=st.get("count"), weights=st.get("weights"))
    except Exception as e:
        step(4, "verify_state_replica", error=str(e))

    try:
        with open("edge/active_region", "r") as f:
            curr_region = f.read().strip()
        step(5, "dns_cutover", target=target, current=curr_region)
    except Exception as e:
        step(5, "dns_cutover", error=str(e))

    errs = 0
    latencies = []
    for _ in range(10):
        t_req = time.time()
        try:
            if httpx.get("http://127.0.0.1:8080/v1/infer").status_code != 200:
                errs += 1
        except:
            errs += 1
        latencies.append(time.time() - t_req)
        
    latencies.sort()
    p95 = latencies[int(len(latencies)*0.95)] if latencies else 0
    step(6, "verify_golden_signals", p95=p95, errors=errs)

    step(7, "post_incident", elapsed_s=time.time() - t0)
    
    return {"status": "done"}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--interval", type=float, default=5.0,
                    help="phai khop voi dr/health_checker.py dang chay song song")
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--max-wait", type=float, default=60.0,
                    help="cho toi da bao nhieu giay de xac nhan outage truoc khi bo cuoc")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto,
                          a.interval, a.threshold, a.max_wait), indent=2))
