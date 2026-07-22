# Hệ Thống Plugin (Tương Lai)

Ghi chú thiết kế cho hệ thống plugin do cộng đồng đóng góp, lấy cảm hứng từ mô hình
`reachy_mini_python_app` của Pollen Robotics. Các plugin chạy **bên ngoài HAL** như
các tiến trình độc lập và gọi HTTP API của HAL (`:5001`) để truy cập phần cứng.

Viết tháng 7 năm 2026. Trạng thái: **chỉ là thiết kế, chưa được triển khai.**

## Vấn Đề

Hiện tại, để thêm một hành vi mới vào Autonomous OS cần:

1. PR vào HAL (thay đổi driver/route Python)
2. PR vào skills/ (SKILL.md để agent nhận biết)
3. Xem xét code + merge + đẩy OTA

Điều này tạo ra rào cản cao cho các cộng tác viên cộng đồng. Pollen đã giải quyết
vấn đề này bằng cách cài đặt ứng dụng 1 nhấp từ HF Spaces — hơn 200 ứng dụng từ
hơn 150 người tạo, phần lớn không có nền tảng về robot học.

## Kiến Trúc

HAL giữ nguyên như cũ. Các plugin là các tiến trình độc lập sử dụng HAL như một dịch vụ:

```
┌─────────────────────────────────────────────┐
│  Agent Runtime (OpenClaw / Hermes)           │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ Skills   │  │ MCP Tools│  │ Plugins   │  │
│  │ (local)  │  │ (remote) │  │ (local)   │  │
│  └──────────┘  └──────────┘  └─────┬─────┘  │
└────────────────────────────────────┼────────┘
                                     │ subprocess
┌────────────────────────────────────▼────────┐
│  Plugin Process                              │
│  - Own Python venv                           │
│  - Calls HAL API (localhost:5001)            │
│  - Registers skills with agent               │
└─────────────────────────┬───────────────────┘
                          │ HTTP
┌─────────────────────────▼───────────────────┐
│  HAL (:5001)                                 │
│  LED, servo, audio, camera, sensing          │
└─────────────────────────────────────────────┘
```

## Các Điểm Thiết Kế Chính

### 1. Định Dạng Plugin

Một plugin là một gói Python với điểm vào tiêu chuẩn:

```python
# plugin.json (metadata)
{
  "name": "dance-party",
  "version": "1.0.0",
  "description": "Syncs robot dance to music beats",
  "entry": "main.py",
  "skills": ["dance_to_music", "stop_dance"],
  "hal_endpoints": ["/servo/*", "/led/*", "/audio/*"]
}
```

### 2. HAL Như SDK

Các plugin truy cập phần cứng thông qua HTTP API hiện có của HAL — không import nội bộ:

```python
import requests

# Move servo
requests.post("http://localhost:5001/servo/move", json={"pan": 45, "tilt": 10})

# Set LED
requests.post("http://localhost:5001/led/set", json={"effect": "pulse", "color": "blue"})

# Play audio
requests.post("http://localhost:5001/audio/speak", json={"text": "Let's dance!"})
```

Không cần code HAL mới — API đã tồn tại sẵn.

### 3. Tự Động Đăng Ký Skill

Khi một plugin khởi động, nó đăng ký các skill của mình với agent runtime để
agent biết khi nào cần gọi nó:

```
Plugin starts → POST /api/device/plugins/:name/skills
  → agent runtime sees new tools available
  → user says "play some dance music"
  → agent calls plugin's skill endpoint
```

### 4. Vòng Đời (OS Server quản lý)

```
POST   /api/device/plugins/install   — download from URL (HF Space / Git)
GET    /api/device/plugins           — list installed plugins
POST   /api/device/plugins/:name/start
POST   /api/device/plugins/:name/stop
DELETE /api/device/plugins/:name     — uninstall
```

### 5. Cô Lập

- Mỗi plugin chạy trong subprocess + Python venv riêng của nó
- Lỗi trong plugin không ảnh hưởng đến HAL hoặc agent
- OS Server theo dõi tình trạng plugin, khởi động lại khi có lỗi
- Các plugin chỉ truy cập HAL qua HTTP — không có quyền truy cập filesystem vào nội bộ HAL

### 6. Phân Phối

Cùng mô hình với Pollen:
- Xuất bản dưới dạng HF Space (được gắn thẻ để khám phá)
- Cài đặt bằng URL từ web UI (Settings > Plugins)
- Hoặc từ CLI: `POST /api/device/plugins/install {"url": "..."}`

## So Sánh Với Các Điểm Mở Rộng Hiện Có

| Mở rộng | Chạy ở | Phạm vi | Rào cản |
|---------|--------|---------|---------|
| SKILL.md | Agent runtime | Dựa trên prompt | Thấp (tệp văn bản) |
| MCP Tools | Cloud (HF Space) | Hàm không trạng thái | Thấp (chỉ URL) |
| **Plugins** | Thiết bị (subprocess) | Hành vi đầy đủ | Trung bình (gói Python) |
| HAL code | Thiết bị (in-process) | Driver phần cứng | Cao (PR + xem xét) |

## Câu Hỏi Mở

- Các plugin có nên có quyền truy cập WebSocket trực tiếp vào agent runtime, hay chỉ
  thông qua API đăng ký skill?
- Cách xử lý các plugin cần trạng thái liên tục (cơ sở dữ liệu, tệp cấu hình)?
- Có nên có một marketplace/registry plugin ngoài HF Spaces không?
- Giới hạn tài nguyên (CPU, bộ nhớ) mỗi plugin trên các thiết bị bị hạn chế (CM4)?

## Tài Liệu Tham Khảo

- Mô hình ứng dụng của Pollen: `devices/reachy-mini/docs/pollen-ecosystem-analysis.md`
  §App Distribution
- [Make and Publish Reachy Mini Apps (HF Blog)](https://huggingface.co/blog/pollen-robotics/make-and-publish-your-reachy-mini-apps)
- [Robot App Store (VentureBeat)](https://venturebeat.com/technology/the-app-store-for-robots-has-arrived-hugging-face-launches-open-source-reachy-mini-app-store-with-200-apps)
