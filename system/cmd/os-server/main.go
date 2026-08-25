package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/joho/godotenv"

	ccgatewayd "go.autonomous.ai/os/runtimes/claudecode/gatewayd"
	"go.autonomous.ai/os/runtimes/codex/gatewayd"
	ocgatewayd "go.autonomous.ai/os/runtimes/opencode/gatewayd"
	"go.autonomous.ai/os/system/lib/logger"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/server"
	"go.autonomous.ai/os/system/server/config"
)

func main() {
	// Subcommand dispatch before flag parsing: `os-server codex-gatewayd` /
	// `os-server claudecode-gatewayd` / `os-server opencode-gatewayd` run the
	// backend WS bridges (systemd units codex.service / claudecode.service /
	// opencode.service) instead of the API server — the bridges ship inside this
	// binary so they OTA-update with it.
	if len(os.Args) > 1 && os.Args[1] == "codex-gatewayd" {
		os.Exit(gatewayd.Main())
	}
	if len(os.Args) > 1 && os.Args[1] == "claudecode-gatewayd" {
		os.Exit(ccgatewayd.Main())
	}
	if len(os.Args) > 1 && os.Args[1] == "opencode-gatewayd" {
		os.Exit(ocgatewayd.Main())
	}
	// `os-server claude-sessions` is the terminal coding-session picker (cc.go)
	// — installed on the device as the /usr/local/bin/claude-sessions wrapper
	// by the claudecode presync.
	if len(os.Args) > 1 && os.Args[1] == "claude-sessions" {
		os.Exit(ccMain(os.Args[2:]))
	}

	var showVersion bool
	flag.BoolVar(&showVersion, "version", false, "print version and exit")
	flag.Parse()

	if showVersion {
		fmt.Println(config.OSVersion)
		return
	}

	// Load shared env file before logger init (so GELF_* env vars are visible).
	// Missing file is non-fatal — env may also be supplied by systemd.
	_ = godotenv.Load("/opt/hal/.env")

	cleanup := logger.Init(syspath.LogFile())
	defer cleanup()

	srv, err := server.InitializeServer()
	if err != nil {
		log.Fatal("initialize server: ", err)
	}
	if err := srv.Serve(func() {}); err != nil {
		log.Fatal("http server: ", err)
	}
}
