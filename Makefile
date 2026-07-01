# Telegram -> Immich bot — server operations.
# Usage: `make deploy` to pull, rebuild and (re)start the container.

COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: deploy pull build up down restart logs ps help

## deploy: pull latest code, rebuild the image and (re)create the container
deploy: pull build up
	@echo "Deployed. Follow logs with: make logs"

## pull: fetch and fast-forward the current branch from origin
pull:
	git pull --ff-only

## build: build the Docker image from the current source
build:
	$(COMPOSE) build

## up: (re)create and start the container in the background
up:
	$(COMPOSE) up -d

## down: stop and remove the container
down:
	$(COMPOSE) down

## restart: restart the running container without rebuilding
restart:
	$(COMPOSE) restart

## logs: follow the container logs
logs:
	$(COMPOSE) logs -f

## ps: show container status
ps:
	$(COMPOSE) ps

## help: list available targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
