# kenny-server image. Build from the repo root:
#   docker build -f kenny-server/Dockerfile -t kenny-server .
# (compose.yaml does this for you.)
FROM python:3.14-slim

WORKDIR /app
COPY kenny-server/ /app/
# Ship the shared deny-rule catalog so the best-effort policy mirror can load it
# (the resolver's container candidate is /app/docs/policy/deny_rules.json). ADR-0021.
COPY docs/policy/ /app/docs/policy/

RUN pip install --no-cache-dir . && mkdir -p /data

# Bind to all interfaces inside the container; persist the SQLite store on a volume.
ENV KENNY_HOST=0.0.0.0 \
    KENNY_PORT=8000 \
    KENNY_DB_PATH=/data/kenny.sqlite

EXPOSE 8000
VOLUME ["/data"]

CMD ["kenny-server"]
