package main

import (
	"flag"
	"fmt"
	"log"
	"log/slog"

	"github.com/joho/godotenv"

	"go.autonomous.ai/os/lib/logger"
	"go.autonomous.ai/os/server"
	"go.autonomous.ai/os/server/config"
)

func main() {
	var showVersion bool
	flag.BoolVar(&showVersion, "version", false, "print version and exit")
	flag.Parse()

	if showVersion {
		fmt.Println(config.LampVersion)
		return
	}

	// Load shared env file before logger init (so GELF_* env vars are visible).
	// Missing file is non-fatal — env may also be supplied by systemd.
	_ = godotenv.Load("/opt/hal/.env")

	cleanup := logger.Init(slog.LevelDebug, "/var/log/os-server.log")
	defer cleanup()

	srv, err := server.InitializeServer()
	if err != nil {
		log.Fatal("initialize server: ", err)
	}
	if err := srv.Serve(func() {}); err != nil {
		log.Fatal("http server: ", err)
	}
}
