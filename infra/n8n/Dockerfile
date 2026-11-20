ARG ALPINE_VERSION
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
USER node
