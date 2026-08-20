# Build the React frontend.
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ENV VITE_API_BASE_URL=
RUN npm run build

# Run FastAPI and serve the compiled frontend from the same container.
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY scripts ./scripts
COPY --from=frontend-build /app/frontend/dist ./frontend-dist
RUN pip install --no-cache-dir ".[refresh]" \
    && chmod +x /app/scripts/container_entrypoint.sh
RUN mkdir -p /app/data

ENV DATABASE_PATH=/app/data/app.sqlite3
ENV TILESET_PATH=/app/data/collection_streets.mbtiles
ENV DATA_MANIFEST_PATH=/app/data/data_manifest.json
ENV DATA_REFRESH_ENABLED=true
ENV DATA_REFRESH_INTERVAL_DAYS=14
ENV DATA_REFRESH_ON_STARTUP=true
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"]
CMD ["/app/scripts/container_entrypoint.sh"]
