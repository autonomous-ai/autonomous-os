package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

const helpText = `
mock-lamp REPL — type a command to send it to the paired buddy:
  ping                       quickest sanity check (no permission needed)
  notification <title>       e.g. notification meeting in 5    (needs noti permission)
  open_url <url>             e.g. open_url https://news.ycombinator.com
  open_app <name>            e.g. open_app Calculator          (or bundle id)
  close_app <name>           e.g. close_app Calculator
  type_text <text>           e.g. type_text hello world        (needs Accessibility)
  key_combo <keys>           e.g. key_combo cmd space          (needs Accessibility)
  code                       re-issue a new pairing code
  status                     show pairing / connection state
  help | ?                   this list
  quit                       exit
`

func RunREPL(ctx context.Context, state *State) {
	fmt.Print(helpText)
	scanner := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("> ")
		if !scanner.Scan() {
			return
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		switch line {
		case "help", "?":
			fmt.Print(helpText)
			continue
		case "quit", "exit":
			os.Exit(0)
		case "code":
			state.IssueCode()
			continue
		case "status":
			printStatus(state)
			continue
		}

		cmd, ok := parseREPL(line)
		if !ok {
			fmt.Printf("  unknown command — type 'help'\n\n")
			continue
		}
		resp, err := state.Dispatch(ctx, cmd)
		if err != nil {
			fmt.Printf("  ✗ %v\n\n", err)
			continue
		}
		printResponse(resp)
	}
}

func printStatus(state *State) {
	paired := state.pairedSnapshot()
	ws := state.currentWS()
	if paired == nil {
		fmt.Println("  pairing: none")
	} else {
		fmt.Printf("  pairing: id=%s name=%q os=%q\n", paired.BuddyID, paired.Name, paired.OSVersion)
	}
	if ws == nil {
		fmt.Println("  ws: not connected")
	} else {
		fmt.Println("  ws: connected")
	}
	fmt.Println()
}

func printResponse(raw json.RawMessage) {
	var pretty map[string]any
	if err := json.Unmarshal(raw, &pretty); err != nil {
		fmt.Printf("  raw: %s\n\n", raw)
		return
	}
	okVal, _ := pretty["ok"].(bool)
	duration := pretty["duration_ms"]
	if okVal {
		fmt.Printf("  ✓ %v  (%vms)\n\n", pretty["result"], duration)
	} else {
		fmt.Printf("  ✗ %v  (%vms)\n\n", pretty["error"], duration)
	}
}
