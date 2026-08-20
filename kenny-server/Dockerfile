# kenny-server image. Build from the repo root:
#   docker build -f kenny-server/Dockerfile -t kenny-server .
# (compose.yaml does this for you.)

# Stage 1: compile the dashboard SPA (kenny-web/) to static files. Nothing
# from this stage ends up in the final image except its dist/ output -- the
# runtime never needs Node (ADR-0052).
FROM node:26-slim AS web-build
WORKDIR /repo
COPY kenny-web/package.json kenny-web/package-lock.json ./kenny-web/
RUN npm --prefix kenny-web ci
COPY kenny-web/ ./kenny-web/
# vite.config.ts's outDir (../kenny-server/kenny_server/webui/dist, relative
# to kenny-web/) lands this at /repo/kenny-server/kenny_server/webui/dist.
RUN npm --prefix kenny-web run build

FROM python:3.14-slim

WORKDIR /app
COPY kenny-server/ /app/
# The compiled SPA (ADR-0052). Placed after the bulk COPY above so it wins
# over anything already at this path (there shouldn't be one -- dist/ is
# gitignored and .dockerignore keeps a stray local build out of the context
# -- but this ordering makes that guarantee independent of either).
COPY --from=web-build /repo/kenny-server/kenny_server/webui/dist /app/kenny_server/webui/dist
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
