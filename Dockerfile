FROM oven/bun:1.3.0 AS bun

FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV REFLEX_USE_SYSTEM_BUN=1
ENV REFLEX_USE_SYSTEM_NODE=1

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends curl nodejs npm unzip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/scripts/docker_app_entrypoint.sh

EXPOSE 3000 8000

CMD ["/app/scripts/docker_app_entrypoint.sh"]
