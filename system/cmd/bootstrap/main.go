package main

import (
	"flag"
	"fmt"
	"log"
	"log/slog"

	"github.com/joho/godotenv"

	"go.autonomous.ai/os/system/bootstrap"
	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/lib/logger"
)

func main() {
	var showVersion bool
	flag.BoolVar(&showVersion, "version", false, "print version and exit")
	flag.Parse()

	if showVersion {
		fmt.Println(config.BootstrapVersion)
		return
	}

	// Load shared env file before logger init (so GELF_* env vars are visible).
	// Missing file is non-fatal — env may also be supplied by systemd.
	_ = godotenv.Load("/opt/hal/.env")

	cleanup := logger.Init("/var/log/bootstrap.log")
	defer cleanup()

	b, err := bootstrap.ProvideServer()
	if err != nil {
		log.Fatalf("bootstrap: initialize: %v", err)
	}
	if err := b.Serve(); err != nil {
		log.Fatalf("bootstrap: %v", err)
	}
	slog.Info("bootstrap stopped", "component", "bootstrap")
}
