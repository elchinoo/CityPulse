.PHONY: up down test test-db test-aidb serve lint
up:        ; docker compose up -d db
down:      ; docker compose down
serve:     ; .venv/bin/citypulse start
lint:      ; .venv/bin/ruff check src tests
test:      ; .venv/bin/pytest -q
# Needs POSTGRES_PASSWORD exported (set -a; . ./.env; set +a). Uses a throwaway database named citypulse_test on the compose Postgres.
test-db:   ; docker compose exec -T db psql -U citypulse -c "DROP DATABASE IF EXISTS citypulse_test" -c "CREATE DATABASE citypulse_test" && \
	TEST_DATABASE_URL=postgresql://citypulse:$$POSTGRES_PASSWORD@127.0.0.1:5434/citypulse_test .venv/bin/pytest -q

# AIDB tests (marker `aidb`). The module starts and removes its OWN throwaway container from the AIDB image (build it from the ecosystem repository,
# docker/aidb-postgres; tag aidb-postgres:17); AIDB_TEST_URL only opts in. AIDB_TEST_CALLBACK_HOST must be an address of this machine that the container can
# reach (Docker Desktop: 192.168.65.254): the scripted chat model runs in this process. The first run downloads small local models from Hugging Face.
test-aidb: ; AIDB_TEST_URL=1 AIDB_TEST_CALLBACK_HOST=$${AIDB_TEST_CALLBACK_HOST:-192.168.65.254} .venv/bin/pytest -q -m aidb tests/test_aidb_integration.py
