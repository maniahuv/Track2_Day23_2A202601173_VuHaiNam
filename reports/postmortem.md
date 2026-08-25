# Postmortem — DR Drill Lab 23 (TEMPLATE)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T16:37:07Z | outage bắt đầu (kill region a) | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T16:37:07Z | user đầu tiên bị ảnh hưởng (+2.1s, status 503) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T16:37:28Z | health check alert (region a → UNHEALTHY, +20.9s) | `reports/health-events.jsonl:2` |
| 2026-08-25T16:37:29Z | runbook xác nhận outage + gọi failover (+21.3s) | `reports/runbook-run.jsonl:1` |
| 2026-08-25T16:37:38Z | resolved (request đầu tiên OK từ region b, +30.7s) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `30.7s` · gap: `269.3s` (còn nhiều dư địa, verdict PASS)
- RPO mục tiêu: 300s · đo được: `8.0s` (`4` doc bị mất) · gap: `292.0s`
- **Bước tốn nhiều giây nhất:** `Health-check detect floor` (20.9s / 30.7s ≈ 68% RTO) — vì sao?
  Vì `interval=5s, threshold=3` đặt ra sàn lý thuyết 15s, cộng thêm việc phải chờ tới poll
  cycle kế tiếp mới đủ 3 lần fail liên tiếp (chống flapping) nên thực tế mất 20.9s.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

Vì health-check threshold=3 lần liên tiếp mới báo — đây là đánh đổi có chủ đích (chống
flapping, xem [[reports/rto-evidence.md]]) chứ không phải lỗi, nhưng nó chiếm gần 70%
RTO. Nếu outage thật kéo dài và không ai theo dõi dashboard, độ trễ thông báo cho operator
(runbook chỉ tự động hoá sau khi có tín hiệu UNHEALTHY) sẽ là điểm nghẽn kế tiếp — hệ thống
hiện tại giả định có người/chuông báo túc trực để `--auto` hoặc xác nhận `y/N` được gọi kịp
thời; nếu không, RTO thực tế sẽ dài hơn nhiều so với 30.7s đo được ở đây.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm health check interval xuống 2s (threshold giữ 3) | Admin | Q3 | ~9s RTO |
| 2 | Gắn alert (Slack/PagerDuty) khi health_checker ghi UNHEALTHY, tránh phụ thuộc người trực xem log | Admin | Q3 | giảm độ trễ thông báo, khó đo bằng giây cụ thể |
| 3 | Rút ngắn `WARMUP_SECONDS` hoặc giữ pool region phụ luôn ấm (active-active) | Admin | Q4 | ~6s RTO |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
   Là 15 giây (sàn lý thuyết); thực tế phần phát hiện mất 20.9s trong tổng RTO 30.7s ≈ 68%.
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
   Sàn lý thuyết giảm còn 3s (interval×threshold = 1×3), RTO có thể giảm khoảng 12-17s.
   Trả giá: mỗi lần mạng lag nhẹ hoặc region chỉ chậm tạm thời cũng đủ 3 lần fail liên tiếp
   trong 3s, dễ trigger failover giả (flapping) — đặc biệt nguy hiểm nếu failover không có
   circuit breaker chống chạy 2 chiều liên tục.
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
   bạn có nghĩa gì với khách hàng?
   Có nghĩa là 4 tài liệu khách hàng đã upload trong khoảng thời gian giữa lần replicate
   gần nhất và lúc outage sẽ bị mất vĩnh viễn, không thể khôi phục — khách phải tự upload
   lại. Với chu kỳ replicate 30s như trong drill này, con số 4 doc là nhỏ, nhưng nếu chu kỳ
   replicate thưa hơn (vd 1 giờ) thì `docs_lost` có thể lên tới hàng trăm/nghìn tài liệu.
