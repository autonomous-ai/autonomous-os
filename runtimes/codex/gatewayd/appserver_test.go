package gatewayd

import "testing"

func TestAppInputIncludesTextAndImageDataURLs(t *testing.T) {
	p := turnPayload{Content: "look at this"}
	p.Attachments = append(p.Attachments,
		struct {
			Type string `json:"type"`
			URL  string `json:"url"`
		}{Type: "image", URL: "data:image/png;base64,AA=="},
		struct {
			Type string `json:"type"`
			URL  string `json:"url"`
		}{Type: "image", URL: "https://example.test/image.png"},
	)
	in := appInput(p)
	if len(in) != 2 || in[0]["type"] != "text" || in[0]["text"] != "look at this" {
		t.Fatalf("unexpected app input: %#v", in)
	}
	if in[1]["type"] != "image" || in[1]["url"] != "data:image/png;base64,AA==" {
		t.Fatalf("unexpected image input: %#v", in[1])
	}
}

func TestLegacyItemUsesExecTranslatorItemKinds(t *testing.T) {
	item := legacyItem([]byte(`{"id":"m1","type":"agentMessage","text":"hello"}`))
	if item["item_type"] != "agent_message" {
		t.Fatalf("item kind = %#v, want agent_message", item)
	}
}

func TestSnakeItemType(t *testing.T) {
	for got, want := range map[string]string{
		snakeItemType("commandExecution"): "command_execution",
		snakeItemType("mcpToolCall"):      "mcp_tool_call",
	} {
		if got != want {
			t.Fatalf("snake item type = %q, want %q", got, want)
		}
	}
}
