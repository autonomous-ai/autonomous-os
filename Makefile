# AI Lamp — Makefile
# 4 components: Go (lamp + bootstrap + buddy), Python (lelamp), TypeScript (web)

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Directories
LAMP_DIR       := lamp
LELAMP_DIR     := lelamp
BUDDY_DIR      := claude-desktop-buddy
TWITCH_DIR     := chat-hooks/twitch-chat-hook
AUTONOMOUS_DIR := chat-hooks/autonomous-chat-hook
WEB_DIR        := $(LAMP_DIR)/web

# Go build
MODULE         := go-lamp.autonomous.ai
LDFLAGS_LAMP   := -X $(MODULE)/server/config.LampVersion=$(VERSION)
LDFLAGS_BOOT   := -X $(MODULE)/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_IRC    := -X main.Version=$(VERSION)
LDFLAGS_AUTONOMOUS_CHAT := -X main.Version=$(VERSION)

# LeLamp
LELAMP_PORT    := 5001

# ============================================================================
# Lamp (Go) — build | generate | lint | test
# ============================================================================

.PHONY: lamp-build lamp-build-bootstrap lamp-generate lamp-lint lamp-test

lamp-build:
	cd $(LAMP_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_LAMP)" -o lamp-server ./cmd/lamp


lamp-build-bootstrap:
	cd $(LAMP_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap


lamp-generate:
	cd $(LAMP_DIR) && GOFLAGS=-mod=mod go generate ./...

lamp-lint:
	cd $(LAMP_DIR) && golangci-lint run

lamp-test:
	cd $(LAMP_DIR) && go test ./...

# ============================================================================
# LeLamp (Python) — dev | run | test
# ============================================================================

.PHONY: lelamp lelamp-dev lelamp-run lelamp-test lelamp-clean

lelamp: lelamp-dev

lelamp-dev:
	cd $(LELAMP_DIR) && PYTHONPATH=.. LELAMP_MODE=developer .venv/bin/uvicorn lelamp.server:app --host 0.0.0.0 --port $(LELAMP_PORT) --reload

lelamp-run:
	cd $(LELAMP_DIR) && PYTHONPATH=.. .venv/bin/python -m lelamp.server

lelamp-test:
	cd $(LELAMP_DIR) && .venv/bin/python -m pytest test/

lelamp-clean:
	rm -rf $(LELAMP_DIR)/.venv $(LELAMP_DIR)/__pycache__

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

.PHONY: upload-lamp upload-bootstrap upload-lelamp upload-claude-desktop-buddy upload-lamp-buddy upload-web upload-skills upload-hooks upload-setup upload-setup-ap upload-openclaw upload-twitch-irc upload-autonomous-chat upload-all

upload-lamp:
	bash scripts/upload-lamp.sh

upload-bootstrap:
	bash scripts/upload-bootstrap.sh

upload-lelamp:
	bash scripts/upload-lelamp.sh

upload-claude-desktop-buddy:
	bash scripts/upload-claude-desktop-buddy.sh

upload-lamp-buddy:
	bash scripts/upload-lamp-buddy.sh

upload-web:
	bash scripts/upload-web.sh

upload-skills:
	bash scripts/upload-skills.sh

upload-hooks:
	bash scripts/upload-hooks.sh

upload-setup:
	bash scripts/upload-setup.sh

upload-setup-ap:
	bash scripts/upload-setup-ap.sh

upload-twitch-irc:
	bash scripts/upload-twitch-irc.sh

upload-autonomous-chat:
	bash scripts/upload-autonomous-chat.sh

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
	bash scripts/upload-openclaw.sh "$(OPENCLAW_VERSION_ARG)"

# upload-openclaw is intentionally NOT in upload-all — bumping the OpenClaw
# version is an explicit decision, not a side effect of pushing other artifacts.
upload-all: upload-lamp upload-bootstrap upload-lelamp upload-claude-desktop-buddy upload-web upload-skills upload-hooks

# ============================================================================
# Release tagging — GPL v3 §6 compliance
# ============================================================================
# Annotated git tag with current OTA metadata.json embedded as message, then
# pushed. Lets buyers map "lamp-server --version" on the board back to a
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
	bash scripts/tag-release.sh "$(TAG_VERSION_ARG)"

# ============================================================================
# Clean
# ============================================================================

.PHONY: clean

clean:
	rm -f $(LAMP_DIR)/lamp-server $(LAMP_DIR)/bootstrap-server
	rm -f $(BUDDY_DIR)/buddy-plugin $(BUDDY_DIR)/claude-desktop-buddy
	rm -f $(TWITCH_DIR)/twitch-irc
	rm -rf $(LELAMP_DIR)/.venv $(LELAMP_DIR)/__pycache__
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules
