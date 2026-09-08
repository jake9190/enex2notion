FROM python:3.12-slim-bookworm AS build

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip

FROM build AS build-venv

COPY . /app
WORKDIR /app

RUN /venv/bin/pip wheel --disable-pip-version-check --no-deps --wheel-dir dist .
RUN /venv/bin/pip install --disable-pip-version-check -r requirements.txt && \
    /venv/bin/pip install --disable-pip-version-check --no-deps dist/*.whl

FROM python:3.12-slim-bookworm

ENV INSIDE_DOCKER_CONTAINER=1

LABEL   maintainer="vzhd1701 <vzhd1701@gmail.com>" \
        org.opencontainers.image.title="enex2notion" \
        org.opencontainers.image.description="Import Evernote ENEX files to Notion " \
        org.opencontainers.image.authors="vzhd1701 <vzhd1701@gmail.com>" \
        org.opencontainers.image.licenses="MIT" \
        org.opencontainers.image.documentation="https://github.com/vzhd1701/enex2notion" \
        org.opencontainers.image.url="https://github.com/vzhd1701/enex2notion" \
        org.opencontainers.image.source="https://github.com/vzhd1701/enex2notion.git"

COPY --from=build-venv /venv /venv

WORKDIR /input

ENTRYPOINT ["/venv/bin/enex2notion"]
