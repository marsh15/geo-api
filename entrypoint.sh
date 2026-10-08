#!/bin/sh
set -eu
alembic upgrade head
exec uvicorn geo_api.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-keep-alive 5
