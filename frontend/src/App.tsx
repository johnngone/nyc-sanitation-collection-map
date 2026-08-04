import { useEffect, useRef, useState, type MutableRefObject } from "react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;
type Weekday = (typeof weekdays)[number];
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const sourceId = "collection-streets";
const layerId = "collection-streets-line";
const lowZoomLayerId = "collection-streets-lowzoom";
const collectionTypes = [["REFUSE", "Refuse", "#111111"], ["RECYCLING", "Recycling", "#1479d1"], ["ORGANICS", "Organics", "#8b4a22"], ["BULK", "Bulk", "#7a3db8"]] as const;
type CollectionType = (typeof collectionTypes)[number][0];

function dayCode(day: Weekday): string {
  return day.slice(0, 3).toUpperCase();
}

function dayFromCode(code: string | null): Weekday {
  return weekdays.find((day) => dayCode(day) === code) ?? "Monday";
}

export function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const [selectedDay, setSelectedDay] = useState<Weekday>(() => dayFromCode(new URLSearchParams(window.location.search).get("day")));
  const selectedDayRef = useRef<Weekday>(selectedDay);
  const [backendStatus, setBackendStatus] = useState("Checking backend…");
  const [mapStatus, setMapStatus] = useState("Loading map data…");
  const [featureCount, setFeatureCount] = useState(0);
  const [hasMapData, setHasMapData] = useState(false);
  const [selectedTypes, setSelectedTypes] = useState<CollectionType[]>(["REFUSE"]);
  const [dataUpdated, setDataUpdated] = useState<string | null>(null);
  const [showInfo, setShowInfo] = useState(false);
  const selectedTypesRef = useRef<CollectionType[]>(selectedTypes);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${apiBaseUrl}/api/health`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`Backend returned HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        setBackendStatus(`Backend connected · ${payload.processed_records} processed block faces`);
        setDataUpdated(payload.data_updated ?? null);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        console.error("Backend health check failed", error);
        setBackendStatus("Backend unavailable");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!mapNode.current) return;
    const map = new maplibregl.Map({
      container: mapNode.current,
      center: [-73.95, 40.72],
      zoom: 11.5,
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
    map.addControl(new maplibregl.NavigationControl(), "bottom-right");
    map.on("load", () => {
      map.addSource(sourceId, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: lowZoomLayerId,
        type: "line",
        source: sourceId,
        maxzoom: 13,
        filter: ["all", ["==", ["get", "collection_type"], "REFUSE"]],
        paint: {
          "line-color": ["match", ["get", "collection_type"], "RECYCLING", "#1479d1", "ORGANICS", "#8b4a22", "BULK", "#7a3db8", "#111111"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 1.5, 13, 3],
          "line-opacity": 0.9,
          "line-offset": 0,
        },
      });
      map.addLayer({
        id: layerId,
        type: "line",
        source: sourceId,
        minzoom: 13,
        filter: ["in", ["get", "collection_type"], ["literal", ["REFUSE"]]],
        paint: {
          "line-color": ["match", ["get", "collection_type"], "RECYCLING", "#1479d1", "ORGANICS", "#8b4a22", "BULK", "#7a3db8", "#111111"],
          "line-color-transition": { duration: 0 },
          "line-width": ["interpolate", ["linear"], ["zoom"], 9, 2, 14, 5],
          "line-opacity": 0.9,
          "line-offset": [
            "*",
            ["match", ["get", "side"], "LEFT", -1, "RIGHT", 1, 0],
            ["match", ["get", "collection_type"], "REFUSE", 0, "BULK", 3, "RECYCLING", 6, "ORGANICS", 9, 0],
          ],
        },
      });
      map.on("click", layerId, (event) => {
        const feature = event.features?.[0];
        if (!feature?.properties) return;
        const properties = feature.properties;
        new maplibregl.Popup()
          .setLngLat(event.lngLat)
          .setHTML(`<strong>${escapeHtml(properties.street_name ?? "Unnamed street")}</strong><br />${escapeHtml(properties.borough ?? "Unknown borough")} · ${escapeHtml(properties.side ?? "Unknown side")}<br /><br /><strong>${escapeHtml(properties.collection_type ?? "Collection")}:</strong> ${escapeHtml(properties.collection_days ?? "Unknown")}<br /><small>Source: ${escapeHtml(properties.source ?? "Unknown")}<br />Retrieved: ${escapeHtml(properties.retrieved_at ?? "Unknown")}</small>`)
          .addTo(map);
      });
      map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
      let moveTimer: number | undefined;
      map.on("moveend", () => {
        window.clearTimeout(moveTimer);
        moveTimer = window.setTimeout(() => {
          void fetchMapData(map, selectedDayRef.current, selectedTypesRef.current, requestRef, setMapStatus, setFeatureCount, setHasMapData);
        }, 250);
      });
      mapRef.current = map;
      void fetchMapData(map, selectedDayRef.current, selectedTypesRef.current, requestRef, setMapStatus, setFeatureCount, setHasMapData);
    });
    return () => {
      requestRef.current?.abort();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    selectedDayRef.current = selectedDay;
    const map = mapRef.current;
    if (!map) return;
    const timeout = window.setTimeout(() => {
          void fetchMapData(map, selectedDayRef.current, selectedTypesRef.current, requestRef, setMapStatus, setFeatureCount, setHasMapData);
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [selectedDay]);

  useEffect(() => {
    selectedTypesRef.current = selectedTypes;
    const map = mapRef.current;
    if (map?.getLayer(layerId)) {
      const typeFilter: any = selectedTypes.length ? ["in", ["get", "collection_type"], ["literal", selectedTypes]] : ["==", ["get", "collection_type"], "__NONE__"];
      map.setFilter(layerId, typeFilter);
      map.setFilter(lowZoomLayerId, typeFilter);
      const source = map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined;
      if (!selectedTypes.length) {
        source?.setData({ type: "FeatureCollection", features: [] });
        setFeatureCount(0);
        setHasMapData(false);
        setMapStatus("No collection types selected");
      }
    }
  }, [selectedTypes]);

  function selectDay(day: Weekday) {
    setSelectedDay(day);
    const url = new URL(window.location.href);
    url.searchParams.set("day", dayCode(day));
    window.history.replaceState({}, "", url);
  }

  function clearMap() {
    const source = mapRef.current?.getSource(sourceId) as maplibregl.GeoJSONSource | undefined;
    source?.setData({ type: "FeatureCollection", features: [] });
    setFeatureCount(0);
    setHasMapData(false);
    setMapStatus("Map selection cleared");
  }

  return (
    <main className="app-shell">
      <section className="map-panel" aria-label="NYC refuse collection map">
        <div ref={mapNode} className="map" />
        <aside className="map-overlay">
          <div className="title-row"><strong>NYC Sanitation - Visual Collection Schedule</strong><button className="info-button" type="button" aria-label="About this data" onClick={() => setShowInfo(true)}>i</button></div>
          <small>Data updated: {dataUpdated ? new Date(dataUpdated).toLocaleDateString() : "Not available"}</small>
          <div className="day-picker" aria-label="Collection day">{weekdays.map((day) => <button key={day} type="button" className={selectedDay === day ? "day-button selected" : "day-button"} onClick={() => selectDay(day)} aria-pressed={selectedDay === day}>{dayCode(day)[0]}</button>)}</div>
          <div className="type-filters">{collectionTypes.map(([type, label, color]) => <label key={type}><input type="checkbox" checked={selectedTypes.includes(type)} onChange={() => setSelectedTypes((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type])} /><i className="swatch" style={{ backgroundColor: color }} />{label}</label>)}</div>
          <span><i className="swatch unavailable" /> Missing or unprocessed data</span>
          <small>{mapStatus}</small>
          <small>{hasMapData ? `${featureCount} block faces shown` : "No collection data available in the current map area"}</small>
          <small>{backendStatus}</small>
        </aside>
      </section>
      {showInfo && <div className="modal-backdrop" role="presentation" onClick={() => setShowInfo(false)}><section className="info-modal" role="dialog" aria-modal="true" aria-labelledby="data-info-title" onClick={(event) => event.stopPropagation()}><button className="modal-close" type="button" aria-label="Close information" onClick={() => setShowInfo(false)}>×</button><h2 id="data-info-title">About this map</h2><p>Official NYC Department of Sanitation frequency data supplies collection schedules. NYC Department of City Planning LION data supplies street centerlines and separate left/right block-face identifiers.</p><p>The sources are downloaded, schema-checked, reprojected, spatially joined, and normalized into weekday schedules. Validated results are stored in a local SQLite database.</p><p>The FastAPI backend returns only matching streets within the current map bounds. MapLibre draws those GeoJSON features over the CARTO/OpenStreetMap basemap.</p><p>Missing, unmatched, or unvalidated source records are reported during processing and are not treated as proof that no collection exists.</p></section></div>}
    </main>
  );
}

async function fetchMapData(
  map: MapLibreMap,
  day: Weekday,
  types: CollectionType[],
  requestRef: MutableRefObject<AbortController | null>,
  setMapStatus: (value: string) => void,
  setFeatureCount: (value: number) => void,
  setHasMapData: (value: boolean) => void,
) {
  requestRef.current?.abort();
  const controller = new AbortController();
  requestRef.current = controller;
  setMapStatus(`Loading ${day} data…`);
  const bounds = map.getBounds();
  const params = new URLSearchParams({
    day: dayCode(day),
    types: collectionTypes.map(([type]) => type).join(","),
    west: bounds.getWest().toFixed(6),
    south: bounds.getSouth().toFixed(6),
    east: bounds.getEast().toFixed(6),
    north: bounds.getNorth().toFixed(6),
  });
  try {
    const response = await fetch(`${apiBaseUrl}/api/refuse-streets?${params}`, { signal: controller.signal });
    if (!response.ok) throw new Error(`Map data request failed with HTTP ${response.status}`);
    const data = await response.json();
    const source = map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined;
    if (!source) throw new Error("Map source is not initialized");
    source.setData(data);
    const count = Array.isArray(data.features) ? data.features.length : 0;
    setFeatureCount(count);
    setHasMapData(count > 0);
    setMapStatus(count > 0 ? "Map data loaded" : "No processed collection data in this area");
  } catch (error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") return;
    console.error("Map data request failed", error);
    setFeatureCount(0);
    setHasMapData(false);
    setMapStatus("Map data unavailable; see server logs");
  }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character] ?? character);
}
