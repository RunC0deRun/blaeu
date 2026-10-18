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

1. Create and activate a Python virtual environment, with the same Python version as the Docker image and CI (3.14; its SQLite must support loading extensions, which the AI coach's library search needs):
   ```bash
   python3.14 -m venv venv
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
| `CARTO_API_KEY` | *None* | API key for the CARTO Dark Matter basemap tiles. Required since CARTO's September 2026 basemap terms; without it every tile shows "API KEY REQUIRED". Request a free key at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey). |
| `BLAEU_OVERPASS_URL` | `https://overpass-api.de/api` | Overpass API endpoint used by OSMnx to fetch poster map data. Point it at another Overpass instance if the public one is overloaded. |
| `BLAEU_OVERPASS_TIMEOUT` | `180` | Timeout in seconds for Overpass queries when generating poster maps. |
| `BLAEU_WORKER_TIMEOUT` | `300` | Gunicorn worker timeout in seconds. Keep it above `BLAEU_OVERPASS_TIMEOUT`, since poster maps are generated within the request. |
| `BLAEU_OVERPASS_MIN_INTERVAL` | `5` | Minimum seconds between background poster generations, which are processed one at a time per worker process. |
| `BLAEU_POSTER_MAX_ATTEMPTS` | `5` | Attempts for a background poster before it is marked failed. When Overpass is unreachable, the queue backs off exponentially (30s, 60s, … up to 15 min) before retrying. |
| `BLAEU_DISABLE_AUTO_SYNC` | `false` | Set to `true` to stop the background Garmin/Intervals.icu auto-sync, regardless of user settings. Used by the test instance; manual syncs and imports still work. |
| `BLAEU_INTERVALS_REQUEST_DELAY` | `1.0` | Seconds to wait between Intervals.icu requests during syncs, to stay well clear of API throttling. |
| `BLAEU_THREADS` | `8` | Threads per Gunicorn worker. AI coach answers stream for a while; threads keep the app responsive meanwhile. |
| `BLAEU_LLM_BASE_URL` | *None* | Default OpenAI-compatible endpoint for the AI coach, e.g. `http://host.docker.internal:11434/v1` for Ollama on the host. Admins can also set it in Settings > Admin. |
| `BLAEU_LLM_MODEL` | *None* | Default model for the AI coach, e.g. `qwen3:8b`. |
| `BLAEU_LLM_API_KEY` | *None* | API key for a hosted LLM provider. When set, it overrides a key stored through the admin page, so the secret stays out of the database. |
| `BLAEU_LLM_ENABLED` | `false` | Enables the AI coach when it's configured only through environment variables. |
| `BLAEU_LLM_CONTEXT_WINDOW` | `8192` | Context window of the model, in tokens. Blaeu fits the training summary and conversation into it. |
| `BLAEU_LLM_REASONING` | *Model default* | How long a reasoning model thinks: `off`, `low`, `medium` or `high`. See [Choosing a model](#choosing-a-model). |
| `BLAEU_LLM_EMBEDDING_MODEL` | *None* | Embedding model for the training literature library, e.g. `bge-m3`. Without it, documents can be uploaded but aren't searchable. |
| `BLAEU_LLM_EMBEDDING_BASE_URL` | *Chat endpoint* | Separate OpenAI-compatible endpoint for embeddings, e.g. a local Ollama next to a hosted chat model. It never receives `BLAEU_LLM_API_KEY`. |
| `BLAEU_LLM_LIBRARY_SEARCH` | `auto` | How the coach uses the library: `auto`, `tools` (the coach decides what to look up), `always` (search with the question first) or `off`. |
| `BLAEU_LLM_DATA_TOOLS` | `auto` | Whether the coach may look up training data with tools (`auto`: when the model supports tools) or only uses the overview in its prompt (`off`). |

---

## 🤖 AI Coach

Blaeu can coach athletes through a chat panel on the dashboard. The coach sees the athlete's goals (Settings > Coach), recent training, thresholds, fitness and, with their opt-in, wellness data.

1. Run an LLM. For privacy, run it locally with [Ollama](https://ollama.com), for example `ollama pull qwen3:8b` (see [Choosing a model](#choosing-a-model)). Any OpenAI-compatible endpoint (OpenAI, OpenRouter, vLLM, LM Studio, llama.cpp) works too.
2. As an admin, open Settings > Admin > AI Coach, enter the endpoint URL (`http://host.docker.internal:11434/v1` reaches Ollama on the Docker host) and the model, click **Test connection**, enable the coach and save.
3. Users then see an **AI Coach** button on the dashboard.

With Ollama, Blaeu sets the context window per request, so long prompts aren't silently cut off. When the endpoint is outside the local network, admins and users are told that training data is sent to that provider.

### Training literature library

Users can add training books and articles in Settings > Library: personal documents only they can see, and (for admins) a shared library for everyone. Blaeu extracts the text (PDF, EPUB, Markdown or plain text; scanned books without a text layer aren't supported), splits it into passages and indexes them in the background. *Try a search* shows which passages would be found for a question.

Search combines meaning and keywords, so it needs an **embedding model** (Settings > Admin > AI Coach). Use a multilingual one such as `bge-m3` (`ollama pull bge-m3`, about 1.2 GB): with it, a question in Dutch finds the right passage in an English book. Changing the embedding model re-indexes the whole library in the background.

The coach's prompt carries a compact overview of the recent past (profile, thresholds, fitness, the last 7 days of workouts and 4 weeks of totals). When a question needs more, a model with tool support looks it up itself: workouts in a period, one workout in detail (time in heart rate and pace zones, pace, intervals), totals per week or month, the fitness trend and, with the athlete's opt-in, wellness. **Training data lookups** (Settings > Admin > AI Coach) can turn this off; models without tool support get a longer overview instead.

When a user asks the coach a question, the coach searches the library itself: it first looks at the athlete's training data, then searches for the topics that matter (for "plan tomorrow" after a race, for example "recovery after a race"), up to three times per answer. The chat shows each search, the answer cites passages like [1], and the sources are listed below it with book, chapter and page. **Library search** (Settings > Admin > AI Coach) controls this: *Automatic* uses tool calling when the model supports it (Ollama reports this per model) and otherwise searches once with the question before answering. Searching takes an extra round, so expect answers to take longer; with the library in use, set the context window to 16,384 tokens.

Only upload material you're allowed to use; documents in the shared library are visible to every user of the server.

### Choosing a model

The coach needs a model that follows instructions, reasons correctly with numbers and answers in the athlete's language. It doesn't need a huge context: Blaeu's training summary is about 2,000 tokens, and the default 8,192-token window leaves room for a conversation.

**Reasoning** (Settings > Admin > AI Coach) decides how long a reasoning model such as Qwen3 thinks before answering. For Ollama it is sent as `think`, for other endpoints as `reasoning_effort`; *Model default* sends nothing. Reasoning makes answers noticeably more accurate, but at the highest levels it takes minutes. Start with **Low**.

What we measured, with the same question about a real training week (Qwen3.8 27B, Q4_K_M, on a GPU server generating about 35 tokens per second):

| Reasoning | Answer starts after | Complete after | Quality |
|---|---|---|---|
| Model default (highest) | 146 s | 167 s | Accurate and thorough |
| Medium | 36 s | 67 s | Good, with a small factual slip |
| **Low** | **29 s** | **47 s** | **Accurate, with the most useful insight: the best balance** |
| Off | 5 s | 36 s | Fluent, but with factual mistakes (a race placed in the wrong week, wrong pace arithmetic) |

Suggested starting points, by the memory available for the model (download sizes of the 4-bit versions in the [Ollama library](https://ollama.com/library)):

| Memory for the model | Model | Download | Notes |
|---|---|---|---|
| About 8 GB | `qwen3:8b` | 5.2 GB | Reasoning model; set Reasoning to Low |
| About 12 GB | `qwen3:14b` | 9.3 GB | Reasoning model; set Reasoning to Low |
| About 12 GB | `gemma3:12b` | 8.2 GB | No reasoning, so quick; check its numbers |
| 16 GB or more | `gpt-oss:20b` | 14 GB | Reasoning levels low, medium or high |
| 20–24 GB | Qwen3.8 27B (`hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M`) | 17 GB | The model measured above |

Only Qwen3.8 27B has been tested with Blaeu so far; the others are suggestions based on their size and features, and newer model families appear all the time. To evaluate a model, use **Test connection**, then ask the coach about your own last week and check the numbers it quotes against your training log. On a CPU without a GPU, expect answers several times slower than in the table.

A larger context window (for example 16,384) allows longer conversations but needs more memory on the Ollama server.

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
