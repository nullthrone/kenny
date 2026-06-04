# kenny-server image. Build from the repo root:
#   docker build -f kenny-server/Dockerfile -t kenny-server .
# (compose.yaml does this for you.)
FROM python:3.11-slim

WORKDIR /app
COPY kenny-server/ /app/

RUN pip install --no-cache-dir . && mkdir -p /data

# Bind to all interfaces inside the container; persist the SQLite store on a volume.
ENV KENNY_HOST=0.0.0.0 \
    KENNY_PORT=8000 \
    KENNY_DB_PATH=/data/kenny.sqlite

EXPOSE 8000
VOLUME ["/data"]

CMD ["kenny-server"]
