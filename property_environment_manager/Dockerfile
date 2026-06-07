ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.21
FROM ${BUILD_FROM}

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
