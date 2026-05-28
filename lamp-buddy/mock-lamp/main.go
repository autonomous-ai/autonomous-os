package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const listenAddr = "127.0.0.1:8765"

func main() {
	state := NewState()

	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/buddy/pair/start", state.HandlePairStart)
	mux.HandleFunc("POST /api/buddy/pair/confirm", state.HandlePairConfirm)
	mux.HandleFunc("DELETE /api/buddy/self", state.HandleSelfRevoke)
	mux.HandleFunc("GET /api/buddy/ws", state.HandleWS)
	mux.HandleFunc("POST /api/buddy/command", state.HandleCommand)

	srv := &http.Server{
		Addr:              listenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	fmt.Printf("[mock-lamp] listening on http://%s\n", listenAddr)
	fmt.Println("[hint] In Lumi Buddy: menu → 'Pair with Lumi…' → host: localhost:8765 + code below")
	state.IssueCode()

	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("[mock-lamp] listen: %v", err)
		}
	}()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go RunREPL(ctx, state)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	fmt.Println("\n[mock-lamp] shutting down")

	shutdownCtx, sc := context.WithTimeout(context.Background(), 2*time.Second)
	defer sc()
	_ = srv.Shutdown(shutdownCtx)
}
