SHELL := /bin/zsh

PYTHON ?= .venv/bin/python
PYTHONPATH ?= src
PROJECT_ROOT ?= .
WORKSPACE_ROOT ?= var/gleskit_mcp

MCP_HOST ?= 127.0.0.1
MCP_PORT ?= 8010
MCP_TRANSPORT ?= streamable-http
EXPOSE_HOST ?= 127.0.0.1

.PHONY: help check-python install-mcp test reference_status update_references workspace_status serve_mcp expose_mcp desk_app ui web_app close_ui ui_docker close_ui_docker status_ui_docker kill_opencode opencode_setup eval_bixbench judge_bixbench eval_repobench judge_repobench release_license_check

help:
	@echo "GLESKit MCP Make targets"
	@echo ""
	@echo "  make install-mcp          Install MCP optional dependency"
	@echo "  make test                 Run Python unit tests"
	@echo "  make release_license_check Verify exact-byte license/dependency review (not publication approval)"
	@echo "  make eval_bixbench        Run the BixBench evaluation pipeline"
	@echo "  make eval_repobench      Run the RepoBench evaluation pipeline"
	@echo "  make reference_status     Show local reference bundle status"
	@echo "  make update_references    Refresh local reference bundle"
	@echo "  make workspace_status     Show workspace bridge status"
	@echo "  make serve_mcp            Start MCP server (default transport: $(MCP_TRANSPORT))"
	@echo "  make expose_mcp           Blocked: authenticated remote deployment required"
	@echo "  make desk_app             Compatibility alias for make ui (native shell retired)"
	@echo "  make ui                   Build/open the local browser app (web_app alias)"
	@echo "  make close_ui             Stop this checkout's web host and all owned workers; keep data"
	@echo "  make ui_docker            Build once/open the Docker app (Apple Silicon); no host Python/R/Node"
	@echo "  make close_ui_docker      Stop the Docker app and children; keep its private data volume"
	@echo "  make status_ui_docker     Show Docker app status without opening or starting it"
	@echo "  make web_app              Compatibility alias for make ui"
	@echo "  make kill_opencode        Compatibility alias for scoped make close_ui (not global pkill)"
	@echo "  make opencode_setup       Mirror repo OpenCode files into ~/.config/opencode"
	@echo "  make judge_bixbench       Run the BixBench judge committee over an existing run"
	@echo "  make judge_repobench      Run the RepoBench judge committee over an existing run"
	@echo ""
	@echo "OpenCode setup:"
	@echo "  make opencode_setup"
	@echo ""
	@echo "Overrides:"
	@echo "  make serve_mcp MCP_PORT=8010 MCP_HOST=127.0.0.1"
	@echo "  make eval_bixbench BIXBENCH_SCOPE=1 BIXBENCH_LIMIT=3 BIXBENCH_TIMEOUT_SECONDS=600"
	@echo "  make eval_bixbench BIXBENCH_RUN_ID=TEST01 BIXBENCH_CONTINUE_MODE=append BIXBENCH_SCOPE=1 BIXBENCH_RANDOM_COUNT=12"
	@echo "  make eval_bixbench BIXBENCH_SKILLS_MODE=off BIXBENCH_MODEL=openai/gpt-5.4-mini"
	@echo "  make eval_bixbench BIXBENCH_POLL_INTERVAL_SECONDS=60 BIXBENCH_OPENCODE_TOOL_TIMEOUT_MS=120000"
	@echo "  make eval_bixbench BIXBENCH_AUTO_JUDGE=1"
	@echo "  make judge_bixbench BIXBENCH_RUN_ROOT=var/bixbench/runs/<run_id> BIXBENCH_JUDGE_SCOPE=all"
	@echo "  make judge_bixbench BIXBENCH_JUDGE_PROVIDER=openrouter BIXBENCH_JUDGE_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free"
	@echo "  make eval_repobench REPOBENCH_SCOPE=1 REPOBENCH_LIMIT=3 REPOBENCH_MODEL=openai/gpt-5.4-mini"
	@echo "  make eval_repobench REPOBENCH_RUN_ID=TEST01 REPOBENCH_CONTINUE_MODE=append REPOBENCH_RANDOM_COUNT=2"
	@echo "  make eval_repobench REPOBENCH_SKILLS_MODE=off REPOBENCH_MODEL=openai/gpt-5.4-mini"
	@echo "  make judge_repobench REPOBENCH_RUN_ROOT=var/repobench/runs/<run_id> REPOBENCH_JUDGE_PROVIDER=openrouter REPOBENCH_JUDGE_MODEL=deepseek/deepseek-v4-flash"

