# n8n-execution-viewer — sidecar container for n8n (SQLite instances).
#
# Build on the host that runs n8n:
#   docker build -t n8n-execution-viewer .
#
# The n8n data volume must be mounted READ-ONLY at /data (see
# docker-compose.example.yml). The SQLite database is expected at
# /data/database.sqlite, which is where it lands when your n8n
# service mounts the volume at /home/node/.n8n (the official default).
# Override with N8N_DB_PATH if yours differs.
FROM python:3.12-slim

# n8n's volume is owned by uid 1000 and its files are 0600, so this
# container runs as root. That is contained: the image holds exactly
# three small Python files, no other tools, and the n8n mount is
# read-only at the kernel level.
WORKDIR /app
COPY backend/app.py backend/sync.py backend/schema.py ./

ENV N8N_DB_PATH=/data/database.sqlite \
    N8N_VIEWER_DB=/var/lib/n8n-viewer/history.db \
    N8N_VIEWER_BIND=0.0.0.0 \
    N8N_VIEWER_PORT=8391 \
    N8N_VIEWER_SYNC_INTERVAL=300

# The history database lives here; keep it on a volume so it survives
# image rebuilds and n8n upgrades.
VOLUME ["/var/lib/n8n-viewer"]
EXPOSE 8391

CMD ["python3", "-u", "app.py"]
