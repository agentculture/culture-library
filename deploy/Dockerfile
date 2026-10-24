# syntax=docker/dockerfile:1
# culture-library image: one image runs both the server (API + site) and the MCP server.
# Build from the repo root:  docker compose -f deploy/compose.yaml build
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md LICENSE /src/
COPY culture_library /src/culture_library
RUN pip install --prefix=/install ".[server]"

FROM python:3.12-slim-bookworm AS runtime
# poppler-utils provides pdftotext, used for PDF text extraction.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
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
