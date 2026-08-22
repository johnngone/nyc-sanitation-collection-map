import { useEffect, useRef, useState } from "react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";

import { resolveApiUrl } from "./apiUrl";

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;
type Weekday = (typeof weekdays)[number];
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const sourceId = "collection-streets";
const collectionTypes = [
  ["REFUSE", "Refuse", "#111111", "refuse_days", 0],
  ["RECYCLING", "Recycling", "#1479d1", "recycling_days", 6],
  ["ORGANICS", "Organics", "#8b4a22", "organics_days", 9],
  ["BULK", "Bulk", "#7a3db8", "bulk_days", 3],
] as const;
type CollectionType = (typeof collectionTypes)[number][0];
type CollectionDefinition = (typeof collectionTypes)[number];
const collectionLayerIds = collectionTypes.flatMap(([type]) => [lowZoomLayerId(type), highZoomLayerId(type)]);

interface MapConfig {
  available: boolean;
  tile_schema_revision: number | null;
  version: string | null;
  tiles_url: string | null;
  source_layer: string;
  minzoom: number | null;
  maxzoom: number | null;
  bounds: [number, number, number, number] | null;
  data_updated: string | null;
}

function dayCode(day: Weekday): string {
  return day.slice(0, 3).toUpperCase();
}

function dayFromCode(code: string | null): Weekday {
  return weekdays.find((day) => dayCode(day) === code) ?? "Monday";
}

