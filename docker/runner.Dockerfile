FROM python:3.12-slim
WORKDIR /w
COPY pyproject.toml ./
COPY dbench dbench
RUN pip install --no-cache-dir .
ENTRYPOINT ["dbench"]
