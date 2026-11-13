FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install -c requirements.lock . && useradd --create-home --uid 10001 factory
USER factory
ENV HOME=/home/factory
RUN python -c "from copilot._cli_download import download_cli; from ai_factory.contracts import CLI_VERSION; download_cli(CLI_VERSION)"
ENTRYPOINT ["python", "-m", "ai_factory.job"]