export function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const selectedDayRef = useRef<Weekday>(dayFromCode(new URLSearchParams(window.location.search).get("day")));
  const selectedTypesRef = useRef<CollectionType[]>(["REFUSE"]);
  const updateTileStatusRef = useRef<(() => void) | null>(null);
  const tileErrorRef = useRef<string | null>(null);
  const [selectedDay, setSelectedDay] = useState<Weekday>(selectedDayRef.current);
  const [backendStatus, setBackendStatus] = useState("Checking backend…");
  const [mapStatus, setMapStatus] = useState("Loading map tiles…");
  const [selectedTypes, setSelectedTypes] = useState<CollectionType[]>(selectedTypesRef.current);
  const [dataUpdated, setDataUpdated] = useState<string | null>(null);
  const [showInfo, setShowInfo] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let retryTimer: number | undefined;
    let retryDelayMs = 500;
    const checkHealth = () => {
      if (controller.signal.aborted) return;
      void fetch(apiUrl("/api/health"), { signal: controller.signal })
        .then((response) => {
          if (!response.ok) {
            const error = new Error(`Backend returned HTTP ${response.status}`);
            Object.assign(error, { status: response.status });
            throw error;
          }
          return response.json();
        })
        .then((payload: unknown) => {
          if (controller.signal.aborted) return;
          if (!isValidHealthPayload(payload)) {
            throw new Error("Backend returned an invalid health payload");
          }
          retryDelayMs = 500;
          setBackendStatus(`Backend connected · ${payload.processed_records} mapped street features`);
          setDataUpdated(payload.data_updated ?? null);
        })
        .catch((error: unknown) => {
          if (isAbortError(error) || controller.signal.aborted) return;
          console.error("Backend health check failed", error);
          const status = error instanceof Error && "status" in error ? error.status : undefined;
          setBackendStatus(status === 503 ? "Backend verifying data; retrying…" : "Backend unavailable; retrying…");
          window.clearTimeout(retryTimer);
          retryTimer = window.setTimeout(checkHealth, retryDelayMs);
          retryDelayMs = Math.min(retryDelayMs * 2, 15_000);
        });
    };
    checkHealth();
    return () => {
      controller.abort();
      window.clearTimeout(retryTimer);
    };
  }, []);

  useEffect(() => {
    if (!mapNode.current) return;

    const controller = new AbortController();
    let configRetryTimer: number | undefined;
    let configRetryDelayMs = 500;
    let tilesInitialized = false;
    const map = new maplibregl.Map({
      container: mapNode.current,
      center: [-73.95, 40.72],
      zoom: 13,
      style: {
        version: 8,
        sources: {
          "carto-raster": {
            type: "raster",
            tileSize: 256,
            tiles: [
              "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
              "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
              "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
              "https://d.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            ],
            attribution: "© OpenStreetMap contributors © CARTO",
          },
        },
        layers: [{ id: "carto-raster-layer", type: "raster", source: "carto-raster" }],
      },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl(), "bottom-right");

    map.once("style.load", () => {
      const loadMapTiles = () => {
        if (controller.signal.aborted || tilesInitialized) return;
        const scheduleRetry = () => {
          window.clearTimeout(configRetryTimer);
          configRetryTimer = window.setTimeout(loadMapTiles, configRetryDelayMs);
          configRetryDelayMs = Math.min(configRetryDelayMs * 2, 15_000);
        };
        void fetchMapConfig(controller.signal)
        .then((config) => {
          if (controller.signal.aborted) return;
          if (config.data_updated) setDataUpdated(config.data_updated);
          if (!config.available) {
            setMapStatus("Map tiles are not ready yet; retrying…");
            scheduleRetry();
            return;
          }
          if (
            !config.tiles_url
            || !config.version
            || !config.source_layer
            || config.tile_schema_revision !== 2
            || typeof config.minzoom !== "number"
            || typeof config.maxzoom !== "number"
            || !Number.isInteger(config.minzoom)
            || !Number.isInteger(config.maxzoom)
            || config.minzoom < 0
            || config.maxzoom < config.minzoom
            || config.maxzoom > 30
            || !isValidBounds(config.bounds)
          ) {
            console.error("The backend returned an invalid map tile configuration", config);
            setMapStatus("Map tiles need a compatible refresh; retrying…");
            scheduleRetry();
            return;
          }

          tilesInitialized = true;
          // Below the archive's minimum zoom the collection overlay would be
          // blank while the legend still looked authoritative. Keep the map in
          // the range where a validated collection layer can be rendered.
          map.setMinZoom(config.minzoom);
          map.addSource(sourceId, {
            type: "vector",
            tiles: [apiUrl(config.tiles_url)],
            minzoom: config.minzoom,
            maxzoom: config.maxzoom,
            bounds: config.bounds,
          });
          for (const [type, , color, daysProperty, offset] of collectionTypes) {
            const filter = makeDayFilter(selectedDayRef.current, daysProperty);
            const visibility = selectedTypesRef.current.includes(type) ? "visible" : "none";
            const lineOffset: maplibregl.ExpressionSpecification = [
              "*",
              ["match", ["get", "side"], "LEFT", -1, "RIGHT", 1, 0],
              offset,
            ];
            map.addLayer({
              id: lowZoomLayerId(type),
              type: "line",
              source: sourceId,
              "source-layer": config.source_layer,
              maxzoom: 13,
              filter,
              layout: { visibility },
              paint: {
                "line-color": color,
                "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.5, 13, 3],
                "line-opacity": 0.9,
                "line-offset": lineOffset,
              },
            });
            map.addLayer({
              id: highZoomLayerId(type),
              type: "line",
              source: sourceId,
              "source-layer": config.source_layer,
              minzoom: 13,
              filter,
              layout: { visibility },
              paint: {
                "line-color": color,
                "line-color-transition": { duration: 0 },
                "line-width": ["interpolate", ["linear"], ["zoom"], 9, 2, 14, 5],
                "line-opacity": 0.9,
                "line-offset": lineOffset,
              },
            });
          }

          const updateTileStatus = () => {
            if (!hasCollectionLayers(map)) return;
            if (!selectedTypesRef.current.length) {
              setMapStatus("No collection types selected");
            } else if (tileErrorRef.current) {
              setMapStatus(`Map tile failed: ${tileErrorRef.current}`);
            } else {
              setMapStatus(`Map tiles loaded for ${selectedDayRef.current}`);
            }
          };
          updateTileStatusRef.current = updateTileStatus;

          const showPopup = (event: maplibregl.MapLayerMouseEvent) => {
            const feature = event.features?.[0];
            if (!feature?.properties) return;
            const collection = collectionForLayerId(feature.layer.id);
            if (!collection) return;
            const [, label, , daysProperty] = collection;
            const properties = feature.properties;
            const collectionDays = properties[daysProperty] ?? "Unknown";
            const blockFaceId = properties.origin_block_face_id ?? properties.source_block_face_id ?? properties.id;
            const metadata = [
              blockFaceId ? `Block face: ${escapeHtml(blockFaceId)}` : null,
              properties.source ? `Source: ${escapeHtml(properties.source)}` : null,
              properties.retrieved_at ? `Retrieved: ${escapeHtml(properties.retrieved_at)}` : null,
            ].filter((value): value is string => value !== null);
            const metadataHtml = metadata.length ? `<br /><small>${metadata.join("<br />")}</small>` : "";
            new maplibregl.Popup()
              .setLngLat(event.lngLat)
              .setHTML(`<strong>${escapeHtml(properties.street_name ?? properties.name ?? "Unnamed street")}</strong><br />${escapeHtml(properties.borough ?? "Unknown borough")} · ${escapeHtml(properties.side ?? "Unknown side")}<br /><br /><strong>${escapeHtml(label)}:</strong> ${escapeHtml(collectionDays)}${metadataHtml}`)
              .addTo(map);
          };
          map.on("click", collectionLayerIds, showPopup);
          map.on("mouseenter", collectionLayerIds, () => { map.getCanvas().style.cursor = "pointer"; });
          map.on("mouseleave", collectionLayerIds, () => { map.getCanvas().style.cursor = ""; });

          map.on("movestart", () => {
            tileErrorRef.current = null;
            if (selectedTypesRef.current.length) setMapStatus(`Loading ${selectedDayRef.current} map tiles…`);
          });
          map.on("sourcedataloading", (event) => {
            if (event.sourceId === sourceId && selectedTypesRef.current.length) {
              setMapStatus(`Loading ${selectedDayRef.current} map tiles…`);
            }
          });
          map.on("idle", updateTileStatus);
          map.on("error", (event) => {
            const tileEvent = event as typeof event & { sourceId?: string };
            if (tileEvent.sourceId !== sourceId) return;
            tileErrorRef.current = mapErrorMessage(event.error);
            console.error("Collection map tile failed to load", event.error);
            setMapStatus(`Map tile failed: ${tileErrorRef.current}`);
          });

          setMapStatus(`Loading ${selectedDayRef.current} map tiles…`);
        })
        .catch((error: unknown) => {
          if (isAbortError(error) || controller.signal.aborted) return;
          console.error("Map tile initialization failed", error);
          if (tilesInitialized) {
            setMapStatus("Map tiles are unavailable; see server logs");
            return;
          }
          setMapStatus("Map tile configuration is unavailable; retrying…");
          scheduleRetry();
        });
      };
      loadMapTiles();
    });

    return () => {
      controller.abort();
      window.clearTimeout(configRetryTimer);
      updateTileStatusRef.current = null;
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    selectedDayRef.current = selectedDay;
    selectedTypesRef.current = selectedTypes;
    tileErrorRef.current = null;

    const map = mapRef.current;
    if (!map || !hasCollectionLayers(map)) return;
    updateCollectionLayers(map, selectedDay, selectedTypes);
    if (!selectedTypes.length) {
      setMapStatus("No collection types selected");
      return;
    }

    setMapStatus(`Showing ${selectedDay} collection data`);
    const animationFrame = window.requestAnimationFrame(() => updateTileStatusRef.current?.());
    return () => window.cancelAnimationFrame(animationFrame);
  }, [selectedDay, selectedTypes]);

  function selectDay(day: Weekday) {
    setSelectedDay(day);
    const url = new URL(window.location.href);
    url.searchParams.set("day", dayCode(day));
    window.history.replaceState({}, "", url);
  }

  return (
    <main className="app-shell">
      <section className="map-panel" aria-label="NYC sanitation collection map">
        <div ref={mapNode} className="map" />
        <aside className="map-overlay">
          <div className="title-row"><strong>NYC Sanitation - Visual Collection Schedule</strong><button className="info-button" type="button" aria-label="About this data" onClick={() => setShowInfo(true)}>i</button></div>
          <small>Data updated: {dataUpdated ? new Date(dataUpdated).toLocaleDateString() : "Not available"}</small>
          <div className="day-picker" aria-label="Collection day">{weekdays.map((day) => <button key={day} type="button" className={selectedDay === day ? "day-button selected" : "day-button"} onClick={() => selectDay(day)} aria-pressed={selectedDay === day}>{dayCode(day)[0]}</button>)}</div>
          <div className="type-filters">{collectionTypes.map(([type, label, color]) => <label key={type}><input type="checkbox" checked={selectedTypes.includes(type)} onChange={() => setSelectedTypes((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type])} /><i className="swatch" style={{ backgroundColor: color }} />{label}</label>)}</div>
          <span><i className="swatch unavailable" /> Zoom in for full street detail; no line there means no validated schedule match</span>
          <small>{mapStatus}</small>
          <small>{selectedTypes.length ? "Vector tiles load on demand for the current view" : "Select a collection type to show data"}</small>
          <small>{backendStatus}</small>
        </aside>
      </section>
      {showInfo && <div className="modal-backdrop" role="presentation" onClick={() => setShowInfo(false)}><section className="info-modal" role="dialog" aria-modal="true" aria-labelledby="data-info-title" onClick={(event) => event.stopPropagation()}><button className="modal-close" type="button" aria-label="Close information" onClick={() => setShowInfo(false)}>×</button><h2 id="data-info-title">About this map</h2><p>Official NYC Department of Sanitation frequency data supplies collection schedules. NYC Department of City Planning LION data supplies street centerlines and separate left/right block-face identifiers.</p><p>The sources are downloaded, schema-checked, reprojected, spatially joined, and normalized into weekday schedules. Validated results are stored in a local SQLite database.</p><p>The backend publishes versioned vector tiles, and MapLibre loads only the small tiles needed for the current view. Day and collection-type controls filter those tiles directly in the browser.</p><p>Missing, unmatched, or unvalidated source records are reported during processing and are not treated as proof that no collection exists.</p></section></div>}
    </main>
  );
}

async function fetchMapConfig(signal: AbortSignal): Promise<MapConfig> {
  const response = await fetch(apiUrl("/api/map-config"), { signal });
  if (!response.ok) throw new Error(`Map configuration request failed with HTTP ${response.status}`);
  const payload: unknown = await response.json();
  if (!payload || typeof payload !== "object" || typeof (payload as Partial<MapConfig>).available !== "boolean") {
    throw new Error("The backend returned an invalid map tile configuration");
  }
  return payload as MapConfig;
}

function lowZoomLayerId(type: CollectionType): string {
  return `${sourceId}-${type.toLowerCase()}-lowzoom`;
}

function highZoomLayerId(type: CollectionType): string {
  return `${sourceId}-${type.toLowerCase()}-line`;
}

function makeDayFilter(day: Weekday, daysProperty: CollectionDefinition[3]): maplibregl.FilterSpecification {
  return ["in", dayCode(day), ["split", ["coalesce", ["get", daysProperty], ""], ","]];
}

function hasCollectionLayers(map: MapLibreMap): boolean {
  return collectionLayerIds.every((id) => Boolean(map.getLayer(id)));
}

function updateCollectionLayers(map: MapLibreMap, day: Weekday, selectedTypes: CollectionType[]): void {
  for (const [type, , , daysProperty] of collectionTypes) {
    const filter = makeDayFilter(day, daysProperty);
    const visibility = selectedTypes.includes(type) ? "visible" : "none";
    for (const id of [lowZoomLayerId(type), highZoomLayerId(type)]) {
      map.setFilter(id, filter);
      map.setLayoutProperty(id, "visibility", visibility);
    }
  }
}

function collectionForLayerId(id: string): CollectionDefinition | undefined {
  return collectionTypes.find(([type]) => id === lowZoomLayerId(type) || id === highZoomLayerId(type));
}

function apiUrl(path: string): string {
  return resolveApiUrl(path, apiBaseUrl, window.location.origin);
}

function mapErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? "Unknown map error");
  return message.length > 180 ? `${message.slice(0, 177)}...` : message;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isValidBounds(value: unknown): value is [number, number, number, number] {
  if (!Array.isArray(value) || value.length !== 4 || !value.every(Number.isFinite)) return false;
  const [west, south, east, north] = value;
  return west >= -180 && east <= 180 && south >= -90 && north <= 90 && west < east && south < north;
}

function isValidHealthPayload(value: unknown): value is {
  status: "ok";
  processed_records: number;
  data_updated?: string | null;
} {
  if (!value || typeof value !== "object") return false;
  const payload = value as Record<string, unknown>;
  return payload.status === "ok"
    && Number.isInteger(payload.processed_records)
    && (payload.processed_records as number) >= 0
    && (payload.data_updated === undefined || payload.data_updated === null || typeof payload.data_updated === "string");
}

function escapeHtml(value: unknown): string {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character] ?? character);
}
