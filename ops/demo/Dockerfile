# Build the actual KoalaTech frontend first: docker build -t koalatech-base frontend
ARG BASE_IMAGE=koalatech-base
FROM ${BASE_IMAGE}
ARG MODE=healthy
ARG RELEASE=baseline
COPY ${MODE}.conf /etc/nginx/conf.d/default.conf
RUN printf '%s\n' "$RELEASE" > /usr/share/nginx/html/release.txt
