# Autonomous — Makefile
# 4 components: Go (os + bootstrap + buddy), Python (hal), TypeScript (web)

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Directories
OS_DIR         := os/services
HAL_DIR        := os/hal
BUDDY_DIR      := companions/claude-desktop-buddy
TWITCH_DIR     := chat-hooks/twitch-chat-hook
AUTONOMOUS_DIR := chat-hooks/autonomous-chat-hook
WEB_DIR        := $(OS_DIR)/web

# Go build
MODULE         := go.autonomous.ai/os
# os-server version injected into config.OSVersion (internal build var).
LDFLAGS_OS     := -X $(MODULE)/server/config.OSVersion=$(VERSION)
LDFLAGS_BOOT   := -X $(MODULE)/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_IRC    := -X main.Version=$(VERSION)
LDFLAGS_AUTONOMOUS_CHAT := -X main.Version=$(VERSION)

# HAL
HAL_PORT       := 5001

# ============================================================================
# OS services (Go) — build | generate | lint | test
# ============================================================================

.PHONY: os-build os-build-bootstrap os-generate os-lint os-test

os-build:
	cd $(OS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_OS)" -o os-server ./cmd/os-server


os-build-bootstrap:
	cd $(OS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap


os-generate:
	cd $(OS_DIR) && GOFLAGS=-mod=mod go generate ./...

os-lint:
	cd $(OS_DIR) && golangci-lint run

os-test:
	cd $(OS_DIR) && go test ./...

# ============================================================================
# HAL (Python) — dev | run | test
# ============================================================================

.PHONY: hal hal-dev hal-run hal-lint hal-test hal-clean

hal: hal-dev

hal-dev:
	cd $(HAL_DIR) && PYTHONPATH=.. HAL_MODE=developer .venv/bin/uvicorn hal.server:app --host 0.0.0.0 --port $(HAL_PORT) --reload

hal-run:
	cd $(HAL_DIR) && PYTHONPATH=.. .venv/bin/python -m hal.server

# Catch refactor-leftover bugs (broken local imports + undefined names) that
# py_compile/tests miss off-hardware. Needs the `dev` extra (pyflakes).
hal-lint:
	cd $(HAL_DIR) && .venv/bin/python scripts/lint.py

hal-test:
	cd $(HAL_DIR) && .venv/bin/python -m pytest test/

hal-clean:
	rm -rf $(HAL_DIR)/.venv $(HAL_DIR)/__pycache__

# ============================================================================
# Web (React/Vite/Tailwind) — install | dev | build
# ============================================================================

.PHONY: web web-install web-dev web-build

web: web-dev

web-install:
	cd $(WEB_DIR) && npm install

web-dev:
	cd $(WEB_DIR) && npm run dev

web-build:
	cd $(WEB_DIR) && npm run build

# ============================================================================
# Claude Desktop Buddy (Go) — build
# ============================================================================

.PHONY: buddy-build

buddy-build:
	cd $(BUDDY_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w" -o buddy-plugin .

# ============================================================================
# Twitch chat hook (Go) — build IRC fallback reader
# ============================================================================

.PHONY: twitch-build-irc

twitch-build-irc:
	cd $(TWITCH_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_IRC)" -o twitch-irc ./cmd/irc

# ============================================================================
# Autonomous chat hook (Go) — MQTT subscriber bridging BE web chat → lamp
# ============================================================================

.PHONY: autonomous-build-chat

autonomous-build-chat:
	cd $(AUTONOMOUS_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_AUTONOMOUS_CHAT)" -o autonomous-chat ./cmd/mqtt

# ============================================================================
# Upload (OTA to GCS) — unified format: make upload-<component>
# ============================================================================

.PHONY: upload-os-server upload-bootstrap upload-hal upload-claude-desktop-buddy upload-autonomous-buddy upload-web upload-skills upload-hooks upload-setup upload-setup-ap upload-openclaw upload-device upload-twitch-irc upload-autonomous-chat upload-all promote-os-server promote-bootstrap promote-web promote-hal promote-claude-desktop-buddy promote-openclaw promote-device

upload-os-server:
	bash scripts/release/upload-os-server.sh

upload-bootstrap:
	bash scripts/release/upload-bootstrap.sh

upload-hal:
	bash scripts/release/upload-hal.sh

upload-claude-desktop-buddy:
	bash scripts/release/upload-claude-desktop-buddy.sh

upload-autonomous-buddy:
	bash scripts/release/upload-autonomous-buddy.sh

upload-web:
	bash scripts/release/upload-web.sh

upload-skills:
	bash scripts/release/upload-skills.sh

upload-hooks:
	bash scripts/release/upload-hooks.sh

upload-setup:
	bash scripts/release/upload-setup.sh

upload-setup-ap:
	bash scripts/release/upload-setup-ap.sh

upload-twitch-irc:
	bash scripts/release/upload-twitch-irc.sh

upload-autonomous-chat:
	bash scripts/release/upload-autonomous-chat.sh

# Allow positional version: `make upload-openclaw 2026.5.2`. The eval
# stub below creates a no-op rule for the version arg so make doesn't
# try to build it as a target ("no rule to make target '2026.5.2'").
# Scoped to when upload-openclaw is the first goal so this doesn't
# silence missing-target errors elsewhere.
ifeq (upload-openclaw,$(firstword $(MAKECMDGOALS)))
  OPENCLAW_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(OPENCLAW_VERSION_ARG),)
    $(eval $(OPENCLAW_VERSION_ARG):;@:)
  endif
endif

upload-openclaw:
	@if [ -z "$(OPENCLAW_VERSION_ARG)" ]; then echo "Usage: make upload-openclaw <version>" >&2; exit 1; fi
	bash scripts/release/upload-openclaw.sh "$(OPENCLAW_VERSION_ARG)"

# Allow positional device type: `make upload-device lamp` (publishes ONE device
# profile). Per-device by design — each type versions + publishes independently,
# so it's NOT in upload-all (publishing lamp must not touch intern).
ifeq (upload-device,$(firstword $(MAKECMDGOALS)))
  DEVICE_TYPE_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(DEVICE_TYPE_ARG),)
    $(eval $(DEVICE_TYPE_ARG):;@:)
  endif
