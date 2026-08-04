# Build the React frontend.
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# Run FastAPI and serve the compiled frontend from the same container.
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend
COPY scripts ./scripts
COPY --from=frontend-build /app/frontend/dist ./frontend-dist
RUN pip install --no-cache-dir .

ENV API_HOST=0.0.0.0
ENV API_PORT=8000
ENV DATABASE_PATH=/app/data/app.sqlite3
ENV DATA_MANIFEST_PATH=/app/data/data_manifest.json
ENV DATA_REFRESH_ENABLED=false
ENV DATA_REFRESH_INTERVAL_DAYS=14
ENV DATA_REFRESH_ON_STARTUP=false

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
