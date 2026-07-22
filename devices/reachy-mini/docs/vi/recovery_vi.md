# Reachy Mini Khôi Phục & Xử Lý Sự Cố

Tài liệu này hướng dẫn khôi phục Pollen OS, truy cập SSH, và tác động của
`setup.sh` (Autonomous) lên cấu hình mạng của Pollen OS gốc.

## Truy Cập SSH

Thông tin đăng nhập mặc định trên Pollen OS gốc:

```bash
ssh pollen@reachy-mini.local   # mật khẩu: root
```

Nếu mDNS không khả dụng, dùng IP trực tiếp: `ssh pollen@<IP>`.

Sau khi chạy `setup.sh` của Autonomous, hostname đổi thành
`reachy-mini-<suffix>` (suffix lấy từ serial number của Pi):

```bash
ssh pollen@reachy-mini-abcd.local   # thay suffix thật
```

Tham khảo: [Pollen troubleshooting](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/troubleshooting)

## Các Phương Pháp Khôi Phục

Sắp xếp từ nhẹ đến nặng. Thử từng cấp trước khi leo thang.

### Cấp A: Khởi Động Lại

Nhấn OFF, chờ 5 giây, nhấn ON. Sửa lỗi daemon treo tạm thời.

### Cấp B: Reset Qua Bluetooth (Không Cần SSH / WiFi)

Reachy Mini có dịch vụ BLE GATT để khôi phục ngoài băng. Ba cách kết nối:

1. **Reachy Mini Control App** (desktop) — "First time WiFi setup" → "Try the
   Bluetooth Console"
2. **Web Bluetooth Dashboard** (Chrome/Edge/Opera) — không cần cài đặt
3. **nRF Connect** (điện thoại) — BLE client tổng quát, cho người dùng nâng cao

**PIN**: 5 số cuối serial number của robot, gửi trước mỗi lệnh.

| Lệnh BLE | Tác dụng |
|-----------|----------|
| `STATUS` | Kiểm tra trạng thái robot |
| `CMD_HOTSPOT` | Reset WiFi hotspot về mặc định (`reachy-mini-ap` / `reachy-mini`) |
| `CMD_RESTART_DAEMON` | Khởi động lại dịch vụ Pollen daemon |
| `CMD_SOFTWARE_RESET` | Reset phần mềm toàn bộ (~5 phút để khởi động lại) |

Tham khảo: [Hướng dẫn BLE reset (Seeed Studio)](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)

### Cấp C: Khôi Phục venv Qua SSH (Daemon Restart Loop)

Nếu Pollen daemon rơi vào vòng lặp restart vô hạn (thường xảy ra sau mất điện
khi đang cài app), virtual environment có thể bị hỏng. Robot có bản backup sạch
tại `/restore/venvs`:

```bash
ssh pollen@reachy-mini.local   # mật khẩu: root
sudo systemctl stop reachy-mini-daemon   # hoặc tên service tương ứng
sudo mv /venvs /venvs.broken
sudo cp -a /restore/venvs /venvs
sudo reboot
```

