# syntax=docker/dockerfile:1
# culture-library image: one image runs both the server (API + site) and the MCP server.
# Build from the repo root:  docker compose -f deploy/compose.yaml build
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE /src/
COPY culture_library /src/culture_library
# events-cli publishes send-to-device events (library.item.send_requested) to the MQTT broker.
RUN pip install --prefix=/install ".[server,s3]" "events-cli==0.10.0"

FROM python:3.12-slim-bookworm AS runtime
# poppler-utils provides pdftotext, used for PDF text extraction. postgresql-client-17 (PGDG,
# since bookworm ships 15) provides pg_dump and pg_restore for `culture-library backup`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /home/library library \
 && mkdir -p /data && chown library:library /data
COPY --from=build /install /usr/local
ENV PYTHONUNBUFFERED=1 \
    CULTURE_LIBRARY_DATA_DIR=/data
USER library
VOLUME ["/data"]
EXPOSE 8796 8797
CMD ["culture-library", "server", "serve", "--host", "0.0.0.0", "--port", "8796"]
