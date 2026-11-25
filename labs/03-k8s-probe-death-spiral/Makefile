CLUSTER_NAME := sre-lab03
KCTX := kind-$(CLUSTER_NAME)
IMAGE := sre-lab03-app:local
RUN ?= 1

.PHONY: cluster-up build-load run-broken run-fixed down

cluster-up:
	@if ! kind get clusters | grep -qx "$(CLUSTER_NAME)"; then \
		kind create cluster --name "$(CLUSTER_NAME)"; \
	else \
		echo "kind cluster $(CLUSTER_NAME) already exists"; \
	fi
	kubectl --context "$(KCTX)" apply -f k8s/namespace.yaml

build-load:
	docker build -t "$(IMAGE)" ./app
	kind load docker-image "$(IMAGE)" --name "$(CLUSTER_NAME)"
	kubectl --context "$(KCTX)" apply -f k8s/downstream.yaml
	kubectl --context "$(KCTX)" -n lab03 rollout status deployment/downstream --timeout=60s

run-broken:
	bash ./run-experiment.sh broken "$(RUN)"

run-fixed:
	bash ./run-experiment.sh fixed "$(RUN)"

down:
	kind delete cluster --name "$(CLUSTER_NAME)"
