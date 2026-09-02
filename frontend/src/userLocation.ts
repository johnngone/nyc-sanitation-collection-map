import maplibregl, {
  type IControl,
  type Map as MapLibreMap,
  type Marker,
} from "maplibre-gl";

import { cameraTransitionDuration } from "./mapView";

const COMPASS_FRESHNESS_MS = 5_000;
const STATUS_DURATION_MS = 5_000;

export type UserLocationState = "inactive" | "requesting" | "active" | "stale" | "denied" | "unavailable";

type PermissionResult = "granted" | "denied";
type PermissionCapableOrientationConstructor = typeof DeviceOrientationEvent & {
  requestPermission?: () => Promise<PermissionResult>;
};
type CompassOrientationEvent = DeviceOrientationEvent & {
  webkitCompassHeading?: number;
  webkitCompassAccuracy?: number;
};

export function normalizeHeading(heading: number): number {
  return ((heading % 360) + 360) % 360;
}

export function validHeading(heading: number | null | undefined): number | undefined {
  return typeof heading === "number" && Number.isFinite(heading)
    ? normalizeHeading(heading)
    : undefined;
}

export function orientationHeading(
  event: Pick<CompassOrientationEvent, "absolute" | "alpha" | "webkitCompassHeading" | "webkitCompassAccuracy">,
  screenAngle = 0,
): number | undefined {
  const webkitHeading = validHeading(event.webkitCompassHeading);
  const webkitAccuracy = event.webkitCompassAccuracy;
  if (webkitHeading !== undefined && (webkitAccuracy === undefined || webkitAccuracy >= 0)) {
    return webkitHeading;
  }
  const alpha = validHeading(event.alpha);
  if (!event.absolute || alpha === undefined) return undefined;
  return normalizeHeading(360 - alpha + screenAngle);
}

export function preferredHeading(
  compassHeading: number | undefined,
  movementHeading: number | null | undefined,
): number | undefined {
  return validHeading(compassHeading) ?? validHeading(movementHeading);
}

export function locationStatePresentation(state: UserLocationState): {
  label: string;
  pressed: boolean;
} {
  switch (state) {
    case "requesting": return { label: "Waiting for your location", pressed: true };
    case "active": return { label: "Center map on your location", pressed: true };
    case "stale": return { label: "Center map on your last known location", pressed: true };
    case "denied": return { label: "Location permission was denied", pressed: false };
    case "unavailable": return { label: "Location is unavailable", pressed: false };
    default: return { label: "Show your location", pressed: false };
  }
}

export async function locationPermissionIsGranted(
  permissions: Pick<Permissions, "query"> | undefined,
): Promise<boolean> {
  if (!permissions) return false;
  try {
    const status = await permissions.query({ name: "geolocation" });
    return status.state === "granted";
  } catch {
    // Some browsers expose Permissions without supporting geolocation queries.
    return false;
  }
}

function screenOrientationAngle(): number {
  return window.screen.orientation?.angle ?? 0;
}

function createLocationMarkerElement(): HTMLDivElement {
  const marker = document.createElement("div");
  marker.className = "user-location-marker";
  marker.setAttribute("aria-hidden", "true");
  const direction = document.createElement("span");
  direction.className = "user-location-direction";
  const dot = document.createElement("span");
  dot.className = "user-location-dot";
  marker.append(direction, dot);
  return marker;
}

function createLocationControlIcon(): SVGSVGElement {
  const namespace = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(namespace, "svg");
  icon.setAttribute("class", "user-location-control-icon");
  icon.setAttribute("viewBox", "0 0 32 32");
  icon.setAttribute("aria-hidden", "true");
  const pulse = document.createElementNS(namespace, "circle");
  pulse.setAttribute("class", "user-location-control-pulse");
  pulse.setAttribute("cx", "16");
  pulse.setAttribute("cy", "16");
  pulse.setAttribute("r", "6");
  const dot = document.createElementNS(namespace, "circle");
  dot.setAttribute("class", "user-location-control-dot");
  dot.setAttribute("cx", "16");
  dot.setAttribute("cy", "16");
  dot.setAttribute("r", "6");
  icon.append(pulse, dot);
  return icon;
}

export class UserLocationControl implements IControl {
  private map?: MapLibreMap;
  private container?: HTMLDivElement;
  private button?: HTMLButtonElement;
  private statusElement?: HTMLDivElement;
  private marker?: Marker;
  private markerElement?: HTMLDivElement;
  private accuracyMarker?: Marker;
  private accuracyElement?: HTMLDivElement;
  private watchId?: number;
  private statusTimer?: number;
  private compassTimer?: number;
  private lastPosition?: GeolocationPosition;
  private compassHeading?: number;
  private movementHeading?: number;
  private centeredFirstFix = false;
  private orientationListening = false;
  private orientationRequestPending = false;
  private state: UserLocationState = "inactive";
  private blockedReason?: string;

  trigger(): boolean {
    if (!this.map) return false;
    this.activate(false);
    return true;
  }

