# Agent runtime của Lamp

Lamp mặc định dùng **Hermes**, khai báo bằng `gateway.default: hermes` trong
[`ROBOT.md`](../../ROBOT.md).

Thứ tự ưu tiên chọn runtime:

1. `agent_runtime` đã lưu trong `config.json` của thiết bị.
2. `/root/config/f_r_default_agent`, được bake vào image bằng `DEFAULT_AGENT`.
3. `gateway.default` trong `ROBOT.md` của Lamp.

Đổi khai báo không tự chuyển runtime trên máy đã cấu hình. Mặc định riêng của
image cũng được giữ sau factory reset và ưu tiên hơn khai báo này.
