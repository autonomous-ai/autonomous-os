# Model Weights Trên Thiết Bị (HAL)

HAL chạy một số model ONNX trên thiết bị (nhận diện khuôn mặt, pose 2D, …). Các
file weight này là binary lớn và **không commit vào git**. Tài liệu này mô tả nơi
chúng nằm, cách weight nhận diện khuôn mặt được tải về khi dùng lần đầu, và các
biến môi trường điều khiển cả hai.

> Code: `hal/drivers/sensing/perceptions/processors/faceid/model_store.py`
> (đường dẫn + tải về) và `.../faceid/recognizer.py` (`FaceRecognizer.start`, điểm
> kích hoạt tải lần đầu).

## Vị trí cache

Toàn bộ weight trên thiết bị nằm trong một thư mục cache duy nhất, mặc định
**`/root/local/models/`**. Điều này theo đúng quy ước của pose model
(`HAL_POSE_MODEL_PATH` mặc định là `/root/local/models/rtmpose-m.onnx`, xem
`hal/config.py`). Tên file trong cache luôn là basename của model.

## Model nhận diện khuôn mặt

Pipeline khuôn mặt v2 (`faceid/`) dùng ba model ONNX:

| Vai trò | File | Object trên bucket | Biến env override đường dẫn |
|---------|------|--------------------|------------------------------|
| SCRFD face detector | `scrfd_2.5g_fp32.onnx` | `onnx_models/scrfd_2.5g_fp32.onnx` | `HAL_FACE_SCRFD_MODEL_PATH` |
| EdgeFace embedder | `edgeface_s_gamma_05_opt.onnx` | `onnx_models/edgeface_s_gamma_05_opt.onnx` | `HAL_FACE_EDGEFACE_MODEL_PATH` |
| MediaPipe landmark regressor | `MediaPipeFaceLandmarkDetector.onnx` | `onnx_models/MediaPipeFaceLandmarkDetector.onnx` | `HAL_FACE_LANDMARK_MODEL_PATH` |

URL tải đầy đủ của mỗi model là `<cdn_base>/<object>`, tức mặc định là
`https://storage.googleapis.com/autonomous-models/onnx_models/<file>`. Bố cục
remote khớp với bucket weight của cloud perception-service
(`integrations/perception-service/src/core/utils/files.py`).

## Tải về khi dùng lần đầu

Weight được tải lazy — không có gì tải lúc import.

1. Khi `FaceRecognizer.start()` chạy (một lần, trước khi tạo các ONNX session), nó
   gọi `ensure_face_models(...)` với ba đường dẫn model.
2. Với mỗi đường dẫn **chưa** tồn tại cục bộ, URL remote được suy ra từ basename
   của file và file được tải vào thư mục cache.
3. Việc tải là **nguyên tử (atomic)**: ghi vào file tạm theo PID
   (`<name>.part.<pid>`) rồi rename vào đúng chỗ khi thành công, nên một lần crash
   hay kill giữa chừng không bao giờ để lại file cụt mà lần chạy sau nhầm là model
   hoàn chỉnh.

Các trường hợp lỗi:

- **Tên file lạ mà không có bản cục bộ** → `FileNotFoundError` (không suy ra được
  remote; hãy trỏ `HAL_FACE_*_MODEL_PATH` tương ứng tới file đã provision sẵn).
- **Lỗi tải** (mạng/404) → `RuntimeError`.

> **Trạng thái:** weight khuôn mặt **chưa được upload lên bucket**. Cho đến khi
> upload, thiết bị không có bản cục bộ sẽ raise ở `start()`. Đường dẫn tải đã sẵn
> sàng và tự động hoạt động ngay khi các object tồn tại tại URL ở trên — không cần
> sửa code.

## Biến môi trường

| Biến | Mặc định | Mục đích |
|------|----------|----------|
| `HAL_FACE_MODEL_PATH` | `/root/local/models` | Thư mục cache gốc cho weight khuôn mặt |
| `HAL_FACE_SCRFD_MODEL_PATH` | `<dir>/scrfd_2.5g_fp32.onnx` | Đường dẫn đầy đủ tới model SCRFD |
| `HAL_FACE_EDGEFACE_MODEL_PATH` | `<dir>/edgeface_s_gamma_05_opt.onnx` | Đường dẫn đầy đủ tới model EdgeFace |
| `HAL_FACE_LANDMARK_MODEL_PATH` | `<dir>/MediaPipeFaceLandmarkDetector.onnx` | Đường dẫn đầy đủ tới model landmark |
| `HAL_FACE_MODEL_CDN_BASE` | `https://storage.googleapis.com/autonomous-models` | URL gốc của bucket weight |
| `HAL_FACE_LANDMARK_CONF_THRESHOLD` | `0.99` | Ngưỡng face-presence cho việc align landmark. Điểm số bão hoà (median đúng bằng 1.000 trên 990 frame đã log) nên khoảng dùng được chỉ nằm ở phần trăm cuối — 0.6 chưa bao giờ chặn được gì. Lọc ra các crop tuy là mặt nhưng không mang danh tính, ví dụ vành tai ở cự ly gần |
| `HAL_POSE_MODEL_PATH` | `/root/local/models/rtmpose-m.onnx` | Đường dẫn model pose 2D (xem bên dưới) |

## Upload / thêm model

- **Upload weight khuôn mặt:** đặt mỗi file trong bucket tại `onnx_models/<file>`
  (khớp cột "Object trên bucket"). Không cần sửa code — đường dẫn tải tự suy ra
  theo basename.
- **Thêm model khuôn mặt mới:** thêm entry `basename → onnx_models/<file>` vào
  `_CDN_OBJECTS` trong `model_store.py`, và đưa đường dẫn của nó vào lời gọi
  `ensure_face_models(...)`.

## Liên quan

- **Model pose 2D** (`rtmpose-m.onnx`): dùng chung thư mục cache
  `/root/local/models` qua `HAL_POSE_MODEL_PATH`, nhưng hiện được **provision từ
  bên ngoài** (image thiết bị / OTA), HAL không tự tải.
- **Cloud perception-service** có cơ chế tải model riêng
  (`files.py` + `settings.cdn_base`), tài liệu tại
  `integrations/perception-service/docs/configuration.md` ("Model downloading").
