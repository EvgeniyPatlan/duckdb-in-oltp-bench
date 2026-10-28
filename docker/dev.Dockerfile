FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /w
# Install dependencies from the project metadata; the repo is mounted over /w at run time.
COPY pyproject.toml ./
COPY dbench/ dbench/
RUN pip install --no-cache-dir -e '.[dev]'
