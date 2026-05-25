# AI Lamp (Lumi) — Makefile
# 4 components: Go (lumi + bootstrap + buddy), Python (lelamp), TypeScript (web)

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Directories
LUMI_DIR       := lumi
LELAMP_DIR     := lelamp
BUDDY_DIR      := claude-desktop-buddy
TWITCH_DIR     := twitch-chat-hook
WEB_DIR        := $(LUMI_DIR)/web

# Go build
MODULE         := go-lamp.autonomous.ai
LDFLAGS_LAMP   := -X $(MODULE)/server/config.LumiVersion=$(VERSION)
LDFLAGS_BOOT   := -X $(MODULE)/bootstrap/config.BootstrapVersion=$(VERSION)
LDFLAGS_IRC    := -X main.Version=$(VERSION)

# LeLamp
LELAMP_PORT    := 5001

# ============================================================================
# Lumi (Go) — build | generate | lint | test
# ============================================================================

.PHONY: lumi-build lumi-build-bootstrap lumi-generate lumi-lint lumi-test

lumi-build:
	cd $(LUMI_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_LAMP)" -o lumi-server ./cmd/lamp


lumi-build-bootstrap:
	cd $(LUMI_DIR) && GOOS=linux GOARCH=arm64 go build -ldflags "-s -w $(LDFLAGS_BOOT)" -o bootstrap-server ./cmd/bootstrap


lumi-generate:
	cd $(LUMI_DIR) && GOFLAGS=-mod=mod go generate ./...

lumi-lint:
	cd $(LUMI_DIR) && golangci-lint run

lumi-test:
	cd $(LUMI_DIR) && go test ./...

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
# Upload (OTA to GCS) — unified format: make upload-<component>
# ============================================================================

.PHONY: upload-lumi upload-bootstrap upload-lelamp upload-claude-desktop-buddy upload-lumi-buddy upload-web upload-skills upload-hooks upload-setup upload-setup-ap upload-openclaw upload-twitch-irc upload-all

upload-lumi:
	bash scripts/upload-lumi.sh

upload-bootstrap:
	bash scripts/upload-bootstrap.sh

upload-lelamp:
	bash scripts/upload-lelamp.sh

upload-claude-desktop-buddy:
	bash scripts/upload-claude-desktop-buddy.sh

upload-lumi-buddy:
	bash scripts/upload-lumi-buddy.sh

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
upload-all: upload-lumi upload-bootstrap upload-lelamp upload-claude-desktop-buddy upload-web upload-skills upload-hooks

# ============================================================================
# Clean
# ============================================================================

.PHONY: clean

clean:
	rm -f $(LUMI_DIR)/lumi-server $(LUMI_DIR)/bootstrap-server
	rm -f $(BUDDY_DIR)/buddy-plugin
	rm -f $(TWITCH_DIR)/twitch-irc
	rm -rf $(LELAMP_DIR)/.venv $(LELAMP_DIR)/__pycache__
	rm -rf $(WEB_DIR)/dist $(WEB_DIR)/node_modules
