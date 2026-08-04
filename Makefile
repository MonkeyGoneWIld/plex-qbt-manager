# Plex-qBittorrent Speed Manager
# Thin wrapper around the common docker compose operations.

COMPOSE ?= docker compose
IMAGE   ?= ghcr.io/monkeygonewild/plex-qbt-manager:latest
PORT    ?= 5252

.PHONY: help setup up pull update down restart logs status health build test clean shell

help:
	@echo "Plex-qBittorrent Speed Manager"
	@echo "=============================="
	@echo ""
	@echo "  setup    - Create .env from the template and the docker network"
	@echo "  up       - Pull the published image and start the stack"
	@echo "  update   - Pull a newer image and recreate the container"
	@echo "  down     - Stop and remove the stack"
	@echo "  restart  - Restart the container"
	@echo "  logs     - Follow container logs"
	@echo "  status   - Container state plus the /status endpoint"
	@echo "  health   - Check the /health endpoint"
	@echo "  test     - Run integration tests against a running instance"
	@echo "  build    - Build the image locally instead of pulling"
	@echo "  shell    - Open a shell in the running container"
	@echo "  clean    - Remove the stack and the local image"
	@echo ""
	@echo "First run:  make setup  &&  \$$EDITOR .env  &&  make up"

setup:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example - edit it with your credentials before 'make up'"; \
	else \
		echo ".env already exists, leaving it alone"; \
	fi
	@docker network create plex-network 2>/dev/null \
		&& echo "Created network plex-network" \
		|| echo "Network plex-network already exists"

check-env:
	@if [ ! -f .env ]; then \
		echo "Error: .env not found. Run 'make setup' first."; \
		exit 1; \
	fi
	@if grep -q "your_.*_here" .env; then \
		echo "Error: .env still contains placeholder values. Edit it with real credentials."; \
		exit 1; \
	fi

up: check-env
	$(COMPOSE) up -d
	@echo "Waiting for the service to come up..."
	@sleep 8
	@$(MAKE) --no-print-directory health

pull:
	$(COMPOSE) pull

update: pull
	$(COMPOSE) up -d
	@sleep 8
	@$(MAKE) --no-print-directory health

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart
	@sleep 5
	@$(MAKE) --no-print-directory health

logs:
	$(COMPOSE) logs -f --tail 100

status:
	@$(COMPOSE) ps
	@echo ""
	@curl -s http://localhost:$(PORT)/status | python3 -m json.tool \
		|| echo "Service not responding on port $(PORT)"

health:
	@curl -f -s http://localhost:$(PORT)/health >/dev/null \
		&& echo "Healthy" \
		|| echo "Unhealthy - run 'make logs' for details"

test:
	@python3 test_integration.py http://localhost:$(PORT)

# Local development: build from source rather than pulling the published image.
build:
	docker build -t $(IMAGE) .

shell:
	$(COMPOSE) exec plex-qbt-manager /bin/bash

clean:
	$(COMPOSE) down -v --remove-orphans
	-docker rmi $(IMAGE)
