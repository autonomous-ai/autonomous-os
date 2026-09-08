.PHONY: build install open check

APP := artifacts/package/Autonomous Buddy-darwin-$(shell node -p process.arch)/Autonomous Buddy.app

build: ## Compile and package the local macOS application
	npm run build
	npm run postinstall
	node scripts/package-macos.mjs

install: ## Install an already-built manager alongside the native companion
	node scripts/install-macos.mjs

open: ## Open the installed manager
	open "/Applications/Autonomous Buddy.app"

check: ## Run the desktop quality gates
	npm run lint
	npm test
	npm run build
	npm run test:e2e
