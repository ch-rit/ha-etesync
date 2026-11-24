#!/usr/bin/env bash
# Run a throwaway Etebase server (open signup, sqlite, DEBUG) for the live
# interop tests: ETEBASE_TEST_SERVER=http://127.0.0.1:8033 pytest tests/etebase
#
# Requires git and uv. Everything is kept in .etebase-test-server/ (gitignored);
# delete that directory to start from scratch.
set -euo pipefail

SERVER_REPO=https://github.com/etesync/server
SERVER_REF=6ac54121048e104437eec861f142e7dc1bebf7eb
HOST=${ETEBASE_HOST:-127.0.0.1}
PORT=${ETEBASE_PORT:-8033}

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK="$ROOT/.etebase-test-server"
SRC="$WORK/server"
VENV="$WORK/venv"
CONF="$WORK/config"
DATA="$WORK/data"

if [ ! -d "$SRC/.git" ]; then
  git clone --quiet "$SERVER_REPO" "$SRC"
fi
git -C "$SRC" checkout --quiet "$SERVER_REF"

if [ ! -x "$VENV/bin/python" ]; then
  # The server's requirements are pinned for Python 3.12.
  uv venv --quiet --python 3.12 "$VENV"
  VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$SRC/requirements.txt"
fi

mkdir -p "$CONF" "$DATA/media" "$DATA/static"
cat > "$CONF/etebase_server_settings.py" <<EOF
DEBUG = True
ALLOWED_HOSTS = ["*"]
SECRET_KEY = "etebase-test-server-not-secret"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "$DATA/db.sqlite3"}}
MEDIA_ROOT = "$DATA/media"
SENDFILE_ROOT = MEDIA_ROOT
STATIC_ROOT = "$DATA/static"
# The default (create_user_blocked) refuses signups of unknown users.
ETEBASE_CREATE_USER_FUNC = None
EOF

export PYTHONPATH="$CONF"
export DJANGO_SETTINGS_MODULE=etebase_server.settings
cd "$SRC"
"$VENV/bin/python" manage.py migrate --noinput >/dev/null
echo "Etebase test server listening on http://$HOST:$PORT/"
exec "$VENV/bin/python" -m uvicorn etebase_server.asgi:application --host "$HOST" --port "$PORT"
