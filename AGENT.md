# Agent Documentation: Blaeu GPX Cartographer

This document details the architecture, design choices, and implementation details of **Blaeu**, a standalone GPX cartography application.

---

## 1. Application Architecture

Blaeu is built using a lightweight **Python (Flask) backend** and a single-page **HTML5/CSS3/Vanilla JS frontend**. 

### Storage & Persistence
- **SQLite Database**: A database file (`blaeu.db`) is stored in the mounted volume `/data`. The database organizes route metadata, calculated statistics, folders, and tags.
- **Physical GPX Files**: Uploaded files are saved on the filesystem as `/data/gpx/{sha256_hash}.gpx` to prevent duplicate files and allow quick coordinate retrieval.
- **Map Tile Cache**: OpenStreetMap tiles are cached in `/data/tiles_cache/{z}/{x}/{y}.png`.
- **Garmin OAuth Tokens**: Session and OAuth tokens for passwordless Garmin Connect authentication are persisted under `/data/garmin_tokens/<user_id>/`.
- **Poster Maps Cache**: Rendered minimalist background map images are cached under `/data/poster_maps/`.
- **OSMnx Cache**: Raw OSM data queries fetched by OSMnx are cached to `/data/osmnx_cache/` to minimize API limits and speed up subsequent builds.

---

## 2. Key Backend Features

### A. Zero-Dependency GPX Parser (`gpx_parser.py`)
To avoid dependency bloat and guarantee robustness, we implemented a custom XML parser using Python's standard `xml.etree.ElementTree`.
- **Namespace Stripping**: Strips XML namespaces dynamically from tags, supporting any standard GPX namespace schema.
- **Haversine Distance**: Calculates the distance between consecutive coordinate pairs:
  $$d = 2 R \arcsin\left(\sqrt{\sin^2\left(\frac{\Delta \text{lat}}{2}\right) + \cos(\text{lat}_1)\cos(\text{lat}_2)\sin^2\left(\frac{\Delta \text{lon}}{2}\right)}\right)$$
- **Speed Computations**:
  1. **Average Speed (Total)**: Calculated as `total_distance / total_duration`.
  2. **Average Moving Speed**: Filters out pauses by accumulating distance and duration only when the instantaneous speed between consecutive points is $\ge 0.5 \text{ m/s}$ (approx. $1.8 \text{ km/h}$).
  3. **Maximum Speed**: Tracks the peak speed between consecutive points while filtering out noise outliers ($> 100 \text{ m/s}$).
- **Elevation and Counts**: Totals all positive changes (gain) and negative changes (loss) separately, and sums the total waypoints, tracks, segments, and points.

### B. Map Tile Proxy & Local Cache (`app.py`)
To enable HTML5 Canvas-based video exports, drawing map tiles onto the canvas cannot trigger browser CORS security exceptions.
- The Flask endpoint `/api/tiles/<z>/<x>/<y>.png` acts as a local proxy fetching tiles from OpenStreetMap.
- Once fetched, tiles are cached to the mounted volume `/data/tiles_cache/`. Subsequent loads serve directly from the disk cache, improving performance, saving bandwidth, and offering offline support for previously viewed maps.

### C. Garmin Connect Integration (`app.py`)
Provides seamless passwordless sync with Garmin:
- Implements stateless login capturing MFA requirements, allowing users to enter codes in an overlay modal.
- Extracts token sets, persists them locally, and triggers activity search (latest 15 items).
- Automatically cleans up directory folders when authentication fails and correctly routes rate-limit responses (HTTP 429).

### D. Minimalist Poster Map Generator (`poster_map.py`)
Provides stunning map backdrops using OpenStreetMap:
- Translates coordinates from EPSG:4326 to EPSG:3857 (Web Mercator) to achieve mathematically perfect alignment with Leaflet vectors.
- Clamps aspect ratios (0.5 to 2.0) and adds padding to bounding boxes.
- Queries street networks, water bodies, and parks using OSMnx, rendering them using custom thematic color configurations (loaded from `/static/themes/*.json`) via `matplotlib` at 300 DPI.


---

## 3. Frontend & UX Design

The user interface is designed with a premium, modern dark-mode HUD (Heads-Up Display) dashboard theme.

### Aesthetics
- **Color Palette**: Rich dark background (`#070a13`), panel backgrounds (`#0e1320`), neon cyan primary accents (`#00f0ff`), and glowing neon purple secondary accents (`#9333ea`).
- **Typography**: Modern typography utilizing Google Fonts' "Outfit" for headers/text and "JetBrains Mono" for numeric displays and code-style elements.
- **Map Visual Treatment**: Leaflet map tiles are styled with a modern dark-mode inversion filter (`invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%)`), coupled with a custom radial vignette overlay.
- **Micro-Animations**:
  - The HUD logo icon in the sidebar features a smooth rotational animation.
  - The map compass needle rotates dynamically during route playback to align with the active heading of movement.

### Animation & Playback Scrubber
- Route coordinates are flattened and animated frame-by-frame.
- The path draws in a bright neon cyan line with a glowing marker over a low-opacity guide line.

---

## 4. WebM Video Exporting

The animation is recorded entirely client-side using the browser's native `MediaRecorder` API.
1. Clicking **Export Video** preloads all tile images needed to cover a 1280x720 canvas viewport centered at the current map view and zoom.
2. A hidden `<canvas>` element draws the map tiles, applies the dark-mode inversion filter, renders a dark vignette overlay, and animates the GPX path.
3. Waypoints are drawn as modern nodes (neon purple with cyan border), and the route is drawn in neon cyan step-by-step over a target video duration of 12 seconds.
4. A custom watermark is drawn in the bottom-left corner of the video showing the route title and "BLAEU GPX CARTOGRAPHER" using the Outfit font.
5. The `canvas.captureStream(30)` feeds the frame buffer to the WebM recorder, exporting a high-fidelity video.

---

## 5. Automated Verification

The test suite covers both backend logic and integration flows:
- **`tests/test_gpx_parser.py`**: Validates the statistics logic against single-track, multi-track, and no-data edge cases.
- **`tests/test_api.py`**: Verifies duplicate uploading hashes (returning `409 Conflict`), folder CRUD operations, tag filters, route detail edits, and chronological route timeline lists.
- **`tests/test_integration.py`**: End-to-end browser integration tests utilizing Playwright. Simulates starting the server, loading the page, dragging and dropping a GPX file, playing the route animation, and downloading the exported WebM video file.

Run tests using:
```bash
PYTHONPATH=. ./venv/bin/pytest tests/
```

---

## 6. How to Deploy

Start the application inside the standalone Docker container:
```bash
# Build and run using Docker Compose
docker compose up -d
```
Access the application locally at `http://localhost:5000`. All persistence data (database, GPX logs, tile cache) is persisted inside the `blaeu_data` Docker volume.
