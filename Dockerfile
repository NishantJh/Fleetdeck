FROM python:3.12-slim
WORKDIR /app
# Exact, hash-checked dependencies (regenerate with the command at the top of requirements.lock).
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY homelab ./homelab
RUN pip install --no-cache-dir --no-deps . \
 && groupadd --system --gid 10001 homelab \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin homelab
# The dashboard holds root keys to the lab; it doesn't need to be root itself. Files in /config
# must be readable by uid 10001 (see README).
USER 10001:10001
ENV HOMELAB_CONFIG=/config/config.yaml PORT=8080
EXPOSE 8080
CMD ["python", "-m", "homelab"]