  async autoStart(): Promise<boolean> {
    if (!this.map || this.blockedReason) return false;
    // Never turn a page load into a permission prompt. A real button press is
    // still available when the state is "prompt" or cannot be queried.
    const permissions = "permissions" in navigator ? navigator.permissions : undefined;
    if (!await locationPermissionIsGranted(permissions)) return false;
    return this.trigger();
  }

  onAdd(map: MapLibreMap): HTMLElement {
    this.map = map;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl user-location-control";
    const buttonFrame = document.createElement("div");
    buttonFrame.className = "maplibregl-ctrl-group user-location-button-frame";
    this.button = document.createElement("button");
    this.button.type = "button";
    this.button.className = "user-location-button";
    this.button.append(createLocationControlIcon());
    this.statusElement = document.createElement("div");
    this.statusElement.className = "user-location-status";
    this.statusElement.hidden = true;
    this.statusElement.setAttribute("role", "status");
    this.statusElement.setAttribute("aria-live", "polite");
    buttonFrame.append(this.button);
    this.container.append(buttonFrame, this.statusElement);

    if (!window.isSecureContext) {
      this.blockedReason = "Location requires HTTPS or localhost.";
      this.state = "unavailable";
      this.button.setAttribute("aria-disabled", "true");
    } else if (!navigator.geolocation) {
      this.blockedReason = "Location is not supported by this browser.";
      this.state = "unavailable";
      this.button.setAttribute("aria-disabled", "true");
    }
    this.syncPresentation();
    this.button.addEventListener("click", this.handleClick);
    map.on("zoom", this.updateAccuracyCircle);
    map.on("move", this.updateAccuracyCircle);
    map.on("rotate", this.updateAccuracyCircle);
    map.on("pitch", this.updateAccuracyCircle);
    return this.container;
  }

  onRemove(): void {
    this.stopWatch();
    this.stopOrientation();
    if (this.map) {
      this.map.off("zoom", this.updateAccuracyCircle);
      this.map.off("move", this.updateAccuracyCircle);
      this.map.off("rotate", this.updateAccuracyCircle);
      this.map.off("pitch", this.updateAccuracyCircle);
    }
    this.button?.removeEventListener("click", this.handleClick);
    this.marker?.remove();
    this.accuracyMarker?.remove();
    this.container?.remove();
    if (this.statusTimer !== undefined) window.clearTimeout(this.statusTimer);
    if (this.compassTimer !== undefined) window.clearTimeout(this.compassTimer);
    this.map = undefined;
  }

  private handleClick = (): void => {
    this.activate(true);
  };

  private activate(canPromptForOrientation: boolean): void {
    if (this.blockedReason) {
      this.showStatus(this.blockedReason);
      return;
    }
    if (this.watchId === undefined) {
      this.startTracking(canPromptForOrientation);
      return;
    }
    if (canPromptForOrientation) this.requestOrientationAccess(true);
    if (this.lastPosition) {
      this.centerOn(this.lastPosition);
    } else {
      this.showStatus("Waiting for your location…");
    }
  }

  private startTracking(canPromptForOrientation: boolean): void {
    if (!this.lastPosition) this.centeredFirstFix = false;
    this.state = "requesting";
    this.syncPresentation();
    this.requestOrientationAccess(canPromptForOrientation);
    try {
      this.watchId = navigator.geolocation.watchPosition(
        this.handlePosition,
        this.handleError,
        { enableHighAccuracy: true, maximumAge: 5_000, timeout: 10_000 },
      );
    } catch {
      this.state = "unavailable";
      this.syncPresentation();
      this.showStatus("Location is unavailable.");
    }
  }

  private handlePosition = (position: GeolocationPosition): void => {
    if (!this.map || this.watchId === undefined) return;
    this.lastPosition = position;
    this.movementHeading = validHeading(position.coords.heading);
    this.state = "active";
    this.ensureMarkers();
    const coordinates: [number, number] = [position.coords.longitude, position.coords.latitude];
    this.accuracyMarker?.setLngLat(coordinates).addTo(this.map);
    this.marker?.setLngLat(coordinates).addTo(this.map);
    this.markerElement?.classList.remove("is-stale");
    this.accuracyElement?.classList.remove("is-stale");
    this.updateMarkerHeading();
    this.updateAccuracyCircle();
    this.syncPresentation();
    if (!this.centeredFirstFix) {
      this.centeredFirstFix = true;
      this.centerOn(position);
    }
  };

  private handleError = (error: GeolocationPositionError): void => {
    if (!this.map || this.watchId === undefined) return;
    if (error.code === error.PERMISSION_DENIED) {
      this.state = "denied";
      this.stopWatch();
      this.stopOrientation();
      this.lastPosition = undefined;
      this.marker?.remove();
      this.accuracyMarker?.remove();
      this.showStatus("Location permission was denied. Enable it in your browser settings to try again.");
    } else if (this.lastPosition) {
      this.state = "stale";
      this.markerElement?.classList.add("is-stale");
      this.accuracyElement?.classList.add("is-stale");
      this.showStatus("Using your last known location while a fresh fix is unavailable.");
    } else {
      this.state = "unavailable";
      this.showStatus(error.code === error.TIMEOUT
        ? "Location timed out. Press the button to try again."
        : "Your location is currently unavailable.");
      this.stopWatch();
      this.stopOrientation();
    }
    this.syncPresentation();
  };

