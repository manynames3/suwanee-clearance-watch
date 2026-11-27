# Clearance Watch: scanner image. The scanner sends its requests through curl
# (Home Depot's gateway turns away Python HTTP clients), so curl is required.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[dashboard]"

# All state (database, cooldown, digests, the generated phone page) lives in
# /data so it survives container upgrades.
RUN useradd --create-home --uid 1000 hd && mkdir /data && chown hd /data
USER hd
WORKDIR /data
VOLUME /data
ENV DATABASE_URL=sqlite+aiosqlite:////data/hd.db \
    DIGEST_DIR=/data \
    SITE_OUT_DIR=/data/site \
    PYTHONUNBUFFERED=1

COPY --chown=hd docker/scan-loop.sh /usr/local/bin/scan-loop
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["scan-loop"]
