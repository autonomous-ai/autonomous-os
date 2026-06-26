# DL Backend

Backend nhận thức (perception) tăng tốc GPU cho thiết bị Autonomous. Nó chạy các mô
hình deep-learning mà HAL trên thiết bị không chạy nổi cục bộ (nhận dạng hành động,
cảm xúc khuôn mặt và giọng nói, tư thế + công thái học, phát hiện vật thể, embedding
người nói) và expose qua WebSocket và HTTP, phía sau một load balancer mã hóa tùy chọn.

Trang này là **tổng quan nền tảng** — backend là gì và nằm ở đâu. **Tài liệu đầy đủ
nằm cạnh code** trong [`dlbackend/docs/`](../../dlbackend/docs/); schema, danh sách
model, endpoint và biến môi trường để ở đó, không để ở đây.

| Cần… | Đọc |
|------|-----|
| Topology tiến trình, cổng, prefix URL, vòng đời request | [`dlbackend/docs/architecture.md`](../../dlbackend/docs/architecture.md) |
| Từng endpoint kèm schema request/response | [`dlbackend/docs/api.md`](../../dlbackend/docs/api.md) |
| Mô hình perception, enum, kiểu output | [`dlbackend/docs/perceptions.md`](../../dlbackend/docs/perceptions.md) |
| Load balancer + mã hóa RSA/AES + nginx | [`dlbackend/docs/crypto-and-loadbalancer.md`](../../dlbackend/docs/crypto-and-loadbalancer.md) |
| Deploy: cài đặt, scale GPU, RunPod, Docker, TLS | [`dlbackend/docs/deployment.md`](../../dlbackend/docs/deployment.md) |
| Toàn bộ biến môi trường kèm giá trị mặc định | [`dlbackend/docs/configuration.md`](../../dlbackend/docs/configuration.md) |

## Kiến trúc tổng quát

Hai tiến trình FastAPI đứng sau cửa ngõ nginx:

```
HAL / client
   │  https / wss  :8899
   ▼
┌─────────┐   /lelamp/ → /hal/      ┌──────────┐  round-robin   ┌──────────┐
│  nginx  │ ─────────────────────▶ │ lbserver │ ─────────────▶ │ dlserver │
│  :8899  │     (nâng cấp WS)       │  :7999   │  giải mã →      │  :8001   │
└─────────┘                         └──────────┘   plaintext     └──────────┘
```

- **`dlserver`** (`:8001`) — nạp các mô hình ML, phục vụ các endpoint perception.
- **`lbserver`** (`:7999`) — proxy round-robin trước một hoặc nhiều `dlserver`; kết
  thúc (terminate) mã hóa RSA+AES để `dlserver` luôn nhận plaintext.
- **`nginx`** (`:8899`) — cửa ngõ công khai; ánh xạ prefix `/lelamp/` (phía thiết
  bị) sang prefix nội bộ `/hal/` và nâng cấp WebSocket.

Khi dev một node, có thể gọi thẳng `dlserver:8001` với mã hóa tắt.

## Cung cấp những gì

Các subsystem perception expose cho thiết bị: nhận dạng hành động, cảm xúc khuôn mặt,
cảm xúc giọng nói (SER), ước lượng tư thế (kèm công thái học RULA), phát hiện vật thể
và embedding người nói. Phát hiện khuôn mặt và phát hiện người chạy nội bộ để cấp dữ
liệu cho các pipeline đó. Các request từ nhiều session đồng thời được gom lại (batch)
trước khi gửi lên GPU — cấu hình `BATCH_SIZE` và `BATCH_TIMEOUT` cho từng model.
Input được giới hạn kích thước (ảnh, audio) để chống tấn công DoS — xem
[`configuration.md#input-limits`](../../dlbackend/docs/configuration.md#input-limits).
Lựa chọn model và output:
[`dlbackend/docs/perceptions.md`](../../dlbackend/docs/perceptions.md).

## Thiết bị dùng nó thế nào

HAL là client chính. Trỏ nó tới backend bằng `DL_BACKEND_URL` và `DL_API_KEY` dùng
chung (gửi qua `X-API-Key`), và tùy chọn bật mã hóa phía client. Sensing stream khung
hình camera tới các endpoint hành động/tư thế/cảm xúc; voice POST âm thanh cuối câu
nói tới endpoint cảm xúc giọng nói. Endpoint và payload chính xác nằm trong
[`dlbackend/docs/api.md`](../../dlbackend/docs/api.md); mọi tham số cấu hình nằm trong
[`dlbackend/docs/configuration.md`](../../dlbackend/docs/configuration.md).

## Triển khai

`dlbackend/` có sẵn `Dockerfile` (CUDA + nginx), `nginx.conf` / `nginx-ssl.conf` và
một `Makefile`; hai tiến trình chạy bằng `python -m dlserver` và `python -m lbserver`.
Cài đặt, single-node vs scale master/slave nhiều GPU, RunPod, Docker và TLS:
[`dlbackend/docs/deployment.md`](../../dlbackend/docs/deployment.md).

---

> **Đồng bộ tài liệu:** trang này chỉ là tổng quan. Khi code đổi, cập nhật tài liệu
> chi tiết trong [`dlbackend/docs/`](../../dlbackend/docs/) **và** bản tiếng Anh
> [`docs/dlbackend.md`](../dlbackend.md). Code là nguồn chân lý.
</content>
