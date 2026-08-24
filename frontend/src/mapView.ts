import type { ControlPosition, IControl, Map as MapLibreMap } from "maplibre-gl";

import { EXTRUDED_BUILDING_LAYER_ID, FLAT_BUILDING_LAYER_ID } from "./basemap";

export const CAMERA_TOLERANCE_DEGREES = 1;
const THREE_DIMENSIONAL_PITCH = 45;
const THREE_DIMENSIONAL_MIN_ZOOM = 14;

export interface CameraViewState {
  isThreeDimensional: boolean;
  showCompass: boolean;
}

export interface CameraPosition {
  bearing: number;
  pitch: number;
  zoom: number;
}

export interface CameraTransition {
  bearing?: number;
  pitch: number;
  zoom?: number;
}

export function cameraViewState(bearing: number, pitch: number): CameraViewState {
  const normalizedBearing = ((bearing + 180) % 360 + 360) % 360 - 180;
  const isThreeDimensional = pitch > CAMERA_TOLERANCE_DEGREES;
  return {
    isThreeDimensional,
    showCompass: isThreeDimensional || Math.abs(normalizedBearing) > CAMERA_TOLERANCE_DEGREES,
  };
}

export function nextCameraTransition(action: "toggle" | "reset", camera: CameraPosition): CameraTransition {
  if (action === "reset") return { bearing: 0, pitch: 0 };
  if (cameraViewState(camera.bearing, camera.pitch).isThreeDimensional) return { pitch: 0 };
  return { pitch: THREE_DIMENSIONAL_PITCH, zoom: Math.max(camera.zoom, THREE_DIMENSIONAL_MIN_ZOOM) };
}

function transitionDuration(): number {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 500;
}

function setLayerVisibility(map: MapLibreMap, layerId: string, visible: boolean): void {
  if (!map.getLayer(layerId)) return;
  const desired = visible ? "visible" : "none";
  const current = map.getLayoutProperty(layerId, "visibility") ?? "visible";
  if (current !== desired) map.setLayoutProperty(layerId, "visibility", desired);
}

export class MapViewControl implements IControl {
  private map?: MapLibreMap;
  private container?: HTMLDivElement;
  private compassButton?: HTMLButtonElement;
  private modeButton?: HTMLButtonElement;

  getDefaultPosition(): ControlPosition {
    return "top-right";
  }

  onAdd(map: MapLibreMap): HTMLElement {
    this.map = map;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group map-view-control";

    this.compassButton = document.createElement("button");
    this.compassButton.type = "button";
    this.compassButton.className = "maplibregl-ctrl-compass map-view-compass";
    this.compassButton.title = "Reset north and return to 2D";
    this.compassButton.setAttribute("aria-label", "Reset north and return to 2D");
    const compassIcon = document.createElement("span");
    compassIcon.className = "maplibregl-ctrl-icon";
    compassIcon.setAttribute("aria-hidden", "true");
    this.compassButton.append(compassIcon);
    this.compassButton.addEventListener("click", this.resetView);

    this.modeButton = document.createElement("button");
    this.modeButton.type = "button";
    this.modeButton.className = "map-view-mode";
    this.modeButton.addEventListener("click", this.toggleViewMode);

    this.container.append(this.compassButton, this.modeButton);
    map.on("rotate", this.sync);
    map.on("pitch", this.sync);
    this.sync();
    return this.container;
  }

  onRemove(): void {
    if (this.map) {
      this.map.off("rotate", this.sync);
      this.map.off("pitch", this.sync);
    }
    this.compassButton?.removeEventListener("click", this.resetView);
    this.modeButton?.removeEventListener("click", this.toggleViewMode);
    this.container?.remove();
    this.map = undefined;
  }

  sync = (): void => {
    if (!this.map || !this.compassButton || !this.modeButton) return;
    const state = cameraViewState(this.map.getBearing(), this.map.getPitch());
    this.compassButton.hidden = !state.showCompass;
    this.compassButton.setAttribute("aria-hidden", String(!state.showCompass));
    this.modeButton.textContent = state.isThreeDimensional ? "2D" : "3D";
    this.modeButton.title = state.isThreeDimensional ? "Return to flat map" : "Show 3D buildings";
    this.modeButton.setAttribute("aria-label", this.modeButton.title);
    this.modeButton.setAttribute("aria-pressed", String(state.isThreeDimensional));
    setLayerVisibility(this.map, FLAT_BUILDING_LAYER_ID, !state.isThreeDimensional);
    setLayerVisibility(this.map, EXTRUDED_BUILDING_LAYER_ID, state.isThreeDimensional);
  };

  private resetView = (): void => {
    if (!this.map) return;
    this.map.easeTo({
      ...nextCameraTransition("reset", {
        bearing: this.map.getBearing(),
        pitch: this.map.getPitch(),
        zoom: this.map.getZoom(),
      }),
      duration: transitionDuration(),
    });
  };

  private toggleViewMode = (): void => {
    if (!this.map) return;
    const transition = nextCameraTransition("toggle", {
      bearing: this.map.getBearing(),
      pitch: this.map.getPitch(),
      zoom: this.map.getZoom(),
    });
    this.map.easeTo({
      ...transition,
      duration: transitionDuration(),
    });
  };
}
