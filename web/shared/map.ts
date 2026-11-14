// Leaflet map with the phone's position, GPS accuracy circle and heading marker.

import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import type { GpsStatus, HeadingStatus } from '../shared/status';
import { MARKER_SIZE_PX, dimmed, markerKey, markerShape, markerSvg, type MarkerShape } from './marker';

const START_ZOOM = 19;
const MAX_ZOOM = 21;
const MAX_NATIVE_ZOOM = 19;
const CIRCLE_COLOR = '#4c8dff';
const FRESH_STYLE: L.PathOptions = { opacity: 1, fillOpacity: 0.15 };
const STALE_STYLE: L.PathOptions = { opacity: 0.4, fillOpacity: 0.05 };

export interface PositionMap {
  update(gps: GpsStatus | null, heading: HeadingStatus | null): void;
  /** Dim everything: the viewer lost the service, so the position is no longer live. */
  markStale(): void;
}

function markerIcon(shape: MarkerShape): L.DivIcon {
  return L.divIcon({
    className: '',
    html: markerSvg(shape),
    iconSize: [MARKER_SIZE_PX, MARKER_SIZE_PX],
    iconAnchor: [MARKER_SIZE_PX / 2, MARKER_SIZE_PX / 2],
  });
}

export function createPositionMap(element: HTMLElement): PositionMap {
  const map = L.map(element, { zoomControl: true, attributionControl: true }).setView([0, 0], 2);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: MAX_ZOOM,
    maxNativeZoom: MAX_NATIVE_ZOOM,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  let marker: L.Marker | null = null;
  let circle: L.Circle | null = null;
  let centred = false;
  let shape: MarkerShape | null = null;

  const showShape = (next: MarkerShape): void => {
    if (marker !== null && (shape === null || markerKey(shape) !== markerKey(next))) {
      marker.setIcon(markerIcon(next));
    }
    shape = next;
  };

  return {
    update(gps, heading) {
      if (gps === null) return;
      const position = L.latLng(gps.lat, gps.lon);
      const next = markerShape(heading, gps.fresh);
      if (marker === null || circle === null) {
        circle = L.circle(position, { radius: gps.accuracy_m, color: CIRCLE_COLOR, weight: 1 }).addTo(map);
        marker = L.marker(position, { icon: markerIcon(next), keyboard: false }).addTo(map);
        shape = next;
      } else {
        circle.setLatLng(position).setRadius(gps.accuracy_m);
        marker.setLatLng(position);
        showShape(next);
      }
      if (!centred) {
        map.setView(position, START_ZOOM);
        centred = true;
      }
      circle.setStyle(gps.fresh ? FRESH_STYLE : STALE_STYLE);
    },
    markStale() {
      circle?.setStyle(STALE_STYLE);
      if (shape !== null) showShape(dimmed(shape));
    },
  };
}
