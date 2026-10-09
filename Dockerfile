# wireproxy: a userspace WireGuard client, for tunnels to machines the dashboard can't reach
# directly (homelab/tunnels.py). Built from source at a pinned version; the Go module proxy and
# checksum database verify what is fetched.
FROM golang:1.26-alpine AS wireproxy
RUN CGO_ENABLED=0 go install -trimpath -ldflags="-s -w" github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3

FROM python:3.12-slim
COPY --from=wireproxy /go/bin/wireproxy /usr/local/bin/wireproxy
WORKDIR /app
# Exact, hash-checked dependencies (regenerate with the command at the top of requirements.txt).
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY pyproject.toml ./
COPY homelab ./homelab
RUN pip install --no-cache-dir --no-deps . \
 && groupadd --system --gid 10001 homelab \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin homelab \
 && mkdir /data && chown 10001:10001 /data
# Everything the app keeps (config.yaml, its SSH key, known_hosts, certificates, history) is in
# /data, written by the setup page. The process starts as root only to run as the owner of /data
# (uid 10001 if root owns it; PUID/PGID to choose), then switches to that user with no
# capabilities left. No chown is ever needed on the host.
ENV HOMELAB_DATA=/data HOMELAB_CONFIG=/data/config.yaml HOMELAB_STATE=/data/state.db PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "homelab"]
