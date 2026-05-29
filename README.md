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

## 🛠️ Technology Stack

- **Backend**: Python 3.14, Flask, SQLite, Gunicorn, GarminConnect, TimezoneFinder, FFmpeg, OSMnx, Matplotlib, Geopandas, Pyproj, Shapely, Requests
- **Frontend**: HTML5, Vanilla CSS3 (Glassmorphic HUD theme), Vanilla JS, Leaflet.js (bundled locally)
- **Containerization**: Docker, Docker Compose



---

## 📁 Repository Structure

```text
├── app.py                  # Main Flask server & API endpoints (cached tile proxy, video transcoder, Garmin sync)
├── db.py                   # SQLite database schema, initialization, and CRUD
├── gpx_parser.py           # Custom GPX XML parser & statistics accumulator
├── Dockerfile              # Python slim image with Gunicorn and FFmpeg
├── docker-compose.yml      # Compose config mounting persistent /data volume
├── requirements.txt        # Python package dependencies
├── .gitignore              # Ignores local databases, venv, and caches
├── .dockerignore           # Excludes development folders from container builds
├── AGENT.md                # System design & architecture documentation
├── static/
│   ├── css/
│   │   └── styles.css      # Custom HUD dashboard styles
│   ├── js/
│   │   └── app.js          # Interactive map drawing, animation, video exporter, and Garmin handlers
│   └── vendor/
│       └── leaflet/        # Bundled Leaflet assets for offline/standalone execution
├── templates/
│   └── index.html          # Main SPA layout and HUD control panel overlays
└── tests/
    ├── test_api.py         # Unit tests for Flask API endpoints & Garmin mock logic
    ├── test_gpx_parser.py  # Unit tests for GPX statistics parsing
    └── test_integration.py # Integration test suite utilizing Playwright
```

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

## 🧪 Running Tests

To run the unit test suite, execute pytest from the repository root:

```bash
PYTHONPATH=. pytest tests/
```

*Note: Playwright integration tests require a headless browser installation on your host and will be skipped if playwright dependencies are not found.*

---

## 📝 License

Distributed under the MIT License. See `LICENSE` for more information.
Attributions:
- Map tiles by **CartoDB** (Dark Matter), under CC BY 3.0. Data by **OpenStreetMap**, under ODbL.