check-python:
	@test -x "$(PYTHON)" || (echo "Missing $(PYTHON). Create your venv first." && exit 1)

install-mcp: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pip install -e ".[mcp]"

test: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

release_license_check: check-python
	@$(PYTHON) scripts/release/licensing_gate.py

reference_status: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m gleskit_mcp reference-status

update_references: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m gleskit_mcp update-references

workspace_status: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m gleskit_mcp --workspace-root $(WORKSPACE_ROOT) workspace-status

serve_mcp: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m gleskit_mcp --project-root $(PROJECT_ROOT) serve \
	--transport $(MCP_TRANSPORT) \
	--host $(MCP_HOST) \
	--port $(MCP_PORT) \
	--streamable-http-path /mcp

expose_mcp:
	@echo "Public MCP exposure is blocked: authenticated HTTPS deployment is not implemented. Use local stdio or make serve_mcp; do not tunnel an unauthenticated endpoint." >&2
	@exit 1

ui web_app:
	@python3 scripts/run_local_web.py $(WEB_ARGS)

close_ui:
	@python3 scripts/close_local_web.py $(CLOSE_UI_ARGS)

ui_docker:
	@sh scripts/docker_ui.sh open

close_ui_docker:
	@sh scripts/docker_ui.sh stop

status_ui_docker:
	@sh scripts/docker_ui.sh status

desk_app: ui

kill_opencode: close_ui

opencode_setup:
	@bash scripts/opencode_setup.sh

eval_bixbench: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/eval/run_bixbench.py \
		$(if $(BIXBENCH_RUN_ID),--run-id $(BIXBENCH_RUN_ID),) \
		$(foreach q,$(BIXBENCH_QUESTION_IDS),--question-id $(q)) \
		$(if $(BIXBENCH_QUESTION_ID),--question-id $(BIXBENCH_QUESTION_ID),) \
		$(if $(BIXBENCH_SCOPE),--scope $(BIXBENCH_SCOPE),--scope 1) \
		$(if $(BIXBENCH_LIMIT),--limit $(BIXBENCH_LIMIT),) \
		$(if $(BIXBENCH_RANDOM_COUNT),--random-count $(BIXBENCH_RANDOM_COUNT),) \
		$(if $(BIXBENCH_RANDOM_SEED),--random-seed $(BIXBENCH_RANDOM_SEED),) \
		$(if $(BIXBENCH_RESUME),--resume,) \
		$(if $(BIXBENCH_CONTINUE_MODE),--continue-mode $(BIXBENCH_CONTINUE_MODE),) \
		$(if $(BIXBENCH_DRY_RUN),--dry-run,) \
		$(if $(BIXBENCH_AGENT),--agent $(BIXBENCH_AGENT),) \
		$(if $(BIXBENCH_SKILLS_MODE),--skills-mode $(BIXBENCH_SKILLS_MODE),) \
		$(if $(BIXBENCH_MODEL),--model $(BIXBENCH_MODEL),) \
		$(if $(BIXBENCH_OPENCODE_BIN),--opencode-bin $(BIXBENCH_OPENCODE_BIN),) \
		$(if $(BIXBENCH_TIMEOUT_SECONDS),--timeout-seconds $(BIXBENCH_TIMEOUT_SECONDS),) \
		$(if $(BIXBENCH_MAX_TURNS),--max-turns $(BIXBENCH_MAX_TURNS),) \
		$(if $(BIXBENCH_MAX_TOOL_CALLS),--max-tool-calls $(BIXBENCH_MAX_TOOL_CALLS),) \
		$(if $(BIXBENCH_NO_PROGRESS_CYCLES),--no-progress-cycles $(BIXBENCH_NO_PROGRESS_CYCLES),) \
		$(if $(BIXBENCH_POLL_INTERVAL_SECONDS),--poll-interval-seconds $(BIXBENCH_POLL_INTERVAL_SECONDS),) \
		$(if $(BIXBENCH_OPENCODE_TOOL_TIMEOUT_MS),--opencode-tool-timeout-ms $(BIXBENCH_OPENCODE_TOOL_TIMEOUT_MS),) \
		$(if $(BIXBENCH_MAX_RETRIES),--max-retries $(BIXBENCH_MAX_RETRIES),) \
		$(if $(BIXBENCH_VERBOSE),--verbose,)

