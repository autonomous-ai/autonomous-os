import logging
import threading
import time
from typing import cast, override

import cv2
import numpy as np
import numpy.typing as npt

from .base import IDevice
from .models import VideoCaptureDeviceInfo, VideoCaptureDeviceResponse


class VideoCaptureDeviceBase(
    IDevice[VideoCaptureDeviceInfo, VideoCaptureDeviceResponse]
):
    def __init__(
        self,
        device_info: VideoCaptureDeviceInfo,
        name: str | None = None,
    ):
        super().__init__(device_info, name)

        self._fps: int | None = device_info.fps
        self._max_width: int | None = device_info.max_width
        self._max_height: int | None = device_info.max_height
        self._rotate: float | None = device_info.rotate
        self._auto_exposure: str | None = device_info.auto_exposure
        self._exposure: int | None = device_info.exposure
        self._gain: int | None = device_info.gain
        self._brightness: int | None = device_info.brightness

    def capture(
        self, need_description: bool = False
    ) -> VideoCaptureDeviceResponse | None:
        """Capture the image (sync mode)"""
        raise NotImplementedError("capture method is not implemented")


class LocalVideoCaptureDevice(VideoCaptureDeviceBase):
    runable: bool = True

    def __init__(
        self,
        device_info: VideoCaptureDeviceInfo,
        name: str | None = None,
    ):
        super().__init__(device_info, name)

        self._last_response: VideoCaptureDeviceResponse | None = None

        self._thread: threading.Thread | None = None
        self._lock: threading.Lock = threading.Lock()
        self._stopped: threading.Event = threading.Event()

        # When > 0, capture runs at full FPS; otherwise throttles to save CPU
        self._active_consumers: int = 0
        self._consumers_lock: threading.Lock = threading.Lock()

        # Digital zoom factor (1.0 = no zoom). Applied in capture loop so all
        # downstream consumers (sensing, tracker, snapshot, stream) see the
        # same zoomed frame. Side effect: zoom > 1 narrows the effective FOV
        # for sensing/tracking. Settable via /camera/zoom route.
        self.zoom: float = 1.0

        # Negotiated capture mode — populated after the device accepts the
        # CAP_PROP_FRAME_WIDTH/HEIGHT/FPS request. None until the capture loop
        # has opened the device once.
        self.actual_width: int | None = None
        self.actual_height: int | None = None
        self.actual_fps: float | None = None

        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    @property
    def last_frame(self) -> npt.NDArray[np.uint8] | None:
        with self._lock:
            if self._last_response and self._last_response.frame is not None:
                return self._last_response.frame.copy()
            else:
                return None

    @property
    def last_frame_description(self) -> str | None:
        with self._lock:
            if self._last_response:
                return self._last_response.frame_description
            else:
                return None

    @property
    def last_response(self) -> VideoCaptureDeviceResponse | None:
        with self._lock:
            if self._last_response:
                return self._last_response.model_copy(deep=True)
            else:
                return None

    @last_response.setter
    def last_response(self, new_frame_info: VideoCaptureDeviceResponse | None):
        with self._lock:
            if new_frame_info:
                self._last_response = new_frame_info.model_copy(deep=True)
            else:
                self._last_response = None

    @override
    def capture(
        self, need_description: bool = False
    ) -> VideoCaptureDeviceResponse | None:
        if self._thread is None:
            msg = f"{self.__class__.__name__} has not started"
            self._logger.info(msg)
            raise RuntimeError(msg)

        return self.last_response

    @override
    def start(self) -> None:
        if self._thread is not None:
            self._logger.info(f"{self.__class__.__name__} has already started")
            return

        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._video_capture_loop,
            name=f"{self.__class__.__name__} video capture loop",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _try_open(device_id):
        """Try opening camera with V4L2 backend, fallback to default."""
        cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(device_id)
        return cap

    def _apply_camera_controls(self, video_capture):
        """Pin exposure (when configured) so auto-exposure can't throttle FPS.

        UVC auto-exposure stretches integration time in low light (~60ms),
        capping delivery at ~16fps regardless of resolution. A fixed exposure
        below the frame budget (e.g. 20ms < 33ms for 30fps) restores the full
        rate; the trade-off is a darker image in dim light, offset by gain /
        brightness, or a longer exposure (fewer fps).

        Opt-in: only runs when auto_exposure == "manual" (default "auto" leaves
        the camera untouched). V4L2/UVC CAP_PROP_AUTO_EXPOSURE: 1 = manual,
        3 = aperture-priority (auto). CAP_PROP_EXPOSURE is exposure_absolute in
        ×100µs units. Best-effort: unsupported controls are logged and skipped.
        """
        if (self._auto_exposure or "auto") != "manual":
            return
        try:
            video_capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            if self._exposure is not None:
                video_capture.set(cv2.CAP_PROP_EXPOSURE, float(self._exposure))
            if self._gain is not None:
                video_capture.set(cv2.CAP_PROP_GAIN, float(self._gain))
            if self._brightness is not None:
                video_capture.set(cv2.CAP_PROP_BRIGHTNESS, float(self._brightness))
            self._logger.info(
                "Camera exposure: manual (auto_exposure=%.0f exposure=%.0f gain=%.0f brightness=%.0f)",
                video_capture.get(cv2.CAP_PROP_AUTO_EXPOSURE),
                video_capture.get(cv2.CAP_PROP_EXPOSURE),
                video_capture.get(cv2.CAP_PROP_GAIN),
                video_capture.get(cv2.CAP_PROP_BRIGHTNESS),
            )
        except Exception:
            self._logger.exception(
                "Camera exposure control failed — continuing with camera defaults"
            )

    def _video_capture_loop(self):

        device_id = self.device_info.device_id

        if isinstance(device_id, str) and device_id.isdigit():
            device_id = int(device_id)

        video_capture = self._try_open(device_id)

        # Fallback: try /dev/cam symlink (udev rule), then scan index 0-5
        if not video_capture.isOpened():
            import os
            fallbacks = ["/dev/cam"] + [i for i in range(6) if i != device_id]
            for fb in fallbacks:
                if isinstance(fb, str) and not os.path.exists(fb):
                    continue
                self._logger.info("Camera fallback: trying %s", fb)
                video_capture = self._try_open(fb)
                if video_capture.isOpened():
                    self._logger.info("Camera fallback success: %s", fb)
                    break

        if not video_capture.isOpened():
            raise ValueError(
                f"Failed to open video capture device: {self.device_info.device_id}"
            )

        # Force MJPEG format — some USB webcams (e.g. Generalplus) fail read()
        # with the default YUYV format on Pi 5 but work fine with MJPEG.
        video_capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))

        # Request the configured resolution from the device. Without this the
        # cam delivers its default mode (often 640x480) regardless of
        # max_width/max_height. The device snaps to its nearest supported mode
        # — we read back the actual values below.
        if self._max_width:
            video_capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._max_width)
        if self._max_height:
            video_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._max_height)

        # Pin exposure (if configured) so auto-exposure doesn't throttle FPS.
        self._apply_camera_controls(video_capture)

        w = int(video_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(video_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        device_fps = video_capture.get(cv2.CAP_PROP_FPS)
        self.actual_width = w
        self.actual_height = h
        self.actual_fps = device_fps if device_fps and device_fps > 0 else None
        self._logger.info(
            "Camera negotiated mode: %dx%d @ %.1f fps (requested %sx%s)",
            w, h, device_fps, self._max_width, self._max_height,
        )

        new_w = min(w, self._max_width) if self._max_width else w
        new_h = min(h, self._max_height) if self._max_height else h

        size_ratio = min(new_w / w, new_h / h)

        last_time_frame = time.time()
        skip_time = (
            1 / self._fps if self._fps is not None and self._fps < device_fps else 0
        )

        # Idle capture interval — only grab a frame every 2s when no streaming clients
        idle_interval = 2.0

        self._logger.info("Starting video capture device loop")
        try:
            while not self._stopped.is_set():
                # Throttle when no active consumers — sleep BEFORE read to avoid
                # burning CPU on blocking video_capture.read() at device FPS
                with self._consumers_lock:
                    has_consumers = self._active_consumers > 0
                if not has_consumers:
                    elapsed = time.time() - last_time_frame
                    if elapsed < idle_interval:
                        self._stopped.wait(min(idle_interval - elapsed, 0.5))
                        continue
                    # Flush stale frames from device buffer after idle sleep
                    video_capture.grab()
                    video_capture.grab()

                ret, frame = video_capture.read()

                if not ret:
                    # USB cameras (e.g. HD USB Camera 32e4:9230 on OrangePi) hit
                    # autosuspend after ~2s idle; the wakeup outlasts a single
                    # 1s retry. Instead of exiting the loop forever, mirror what
                    # the /camera/disable + /camera/enable workaround does:
                    # release the handle and reopen. Same recovery path V4L2
                    # would do under any transient device-error condition.
                    self._logger.warning("Camera read() failed, retrying in 1s...")
                    time.sleep(1)
                    ret, frame = video_capture.read()
                    if not ret:
                        self._logger.warning("Camera read still failing — reopening device")
                        try:
                            video_capture.release()
                        except Exception:
                            self._logger.exception("Camera release failed during recovery")
                        video_capture = self._try_open(device_id)
                        if not video_capture.isOpened():
                            self._logger.error("Camera reopen failed, exiting loop")
                            break
                        video_capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                        # Re-apply resolution + exposure: a fresh open resets the
                        # device to its defaults, which would silently drop manual
                        # exposure (re-introducing the FPS throttle) and snap back
                        # to the default capture mode.
                        if self._max_width:
                            video_capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._max_width)
                        if self._max_height:
                            video_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._max_height)
                        self._apply_camera_controls(video_capture)
                        self._logger.info("Camera reopened, resuming loop")
                        continue

                frame_ts = time.time()

                if frame_ts - last_time_frame < skip_time:
                    continue
                else:
                    last_time_frame = frame_ts

                if size_ratio < 1.0:
                    frame = cv2.resize(frame, None, fx=size_ratio, fy=size_ratio)

                if self._rotate is not None:
                    if self._rotate == 180.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_180)
                    elif self._rotate == 90.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
                    elif self._rotate == -90.0:
                        frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    else:
                        h, w = frame.shape[:2]
                        center = (w // 2, h // 2)
                        M = cv2.getRotationMatrix2D(center, self._rotate, 1.0)
                        frame = cv2.warpAffine(frame, M, (w, h))

                z = self.zoom
                if z > 1.0:
                    fh, fw = frame.shape[:2]
                    cw, ch = int(fw / z), int(fh / z)
                    x0 = (fw - cw) // 2
                    y0 = (fh - ch) // 2
                    frame = cv2.resize(
                        frame[y0:y0 + ch, x0:x0 + cw],
                        (fw, fh),
                        interpolation=cv2.INTER_LINEAR,
                    )

                frame = cast(npt.NDArray[np.uint8], frame)
                response = VideoCaptureDeviceResponse(frame=frame)

                for callback in self.callbacks:
                    callback(self.device_info, response)

                self.last_response = response
        finally:
            video_capture.release()

    def acquire_consumer(self):
        """Register an active consumer (e.g. MJPEG stream) for full-FPS capture."""
        with self._consumers_lock:
            self._active_consumers += 1

    def release_consumer(self):
        """Unregister an active consumer — throttles capture when none remain."""
        with self._consumers_lock:
            self._active_consumers = max(0, self._active_consumers - 1)

    @override
    def stop(self):
        super().stop()
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
