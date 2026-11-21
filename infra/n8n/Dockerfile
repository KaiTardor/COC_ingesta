ARG ALPINE_VERSION=3.24
FROM alpine:${ALPINE_VERSION} AS alpine_tools
RUN apk add --no-cache apk-tools-static \
    && cp "$(command -v apk.static)" /tmp/apk.static

FROM docker.n8n.io/n8nio/n8n:stable
ARG ALPINE_VERSION
USER root
COPY --from=alpine_tools /tmp/apk.static /tmp/apk.static
COPY --from=alpine_tools /etc/apk/keys/ /tmp/coc-apk-keys/
RUN printf '%s\n' \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/main" \
      "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION}/community" \
      > /tmp/coc-apk-repositories \
    && /tmp/apk.static --keys-dir /tmp/coc-apk-keys \
      --repositories-file /tmp/coc-apk-repositories \
      add --no-cache python3 py3-requests \
    && rm -r /tmp/apk.static /tmp/coc-apk-keys /tmp/coc-apk-repositories
RUN python3 -m venv --system-site-packages /opt/coc-python \
    && SNOWFLAKE_DISABLE_COMPILE_ARROW_EXTENSIONS=1 \
       /opt/coc-python/bin/python -m pip install --no-cache-dir \
       "snowflake-connector-python==4.7.4" \
    && /opt/coc-python/bin/python -c 'import requests, snowflake.connector'

ENV PATH="/opt/coc-python/bin:${PATH}"
USER node
