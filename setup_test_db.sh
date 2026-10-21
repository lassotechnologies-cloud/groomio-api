#!/usr/bin/env bash
# Spin up the test Postgres and create the groomio_test database.
# Usage: ./setup_test_db.sh

set -euo pipefail

cd "$(dirname "$0")"

echo "Starting PostgreSQL test container..."
docker compose -f docker-compose.test.yml up -d

echo "Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
  if docker exec "$(docker compose -f docker-compose.test.yml ps -q postgres)" pg_isready -U postgres >/dev/null 2>&1; then
    echo "PostgreSQL is ready."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "PostgreSQL did not become ready in time."
    exit 1
  fi
  sleep 1
done

CONTAINER=$(docker compose -f docker-compose.test.yml ps -q postgres)

echo "Creating groomio_test database..."
docker exec "$CONTAINER" psql -U postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'groomio_test'" | grep -q 1 || \
  docker exec "$CONTAINER" psql -U postgres -c "CREATE DATABASE groomio_test"

echo ""
echo "Done. Run tests with:"
echo "  TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/groomio_test python -m pytest tests/ -v"
echo ""
echo "Note the port: 5433, not 5432. Port 5432 is this machine's development"
echo "PostgreSQL, and the test suite drops and recreates the schema on every run."
echo ""
echo "To stop:"
echo "  docker compose -f docker-compose.test.yml down"
