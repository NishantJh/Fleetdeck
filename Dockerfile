FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY homelab ./homelab
RUN pip install --no-cache-dir .
ENV HOMELAB_CONFIG=/config/config.yaml HOMELAB_STATE=/data/state.db PORT=8080
EXPOSE 8080
CMD ["python", "-m", "homelab"]
