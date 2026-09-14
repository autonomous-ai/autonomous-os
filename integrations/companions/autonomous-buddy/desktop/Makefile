.PHONY: build install open check

BUDDY_ARCH ?= $(shell node -p process.arch)
APP := artifacts/package/Autonomous Buddy-darwin-$(BUDDY_ARCH)/Autonomous Buddy.app

build: ## Compile Electron and Swift, package one local macOS application
	npm run build
	BUDDY_ARCH=$(BUDDY_ARCH) node scripts/package-macos.mjs

install: build ## Build both components and install the unified application
	node scripts/install-macos.mjs

open: ## Open the installed manager
	open "/Applications/Autonomous Buddy.app"

check: ## Run the desktop quality gates
	npm run lint
	npm test
	$(MAKE) build
	BUDDY_APP_EXECUTABLE="$(CURDIR)/$(APP)/Contents/MacOS/Autonomous Buddy" npm run test:e2e
