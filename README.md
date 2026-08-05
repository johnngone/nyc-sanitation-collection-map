# NYC Sanitation Map

Interactive NYC sanitation collection map. The published standalone image contains the compiled React frontend and FastAPI backend, backed by SQLite.

## Standalone architecture

FastAPI serves both applications on container port `8000`:

```text
/              -> React frontend
/api/health    -> FastAPI health endpoint
/api/...       -> FastAPI API endpoints
```

There is no separate frontend container, Nginx port, or backend host port in the normal deployment. The frontend uses same-origin relative `/api/...` requests.

## Ports

| Item | Value | Meaning |
|---|---:|---|
| FastAPI bind address | `0.0.0.0` | All container interfaces; not a browser URL |
| Container port | `8000` | Fixed port serving frontend and API |
| Default host port | `8080` | User-selectable browser-facing port |
| Docker mapping | `8080:8000` | `HOST_PORT:CONTAINER_PORT` |
| Vite | `5173` | Development only |
| Nginx | `80` | Separate frontend image only |

`-p 8080:8000` means visit `http://SERVER-IP:8080`.

`-p 9090:8000` means visit `http://SERVER-IP:9090`.

The left number is selectable; the right number remains `8000`. `0.0.0.0` is a bind address, not a URL. `127.0.0.1` means the current computer; when Docker runs on another computer or unRAID, use that server's LAN IP. `EXPOSE 8000` is image metadata and does not publish a port by itself.

## Deploy the published GHCR image

Choose a persistent host directory, such as `/opt/nyc-sanitation-map/data` on Linux, then run:

```bash
mkdir -p /opt/nyc-sanitation-map/data
docker pull ghcr.io/johnngone/nyc-sanitation-collection-map:latest
docker run -d --name nyc-sanitation-map --restart unless-stopped \
  -p 8080:8000 \
  -v /opt/nyc-sanitation-map/data:/app/data \
  ghcr.io/johnngone/nyc-sanitation-collection-map:latest
```

Open `http://SERVER-IP:8080`; verify at `http://SERVER-IP:8080/api/health`.

## Deploy using Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

`APP_HOST_PORT=8080` controls only the browser-facing host port. Set `APP_HOST_PORT=9090` to use `http://SERVER-IP:9090`; the container remains on port 8000.

```bash
docker compose ps
docker compose logs -f app
docker compose down
```

Compose mounts `./data:/app/data`, so the SQLite database and manifest persist. A refresh worker, if run separately, is a data job and publishes no ports.

## unRAID

Use one container template:

```text
Repository: ghcr.io/johnngone/nyc-sanitation-collection-map:latest
Network type: Bridge
Container port: 8000
Host port: 8080 (or another unused host port)
Protocol: TCP
Container data path: /app/data
Host data path: an appropriate persistent appdata directory
```

The WebUI is `http://UNRAID-IP:HOST-PORT`, for example `http://192.168.1.50:8080`. Create only this one web mapping; do not separately map 80, 5173, or another host port for container 8000.

## Verify and troubleshoot

```bash
curl http://SERVER-IP:8080/api/health
docker logs nyc-sanitation-map
docker ps
```

The health response is JSON containing `"status":"ok"`. Connection failures usually mean a wrong host IP, missing mapping, port conflict, firewall, or stopped container. If the frontend loads but says the backend is unavailable, check `/api/health` and logs. Zero processed records means the backend is connected but the SQLite database has no processed data. For “port already allocated”, change only the left side, such as `9090:8000`.

## Updating the image

```bash
docker compose pull
docker compose up -d
```

For a direct Docker deployment, pull the image and recreate the container with the same `/app/data` bind mount. Recreating the container does not remove data stored in the host-mounted directory.

## Local development

FastAPI development uses port 8000 and Vite uses port 5173. Production does not need two published ports. The separate `frontend/Dockerfile` and `backend/Dockerfile` are development/legacy options; the root `Dockerfile` and combined GHCR image are authoritative.

```bash
cd backend && python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
cd frontend && npm ci && npm run dev
```

## Tests

```bash
python -m pytest
cd frontend && npm ci && npm run build
docker build -f Dockerfile -t nyc-sanitation-map:test .
```

GitHub Actions tests the combined image before publishing `ghcr.io/johnngone/nyc-sanitation-collection-map:latest` and the commit-SHA tag.