judge_bixbench: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/eval/judge_bixbench.py \
		$(if $(BIXBENCH_RUN_ROOT),$(BIXBENCH_RUN_ROOT),) \
		$(if $(BIXBENCH_JUDGE_CONFIG),--config $(BIXBENCH_JUDGE_CONFIG),) \
		$(foreach q,$(BIXBENCH_QUESTION_IDS),--question-id $(q)) \
		$(if $(BIXBENCH_QUESTION_ID),--question-id $(BIXBENCH_QUESTION_ID),) \
		$(if $(BIXBENCH_JUDGE_SCOPE),--scope $(BIXBENCH_JUDGE_SCOPE),--scope all) \
		$(if $(BIXBENCH_JUDGE_LIMIT),--limit $(BIXBENCH_JUDGE_LIMIT),) \
		$(if $(BIXBENCH_JUDGE_RESUME),--resume,) \
		$(if $(BIXBENCH_JUDGE_TIMEOUT_SECONDS),--timeout-seconds $(BIXBENCH_JUDGE_TIMEOUT_SECONDS),) \
		$(if $(BIXBENCH_JUDGE_PROVIDER),--provider $(BIXBENCH_JUDGE_PROVIDER),) \
		$(if $(BIXBENCH_JUDGE_BASE_URL),--base-url $(BIXBENCH_JUDGE_BASE_URL),) \
		$(if $(BIXBENCH_JUDGE_MODEL),--model $(BIXBENCH_JUDGE_MODEL),) \
		$(if $(BIXBENCH_JUDGE_STATS_MODEL),--stats-model $(BIXBENCH_JUDGE_STATS_MODEL),) \
		$(if $(BIXBENCH_JUDGE_BIOLOGY_MODEL),--biology-model $(BIXBENCH_JUDGE_BIOLOGY_MODEL),) \
		$(if $(BIXBENCH_JUDGE_EVIDENCE_MODEL),--evidence-model $(BIXBENCH_JUDGE_EVIDENCE_MODEL),) \
		$(if $(BIXBENCH_JUDGE_ARBITER_MODEL),--arbiter-model $(BIXBENCH_JUDGE_ARBITER_MODEL),) \
		$(if $(BIXBENCH_JUDGE_VERBOSE),--verbose,)

