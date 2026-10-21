.PHONY: up down test test-db serve lint
up:        ; docker compose up -d db
down:      ; docker compose down
serve:     ; .venv/bin/citypulse start
lint:      ; .venv/bin/ruff check src tests
test:      ; .venv/bin/pytest -q
# Needs POSTGRES_PASSWORD exported (set -a; . ./.env; set +a). Uses a throwaway database named citypulse_test on the compose Postgres.
test-db:   ; docker compose exec -T db psql -U citypulse -c "DROP DATABASE IF EXISTS citypulse_test" -c "CREATE DATABASE citypulse_test" && \
	TEST_DATABASE_URL=postgresql://citypulse:$$POSTGRES_PASSWORD@127.0.0.1:5434/citypulse_test .venv/bin/pytest -q
