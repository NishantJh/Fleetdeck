# wireproxy: a userspace WireGuard client, for tunnels to machines the dashboard can't reach
# directly (fleetdeck/tunnels.py). Built from source at a pinned version; the Go module proxy and
# checksum database verify what is fetched.
# Base images come from a Docker Hub mirror (Docker Hub rate-limits CI pulls with 429). Override with
# --build-arg BASE_REGISTRY=docker.io/library to pull from Docker Hub directly.
ARG BASE_REGISTRY=mirror.gcr.io/library

FROM ${BASE_REGISTRY}/golang:1.26-alpine AS wireproxy
RUN CGO_ENABLED=0 go install -trimpath -ldflags="-s -w" github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3

FROM ${BASE_REGISTRY}/python:3.12-slim
COPY --from=wireproxy /go/bin/wireproxy /usr/local/bin/wireproxy
WORKDIR /app
# Exact, hash-checked dependencies (regenerate with the command at the top of requirements.txt).
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY pyproject.toml ./
COPY fleetdeck ./fleetdeck
RUN pip install --no-cache-dir --no-deps . \
 && groupadd --system --gid 10001 homelab \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin homelab \
 && mkdir /data && chown 10001:10001 /data
# Everything the app keeps (config.yaml, its SSH key, known_hosts, certificates, history) is in
# /data, written by the setup page. The process starts as root only to run as the owner of /data
# (uid 10001 if root owns it; PUID/PGID to choose), then switches to that user with no
# capabilities left. No chown is ever needed on the host.
# Defaults are set under the old HOMELAB_* names on purpose: FLEETDECK_* wins when both are set, so
# a user's FLEETDECK_* or older HOMELAB_* override still takes effect instead of being shadowed.
ENV HOMELAB_DATA=/data HOMELAB_CONFIG=/data/config.yaml HOMELAB_STATE=/data/state.db PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "fleetdeck"]