eval_repobench: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/eval/run_repobench.py \
		$(if $(REPOBENCH_RUN_ID),--run-id $(REPOBENCH_RUN_ID),) \
		$(foreach q,$(REPOBENCH_TASK_IDS),--task-id $(q)) \
		$(if $(REPOBENCH_TASK_ID),--task-id $(REPOBENCH_TASK_ID),) \
		$(if $(REPOBENCH_SCOPE),--scope $(REPOBENCH_SCOPE),--scope 1) \
		$(if $(REPOBENCH_REPORT_TYPE),--report-type $(REPOBENCH_REPORT_TYPE),) \
		$(if $(REPOBENCH_LIMIT),--limit $(REPOBENCH_LIMIT),) \
		$(if $(REPOBENCH_RANDOM_COUNT),--random-count $(REPOBENCH_RANDOM_COUNT),) \
		$(if $(REPOBENCH_RANDOM_SEED),--random-seed $(REPOBENCH_RANDOM_SEED),) \
		$(if $(REPOBENCH_RESUME),--resume,) \
		$(if $(REPOBENCH_CONTINUE_MODE),--continue-mode $(REPOBENCH_CONTINUE_MODE),) \
		$(if $(REPOBENCH_DRY_RUN),--dry-run,) \
		$(if $(REPOBENCH_AGENT),--agent $(REPOBENCH_AGENT),) \
		$(if $(REPOBENCH_SKILLS_MODE),--skills-mode $(REPOBENCH_SKILLS_MODE),) \
		$(if $(REPOBENCH_MODEL),--model $(REPOBENCH_MODEL),) \
		$(if $(REPOBENCH_OPENCODE_BIN),--opencode-bin $(REPOBENCH_OPENCODE_BIN),) \
		$(if $(REPOBENCH_TIMEOUT_SECONDS),--timeout-seconds $(REPOBENCH_TIMEOUT_SECONDS),) \
		$(if $(REPOBENCH_MAX_TURNS),--max-turns $(REPOBENCH_MAX_TURNS),) \
		$(if $(REPOBENCH_MAX_TOOL_CALLS),--max-tool-calls $(REPOBENCH_MAX_TOOL_CALLS),) \
		$(if $(REPOBENCH_NO_PROGRESS_CYCLES),--no-progress-cycles $(REPOBENCH_NO_PROGRESS_CYCLES),) \
		$(if $(REPOBENCH_POLL_INTERVAL_SECONDS),--poll-interval-seconds $(REPOBENCH_POLL_INTERVAL_SECONDS),) \
		$(if $(REPOBENCH_OPENCODE_TOOL_TIMEOUT_MS),--opencode-tool-timeout-ms $(REPOBENCH_OPENCODE_TOOL_TIMEOUT_MS),) \
		$(if $(REPOBENCH_MAX_RETRIES),--max-retries $(REPOBENCH_MAX_RETRIES),) \
		$(if $(REPOBENCH_VERBOSE),--verbose,)

judge_repobench: check-python
	@PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/eval/judge_repobench.py \
		$(if $(REPOBENCH_RUN_ROOT),$(REPOBENCH_RUN_ROOT),) \
		$(foreach q,$(REPOBENCH_TASK_IDS),--task-id $(q)) \
		$(if $(REPOBENCH_TASK_ID),--task-id $(REPOBENCH_TASK_ID),) \
		$(if $(REPOBENCH_JUDGE_SCOPE),--scope $(REPOBENCH_JUDGE_SCOPE),--scope all) \
		$(if $(REPOBENCH_JUDGE_REPORT_TYPE),--report-type $(REPOBENCH_JUDGE_REPORT_TYPE),) \
		$(if $(REPOBENCH_JUDGE_LIMIT),--limit $(REPOBENCH_JUDGE_LIMIT),) \
		$(if $(REPOBENCH_JUDGE_RESUME),--resume,) \
		$(if $(REPOBENCH_JUDGE_TIMEOUT_SECONDS),--timeout-seconds $(REPOBENCH_JUDGE_TIMEOUT_SECONDS),) \
		$(if $(REPOBENCH_JUDGE_PROVIDER),--provider $(REPOBENCH_JUDGE_PROVIDER),) \
		$(if $(REPOBENCH_JUDGE_BASE_URL),--base-url $(REPOBENCH_JUDGE_BASE_URL),) \
		$(if $(REPOBENCH_JUDGE_MODEL),--model $(REPOBENCH_JUDGE_MODEL),) \
		$(if $(REPOBENCH_JUDGE_CONTENT_MODEL),--content-model $(REPOBENCH_JUDGE_CONTENT_MODEL),) \
		$(if $(REPOBENCH_JUDGE_EVIDENCE_MODEL),--evidence-model $(REPOBENCH_JUDGE_EVIDENCE_MODEL),) \
		$(if $(REPOBENCH_JUDGE_TEMPLATE_MODEL),--template-model $(REPOBENCH_JUDGE_TEMPLATE_MODEL),) \
		$(if $(REPOBENCH_JUDGE_PROVENANCE_MODEL),--provenance-model $(REPOBENCH_JUDGE_PROVENANCE_MODEL),) \
		$(if $(REPOBENCH_JUDGE_ARBITER_MODEL),--arbiter-model $(REPOBENCH_JUDGE_ARBITER_MODEL),) \
		$(if $(REPOBENCH_JUDGE_VERBOSE),--verbose,)
