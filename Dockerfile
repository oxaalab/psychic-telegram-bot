FROM python:3.12-slim AS base
ARG DEBIAN_FRONTEND=noninteractive

RUN addgroup --system app && adduser --system --ingroup app app \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

FROM python:3.12-slim AS builder
ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && ln -sf /opt/poetry/bin/poetry /usr/local/bin/poetry \
    && poetry self add "poetry-plugin-export>=1.8"

WORKDIR /tmp/build
COPY pyproject.toml poetry.lock* ./

RUN set -eux; \
    if [ ! -f poetry.lock ]; then poetry lock; fi; \
    poetry export -f requirements.txt --without-hashes -o /tmp/requirements.txt

FROM base AS runtime

COPY --from=builder /tmp/requirements.txt /requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /requirements.txt

COPY . /usr/src/app
RUN chmod +x entrypoint.sh && chown -R app:app /usr/src/app

USER app
EXPOSE 50042
ENTRYPOINT ["/usr/src/app/entrypoint.sh"]
