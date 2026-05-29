# Agent Documentation: Blaeu GPX Cartographer

This document details the architecture, design choices, and implementation details of **Blaeu**, a standalone GPX cartography application.

---

## 1. Application Architecture

Blaeu is built using a lightweight **Python (Flask) backend** and a single-page **HTML5/CSS3/Vanilla JS frontend**. 

### Storage & Persistence
- **SQLite Database**: A database file (`blaeu.db`) is stored in the mounted volume `/data`. The database organizes route metadata, calculated statistics, folders, and tags.
- **Physical GPX Files**: Uploaded or synchronized GPX logs are saved on the filesystem as `/data/gpx/{filename}` (where uploaded files are named `{sha256_hash}.gpx` and Garmin activities are named `garmin_{activity_id}.gpx`).
- **Garmin Tokens Session Store**: Authenticated session tokens/credentials returned by Garmin Connect are persisted in `/data/garmin_tokens/{user_id}` for session restoration and token renewal.
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
- Once fetched, tiles are cached to the mounted volume `/data/tiles_cache/`. Subsequent loads serve directly from the disk cache, improving performance, saving bandwidth, and offering offline support.

### C. Garmin Connect Integration (`app.py`)
Integrates the `garminconnect` API to pull activities directly.
- **Stateless Authentication**: Login credentials (email, password) are processed in memory and never stored in the database.
- **MFA Interactive Flow**: Intercepts MFA email challenges by subclassing `GarminConnectAuthenticationError` into `MfaRequiredException` to signal the frontend to display a live verification code prompt.
- **Token Store**: Persists session tokens to the filesystem to perform background token renewals without requiring repeated logins.
- **Rate-Limiting**: Catches and translates Garmin's rate limits (429 HTTP) to keep server operations clean.

### D. Server-Side Video Transcoder (`app.py`)
Transcodes client-side WebM frame recordings into standardized video formats.
- **Constant Framerate Alignment**: Invokes a local `ffmpeg` subprocess using the video filter `setpts=N/(FPS*TB)` to recalculate timestamps based on the frame index, forcing a constant framerate and preventing slow-motion playback.
- **MP4 & WebM Support**: Converts input WebM streams into H.264 MP4 videos (`-c:v libx264 -crf 20 -pix_fmt yuv420p`) or high-quality WebM videos (`-c:v libvpx -crf 4`).

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
- **Typography**: Modern typography utilizing Google Fonts' "Outfit" for headers/text and "JetBrains Mono" for numeric displays.
- **Map Visual Treatment**: Leaflet map tiles are styled with a modern dark-mode inversion filter (`invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%)`), coupled with a custom radial vignette overlay.
- **Micro-Animations**: Features smooth rotational HUD logo animation and dynamic compass needle orientation.

### Playback Control & Animation modes
- **Real-Time Mode**: Plays back the route reflecting actual recorded velocities, ignoring long static pauses.
- **Smooth Mode**: Applies a 7-point moving average filter over GPX coordinates to smooth out GPS jitter, providing fluid route tracing and viewport camera panning.

### Start/Finish Privacy Zone
- Masks precise start and finish coordinates within a user-configurable range from 0m to 1000m.
- Performs client-side geodetic coordinate filtering using the Haversine formula dynamically in the browser.
- Integrates seamlessly with all presentation layers: Leaflet map drawing, playback animations, video exports, and dashboard miniature preview cards.

---

## 4. Multi-Format Video Exporting

The animation is recorded client-side and refined server-side:
1. Preloads all tile images needed to cover the canvas viewport centered at the current map view and zoom.
2. A hidden `<canvas>` element draws the map tiles, applies the dark-mode inversion filter, renders a dark vignette overlay, and animates the GPX path.
3. Proportional vector scaling maps lines, circles, and texts dynamically so that exports look identical at all resolutions (**720p**, **1080p**, **2160p/4K**).
4. The `MediaRecorder` captures the stream at a user-defined FPS and posts the WebM blob to `/api/convert-video`.
5. The backend transcoder outputs the selected video format (WebM or MP4) for download.

---

## 5. Automated Verification

The test suite covers both backend logic and integration flows:
- **`tests/test_gpx_parser.py`**: Validates the statistics logic against single-track, multi-track, and no-data edge cases.
- **`tests/test_api.py`**: Verifies duplicate uploading hashes, folder CRUD operations, tag filters, route detail edits, and Garmin sync/authentication mock flows.
- **`tests/test_integration.py`**: Playwright integration tests simulating UI flows (uploading GPX, configuring privacy settings, playing animations).

Run tests using:
```bash
PYTHONPATH=. ./venv/bin/pytest tests/
```

---

## 6. How to Deploy

Start the application inside the standalone Docker container:
```bash
docker compose up -d --build
```
Access the application locally at `http://localhost:5000`. All persistent data is stored in the `blaeu_data` Docker volume.
