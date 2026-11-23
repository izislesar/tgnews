#!/bin/bash
# Ежедневный бэкап SQLite (WAL-safe через sqlite3 backup API) + ротация 7 дней.
set -euo pipefail
cd /root/tgnews || exit 1
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T -e STAMP="$STAMP" app python -c "
import os, sqlite3
stamp = os.environ['STAMP']
src = sqlite3.connect('/data/queue.db')
dst = sqlite3.connect(f'/data/backup-{stamp}.db')
src.backup(dst)
dst.close(); src.close()
" < /dev/null
find /root/tgnews/data -maxdepth 1 -name 'backup-*.db' -mtime +7 -delete
echo "$(date -u +%FT%TZ) backup ok $STAMP"
