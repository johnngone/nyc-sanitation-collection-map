# NYC Sanitation Map

Interactive map of NYC sanitation collection schedules. The citywide dataset is generated from official NYC DSNY frequency polygons and NYC DCP LION street centerlines, then served from SQLite through FastAPI and rendered with MapLibre.

## Requirements

- Python 3.11+
- Node.js 20+
- npm

## Run the backend

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e "..[test]"
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

The health endpoint is available at http://127.0.0.1:8000/api/health. The map API serves only processed records loaded into the local SQLite database; it does not treat an empty database as proof that no collection exists.

## Run the frontend

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. The page currently displays an empty NYC map and reports backend connectivity. Data research begins only after Phase 1 is verified.

## Build the pilot data

Do not start with a citywide batch. Download official source exports, inspect their schemas, then build a small pilot:

```powershell
cd C:\Users\thing\Desktop\playground\nyc-sanitation-map
.\backend\.venv\Scripts\Activate.ps1
python scripts\inspect_official_sources.py
python scripts\build_pilot.py --lion data\raw\lion.geojson --frequencies data\raw\dsny_frequencies.geojson --limit 100
python scripts\load_processed.py data\processed\pilot.geojson
```

The LION input must be converted to GeoJSON by the operator from the official File Geodatabase export. The pilot script requires an explicit `FREQ_REFUSE`/`refuse_days` field and writes failures to `output/pilot_failures.jsonl`.

## Container deployment

```powershell
docker compose build
docker compose up -d
```

Open http://127.0.0.1:8080. The SQLite database persists in `data/app.sqlite3`. For production, place Nginx or Caddy in front of the frontend container; the included deployment remains self-contained with SQLite.

The standalone deployment uses no external database. Configure ports and refresh behavior in `.env`:

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose logs -f
```

The default source refresh interval is 14 days. Change `DATA_REFRESH_INTERVAL_DAYS`, or disable it with `DATA_REFRESH_ENABLED=false`. The refresh worker stages a new SQLite database and only promotes it after integrity checks. Back up `data/app.sqlite3` and `data/data_manifest.json` before upgrades.

To build images directly without publishing them:

```bash
docker build -f backend/Dockerfile -t nyc-sanitation-backend:local .
docker build -f frontend/Dockerfile -t nyc-sanitation-frontend:local .
```

To run a citywide refresh manually:

```bash
python scripts/run_refresh.py --allow-large-run
```

The command writes a staged database, GeoJSON, failure log, and manifest. Use `scripts/promote_staging.py` to promote a completed staging directory after validation.

The `Data updated` value in the map comes from `data/data_manifest.json`. The information button explains the source-to-browser data flow and identifies missing or unvalidated records.

## GitHub preparation

The repository includes CI configuration for backend tests, frontend compilation, and Docker image builds. It does not publish images or contact GitHub. Generated databases, raw downloads, virtual environments, logs, and frontend dependencies are excluded by `.gitignore`.

## Tests

```powershell
cd backend
python -m pytest
```
