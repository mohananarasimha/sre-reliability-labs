RUN ?= 1

.PHONY: build run-naive run-backoff run-budgeted down

build:
	docker compose build backend

run-naive: build
	bash ./run-experiment.sh naive "$(RUN)"

run-backoff: build
	bash ./run-experiment.sh backoff "$(RUN)"

run-budgeted: build
	bash ./run-experiment.sh budgeted "$(RUN)"

down:
	docker compose --profile client down --remove-orphans
