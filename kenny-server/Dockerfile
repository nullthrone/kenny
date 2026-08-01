# kenny-server image. Build from the repo root:
#   docker build -f kenny-server/Dockerfile -t kenny-server .
# (compose.yaml does this for you.)
FROM python:3.14-slim

WORKDIR /app
COPY kenny-server/ /app/
# Ship the shared deny-rule catalog so the best-effort policy mirror can load it
# (the resolver's container candidate is /app/docs/policy/deny_rules.json). ADR-0021.
COPY docs/policy/ /app/docs/policy/

# Install every optional extra the running server can need. discord.py is an
# extra because a source install without it still works — the gateway degrades
# to "surface off" — but the image is the deployment shape (ADR-0010), so an
# operator configuring the Discord surface here has no pip step to add it at.
# tests/test_packaging.py fails the build if this list and the runtime's
# optional imports drift apart.
RUN pip install --no-cache-dir ".[discord]" && mkdir -p /data

# The release workflow passes the git tag here so the running server reports its
# real version (see kenny_server/__init__.py); unset for local/dev builds.
ARG KENNY_SERVER_VERSION=""

# Bind to all interfaces inside the container; persist the SQLite store on a volume.
ENV KENNY_HOST=0.0.0.0 \
    KENNY_PORT=8000 \
    KENNY_DB_PATH=/data/kenny.sqlite \
    KENNY_SERVER_VERSION=${KENNY_SERVER_VERSION}

EXPOSE 8000
VOLUME ["/data"]

CMD ["kenny-server"]