  private ensureMarkers(): void {
    if (!this.map || this.marker) return;
    this.accuracyElement = document.createElement("div");
    this.accuracyElement.className = "user-location-accuracy";
    this.accuracyElement.setAttribute("aria-hidden", "true");
    this.accuracyMarker = new maplibregl.Marker({
      element: this.accuracyElement,
      pitchAlignment: "map",
      rotationAlignment: "map",
    });
    this.markerElement = createLocationMarkerElement();
    this.marker = new maplibregl.Marker({
      element: this.markerElement,
      pitchAlignment: "viewport",
      rotationAlignment: "map",
    });
  }

  private centerOn(position: GeolocationPosition): void {
    if (!this.map) return;
    this.map.easeTo({
      center: [position.coords.longitude, position.coords.latitude],
      duration: cameraTransitionDuration(window.matchMedia("(prefers-reduced-motion: reduce)").matches),
    });
  }

  private updateAccuracyCircle = (): void => {
    if (!this.map || !this.lastPosition || !this.accuracyElement || !this.accuracyMarker) return;
    const location = this.accuracyMarker.getLngLat();
    if (!Number.isFinite(this.lastPosition.coords.accuracy)) return;
    const screenPosition = this.map.project(location);
    const comparisonLocation = this.map.unproject([screenPosition.x + 100, screenPosition.y]);
    const pixelsToMeters = location.distanceTo(comparisonLocation) / 100;
    if (!Number.isFinite(pixelsToMeters) || pixelsToMeters <= 0) return;
    const diameter = Math.max(1, 2 * this.lastPosition.coords.accuracy / pixelsToMeters);
    this.accuracyElement.style.width = `${diameter.toFixed(2)}px`;
    this.accuracyElement.style.height = `${diameter.toFixed(2)}px`;
  };

  private requestOrientationAccess(canPrompt: boolean): void {
    if (this.orientationListening || this.orientationRequestPending) return;
    const constructor = window.DeviceOrientationEvent as PermissionCapableOrientationConstructor | undefined;
    if (!constructor) return;
    if (typeof constructor.requestPermission === "function") {
      if (!canPrompt) return;
      this.orientationRequestPending = true;
      let permissionRequest: Promise<PermissionResult>;
      try {
        permissionRequest = constructor.requestPermission();
      } catch {
        this.orientationRequestPending = false;
        return;
      }
      void permissionRequest
        .then((result) => {
          if (result === "granted" && this.map && this.watchId !== undefined) this.startOrientation();
        })
        .catch(() => { /* Movement heading remains available as the fallback. */ })
        .finally(() => { this.orientationRequestPending = false; });
      return;
    }
    this.startOrientation();
  }

  private startOrientation(): void {
    if (this.orientationListening) return;
    window.addEventListener("deviceorientationabsolute", this.handleOrientation);
    window.addEventListener("deviceorientation", this.handleOrientation);
    this.orientationListening = true;
  }

  private stopOrientation(): void {
    if (!this.orientationListening) return;
    window.removeEventListener("deviceorientationabsolute", this.handleOrientation);
    window.removeEventListener("deviceorientation", this.handleOrientation);
    this.orientationListening = false;
  }

  private handleOrientation = (event: DeviceOrientationEvent): void => {
    const heading = orientationHeading(event as CompassOrientationEvent, screenOrientationAngle());
    if (heading === undefined) return;
    this.compassHeading = heading;
    if (this.compassTimer !== undefined) window.clearTimeout(this.compassTimer);
    this.compassTimer = window.setTimeout(() => {
      this.compassHeading = undefined;
      this.updateMarkerHeading();
    }, COMPASS_FRESHNESS_MS);
    this.updateMarkerHeading();
  };

  private updateMarkerHeading(): void {
    if (!this.marker || !this.markerElement) return;
    const heading = preferredHeading(this.compassHeading, this.movementHeading);
    this.markerElement.classList.toggle("has-heading", heading !== undefined);
    this.marker.setRotation(heading ?? 0);
  }

  private stopWatch(): void {
    if (this.watchId === undefined) return;
    navigator.geolocation.clearWatch(this.watchId);
    this.watchId = undefined;
  }

  private syncPresentation(): void {
    if (!this.button || !this.container) return;
    const presentation = locationStatePresentation(this.state);
    const label = this.blockedReason ?? presentation.label;
    this.button.title = label;
    this.button.setAttribute("aria-label", label);
    this.button.setAttribute("aria-pressed", String(presentation.pressed));
    this.container.dataset.state = this.state;
  }

  private showStatus(message: string): void {
    if (!this.statusElement) return;
    this.statusElement.textContent = message;
    this.statusElement.hidden = false;
    if (this.statusTimer !== undefined) window.clearTimeout(this.statusTimer);
    this.statusTimer = window.setTimeout(() => {
      if (this.statusElement) this.statusElement.hidden = true;
    }, STATUS_DURATION_MS);
  }
}
