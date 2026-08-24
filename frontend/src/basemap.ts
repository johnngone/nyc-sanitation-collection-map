import type { StyleSpecification, VectorSourceSpecification } from "maplibre-gl";

import sanitationLiberty from "./map/sanitation-liberty.json";

export const BASEMAP_SOURCE_ID = "openmaptiles";
export const FLAT_BUILDING_LAYER_ID = "basemap-building-flat";
export const EXTRUDED_BUILDING_LAYER_ID = "basemap-building-3d";
export const DEFAULT_BASEMAP_TILEJSON_URL = "https://tiles.openfreemap.org/planet";
export const BASEMAP_ATTRIBUTION = '<a href="https://openmaptiles.org/" target="_blank" rel="noopener noreferrer">© OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>';

export function createBasemapStyle(tileJsonUrl = DEFAULT_BASEMAP_TILEJSON_URL): StyleSpecification {
  const style = sanitationLiberty as unknown as StyleSpecification;
  const source = style.sources[BASEMAP_SOURCE_ID] as VectorSourceSpecification;
  return {
    ...style,
    sources: {
      ...style.sources,
      [BASEMAP_SOURCE_ID]: {
        ...source,
        url: tileJsonUrl,
        attribution: BASEMAP_ATTRIBUTION,
      },
    },
  };
}
