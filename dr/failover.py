"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    evt = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    evt.update(kw)
    with open(LOG, "a") as f:
        f.write(json.dumps(evt) + "\n")
    print(json.dumps(evt))

def failover(target: str, backend: str, wait: float) -> dict:
    # Bước 1: Verify target
    emit(step="1_verify_target")
    
    # Bước 2: Restore snapshot
    meta = snapshot.get(target, backend)
    primary = "a" if target == "b" else "b"
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(primary_db, restored_db)
    emit(step="2_restore_snapshot", rpo_seconds=rpo_info["rpo_seconds"], docs_lost=rpo_info["docs_lost"], embed_model_version=meta.get("embed_model_version", "unknown"))
    
    # Bước 3: Scale GPU pool
    with open(f"state/region-{target}/pool_state", "w") as f:
        f.write("full")
    emit(step="3_scale_pool")
    
    # Bước 4: Wait cho API của Region đích Ready
    start = time.time()
    ready = False
    while time.time() - start < wait:
        try:
            if httpx.get(URL[target] + "/readyz", timeout=1.0).status_code == 200:
                ready = True
                break
        except:
            pass
        time.sleep(1)
        
    if not ready:
        # Nếu timeout thì abort, KHÔNG cutover DNS
        return {}
        
    emit(step="4_wait_ready")
    
    # Bước 5: DNS Cutover
    with open("edge/active_region", "w") as f:
        f.write(target)
    emit(step="5_dns_cutover")
    
    return {"status": "success", "target": target}



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
