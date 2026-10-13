# syntax=docker/dockerfile:1

# Stage 1: prepare the site. Runs natively on the build host (the work is
# architecture-independent) and produces layers whose tar archives are
# byte-identical on every runner, so the amd64 and arm64 images share blobs.
FROM --platform=$BUILDPLATFORM docker.io/debian:13 AS build

# Commit time, supplied by CI. Pages get this mtime, so Last-Modified/ETag
# change whenever the site is republished.
ARG SOURCE_DATE_EPOCH=1383783383
# Media (~1.8 GB) get a fixed mtime - the last Wayback capture used
# (2013-11-07T00:16:23Z) - so their layers only change when a file changes.
# A re-encoded recording or slide deck changes size, which still updates the ETag.
ARG MEDIA_DATE_EPOCH=1383783383

RUN --mount=type=bind,target=/ctx set -eux; \
    mkdir -p /app/site /app/media; \
    cp -a /ctx/. /app/site/; \
    mv /app/site/videos /app/site/slides /app/media/; \
    ln -s /videos /app/site/videos; \
    ln -s /slides /app/site/slides; \
    find /app/site -type f \( -name "*.html" -o -name "*.css" -o -name "*.js" -o -name "*.xml" -o -name "*.json" -o -name "*.svg" -o -name "*.ttf" -o -name "*.woff2" -o -name "*.woff" -o -name "*.eot" -o -name "*.otf" \) -print0 | xargs -0 -P4 --no-run-if-empty gzip -9kn; \
    find /app/media -exec touch -h -d "@${MEDIA_DATE_EPOCH}" {} +; \
    find /app/site -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

FROM ghcr.io/nginxinc/nginx-unprivileged:stable-alpine AS webserver

RUN echo "absolute_redirect off;" >/etc/nginx/conf.d/no-absolute_redirect.conf
RUN echo "gzip_static on; gzip_proxied any;" >/etc/nginx/conf.d/gzip_static.conf
# brotli_static not yet available in standard nginx distribution
# RUN echo "brotli_static on; brotli_proxied any;" >/etc/nginx/conf.d/brotli_static.conf

# Content is copied into top-level directories (largest and least volatile
# first): a layer's archive records every parent directory it modifies except
# the root, so this is what keeps each layer dependent on nothing but its own
# files. --link additionally decouples the layers from the base image.
COPY --link --from=build /app/media/videos /videos
COPY --link --from=build /app/media/slides /slides
COPY --link --from=build /app/site /site

# Serve /site (which links videos/ and slides/ to the directories above) from
# nginx's default document root.
ARG SOURCE_DATE_EPOCH=1383783383
USER root
RUN rm -rf /usr/share/nginx/html && ln -s /site /usr/share/nginx/html \
    && touch -h -d "@${SOURCE_DATE_EPOCH}" /usr/share/nginx/html /usr/share/nginx
USER nginx

# Test configuration during docker build
RUN nginx -t

# Port the container will listen on
EXPOSE 8080
