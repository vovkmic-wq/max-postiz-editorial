ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM ${PYTHON_BASE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/chromium HOME=/home/monitor
WORKDIR /opt/monitor
COPY requirements.lock requirements-browser.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock -r requirements-browser.lock \
    && pip install --no-cache-dir setuptools==84.2.1 wheel==0.48.0
RUN python -m playwright install --with-deps --no-shell chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10460 monitor \
    && useradd --uid 10460 --gid 10460 --create-home monitor
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

FROM base AS runtime
COPY deployment/monitor/runner.py deployment/monitor/source_sampler.py ./
USER 10460:10460
ENTRYPOINT ["python", "-B", "/opt/monitor/runner.py"]
CMD ["run"]

FROM base AS login
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc x11-utils novnc websockify \
    && rm -rf /var/lib/apt/lists/*
COPY deployment/monitor/runner.py deployment/monitor/source_sampler.py deployment/monitor/login.sh ./
USER 10460:10460
