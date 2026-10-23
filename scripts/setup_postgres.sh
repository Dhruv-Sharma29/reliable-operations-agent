#!/usr/bin/env bash
# Fetch PostgreSQL 16 from the Ubuntu arm64 archive and unpack it under .tools/pg (user space only:
# nothing is installed system-wide; `rm -rf .tools` undoes it). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" = "Darwin" ]; then
  # macOS: use Homebrew's PostgreSQL; sandbox/paths.py finds it through pg_config (or set PG_BIN).
  command -v pg_config >/dev/null || { echo "run: brew install postgresql@16 && brew link postgresql@16 --force"; exit 1; }
  echo "using $(pg_config --bindir)"; exit 0
fi
PG_VERSION=16.15-0ubuntu0.24.04.1
BIN=.tools/pg/usr/lib/postgresql/16/bin
if [ -x "$BIN/postgres" ]; then echo "postgres already unpacked: $BIN"; exit 0; fi
mkdir -p .tools/debs && cd .tools/debs
apt-get download "postgresql-16=$PG_VERSION" "postgresql-client-16=$PG_VERSION" "libpq5=$PG_VERSION"
cd ..
for deb in debs/*.deb; do dpkg-deb -x "$deb" pg; done
"$PWD/pg/usr/lib/postgresql/16/bin/postgres" --version
