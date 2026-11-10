# syntax=docker/dockerfile:1
ARG PYTHON_BASE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY requirements.lock requirements-browser.lock pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install setuptools==84.2.1 wheel==0.48.0 \
    && /opt/venv/bin/pip install -r requirements.lock -r requirements-browser.lock
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps --no-build-isolation . \
    && /opt/venv/bin/pip check

FROM ${PYTHON_BASE} AS base
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
COPY --from=build /opt/venv /opt/venv
WORKDIR /opt/mobile
EXPOSE 8093

# Изолированный стенд использует тот же код шлюза и синтетические адаптеры.
FROM base AS fixture
COPY scripts/mobile_test_stand.py ./scripts/
COPY alembic.ini TOPIC.md ./
COPY migrations ./migrations
COPY schemas ./schemas
COPY prompts ./prompts
COPY config/app.example.json config/sources.example.json ./config/
USER 10001:10001
ENTRYPOINT ["python", "/opt/mobile/scripts/mobile_test_stand.py"]
CMD ["--runtime", "/fixture/runtime", "--port", "8093", "--container-bind"]

# Браузер нужен для штатной сверки MAX, VK и Пикабу с изолированным профилем.
FROM base AS gateway
RUN python -m playwright install --with-deps chrome chromium \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENTRYPOINT ["mobile-agent"]
CMD ["--root", "/runtime", "--port", "8093", "--container-bind"]
