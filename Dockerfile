# syntax=docker/dockerfile:1.7
ARG NGINX_BASE=nginx:1.30.6-alpine@sha256:583b7f2fce1cdb8da5ad5c39ebd3c33b98b87d493190e548bdee1fa8c619c311
FROM ${NGINX_BASE} AS builder
ARG NGINX_VERSION=1.30.6
ARG NGINX_SHA256=3cf46f5ed7504d460125866b7bea99fb357f31d887e2e63b35503310e9b8b6c8
RUN apk add --no-cache build-base linux-headers openssl-dev pcre2-dev zlib-dev patch
WORKDIR /build
RUN curl -fsSLo nginx.tar.gz "https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" \
    && echo "${NGINX_SHA256}  nginx.tar.gz" | sha256sum -c - \
    && tar -xzf nginx.tar.gz --strip-components=1
COPY patches/nginx-http2-16k.patch /build/http2.patch
RUN patch --dry-run --fuzz=0 -p1 < http2.patch \
    && patch --fuzz=0 -p1 < http2.patch \
    && configure="$(nginx -V 2>&1 | sed -n 's/^configure arguments: //p')" \
    && test -n "$configure" \
    && eval "./configure $configure" \
    && make -j2 \
    && strip objs/nginx \
    && cp objs/nginx /build/nginx-release \
    && configure="$(nginx-debug -V 2>&1 | sed -n 's/^configure arguments: //p')" \
    && test -n "$configure" \
    && make clean \
    && eval "./configure $configure" \
    && make -j2 \
    && strip objs/nginx \
    && cp objs/nginx /build/nginx-debug

FROM ${NGINX_BASE} AS runtime
ARG SOURCE_URL
ARG REVISION
LABEL org.opencontainers.image.title="Cresset Nginx HTTP/2 16 KiB" \
      org.opencontainers.image.version="1.30.6-16k-r1" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${REVISION}"
COPY --from=builder /build/nginx-release /usr/sbin/nginx
COPY --from=builder /build/nginx-debug /usr/sbin/nginx-debug
RUN nginx -t && nginx-debug -t
