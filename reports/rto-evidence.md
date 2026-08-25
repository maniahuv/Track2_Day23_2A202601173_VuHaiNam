# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T16:36:07` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.1s` (status 503, ReadTimeout) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage tới hết drill (31 dòng) | `reports/drill-1-nodr.jsonl:31` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` | `reports/drill-1-nodr.jsonl:17` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill, region:a` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 2.1 | dòng `ok:false` đầu (status 503, ReadTimeout) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 20.9 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 21.3 | `step:2_restore_snapshot → 3_scale_pool` | `reports/failover-events.jsonl:2-3` |
| Region phụ ready (GPU pool warm-up xong) | 28.8 | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 28.8 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **30.7** | dòng `ok:true, served_by:"b"` đầu tiên sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `30.7s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `8.0s` / `4` doc | 300s (5 phút) | PASS |

Nguồn RTO/RPO tổng hợp: output đầy đủ của `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
→ `"valid": true`, `"warnings": []`, `"rto_verdict": "PASS"`, `"recovered_by_region": "b"`,
`"rpo_at_restore_s": 8.0`, `"docs_lost": 4` (RPO chi tiết: `reports/failover-events.jsonl:2`,
`embed_model_version: "embed-model=vi-e5-base@v3"`).

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 20.9 | sàn lý thuyết `interval_s × threshold` = 5.0×3 = 15.0s (`reports/health-events.jsonl:2`); thực tế 20.9s vì phải chờ tới poll cycle tiếp theo phát hiện đủ 3 lần fail liên tiếp | Hạ `interval` — nhưng tăng nguy cơ báo động giả (flapping) |
| Snapshot restore + scale pool | 0.4 | `1_verify_target → 3_scale_pool` (`reports/failover-events.jsonl:1-3`), backend `fs` nên gần như tức thời | Dùng ổ cứng nhanh hơn (ít tác dụng vì đã rất nhanh) |
| GPU pool warm-up | 7.4 | `3_scale_pool → 4_wait_ready` (`reports/failover-events.jsonl:3-4`), `WARMUP_SECONDS` mặc định 6s + thời gian poll `/readyz` | Dùng model nhỏ hơn / pool luôn "ấm" sẵn (active-active) |
| DNS/LB TTL cache | 2.0 | `t_recovered (drill-2-withdr.jsonl:39) − t_cutover (failover-events.jsonl:5)` | Hạ `EDGE_TTL_SECONDS` — đánh đổi với tải lên DNS/LB |
| **Tổng (≈ RTO đo được)** | **30.7** | 20.9 + 0.4 + 7.4 + 2.0 = 30.7 | — |