endif

upload-device:
	@if [ -z "$(DEVICE_TYPE_ARG)" ]; then echo "Usage: make upload-device <type>   (e.g. lamp, intern, unitree-go2w)" >&2; exit 1; fi
	bash scripts/release/upload-device.sh "$(DEVICE_TYPE_ARG)"

# Promote the auto-rollout floor (min_version) so bootstrap pushes a build to the
# fleet. One target per component (mirrors upload-*) so the names never collide
# with real targets like `hal`/`web`. Optional V=<version> pins an explicit floor
# (default: the entry's current version).
#   make promote-hal                # min_version = hal.version
#   make promote-os-server V=1.4.0  # pin floor explicitly
#   make promote-device DT=lamp     # devices.lamp profile
promote-os-server promote-bootstrap promote-web promote-hal promote-claude-desktop-buddy promote-openclaw:
	bash scripts/release/promote-ota.sh $(patsubst promote-%,%,$@) $(V)

promote-device:
	@if [ -z "$(DT)" ]; then echo "Usage: make promote-device DT=<type> [V=<min_version>]" >&2; exit 1; fi
	bash scripts/release/promote-ota.sh device "$(DT)" $(V)

# upload-openclaw is intentionally NOT in upload-all — bumping the OpenClaw
# version is an explicit decision, not a side effect of pushing other artifacts.
upload-all: upload-os-server upload-bootstrap upload-hal upload-claude-desktop-buddy upload-web upload-skills upload-hooks

# ============================================================================
# Release tagging — GPL v3 §6 compliance
# ============================================================================
# Annotated git tag with current OTA metadata.json embedded as message, then
# pushed. Lets buyers map "os-server --version" on the board back to a
# specific commit + component version set in the public repo.
#
# Usage: make tag-release v0.0.8       # after all upload-* targets succeed

ifeq (tag-release,$(firstword $(MAKECMDGOALS)))
  TAG_VERSION_ARG := $(word 2,$(MAKECMDGOALS))
  ifneq ($(TAG_VERSION_ARG),)
    $(eval $(TAG_VERSION_ARG):;@:)
  endif
endif

.PHONY: tag-release

tag-release:
	bash scripts/release/tag-release.sh "$(TAG_VERSION_ARG)"

# ============================================================================
# Clean
# ============================================================================

.PHONY: clean

clean:
	rm -f $(OS_DIR)/os-server $(OS_DIR)/bootstrap-server
	rm -f $(BUDDY_DIR)/buddy-plugin $(BUDDY_DIR)/claude-desktop-buddy
	rm -f $(TWITCH_DIR)/twitch-irc
	rm -rf $(HAL_DIR)/.venv $(HAL_DIR)/__pycache__
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules
