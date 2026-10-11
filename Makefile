# Palimpsest Gateway — common tasks.
#
# Everything here is a thin wrapper around plain `docker compose`, so if you
# would rather not use make, every target has an equivalent command documented
# beside it.

COMPOSE      := docker compose
DEV_COMPOSE  := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help

.PHONY: help up dev down restart logs ps test lint shell redis-cli clean rotate-secrets

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the gateway, waiting until healthy
	$(COMPOSE) up -d --wait --build
	@echo
	@echo "  API        http://localhost:8000"
	@echo "  Dashboard  http://localhost:8000/dashboard/"
	@echo "  API docs   http://localhost:8000/docs"
	@echo "  Operator key: $$($(COMPOSE) exec -T app cat /app/.secrets/operator_key)"

dev: ## Start with hot reload against your working tree
	$(DEV_COMPOSE) up --build

down: ## Stop and remove the containers (keeps your data volumes)
	$(COMPOSE) down

restart: ## Restart the app container
	$(COMPOSE) restart app

logs: ## Tail application logs
	$(COMPOSE) logs -f app

ps: ## Show container status
	$(COMPOSE) ps

test: ## Run the test suite in the dev container
	$(DEV_COMPOSE) run --rm --no-deps app pytest -q

lint: ## Run ruff in the dev container
	$(DEV_COMPOSE) run --rm --no-deps app ruff check app/ tests/

shell: ## Open a shell in the app container
	$(COMPOSE) exec app bash

redis-cli: ## Open redis-cli against the running Redis
	$(COMPOSE) exec redis redis-cli

clean: ## DESTRUCTIVE: remove containers AND all data volumes
	@echo "This deletes Redis data and the generated master key."
	@echo "Existing encrypted credentials cannot be recovered afterwards."
	@read -p "Type 'yes' to continue: " ok; [ "$$ok" = "yes" ] || (echo "aborted"; exit 1)
	$(COMPOSE) down -v

rotate-secrets: ## DESTRUCTIVE: generate a fresh master key and operator key
	@echo "This destroys the ability to decrypt every stored credential."
	@echo "You will have to re-add every pool key through the Operator API."
	@read -p "Type 'yes' to continue: " ok; [ "$$ok" = "yes" ] || (echo "aborted"; exit 1)
	$(COMPOSE) run --rm --no-deps --entrypoint sh app -c 'rm -f /app/.secrets/master_key /app/.secrets/operator_key'
	$(COMPOSE) down
	$(MAKE) up