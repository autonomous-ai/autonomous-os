---
title: Autonomous OS Search Tool
emoji: 🔍
colorFrom: blue
colorTo: purple
sdk: gradio
app_file: app.py
pinned: false
short_description: MCP Space for Autonomous OS web search.
tags:
  - autonomous-os-tool
  - mcp
  - search
---

# Autonomous OS Search Tool

MCP tool for web search with DuckDuckGo, built for the Autonomous OS ecosystem.

Provides a Gradio-based web interface and MCP server endpoint that any
MCP-compatible agent (Claude, OpenClaw, Hermes, etc.) can call to search the
web in real time.

## Usage

### As an MCP tool

Point your MCP client at the Space URL. The server exposes a single tool:

- **`search_web`** -- search the web for current information and return
  structured results (title, snippet, url).

### Local development

```bash
pip install -r requirements.txt
python app.py
```

The Gradio app starts on `http://localhost:7860` with MCP enabled.
