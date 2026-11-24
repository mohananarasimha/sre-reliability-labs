TARGET_FILE := ../../shared/observability/prometheus/targets/lab01.json
LOADGEN_DIR := ../../shared/loadgen
API_HOST := 127.0.0.1
API_PORT := 8081
API_URL := http://$(API_HOST):$(API_PORT)

.PHONY: up-broken up-fixed load inspect-cgroup down

up-broken:
	docker compose --profile fixed down --remove-orphans
	docker compose --profile broken up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-broken:8080"],\n    "labels": {\n      "job": "lab01-cgroup-cpu",\n      "lab": "01-cgroup-cpu-throttling",\n      "profile": "broken"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 01 [broken] up on $(API_URL) (cpus=0.5, WORKERS=1, PARALLELISM=4, WORK_MS=20)"

up-fixed:
	docker compose --profile broken down --remove-orphans
	docker compose --profile fixed up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-fixed:8080"],\n    "labels": {\n      "job": "lab01-cgroup-cpu",\n      "lab": "01-cgroup-cpu-throttling",\n      "profile": "fixed"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 01 [fixed] up on $(API_URL) (cpus=1.0, WORKERS=1, PARALLELISM=4, WORK_MS=20)"

load:
	@mkdir -p $(CURDIR)/tmp
	@PROFILE=$$(curl -fsS $(API_URL)/cgroup-stats | python3 -c "import json,sys; print(json.load(sys.stdin).get('profile','run'))"); \
	BEFORE_FILE="$(CURDIR)/tmp/cgroup-before-$$PROFILE.json"; \
	DELTA_FILE="$(CURDIR)/tmp/cgroup-delta-$$PROFILE.json"; \
	OUT_FILE="$(CURDIR)/tmp/load-$$PROFILE.json"; \
	python3 $(CURDIR)/cgroup_delta.py snapshot --host $(API_HOST) --port $(API_PORT) --out "$$BEFORE_FILE"; \
	echo "Running loadgen against $(API_URL)/work (profile=$$PROFILE, concurrency=1, duration=30s, rps=2)..."; \
	(cd $(LOADGEN_DIR) && uv run loadgen run \
		--url $(API_URL)/work \
		--concurrency 1 \
		--duration 30 \
		--rps 2 \
		--json-out "$$OUT_FILE"); \
	RC=$$?; \
	python3 $(CURDIR)/cgroup_delta.py report --before "$$BEFORE_FILE" --host $(API_HOST) --port $(API_PORT) --out "$$DELTA_FILE"; \
	echo "Saved load JSON to $$OUT_FILE and cgroup delta JSON to $$DELTA_FILE"; \
	exit $$RC

inspect-cgroup:
	@curl -fsS $(API_URL)/cgroup-stats | python3 -m json.tool

down:
	docker compose --profile broken --profile fixed down --remove-orphans
	rm -f $(TARGET_FILE)
