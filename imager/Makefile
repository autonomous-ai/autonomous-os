# =============================================================================
# imager Makefile
# =============================================================================
# Targets:
#   make build                 — OrangePi (Armbian) golden image (default, skeleton)
#   make TARGET=rpi build      — Raspberry Pi 5 (RPi OS) golden image
#   make sd-card-list          — list disks for flashing
#   make sd-card-test DISK=N   — test SD card with 100 MB dd
#   make sd-card-flash DISK=N  — flash the default-target image to /dev/diskN
#   make sd-card-flash TARGET=rpi DISK=N — flash RPi image instead
# =============================================================================

TARGET ?= opi
BUILD_SCRIPT_rpi := build.sh
BUILD_SCRIPT_opi := build-orangepi.sh
BUILD_SCRIPT := $(BUILD_SCRIPT_$(TARGET))

ifndef BUILD_SCRIPT
$(error Unknown TARGET=$(TARGET). Supported: rpi, opi)
endif

# Image filename per target so RPi + OPi builds don't clobber each other.
# OPi build produces .img.xz (compressed); RPi build produces raw .img.
IMG_FILE_rpi := golden.img
IMG_FILE_opi := golden-opi.img.xz
IMG_FILE := $(IMG_FILE_$(TARGET))

build:
	# Build the Docker build environment (shared for both targets)
	docker build -t pi-builder .

	# Run it — needs --privileged for losetup/mount.
	# BUILD_SCRIPT controls which builder runs inside the container.
	docker run --rm \
	--privileged \
	-v ./output:/output \
	-v ./input:/input \
	-e BUILD_SCRIPT=$(BUILD_SCRIPT) \
	pi-builder /work/$(BUILD_SCRIPT)

sd-card-list:
	diskutil list

sd-card-test:
ifndef DISK
	$(error DISK is required. Usage: make sd-card-test DISK=19)
endif
	@echo "==> Testing SD card write (100 MB of zeros)..."
	sudo launchctl unload /System/Library/LaunchDaemons/com.apple.diskarbitrationd.plist 2>/dev/null || true
	diskutil unmountDisk /dev/disk$(DISK) 2>/dev/null || true
	sleep 1
	sudo dd if=/dev/zero of=/dev/rdisk$(DISK) bs=1m count=1000 status=progress; \
	DD_EXIT=$$?; \
	sudo launchctl load /System/Library/LaunchDaemons/com.apple.diskarbitrationd.plist 2>/dev/null || true; \
	if [ $$DD_EXIT -eq 0 ]; then echo "==> SD card OK — hardware is fine"; else echo "==> SD card FAILED — hardware issue (bad card or reader)"; fi; \
	exit $$DD_EXIT

sd-card-flash:
ifndef DISK
	$(error DISK is required. Usage: make sd-card-flash DISK=4)
endif
	@echo "==> Flashing ./output/$(IMG_FILE) to /dev/disk$(DISK)..."
	diskutil unmountDisk force /dev/disk$(DISK)
	@# Compressed (.xz) images decompress on the fly; raw .img dd directly.
	@if [ "$(suffix $(IMG_FILE))" = ".xz" ]; then \
		xz -dc ./output/$(IMG_FILE) | sudo dd of=/dev/disk$(DISK) bs=64m conv=fsync status=progress; \
	else \
		sudo dd if=./output/$(IMG_FILE) of=/dev/disk$(DISK) bs=64m conv=fsync status=progress; \
	fi
	sync
	diskutil eject /dev/disk$(DISK) 2>/dev/null || true
	@echo "==> Flash complete"

# ── upload — push built image to GCS, version-stamped ──────────────────────
# Versioned filename: golden-<target>-<UTC-timestamp>-<git-sha>.img.xz
# Bucket + path mirror the OTA artifact layout (s3-autonomous-upgrade-3/lumi/).
GCS_BUCKET := s3-autonomous-upgrade-3
GCS_PATH   := lumi/imager/output
VERSION    := $(shell date -u +%Y%m%d-%H%M%S)-$(shell git -C .. rev-parse --short HEAD 2>/dev/null || echo nogit)
TARGET_PREFIX_rpi := golden-rpi
TARGET_PREFIX_opi := golden-opi
TARGET_PREFIX := $(TARGET_PREFIX_$(TARGET))
VERSIONED_IMG := $(TARGET_PREFIX)-$(VERSION)$(if $(filter %.xz,$(IMG_FILE)),.img.xz,.img)

upload:
	@[ -f output/$(IMG_FILE) ] || { echo "ERROR: output/$(IMG_FILE) not found. Run 'make build' first."; exit 1; }
	@command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud not installed. brew install --cask gcloud-cli"; exit 1; }
	@echo "==> Uploading output/$(IMG_FILE) → gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)"
	gcloud storage cp output/$(IMG_FILE) gs://$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)
	@echo "==> Public URL:"
	@echo "    https://storage.googleapis.com/$(GCS_BUCKET)/$(GCS_PATH)/$(VERSIONED_IMG)"
