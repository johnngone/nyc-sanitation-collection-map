import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import { describe, expect, it } from "vitest";

import { BASEMAP_ATTRIBUTION, createBasemapStyle, EXTRUDED_BUILDING_LAYER_ID, FLAT_BUILDING_LAYER_ID } from "./basemap";

describe("sanitation basemap style", () => {
  const style = createBasemapStyle();

  it("is a valid MapLibre style", () => {
    expect(validateStyleMin(style)).toEqual([]);
  });

  it("contains only the requested label categories", () => {
    const symbolLayers = style.layers.filter((layer) => layer.type === "symbol");
    expect(symbolLayers.map((layer) => layer.id)).toEqual([
      "basemap-street-names",
      "basemap-waterway-names",
      "basemap-water-point-names",
      "basemap-water-line-names",
      "basemap-protected-park-names",
      "basemap-park-names",
      "basemap-borough-names",
      "basemap-neighborhood-names",
    ]);
    expect(JSON.stringify(style)).not.toMatch(/carto|transit|aerodrome|airport|building-name/i);
  });

  it("contains the required restrained context layers", () => {
    const layerIds = style.layers.map((layer) => layer.id);
    expect(layerIds).toEqual(expect.arrayContaining([
      "basemap-background",
      "basemap-park-protected",
      "basemap-park-wood",
      "basemap-park-grass",
      "basemap-water",
      "basemap-waterway",
      "basemap-road-casing",
      "basemap-road-fill",
      "basemap-borough-boundary",
      "basemap-borough-names",
      "basemap-neighborhood-names",
    ]));
    const boundary = style.layers.find((layer) => layer.id === "basemap-borough-boundary");
    expect(boundary).toMatchObject({ type: "line", source: "openmaptiles", "source-layer": "boundary" });
    expect(JSON.stringify(boundary)).toContain('"admin_level"],6');
    expect(JSON.stringify(boundary)).not.toContain("maritime");
  });

  it("shows only park POIs and the five requested borough labels", () => {
    const parkNames = style.layers.find((layer) => layer.id === "basemap-park-names");
    const boroughNames = style.layers.find((layer) => layer.id === "basemap-borough-names");
    expect(JSON.stringify(parkNames)).toContain('["get","class"],"park"');
    for (const borough of ["Manhattan", "Brooklyn", "Queens", "The Bronx", "Staten Island"]) {
      expect(JSON.stringify(boroughNames)).toContain(borough);
    }
  });

  it("keeps flat buildings by default and 3D buildings opt-in", () => {
    const flat = style.layers.find((layer) => layer.id === FLAT_BUILDING_LAYER_ID);
    const extruded = style.layers.find((layer) => layer.id === EXTRUDED_BUILDING_LAYER_ID);
    expect(flat).toMatchObject({ type: "fill", minzoom: 13, layout: { visibility: "visible" } });
    expect(flat).not.toHaveProperty("maxzoom");
    expect(extruded).toMatchObject({ type: "fill-extrusion", minzoom: 14, layout: { visibility: "none" } });
  });

  it("uses the exact linked attribution and supports a source override", () => {
    const overridden = createBasemapStyle("https://example.test/tiles.json");
    expect(overridden.sources.openmaptiles).toMatchObject({
      url: "https://example.test/tiles.json",
      attribution: BASEMAP_ATTRIBUTION,
    });
    expect(BASEMAP_ATTRIBUTION.replace(/<[^>]+>/g, "")).toBe("© OpenMapTiles Data from OpenStreetMap");
  });
});
