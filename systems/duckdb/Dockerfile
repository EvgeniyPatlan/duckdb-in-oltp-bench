# Plain DuckDB behind a JSON-lines TCP query server (see server.py).
#   Released version:   docker build --build-arg DUCKDB_VERSION=1.5.5 ...
#   From source commit: docker build --build-arg DUCKDB_VERSION=1.5.2-dev736 \
#                         --build-arg DUCKDB_SOURCE_COMMIT=<duckdb/duckdb sha> \
#                         --build-arg DUCKDB_PYTHON_TAG=v1.5.2 \
#                         --build-arg DUCKDB_GIT_DESCRIBE=v1.5.1-736-g8a6622311 ...
FROM python:3.12-slim
ARG DUCKDB_VERSION
ARG DUCKDB_SOURCE_COMMIT=""
ARG DUCKDB_PYTHON_TAG=""
ARG DUCKDB_GIT_DESCRIBE=""
ENV DBENCH_DUCKDB_VERSION=${DUCKDB_VERSION} DBENCH_DUCKDB_SOURCE_COMMIT=${DUCKDB_SOURCE_COMMIT}
RUN set -eux; \
    if [ -z "${DUCKDB_SOURCE_COMMIT}" ]; then \
        pip install --no-cache-dir "duckdb==${DUCKDB_VERSION}"; \
    else \
        apt-get update; \
        apt-get install -y --no-install-recommends build-essential cmake ninja-build git ca-certificates; \
        git clone --depth 1 --branch "${DUCKDB_PYTHON_TAG}" https://github.com/duckdb/duckdb-python /src; \
        git -C /src submodule update --init --depth 1 external/duckdb; \
        git -C /src/external/duckdb fetch --depth 1 origin "${DUCKDB_SOURCE_COMMIT}"; \
        git -C /src/external/duckdb checkout "${DUCKDB_SOURCE_COMMIT}"; \
        git -C /src -c user.name=build -c user.email=build@localhost commit --allow-empty -qm snapshot; \
        pip install --no-cache-dir -C "cmake.define.OVERRIDE_GIT_DESCRIBE=${DUCKDB_GIT_DESCRIBE}" /src; \
        apt-get purge -y build-essential cmake ninja-build git; \
        apt-get autoremove -y; \
        rm -rf /src /var/lib/apt/lists/*; \
    fi
COPY server.py /server.py
VOLUME /var/lib/duckdb
EXPOSE 9999
CMD ["python", "-u", "/server.py"]
