.PHONY: build install open check

APP := artifacts/package/Autonomous Buddy-darwin-$(shell node -p process.arch)/Autonomous Buddy.app

build: ## Compile Electron and Swift, package one local macOS application
	npm run build
	npm run postinstall
	node scripts/package-macos.mjs

install: build ## Build both components and install the unified application
	node scripts/install-macos.mjs

open: ## Open the installed manager
	open "/Applications/Autonomous Buddy.app"

check: ## Run the desktop quality gates
	npm run lint
	npm test
	$(MAKE) build
	BUDDY_APP_EXECUTABLE="$(CURDIR)/$(APP)/Contents/MacOS/Autonomous Buddy" npm run test:e2e
