import { useEffect, useRef, useState } from "react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";

import { resolveApiUrl } from "./apiUrl";

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;
type Weekday = (typeof weekdays)[number];
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const sourceId = "collection-streets";
const mobileControlsMediaQuery = "(max-width: 700px), (max-height: 520px) and (pointer: coarse)";
const collectionTypes = [
  ["REFUSE", "Refuse", "#111111", "refuse_days", 0],
  ["RECYCLING", "Recycling", "#1479d1", "recycling_days", 6],
  ["ORGANICS", "Organics", "#8b4a22", "organics_days", 9],
  ["BULK", "Bulk trash / non-recyclable large items", "#7a3db8", "bulk_days", 3],
] as const;
type CollectionType = (typeof collectionTypes)[number][0];
type CollectionDefinition = (typeof collectionTypes)[number];
type BackendConnection = "checking" | "connected" | "verifying" | "unavailable";
const collectionLayerIds = collectionTypes.flatMap(([type]) => [lowZoomLayerId(type), highZoomLayerId(type)]);
const unknownLayerIds = ["collection-unknown-geometry", "collection-unknown-identity"];

interface MapConfig {
  available: boolean;
  tile_schema_revision: number | null;
  version: string | null;
  tiles_url: string | null;
  source_layer: string;
  known_source_layer?: string;
  unknown_source_layer?: string | null;
  unknown_minzoom?: number | null;
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

function dayShortLabel(day: Weekday): string {
  return day.slice(0, 2);
}

function backendConnectionLabel(connection: BackendConnection): string {
  if (connection === "connected") return "Connected";
  if (connection === "verifying") return "Verifying data · retrying";
  if (connection === "unavailable") return "Unavailable · retrying";
  return "Checking connection";
}

function formatDataUpdated(value: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

export function App() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const sheetContentRef = useRef<HTMLDivElement>(null);
  const infoButtonRef = useRef<HTMLButtonElement>(null);
  const modalCloseRef = useRef<HTMLButtonElement>(null);
  const sheetGestureStartYRef = useRef<number | null>(null);
  const sheetGestureHandledRef = useRef(false);
  const selectedDayRef = useRef<Weekday>(dayFromCode(new URLSearchParams(window.location.search).get("day")));
  const selectedTypesRef = useRef<CollectionType[]>(["REFUSE"]);
  const showUnknownsRef = useRef(true);
  const updateTileStatusRef = useRef<(() => void) | null>(null);
  const tileErrorRef = useRef<string | null>(null);
  const [selectedDay, setSelectedDay] = useState<Weekday>(selectedDayRef.current);
  const [backendConnection, setBackendConnection] = useState<BackendConnection>("checking");
  const [mappedFeatureCount, setMappedFeatureCount] = useState<number | null>(null);
  const [mapStatus, setMapStatus] = useState("Loading map tiles…");
  const [selectedTypes, setSelectedTypes] = useState<CollectionType[]>(selectedTypesRef.current);
  const [dataUpdated, setDataUpdated] = useState<string | null>(null);
  const [showInfo, setShowInfo] = useState(false);
  const [showUnknowns, setShowUnknowns] = useState(true);
  const [unknownLayerAvailable, setUnknownLayerAvailable] = useState(false);
  const [mobileSheetOpen, setMobileSheetOpen] = useState(true);
  const [brandExpanded, setBrandExpanded] = useState(false);
  const [isMobileViewport, setIsMobileViewport] = useState(
    () => window.matchMedia(mobileControlsMediaQuery).matches,
  );

  useEffect(() => {
    const mediaQuery = window.matchMedia(mobileControlsMediaQuery);
    const updateViewport = (event: MediaQueryListEvent) => setIsMobileViewport(event.matches);
    mediaQuery.addEventListener("change", updateViewport);
    return () => mediaQuery.removeEventListener("change", updateViewport);
  }, []);

  useEffect(() => {
    const content = sheetContentRef.current;
    if (!content) return;
    if (isMobileViewport && !mobileSheetOpen) content.setAttribute("inert", "");
    else content.removeAttribute("inert");
  }, [isMobileViewport, mobileSheetOpen]);

  useEffect(() => {
    if (!showInfo) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeInfo();
    };
    window.addEventListener("keydown", closeOnEscape);
    modalCloseRef.current?.focus();
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showInfo]);

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
          setBackendConnection("connected");
          setMappedFeatureCount(payload.processed_records);
          setDataUpdated(payload.data_updated ?? null);
        })
        .catch((error: unknown) => {
          if (isAbortError(error) || controller.signal.aborted) return;
          console.error("Backend health check failed", error);
          const status = error instanceof Error && "status" in error ? error.status : undefined;
          setBackendConnection(status === 503 ? "verifying" : "unavailable");
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
      attributionControl: false,
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
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

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
            || ![2, 3].includes(config.tile_schema_revision ?? 0)
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
          const tileSchemaRevision = config.tile_schema_revision as 2 | 3;
          const hasUnknownLayer = tileSchemaRevision >= 3
            && Boolean(config.unknown_source_layer)
            && typeof config.unknown_minzoom === "number";
          setUnknownLayerAvailable(hasUnknownLayer);
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
            if (tileSchemaRevision >= 3) {
              map.addLayer({
                id: blankLayerId(type),
                type: "line",
                source: sourceId,
                "source-layer": config.source_layer,
                minzoom: config.unknown_minzoom ?? 16,
                filter: ["==", ["get", `${type.toLowerCase()}_status`], "UNKNOWN_SOURCE_BLANK"],
                layout: { visibility },
                paint: {
                  "line-color": "#707981",
                  "line-width": 3,
                  "line-dasharray": [2, 2],
                  "line-opacity": 0.85,
                  "line-offset": lineOffset,
                },
              });
            }
          }

          if (tileSchemaRevision >= 3 && config.unknown_source_layer && typeof config.unknown_minzoom === "number") {
            const unknownSideOffset: maplibregl.ExpressionSpecification = [
              "*", ["match", ["get", "side"], "LEFT", -1, "RIGHT", 1, 0], 4,
            ];
            map.addLayer({
              id: unknownLayerIds[0], type: "line", source: sourceId,
              "source-layer": config.unknown_source_layer, minzoom: config.unknown_minzoom,
              filter: ["in", ["get", "reason_code"], ["literal", ["OUTSIDE_DSNY_COVERAGE", "PARTIAL_GEOMETRY_GAP"]]],
              layout: { visibility: showUnknownsRef.current ? "visible" : "none" },
              paint: { "line-color": "#d68a00", "line-width": 3, "line-dasharray": [2, 2], "line-opacity": 0.9, "line-offset": unknownSideOffset },
            });
            map.addLayer({
              id: unknownLayerIds[1], type: "line", source: sourceId,
              "source-layer": config.unknown_source_layer, minzoom: 17,
              filter: ["==", ["get", "reason_code"], "INSUFFICIENT_ADDRESS_EVIDENCE"],
              layout: { visibility: showUnknownsRef.current ? "visible" : "none" },
              paint: { "line-color": "#687078", "line-width": 3, "line-dasharray": [1, 2], "line-opacity": 0.9, "line-offset": unknownSideOffset },
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
            if (unknownLayerIds.includes(feature.layer.id)) {
              const properties = feature.properties;
              const explanation = unknownReasonText(properties.reason_code);
              new maplibregl.Popup().setLngLat(event.lngLat).setHTML(
                `<strong>${escapeHtml(properties.street_name ?? "Unknown street")}</strong><br />${escapeHtml(explanation)}<br /><small>${escapeHtml(properties.reason ?? "Schedule evidence is unavailable")}</small>`
              ).addTo(map);
              return;
            }
            const collection = collectionForLayerId(feature.layer.id);
            if (!collection) return;
            const [, label, , daysProperty] = collection;
            const properties = feature.properties;
            const collectionDays = properties[daysProperty] ?? "Unknown";
            const status = properties[`${collection[0].toLowerCase()}_status`] ?? "SOURCE_EXPLICIT";
            const scheduleExplanation = scheduleStatusText(
              status,
              properties[`${collection[0].toLowerCase()}_conflict`] === "1",
            );
            const blockFaceId = properties.origin_block_face_id ?? properties.source_block_face_id ?? properties.id;
            const metadata = [
              blockFaceId ? `Block face: ${escapeHtml(blockFaceId)}` : null,
              properties.source ? `Source: ${escapeHtml(properties.source)}` : null,
              properties.retrieved_at ? `Retrieved: ${escapeHtml(properties.retrieved_at)}` : null,
            ].filter((value): value is string => value !== null);
            const metadataHtml = metadata.length ? `<br /><small>${metadata.join("<br />")}</small>` : "";
            new maplibregl.Popup()
              .setLngLat(event.lngLat)
              .setHTML(`<strong>${escapeHtml(properties.street_name ?? properties.name ?? "Unnamed street")}</strong><br />${escapeHtml(properties.borough ?? "Unknown borough")} · ${escapeHtml(properties.side ?? "Unknown side")}<br /><br /><strong>${escapeHtml(label)}:</strong> ${escapeHtml(collectionDays || "Unavailable")}<br />${escapeHtml(scheduleExplanation)}${metadataHtml}`)
              .addTo(map);
          };
          const clickableCollectionLayers = [
            ...collectionLayerIds,
            ...collectionTypes.map(([type]) => blankLayerId(type)).filter((id) => Boolean(map.getLayer(id))),
          ];
          map.on("click", clickableCollectionLayers, showPopup);
          if (tileSchemaRevision >= 3 && config.unknown_source_layer) map.on("click", unknownLayerIds, showPopup);
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

  useEffect(() => {
    showUnknownsRef.current = showUnknowns;
    const map = mapRef.current;
    if (!map) return;
    for (const id of unknownLayerIds) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", showUnknowns ? "visible" : "none");
    }
  }, [showUnknowns]);

  function selectDay(day: Weekday) {
    setSelectedDay(day);
    const url = new URL(window.location.href);
    url.searchParams.set("day", dayCode(day));
    window.history.replaceState({}, "", url);
  }

  function beginSheetGesture(event: React.PointerEvent<HTMLButtonElement>) {
    sheetGestureStartYRef.current = event.clientY;
    sheetGestureHandledRef.current = false;
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function finishSheetGesture(event: React.PointerEvent<HTMLButtonElement>) {
    const startY = sheetGestureStartYRef.current;
    sheetGestureStartYRef.current = null;
    if (startY === null) return;
    const distance = event.clientY - startY;
    if (Math.abs(distance) < 32) return;
    sheetGestureHandledRef.current = true;
    setMobileSheetOpen(distance < 0);
  }

  function cancelSheetGesture() {
    sheetGestureStartYRef.current = null;
    sheetGestureHandledRef.current = false;
  }

  function toggleMobileSheet() {
    if (sheetGestureHandledRef.current) {
      sheetGestureHandledRef.current = false;
      return;
    }
    setMobileSheetOpen((current) => !current);
  }

  function closeInfo() {
    setShowInfo(false);
    window.requestAnimationFrame(() => infoButtonRef.current?.focus());
  }

  return (
    <main className={`app-shell ${mobileSheetOpen ? "mobile-sheet-open" : "mobile-sheet-collapsed"}`}>
      <h1 className="visually-hidden">NYC Sanitation – Collection Map</h1>
      <section className="map-panel" aria-label="NYC sanitation collection map">
        <div ref={mapNode} className="map" />
        <div
          className={`brand-lockup ${brandExpanded ? "is-expanded" : ""}`}
          onPointerEnter={(event) => {
            if (event.pointerType === "mouse") setBrandExpanded(true);
          }}
          onPointerLeave={(event) => {
            if (event.pointerType === "mouse") setBrandExpanded(false);
          }}
        >
          <button
            className="brand-button"
            type="button"
            aria-label={brandExpanded ? "Hide map title" : "Show map title"}
            aria-expanded={brandExpanded}
            onClick={() => setBrandExpanded((current) => !current)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setBrandExpanded(false);
            }}
          >
            <img className="brand-logo" src="/logo.png" alt="" draggable="false" />
            <span className="brand-copy">
              <strong>NYC Sanitation – Collection Map</strong>
              <small>See curbside collection schedules by street, weekday, and service type.</small>
            </span>
          </button>
        </div>
        <aside className={`map-overlay ${mobileSheetOpen ? "is-open" : "is-collapsed"}`} aria-label="Map controls">
          <button
            className="sheet-toggle"
            type="button"
            aria-label={mobileSheetOpen ? "Collapse map controls" : "Expand map controls"}
            aria-expanded={mobileSheetOpen}
            aria-controls="map-control-content"
            onClick={toggleMobileSheet}
            onPointerDown={beginSheetGesture}
            onPointerUp={finishSheetGesture}
            onPointerCancel={cancelSheetGesture}
          >
            <span className="sheet-grabber" aria-hidden="true" />
            <span className="sheet-toggle-label">{mobileSheetOpen ? "Hide controls" : "Show controls"}</span>
            <svg className="sheet-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m5.5 7.5 4.5 4 4.5-4" /></svg>
          </button>
          <div
            ref={sheetContentRef}
            id="map-control-content"
            className="sheet-content"
            aria-hidden={isMobileViewport && !mobileSheetOpen}
          >
            <div className="controls-heading">
              <strong>Collection schedule</strong>
              <button ref={infoButtonRef} className="info-button" type="button" aria-label="About this data" onClick={() => setShowInfo(true)}>i</button>
            </div>
            <div className="day-picker" aria-label="Collection day">{weekdays.map((day) => <button key={day} type="button" className={selectedDay === day ? "day-button selected" : "day-button"} onClick={() => selectDay(day)} aria-label={day} aria-pressed={selectedDay === day}>{dayShortLabel(day)}</button>)}</div>
            <div className="type-filters">{collectionTypes.map(([type, label, color]) => <label key={type}><input type="checkbox" checked={selectedTypes.includes(type)} onChange={() => setSelectedTypes((current) => current.includes(type) ? current.filter((item) => item !== type) : [...current, type])} /><i className="swatch" style={{ backgroundColor: color }} />{label}</label>)}</div>
            {unknownLayerAvailable && <label className="unknown-toggle"><input type="checkbox" checked={showUnknowns} onChange={() => setShowUnknowns((current) => !current)} /> Show source gaps at high zoom</label>}
            <dl className="status-summary" aria-live="polite">
              <div><dt>Backend</dt><dd><i className={`status-dot ${backendConnection}`} aria-hidden="true" />{backendConnectionLabel(backendConnection)}</dd></div>
              <div><dt>Mapped</dt><dd>{mappedFeatureCount === null ? "Waiting for data" : `${mappedFeatureCount.toLocaleString()} street features`}</dd></div>
              <div><dt>Status</dt><dd>{mapStatus}</dd></div>
              <div><dt>Last Updated</dt><dd><time dateTime={dataUpdated ?? undefined}>{formatDataUpdated(dataUpdated)}</time></dd></div>
            </dl>
          </div>
        </aside>
      </section>
      {showInfo && <div className="modal-backdrop" role="presentation" onClick={closeInfo}><section className="info-modal" role="dialog" aria-modal="true" aria-labelledby="data-info-title" onClick={(event) => event.stopPropagation()}><button ref={modalCloseRef} className="modal-close" type="button" aria-label="Close information" onClick={closeInfo}>×</button><h2 id="data-info-title">About this map</h2><p>This map shows NYC sanitation collection schedules by street, making them easier to explore at a glance.</p><p>The city's <a href="https://www.nyc.gov/assets/dsny/forms/collection-schedule" target="_blank" rel="noreferrer">collection schedule lookup</a> provides results for one address at a time. This map brings those schedules together so you can see collection patterns across a neighborhood or the whole city.</p><p>It combines the Department of City Planning's <a href="https://www.nyc.gov/site/planning/data-maps/open-data/dwn-lion.page" target="_blank" rel="noreferrer">LION street data</a> with DSNY's official <a href="https://services.arcgis.com/uKN48PkxmWiqJM9q/ArcGIS/rest/services/DSNY_Frequencies_OFFICIAL/FeatureServer/0" target="_blank" rel="noreferrer">collection-frequency data</a> for refuse, recycling, organics, and bulk pick-up. Records are matched to individual block faces, meaning each side of a street, then converted into map tiles for fast viewing.</p><p>Only matches that pass the project's validation checks are shown as scheduled street lines. Source records that cannot be matched reliably are kept separate instead of being assigned a potentially incorrect schedule. Data is refreshed periodically from these official NYC sources.</p></section></div>}
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

function blankLayerId(type: CollectionType): string {
  return `${sourceId}-${type.toLowerCase()}-unknown`;
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
    const unknownId = blankLayerId(type);
    if (map.getLayer(unknownId)) map.setLayoutProperty(unknownId, "visibility", visibility);
  }
}

function collectionForLayerId(id: string): CollectionDefinition | undefined {
  return collectionTypes.find(([type]) => id === lowZoomLayerId(type) || id === highZoomLayerId(type) || id === blankLayerId(type));
}

function scheduleStatusText(status: unknown, conflict = false): string {
  if (conflict) return "Official explicit DSNY schedule; it differs from the general policy relationship and was preserved.";
  if (status === "POLICY_DERIVED") return "Derived from the explicit Recycling day under DSNY citywide compost policy.";
  if (status === "UNKNOWN_SOURCE_BLANK") return "The official source schedule is unavailable; no day was inferred.";
  if (status === "SOURCE_EXPLICIT") return "Official explicit DSNY schedule.";
  return "Schedule provenance is unavailable.";
}

function unknownReasonText(reason: unknown): string {
  if (reason === "INSUFFICIENT_ADDRESS_EVIDENCE") return "Insufficient address evidence to assign a schedule.";
  if (reason === "PARTIAL_GEOMETRY_GAP") return "Part of this geometry falls outside published DSNY coverage.";
  return "This geometry falls outside published DSNY coverage.";
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
