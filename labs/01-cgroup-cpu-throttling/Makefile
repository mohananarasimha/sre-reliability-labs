TARGET_FILE := ../../shared/observability/prometheus/targets/lab01.json
LOADGEN_DIR := ../../shared/loadgen
API_URL := http://127.0.0.1:8081

.PHONY: up-broken up-fixed load inspect-cgroup down

up-broken:
	docker compose --profile fixed down --remove-orphans
	docker compose --profile broken up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-broken:8080"],\n    "labels": {\n      "job": "lab01-cgroup-cpu",\n      "lab": "01-cgroup-cpu-throttling",\n      "profile": "broken"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 01 [broken] up on $(API_URL) (cpus=0.25, WORKERS=4, WORK_MS=15)"

up-fixed:
	docker compose --profile broken down --remove-orphans
	docker compose --profile fixed up -d --build --wait
	@printf '[\n  {\n    "targets": ["api-fixed:8080"],\n    "labels": {\n      "job": "lab01-cgroup-cpu",\n      "lab": "01-cgroup-cpu-throttling",\n      "profile": "fixed"\n    }\n  }\n]\n' > $(TARGET_FILE)
	@echo "Lab 01 [fixed] up on $(API_URL) (cpus=2.00, WORKERS=2, WORK_MS=15)"

load:
	@mkdir -p $(CURDIR)/tmp
	@PROFILE=$$(curl -fsS $(API_URL)/cgroup-stats | python3 -c "import json,sys; print(json.load(sys.stdin).get('profile','run'))"); \
	OUT_FILE="$(CURDIR)/tmp/load-$$PROFILE.json"; \
	echo "Running loadgen against $(API_URL)/work (profile=$$PROFILE, concurrency=8, duration=20s, rps=25)..."; \
	cd $(LOADGEN_DIR) && uv run loadgen run \
		--url $(API_URL)/work \
		--concurrency 8 \
		--duration 20 \
		--rps 25 \
		--json-out "$$OUT_FILE"; \
	echo "Saved JSON evidence to $$OUT_FILE"

inspect-cgroup:
	@curl -fsS $(API_URL)/cgroup-stats | python3 -m json.tool

down:
	docker compose --profile broken --profile fixed down --remove-orphans
	rm -f $(TARGET_FILE)
