ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base-python:3.12-alpine3.21

ARG BUILD_ARCH
ARG BUILD_VERSION=dev

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN mkdir -p /data

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
