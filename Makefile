# Convenience targets. `make help` lists them.
.PHONY: help install test demo demo-live server docker docker-build docker-up clean

PY ?= python3

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:  ## Install runtime + dev deps
	$(PY) -m pip install -e ".[dev]"

test:  ## Run the pytest suite (no API key needed)
	$(PY) -m pytest tests/

demo:  ## Two deterministic (offline) investigations — no key needed
	$(PY) -m agent investigate --offline --playbook offboarding_leakage --limit 5
	@echo
	@echo "----------------------------------------------------------------"
	@echo
	$(PY) -m agent investigate --offline --playbook access_explain --person "Ariel Chen" --resource "Snowflake"

demo-live:  ## Two LLM-driven investigations (requires OPENROUTER_API_KEY)
	$(PY) -m agent investigate "Does Ariel Chen still have access to any critical applications after their end date?"
	@echo
	@echo "----------------------------------------------------------------"
	@echo
	$(PY) -m agent investigate --playbook access_explain --person "Emi Kim" --resource "Snowflake"

server:  ## Run the FastAPI server on :8080
	uvicorn agent.interfaces.server:app --host 0.0.0.0 --port 8080

docker-build:  ## Build the Docker image
	docker build -t access-investigation-agent:local -f deploy/Dockerfile .

docker-up:  ## Run the container (mounts DB read-only, exposes :8080)
	docker compose -f deploy/docker-compose.yml up --build

docker: docker-up  ## Alias

clean:  ## Remove caches and stored investigation runs
	rm -rf .pytest_cache runs __pycache__
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
