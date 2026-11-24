TARGET_FILE := ../../shared/observability/prometheus/targets/lab02.json
LOADGEN_DIR := ../../shared/loadgen
API_URL := http://127.0.0.1:8082

.PHONY: up-broken up-fixed load inspect-memory down

up-broken:
	docker compose --profile fixed down --remove-orphans
	docker compose --profile broken up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-broken:8080"],\n    "labels": {\n      "job": "lab02-oomkill-memory",\n      "lab": "02-oomkill-memory-pressure",\n      "profile": "broken"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 02 [broken] up on $(API_URL) (mem_limit=96m, memswap_limit=96m, CACHE_MODE=unbounded, PAYLOAD_KB=1024)"

up-fixed:
	docker compose --profile broken down --remove-orphans
	docker compose --profile fixed up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-fixed:8080"],\n    "labels": {\n      "job": "lab02-oomkill-memory",\n      "lab": "02-oomkill-memory-pressure",\n      "profile": "fixed"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 02 [fixed] up on $(API_URL) (mem_limit=96m, memswap_limit=96m, CACHE_MODE=bounded_lru, CACHE_MAX_ENTRIES=24, MEM_SOFT_LIMIT_PCT=75)"

load:
	@mkdir -p $(CURDIR)/tmp
	@PRE_JSON=$$(curl -fsS $(API_URL)/memory-stats 2>/dev/null || echo "{}"); \
	PROFILE=$$(printf '%s' "$$PRE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('profile','run'))" 2>/dev/null || echo "run"); \
	OOM_BEFORE=$$(printf '%s' "$$PRE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin).get('oom_kills',0))" 2>/dev/null || echo "0"); \
	CONTAINER="sre-lab02-api-$$PROFILE"; \
	OUT_FILE="$(CURDIR)/tmp/load-$$PROFILE.json"; \
	EVENTS_FILE="$(CURDIR)/tmp/docker-events-$$PROFILE.jsonl"; \
	INSPECT_FILE="$(CURDIR)/tmp/docker-inspect-$$PROFILE.json"; \
	SUMMARY_FILE="$(CURDIR)/tmp/oom-summary-$$PROFILE.json"; \
	START_EPOCH=$$(date +%s); \
	echo "Running loadgen against $(API_URL)/ingest (profile=$$PROFILE, container=$$CONTAINER, concurrency=4, duration=15s, rps=15, timeout=3.0)..."; \
	(cd $(LOADGEN_DIR) && uv run loadgen run \
		--url $(API_URL)/ingest \
		--concurrency 4 \
		--duration 15 \
		--rps 15 \
		--timeout 3.0 \
		--max-error-rate 0.02 \
		--json-out "$$OUT_FILE"); \
	RC=$$?; \
	END_EPOCH=$$(($$(date +%s) + 1)); \
	docker events --since "$$START_EPOCH" --until "$$END_EPOCH" \
		--filter "container=$$CONTAINER" \
		--filter "event=oom" \
		--filter "event=die" \
		--format '{{json .}}' > "$$EVENTS_FILE" || true; \
	docker inspect "$$CONTAINER" > "$$INSPECT_FILE" 2>/dev/null || echo '[]' > "$$INSPECT_FILE"; \
	OOM_AFTER=$$(curl -fsS $(API_URL)/memory-stats 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('oom_kills',-1))" 2>/dev/null || echo "-1"); \
	python3 $(CURDIR)/summarize_oom.py \
		--profile "$$PROFILE" \
		--container "$$CONTAINER" \
		--events-file "$$EVENTS_FILE" \
		--inspect-file "$$INSPECT_FILE" \
		--oom-before "$$OOM_BEFORE" \
		--oom-after "$$OOM_AFTER" \
		--out "$$SUMMARY_FILE"; \
	echo "Saved load JSON to $$OUT_FILE, events to $$EVENTS_FILE, summary to $$SUMMARY_FILE (loadgen exit=$$RC)"; \
	exit $$RC

inspect-memory:
	@echo "--- /memory-stats ---"
	@curl -fsS $(API_URL)/memory-stats | python3 -m json.tool || echo "Service unreachable (container may be restarting or exited after OOM)."
	@echo "--- docker inspect (OOM / restart state) ---"
	@for c in sre-lab02-api-broken sre-lab02-api-fixed; do \
		if docker inspect "$$c" >/dev/null 2>&1; then \
			docker inspect "$$c" --format 'Container={{.Name}} Status={{.State.Status}} RestartCount={{.RestartCount}} OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}'; \
		fi; \
	done

down:
	docker compose --profile broken --profile fixed down --remove-orphans
	rm -f $(TARGET_FILE)
