.PHONY: install install-cpu install-gpu install-lb install-dev \
       start start-dlserver start-lbserver \
       start-runpod-master start-runpod-slave start-nginx start-runpod-dlserver start-runpod-lbserver stop-runpod-dlserver stop-runpod-lbserver start-jupyter \
       info \
       test test-api test-local test-action test-action-api test-action-local test-emotion test-emotion-api test-emotion-local test-pose test-pose-api test-pose-local lint

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

NGINX_CONF ?= /workspace/ai-lamp-lumi/dlbackend/nginx.conf
HOST ?= 0.0.0.0

# Port layout:
#   8888 — nginx (external)
#   7999 — lbserver (round-robin, prepends /_internal)
#   8000 — old DL server (nginx /_internal/ → here)
#   8001 — DL server (nginx /_internal/lelamp/ → here)
#   8890 — jupyter
LBSERVER_PORT ?= 7999
DLSERVER_PORT ?= 8001
JUPYTER_PORT ?= 8890

DLSERVER_PID ?= /tmp/dlserver.pid
LBSERVER_PID ?= /tmp/lbserver.pid

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

install:
	@if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi > /dev/null 2>&1; then \
		echo "CUDA detected — installing onnxruntime-gpu"; \
		pip install -e ".[dl,gpu]"; \
	else \
		echo "No CUDA detected — installing onnxruntime (CPU)"; \
		pip install -e ".[dl,cpu]"; \
	fi

install-cpu:
	pip install -e ".[dl,cpu]"

install-gpu:
	pip install -e ".[dl,gpu]"

install-lb:
	pip install -e ".[lb]"

install-dev:
	pip install -e ".[dl,cpu,dev]"

# ---------------------------------------------------------------------------
# Local dev (foreground, blocking)
# ---------------------------------------------------------------------------

start: install
	python -m dlserver --host $(HOST) --port $(DLSERVER_PORT)

start-dlserver: install
	python -m dlserver --host $(HOST) --port $(DLSERVER_PORT)

start-lbserver: install-lb
	python -m lbserver --host $(HOST) --port $(LBSERVER_PORT)

# ---------------------------------------------------------------------------
# Server (background, non-blocking)
# ---------------------------------------------------------------------------

start-runpod-master: start-nginx start-runpod-dlserver start-runpod-lbserver

start-runpod-slave: start-nginx start-runpod-dlserver

start-nginx:
	@if ! command -v nginx > /dev/null 2>&1; then \
		apt-get update && apt-get install -y nginx; \
	fi
	nginx -s stop 2>/dev/null || true
	pkill nginx 2>/dev/null || true
	nginx -c $(NGINX_CONF)

start-runpod-dlserver: install stop-runpod-dlserver
	@nohup python -m dlserver --host 127.0.0.1 --port $(DLSERVER_PORT) --log-dir /workspace/logs/dlbackend --pid-file $(DLSERVER_PID) </dev/null >/dev/null 2>&1 &
	@echo "dlserver started on port $(DLSERVER_PORT)"

stop-runpod-dlserver:
	@if [ -f $(DLSERVER_PID) ]; then \
		kill $$(cat $(DLSERVER_PID)) 2>/dev/null || true; \
		rm -f $(DLSERVER_PID); \
		echo "dlserver stopped"; \
	fi

start-runpod-lbserver: install-lb stop-runpod-lbserver
	@nohup python -m lbserver --host 127.0.0.1 --port $(LBSERVER_PORT) --log-dir /workspace/logs/lbserver --pid-file $(LBSERVER_PID) </dev/null >/dev/null 2>&1 &
	@echo "lbserver started on port $(LBSERVER_PORT)"

stop-runpod-lbserver:
	@if [ -f $(LBSERVER_PID) ]; then \
		kill $$(cat $(LBSERVER_PID)) 2>/dev/null || true; \
		rm -f $(LBSERVER_PID); \
		echo "lbserver stopped"; \
	fi

start-jupyter:
	@if ! command -v jupyter-lab > /dev/null 2>&1; then \
		python -m pip install jupyter; \
	fi
	@mkdir -p /workspace/logs/jupyter
	@(sleep 3 && TOKEN=$$(grep -oP 'token=\K[a-f0-9]+' /workspace/logs/jupyter/current 2>/dev/null | tail -1); \
	if [ -n "$$TOKEN" ]; then \
		echo "Jupyter Lab: https://<host>:8888/jupyter/lab?token=$$TOKEN"; \
	else \
		echo "Jupyter Lab started — grep token from /workspace/logs/jupyter/current"; \
	fi) &
	jupyter-lab --ip=127.0.0.1 --port=$(JUPYTER_PORT) --no-browser --allow-root --ServerApp.allow_remote_access=true --ServerApp.base_url=/jupyter/ --notebook-dir=/workspace 2>&1 | multilog t s1048576 n3 /workspace/logs/jupyter

# ---------------------------------------------------------------------------
# Info
# ---------------------------------------------------------------------------

info:
	@echo "=== Port Layout ==="
	@echo "  nginx:     8888"
	@echo "  lbserver:  $(LBSERVER_PORT)"
	@echo "  dlserver:  $(DLSERVER_PORT)"
	@echo "  jupyter:   $(JUPYTER_PORT)"
	@echo ""
	@echo "=== Processes ==="
	@printf "  dlserver:  "; \
	if [ -f $(DLSERVER_PID) ] && kill -0 $$(cat $(DLSERVER_PID)) 2>/dev/null; then \
		echo "running (pid=$$(cat $(DLSERVER_PID)))"; \
	else \
		echo "stopped"; \
	fi
	@printf "  lbserver:  "; \
	if [ -f $(LBSERVER_PID) ] && kill -0 $$(cat $(LBSERVER_PID)) 2>/dev/null; then \
		echo "running (pid=$$(cat $(LBSERVER_PID)))"; \
	else \
		echo "stopped"; \
	fi
	@printf "  nginx:     "; \
	if [ -f /tmp/nginx.pid ] && kill -0 $$(cat /tmp/nginx.pid) 2>/dev/null; then \
		echo "running (pid=$$(cat /tmp/nginx.pid))"; \
	else \
		echo "stopped"; \
	fi
	@echo ""
	@echo "=== Logs ==="
	@echo "  dlserver:  /workspace/logs/dlbackend/"
	@echo "  lbserver:  /workspace/logs/lbserver/"
	@echo "  jupyter:   /workspace/logs/jupyter/"

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test:
	python -m pytest tests/ -v

test-api: test-action-api test-emotion-api test-pose-api

test-local: test-action-local test-emotion-local test-pose-local

test-action: test-action-api test-action-local

test-action-api:
	python -m pytest tests/action_api/ -v

test-action-local:
	python -m pytest tests/action_local/ -v

test-emotion: test-emotion-api test-emotion-local

test-emotion-api:
	python -m pytest tests/emotion_api/ -v

test-emotion-local:
	python -m pytest tests/emotion_local/ -v

test-pose: test-pose-api test-pose-local

test-pose-api:
	python -m pytest tests/pose_api/ -v

test-pose-local:
	python -m pytest tests/pose_local/ -v

lint:
	ruff check src/ tests/
