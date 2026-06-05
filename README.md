# Blaeu: A GPX Cartographer

Blaeu is a standalone, self-hosted web application that parses GPX files, generates detailed activity statistics, plays route animations on a dark-mode map, and exports high-fidelity video animations. 

Built with a clean **Flask + SQLite** backend and a responsive, futuristic **Vanilla JS + CSS HUD** frontend, Blaeu respects map provider policies using an integrated tile proxy and local cache.

---

## ✨ Features

- **GPX Parsing & Stats Extraction**: Zero-dependency parser calculates **11 key statistics** (distance, duration, elevation gain/loss, total average speed, moving average speed, max speed, and counts for waypoints, tracks, segments, and points).
- **Start/Finish Privacy Zone**: Configurable range from **0m (disabled) to 1000m** (in 200m increments) to mask exact starting and ending points. Coordinates and waypoints are dynamically cropped client-side on-the-fly across Leaflet maps, playback animations, video exports, and dashboard miniatures without modifying the source GPX file on disk.
- **Automated Garmin Connect Integration**: Sync activities directly from Garmin Connect. Login credentials are processed securely in memory and never stored in the database. Supports Multi-Factor Authentication (MFA) via live interactive prompts, stateless session token persistence, and robust rate-limit (429 HTTP) error handling.
- **Modern HUD Dark-Mode Interface**: Visual design styled with neon cyan and purple accents, glassmorphic panels, rotating compass micro-animations, and a native dark map theme (**CartoDB Dark Matter**).
- **Selectable Playback Animation Modes**:
  - **Real-Time Mode**: Plays back the route reflecting actual recorded velocities, ignoring long static pauses.
  - **Smooth Mode**: Applies a 7-point moving average filter over GPX coordinates to smooth out GPS jitter, providing fluid route tracing and viewport camera panning.
- **Customizable Video Exporter**: 
  - Dynamic camera-following tracking: Map tiles and vectors slide smoothly under a centered active marker.
  - Configurable resolutions: Export in **720p**, **1080p**, or **2160p (4K)**.
  - Proportional vector scaling: Lines, markers, text, and layout scale automatically to remain visually identical at any resolution.
  - Configurable framerates: Export at **24**, **25**, **30**, **50**, or **60 FPS**.
  - Multiple formats: High-quality server-side transcoding (via FFmpeg in the container) supports **WebM** and **MP4 (H.264/YUV420p)** exports with constant framerate mapping.
- **Minimalist Poster Background Maps (v0.5.0)**: Toggle between default Dark Matter tiles and 17 minimalist map themes (Noir, Blueprint, Sunset, Neon Cyberpunk, etc.). Maps are generated dynamically on-the-fly on the backend using `osmnx` (cached locally) and rendered at high resolution (300 DPI) via `matplotlib`.
- **Organization & Search**: CRUD operations for folder hierarchies, tagging support, and chronological timeline ledger.
- **Tile Proxy & CORS Protection**: Serves tiles from the same origin to avoid Canvas staining, caching them locally on disk to protect external map services.
- **Duplicate Prevention**: Computes SHA-256 hashes of uploaded GPX logs to reject duplicate uploads.

---

## 🚀 Getting Started

### Method 1: Running with Docker Compose (Recommended)

1. Clone this repository to your local system:
   ```bash
   git clone https://github.com/jan/blaeu.git
   cd blaeu
   ```

2. Build and launch the container in detached mode:
   ```bash
   docker compose up -d --build
   ```

3. Open your browser and navigate to:
   ```text
   http://localhost:5000
   ```

All persistent data (sqlite database, parsed GPX files, Garmin session tokens, and cached map tiles) is stored in the mounted `blaeu_data` Docker volume.

---

### Method 2: Running Locally for Development

1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Ensure `ffmpeg` is installed on your host system for video format conversions.

4. Set up path variables (defaults to directory `./data`) and run the Flask server:
   ```bash
   export FLASK_ENV=development
   python app.py
   ```

5. Navigate to `http://127.0.0.1:5000` in your web browser.

---

## 🐳 Environment Variables

The following environment variables can be configured when running the Docker container:

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/data` | Directory where persistent data (database, GPX files, Garmin/Intervals.icu tokens, tiles cache) is saved. |
| `SECRET_KEY` | *Auto-generated* | Secret key for Flask session cookie encryption. We recommend setting a secure random string in production to preserve user sessions across container restarts. |
| `SESSION_COOKIE_SECURE` | `true` | Restricts session cookies to secure HTTPS connections. Set to `false` if hosting on HTTP only. |
| `BLAEU_BEHIND_PROXY` | `false` | Set to `true` if hosting Blaeu behind a reverse proxy (e.g., Nginx, Traefik) to enable Flask `ProxyFix` middleware. |
| `BLAEU_ALLOW_REGISTRATION` | `false` | Set to `true` to allow public user account registration on the login page. |

---

## 🧪 Running Tests

To run the unit test suite, execute pytest from the repository root:

```bash
PYTHONPATH=. pytest tests/
```

*Note: Playwright integration tests require a headless browser installation on your host and will be skipped if playwright dependencies are not found.*

---

## 📝 License

Distributed under the MIT License. See `LICENSE` for more information.

### Attributions & Third-Party Software
- **Map Tiles**: CartoDB (Dark Matter) under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). Map data by [OpenStreetMap](https://www.openstreetmap.org/) under [ODbL](https://opendatacommons.org/licenses/odbl/).
- **Leaflet.js**: Distributed under the [BSD 2-Clause License](https://github.com/Leaflet/Leaflet/blob/main/LICENSE).
- **fix-webm-duration.js**: Distributed under the [MIT License](https://github.com/yusitaro/fix-webm-duration/blob/master/LICENSE).
- **OSMnx**: Distributed under the [BSD 3-Clause License](https://github.com/gboeing/osmnx/blob/main/LICENSE.txt).
- **Matplotlib**: Distributed under the [PSF License Agreement](https://matplotlib.org/stable/users/project/license.html).
- **garminconnect**: Distributed under the [MIT License](https://github.com/cyberjunky/python-garminconnect/blob/master/LICENSE).
- **defusedxml**: Distributed under the [Python Software Foundation License](https://github.com/tiran/defusedxml/blob/main/LICENSE).
