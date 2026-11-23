OBS_COMPOSE := shared/observability/docker-compose.yml
LOADGEN_DIR := shared/loadgen

.PHONY: obs-up obs-down obs-status loadgen-test loadgen-lint smoke-test

obs-up:
	docker compose -f $(OBS_COMPOSE) up -d --wait

obs-down:
	docker compose -f $(OBS_COMPOSE) down

obs-status:
	docker compose -f $(OBS_COMPOSE) ps

loadgen-test:
	cd $(LOADGEN_DIR) && uv run pytest -v

loadgen-lint:
	cd $(LOADGEN_DIR) && uv run ruff check .

smoke-test:
	@mkdir -p $(CURDIR)/tmp
	cd $(LOADGEN_DIR) && uv run loadgen run \
		--url http://localhost:9090/-/healthy \
		--concurrency 4 \
		--duration 5 \
		--rps 20 \
		--json-out $(CURDIR)/tmp/smoke-test.json
	@cat $(CURDIR)/tmp/smoke-test.json
