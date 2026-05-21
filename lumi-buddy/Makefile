# Lumi Buddy — macOS build/run shortcuts.
# Run from this directory: `make <target>`. `make help` for the list.

APP_NAME   := LumiBuddy
BUNDLE_ID  := network.autonomous.ai.lumi-buddy
VERSION    := 0.0.1

MACOS_DIR  := macos
DIST_DIR   := dist
INFO_PLIST := $(MACOS_DIR)/Info.plist
BUILD_DIR  := $(MACOS_DIR)/.build
RELEASE_BIN := $(BUILD_DIR)/release/$(APP_NAME)
APP_BUNDLE := $(DIST_DIR)/$(APP_NAME).app
AUDIT_LOG  := $(HOME)/Library/Application Support/LumiBuddy/audit.log

MOCK_DIR := mock-lamp

.PHONY: help run build release app install open clean audit kill all check mock mock-build

.DEFAULT_GOAL := help

help: ## Show this help
	@echo "Lumi Buddy — make targets:"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Typical flow:"
	@echo "  make run            # dev — runs in foreground (Ctrl-C to stop)"
	@echo "  make app && make open"
	@echo ""
	@echo "Right-click → Open is needed the first time (no code signing yet)."

run: ## Run buddy in dev mode (foreground)
	cd $(MACOS_DIR) && swift run

build: ## Build debug binary
	cd $(MACOS_DIR) && swift build

release: ## Build release binary at macos/.build/release/LumiBuddy
	cd $(MACOS_DIR) && swift build -c release

check: ## Compile-only check (no link)
	cd $(MACOS_DIR) && swift build --build-tests

app: release ## Bundle into dist/LumiBuddy.app (unsigned)
	@echo "==> Bundling $(APP_BUNDLE)"
	@rm -rf "$(APP_BUNDLE)"
	@mkdir -p "$(APP_BUNDLE)/Contents/MacOS"
	@mkdir -p "$(APP_BUNDLE)/Contents/Resources"
	@cp "$(RELEASE_BIN)" "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@cp "$(INFO_PLIST)" "$(APP_BUNDLE)/Contents/Info.plist"
	@chmod +x "$(APP_BUNDLE)/Contents/MacOS/$(APP_NAME)"
	@echo "==> Done: $(APP_BUNDLE)"
	@echo "    First launch: right-click → Open (Gatekeeper blocks unsigned apps)."

install: app ## Copy bundled .app to /Applications (replaces existing)
	@echo "==> Installing to /Applications/$(APP_NAME).app"
	@rm -rf "/Applications/$(APP_NAME).app"
	@cp -R "$(APP_BUNDLE)" "/Applications/"
	@echo "==> Installed. Right-click → Open the first time."

open: ## Launch the bundled .app from dist/
	@if [ ! -d "$(APP_BUNDLE)" ]; then echo "No bundle yet — run 'make app' first."; exit 1; fi
	open "$(APP_BUNDLE)"

kill: ## Stop any running LumiBuddy process
	-@pkill -x $(APP_NAME) 2>/dev/null && echo "Stopped $(APP_NAME)" || echo "$(APP_NAME) not running"

audit: ## Tail the audit log
	@if [ ! -f "$(AUDIT_LOG)" ]; then echo "No audit log yet at $(AUDIT_LOG)"; exit 0; fi
	@tail -f "$(AUDIT_LOG)"

clean: ## Remove all build artifacts
	@echo "==> Cleaning"
	cd $(MACOS_DIR) && swift package clean 2>/dev/null || true
	rm -rf $(BUILD_DIR) $(MACOS_DIR)/.swiftpm $(DIST_DIR)

all: app ## Alias for `make app`

mock: ## Run the mock lamp server (Go) — test buddy without real lumi side
	cd $(MOCK_DIR) && go run .

mock-build: ## Compile-only check for mock-lamp
	cd $(MOCK_DIR) && go vet ./... && go build ./...