Tham khảo: [pollen-robotics/reachy_mini#599](https://github.com/pollen-robotics/reachy_mini/issues/599)

### Cấp D: Flash Lại eMMC (Factory Reset)

Phương án cuối — xóa sạch mọi thứ (dữ liệu người dùng, cấu hình WiFi, app đã
cài) và khôi phục Pollen OS về trạng thái nhà máy. Chỉ dùng khi tất cả cách
khác thất bại.

**Lưu ý phần cứng**: Reachy Mini Wireless dùng Raspberry Pi CM4 với 16 GB eMMC
trên bo (không có khe SD card). Flash cần chế độ USB boot.

#### Cần Chuẩn Bị

| Vật phẩm | Nguồn |
|----------|-------|
| Image OS (`.img.xz`) + file `.bmap` | [reachy-mini-os releases](https://github.com/pollen-robotics/reachy-mini-os/releases) |
| Tool `rpiboot` | [raspberrypi/usbboot](https://github.com/raspberrypi/usbboot) |
| `bmaptool` (Linux/macOS) hoặc Raspberry Pi Imager (Windows) | Package manager hoặc [rpi-imager](https://www.raspberrypi.com/software/) |
| Cáp USB | Cắm vào cổng USB2 trên bo mạch đầu |

#### Các Bước

1. **Tắt** robot hoàn toàn.
2. **Gạt công tắc** trên bo mạch đầu sang vị trí **DOWNLOAD (SW1)**.
3. **Chạy rpiboot** trên máy tính:
   - Linux/macOS: `sudo ./rpiboot -d mass-storage-gadget64`
   - Windows: RPiBoot GUI → chọn `rpiboot-CM4-CM5 - Mass storage Gadget`
4. **Cắm cáp USB** vào cổng USB2 trên bo mạch đầu.
5. **Bật** robot. rpiboot sẽ expose eMMC nội bộ dưới dạng USB storage.
6. **Unmount** các phân vùng tự động mount:
   - macOS: `diskutil unmountDisk /dev/diskX`
   - Linux: `sudo umount /media/$USER/bootfs /media/$USER/rootfs`
7. **Flash** image:
   - macOS: `sudo bmaptool copy <image>.xz --bmap <image>.bmap /dev/rdiskX`
   - Linux: `sudo bmaptool copy <image>.xz --bmap <image>.bmap /dev/sdX`
   - Windows: Raspberry Pi Imager → thiết bị "Raspberry Pi 4" → "Use custom" → chọn image
8. **Khôi phục boot bình thường**: tắt nguồn, gạt switch về DEBUG, rút USB, bật lại.
9. **Kiểm tra**: kết nối WiFi `reachy-mini-ap` (mật khẩu `reachy-mini`), rồi:
   ```bash
   ssh pollen@reachy-mini.local   # mật khẩu: root
   reachyminios_check             # phải xuất "Image validation PASSED"
   ```

**Lưu ý macOS Apple Silicon**: có lỗi đã biết với rpiboot trên Mac M-series —
xem [pollen-robotics/reachy_mini#734](https://github.com/pollen-robotics/reachy_mini/issues/734)
để biết cách xử lý.

Tham khảo: [Hướng dẫn reflash chính thức](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)

## Tác Động Của setup.sh Lên Mạng Pollen OS

Chạy `DEVICE_TYPE=reachy-mini setup.sh` sẽ thay đổi network stack để bật captive
portal AP-mode và chuyển đổi STA-mode của Autonomous. Chi tiết:

| File cấu hình | Hành động | Khôi phục được? |
|----------------|-----------|-----------------|
| `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` | **Thay thế** (config cũ backup thành `.conf.bak`) | Có — khôi phục từ `.bak` |
| `/etc/hostapd/hostapd.conf` | **Thay thế** | Flash lại để khôi phục bản gốc |
| `/etc/dnsmasq.d/99-reachy-mini.conf` | **Thêm mới** (file drop-in) | Có — xóa file là xong |
| `/etc/dnsmasq.conf` | **Sửa** (comment các dòng `interface=wlan0` xung đột) | Có — bỏ comment |
| `/etc/dhcpcd.conf` | **Sửa** (xóa block `interface wlan0` cũ, thêm block AP mới) | Một phần — block gốc không được backup |
| `wpa_supplicant.service` (toàn cục) | **Bị mask** (chỉ dùng instance `wpa_supplicant@wlan0`) | Có — `systemctl unmask wpa_supplicant` |

### Đánh Giá Rủi Ro

- **Nếu Pollen OS dùng `dhcpcd` + `wpa_supplicant`** (stack Raspberry Pi OS
  classic): setup.sh được thiết kế cho stack này. WiFi sẽ hoạt động, AP mode sẽ
  hoạt động, và các script `device-sta-mode` / `device-ap-mode` xử lý chuyển
  đổi. Rủi ro thấp.

- **Nếu Pollen OS dùng `NetworkManager`** (mặc định Bookworm mới): setup.sh sẽ
  dừng và vô hiệu hóa NetworkManager. Điều này phá vỡ quản lý WiFi của Pollen.
  Robot có thể mất kết nối mạng cho đến khi hoàn tất Autonomous setup flow hoặc
  bật lại NetworkManager thủ công. **Kiểm tra stack nào Pollen dùng trước khi
  chạy setup.sh trên thiết bị thật.**

### Cách Kiểm Tra (Trước Khi Chạy setup.sh)

```bash
ssh pollen@reachy-mini.local
# Kiểm tra NetworkManager có đang chạy không
systemctl is-active NetworkManager
# Kiểm tra dhcpcd có đang chạy không
systemctl is-active dhcpcd
# Kiểm tra cái nào quản lý wlan0
nmcli device status 2>/dev/null || echo "Không có NetworkManager"
```

### Khôi Phục WiFi Sau Khi Hỏng

1. **BLE hotspot reset**: Gửi `CMD_HOTSPOT` qua Bluetooth (xem Cấp B ở trên).
   Reset WiFi về AP mode gốc (`reachy-mini-ap` / `reachy-mini`).
2. **Ethernet**: Cắm USB-to-Ethernet adapter, SSH qua kết nối dây, sửa config
   thủ công.
3. **Flash lại**: Cấp D ở trên khôi phục mọi thứ về trạng thái nhà máy.

## Tài Liệu Tham Khảo

- [Reachy Mini OS repo (dựa trên pi-gen)](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini OS releases](https://github.com/pollen-robotics/reachy-mini-os/releases)
- [Reachy Mini SDK & docs](https://github.com/pollen-robotics/reachy_mini)
- [Thông số phần cứng (Hugging Face)](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Hướng dẫn reflash (Hugging Face)](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [Hướng dẫn BLE reset (Seeed Studio)](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Fix daemon restart-loop #599](https://github.com/pollen-robotics/reachy_mini/issues/599)
- [Lỗi rpiboot Apple Silicon #734](https://github.com/pollen-robotics/reachy_mini/issues/734)
- [raspberrypi/usbboot](https://github.com/raspberrypi/usbboot)
