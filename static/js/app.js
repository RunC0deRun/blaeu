// Blaeu GPX Cartographer Frontend Logic

// Global Application State
let map = null;
let currentRoute = null;
let routesList = [];
let foldersList = [];
let activeFolderFilter = '';
let activeTagFilter = '';

// Animation Playback State
let animationPoints = []; // Flattened [{lat, lon, ele, time, elapsed}]
let totalDuration = 0; // seconds
let currentTime = 0; // current playback seconds
let isPlaying = false;
let speedMultiplier = 200;
let lastFrameTime = 0;
let animationFrameId = null;

// Map layers
let routePolyline = null;
let animatedPolyline = null;
let animationMarker = null;
let waypointMarkersGroup = null;

// Initialize Page
document.addEventListener('DOMContentLoaded', () => {
    initMap();
    initAppEvents();
    loadFolders();
    loadTags();
    loadRoutes();
});

// Initialize Leaflet Map
function initMap() {
    // Initial map setup targeting Europe center
    map = L.map('map', {
        zoomControl: true,
        preferCanvas: true
    }).setView([50.0, 10.0], 4);

    // Use our cached tile proxy endpoint
    L.tileLayer('/api/tiles/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
    }).addTo(map);

    waypointMarkersGroup = L.layerGroup().addTo(map);
}

// Bind DOM and custom event handlers
function initAppEvents() {
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('file-input');
    const fileNameDisplay = document.getElementById('file-name-display');
    const uploadForm = document.getElementById('upload-form');
    
    // Drag and drop events
    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('dragover');
    });
    
    dropzone.addEventListener('dragleave', () => {
        dropzone.classList.remove('dragover');
    });
    
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            fileInput.files = e.dataTransfer.files;
            fileNameDisplay.textContent = e.dataTransfer.files[0].name;
        }
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) {
            fileNameDisplay.textContent = fileInput.files[0].name;
        }
    });

    // Upload Form Submit
    uploadForm.addEventListener('submit', (e) => {
        e.preventDefault();
        handleUpload();
    });

    // Filter events
    document.getElementById('folder-filter').addEventListener('change', (e) => {
        activeFolderFilter = e.target.value;
        renderRoutesLedger();
    });

    // Folder Modal Events
    document.getElementById('manage-folders-btn').addEventListener('click', openFoldersModal);
    document.getElementById('close-folders-modal-btn').addEventListener('click', closeFoldersModal);
    document.getElementById('create-folder-form').addEventListener('submit', handleCreateFolder);

    // Edit Route Details Events
    document.getElementById('edit-route-btn').addEventListener('click', openEditRouteModal);
    document.getElementById('cancel-edit-btn').addEventListener('click', closeEditRouteModal);
    document.getElementById('edit-route-form').addEventListener('submit', handleEditRoute);
    
    // Delete Route
    document.getElementById('delete-route-btn').addEventListener('click', handleDeleteRoute);

    // Animation Controls
    document.getElementById('play-pause-btn').addEventListener('click', toggleAnimation);
    document.getElementById('stop-btn').addEventListener('click', stopAnimation);
    
    const scrubber = document.getElementById('animation-scrubber');
    scrubber.addEventListener('input', (e) => {
        if (animationPoints.length === 0) return;
        currentTime = (e.target.value / 100) * totalDuration;
        updateAnimationState(false);
    });

    document.getElementById('speed-select').addEventListener('change', (e) => {
        speedMultiplier = parseInt(e.target.value, 10);
    });

    // Video Export
    document.getElementById('export-video-btn').addEventListener('click', exportVideo);

    // Back to Dashboard
    document.getElementById('back-to-dashboard-btn').addEventListener('click', deselectRoute);
}

// ----------------------------------------------------
// API Requests & Content Loading
// ----------------------------------------------------

async function loadFolders() {
    try {
        const res = await fetch('/api/folders');
        foldersList = await res.json();
        
        // Populate select filter & upload dropdowns
        const folderFilter = document.getElementById('folder-filter');
        const uploadFolder = document.getElementById('upload-folder');
        const editFolder = document.getElementById('edit-folder');
        
        const folderOpts = foldersList.map(f => `<option value="${f.id}">${escapeHTML(f.name)}</option>`).join('');
        
        folderFilter.innerHTML = '<option value="">All Folders</option>' + folderOpts;
        uploadFolder.innerHTML = '<option value="">-- No Folder --</option>' + folderOpts;
        editFolder.innerHTML = '<option value="">-- No Folder --</option>' + folderOpts;
    } catch (err) {
        console.error("Error loading folders", err);
    }
}

async function loadTags() {
    try {
        const res = await fetch('/api/tags');
        const tags = await res.json();
        
        const filterList = document.getElementById('tags-filter-list');
        if (tags.length === 0) {
            filterList.innerHTML = '';
            return;
        }

        filterList.innerHTML = tags.map(tag => {
            const activeClass = activeTagFilter === tag.name ? 'active' : '';
            return `<span class="tag-badge ${activeClass}" onclick="toggleTagFilter('${tag.name}')">#${escapeHTML(tag.name)}</span>`;
        }).join('');
    } catch (err) {
        console.error("Error loading tags", err);
    }
}

function toggleTagFilter(tagName) {
    if (activeTagFilter === tagName) {
        activeTagFilter = '';
    } else {
        activeTagFilter = tagName;
    }
    loadTags();
    renderRoutesLedger();
}

async function loadRoutes() {
    try {
        const res = await fetch('/api/routes');
        routesList = await res.json();
        renderRoutesLedger();
        renderDashboardGrid();
        
        if (currentRoute) {
            showDashboardView(false);
        } else {
            showDashboardView(true);
        }
    } catch (err) {
        console.error("Error loading routes", err);
    }
}

function renderRoutesLedger() {
    const listContainer = document.getElementById('routes-timeline');
    
    // Apply client-side filters
    let filtered = routesList;
    if (activeFolderFilter) {
        filtered = filtered.filter(r => r.folder_id == activeFolderFilter);
    }
    if (activeTagFilter) {
        filtered = filtered.filter(r => r.tags && r.tags.includes(activeTagFilter));
    }

    if (filtered.length === 0) {
        listContainer.innerHTML = '<div class="timeline-empty">No routes match the filters.</div>';
        return;
    }

    listContainer.innerHTML = filtered.map(route => {
        const activeClass = currentRoute && currentRoute.id === route.id ? 'active' : '';
        const distanceStr = formatDistance(route.total_distance);
        const durationStr = formatDuration(route.duration);
        const folderBadge = route.folder_name ? `<span class="folder-badge">${escapeHTML(route.folder_name)}</span>` : '';
        
        const dateObj = new Date(route.created_at + 'Z');
        const formatOptions = { 
            year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            hour12: false
        };
        if (route.timezone) {
            formatOptions.timeZone = route.timezone;
        }
        let formattedDate = dateObj.toLocaleDateString(undefined, formatOptions);
        if (route.timezone_abbr) {
            formattedDate += ` ${route.timezone_abbr}`;
        }

        return `
            <div class="timeline-item ${activeClass}" onclick="selectRoute(${route.id})">
                <div class="timeline-date">${formattedDate}</div>
                <h4 class="timeline-title">${escapeHTML(route.name)}</h4>
                <p class="timeline-desc">${escapeHTML(route.description || 'No description provided.')}</p>
                <div class="timeline-meta">
                    ${folderBadge}
                    <span class="tag-badge btn-sm">${distanceStr}</span>
                    <span class="tag-badge btn-sm">${durationStr}</span>
                </div>
            </div>
        `;
    }).join('');
}

// Manage transitions between Dashboard Mode and Map Mode
function showDashboardView(show) {
    const dashboardView = document.getElementById('dashboard-view');
    const mapViewport = document.querySelector('.map-viewport');
    const routeDetailsPanel = document.getElementById('route-details-panel');
    const sidebarLedger = document.getElementById('sidebar-ledger-container');
    const animationHUD = document.getElementById('animation-controller-overlay');
    
    if (show) {
        dashboardView.classList.remove('hidden');
        mapViewport.classList.add('hidden');
        routeDetailsPanel.classList.add('hidden');
        sidebarLedger.classList.add('hidden');
        animationHUD.classList.add('hidden');
        currentRoute = null;
        // Clean active classes on timeline items
        const activeItems = document.querySelectorAll('.timeline-item.active');
        activeItems.forEach(item => item.classList.remove('active'));
    } else {
        dashboardView.classList.add('hidden');
        mapViewport.classList.remove('hidden');
        sidebarLedger.classList.remove('hidden');
        
        // Fix Leaflet sizing after DOM display update
        if (map) {
            setTimeout(() => {
                map.invalidateSize();
            }, 100);
        }
    }
}

// Deselect selected route and return to dashboard
function deselectRoute() {
    stopAnimation();
    if (routePolyline) map.removeLayer(routePolyline);
    if (animatedPolyline) map.removeLayer(animatedPolyline);
    if (animationMarker) map.removeLayer(animationMarker);
    waypointMarkersGroup.clearLayers();
    
    currentRoute = null;
    showDashboardView(true);
    renderRoutesLedger();
    renderDashboardGrid();
}

// Render Dashboard View grid and aggregate statistics banner
function renderDashboardGrid() {
    const gridContainer = document.getElementById('dashboard-grid');
    const activityCountEl = document.getElementById('dashboard-activity-count');
    
    // Apply filters
    let filtered = routesList;
    if (activeFolderFilter) {
        filtered = filtered.filter(r => r.folder_id == activeFolderFilter);
    }
    if (activeTagFilter) {
        filtered = filtered.filter(r => r.tags && r.tags.includes(activeTagFilter));
    }
    
    // Update count
    activityCountEl.textContent = filtered.length === 1 ? '1 Activity Loaded' : `${filtered.length} Activities Loaded`;
    
    // Calculate aggregate statistics
    let totalDist = 0;
    let totalGain = 0;
    let totalDur = 0;
    
    filtered.forEach(r => {
        totalDist += r.total_distance || 0;
        totalGain += r.elevation_gain || 0;
        totalDur += r.duration || 0;
    });
    
    document.getElementById('summary-total-distance').textContent = formatDistance(totalDist);
    document.getElementById('summary-total-gain').textContent = `${Math.round(totalGain)} m`;
    document.getElementById('summary-total-duration').textContent = formatDuration(totalDur);
    
    if (filtered.length === 0) {
        gridContainer.innerHTML = '<div class="timeline-empty" style="grid-column: 1/-1;">No activities match the current filters. Import a GPX file to get started.</div>';
        return;
    }
    
    gridContainer.innerHTML = filtered.map(route => {
        const folderBadge = route.folder_name ? `<span class="folder-badge">${escapeHTML(route.folder_name)}</span>` : '';
        const distanceStr = formatDistance(route.total_distance);
        const durationStr = formatDuration(route.duration);
        const gainStr = `${Math.round(route.elevation_gain || 0)} m`;
        const avgSpeedStr = formatSpeed(route.avg_speed);
        
        const dateObj = new Date(route.created_at + 'Z');
        const formatOptions = { 
            year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            hour12: false
        };
        if (route.timezone) {
            formatOptions.timeZone = route.timezone;
        }
        let formattedDate = dateObj.toLocaleDateString(undefined, formatOptions);
        if (route.timezone_abbr) {
            formattedDate += ` ${route.timezone_abbr}`;
        }
        
        const tagsHtml = route.tags ? route.tags.map(t => `<span class="tag-badge" onclick="event.stopPropagation(); toggleTagFilter('${t}')">#${escapeHTML(t)}</span>`).join('') : '';

        return `
            <div class="activity-card" onclick="selectRoute(${route.id})">
                <div class="activity-card-preview">
                    <canvas class="activity-mini-canvas" data-route-id="${route.id}"></canvas>
                </div>
                <div class="activity-card-body">
                    <div class="activity-card-time">${formattedDate}</div>
                    <h3 class="activity-card-title" title="${escapeHTML(route.name)}">${escapeHTML(route.name)}</h3>
                    <p class="activity-card-desc">${escapeHTML(route.description || 'No description provided.')}</p>
                    <div class="activity-card-stats-grid">
                        <div class="activity-card-stat">
                            <span class="stat-label">Distance</span>
                            <span class="stat-value">${distanceStr}</span>
                        </div>
                        <div class="activity-card-stat">
                            <span class="stat-label">Elev Gain</span>
                            <span class="stat-value">${gainStr}</span>
                        </div>
                        <div class="activity-card-stat">
                            <span class="stat-label">Duration</span>
                            <span class="stat-value">${durationStr}</span>
                        </div>
                        <div class="activity-card-stat">
                            <span class="stat-label">Avg Speed</span>
                            <span class="stat-value">${avgSpeedStr}</span>
                        </div>
                    </div>
                </div>
                <div class="activity-card-footer">
                    <div class="activity-card-tags">
                        ${folderBadge}
                        ${tagsHtml}
                    </div>
                    <div class="activity-card-actions">
                        <button class="btn-icon" onclick="editRouteFromDashboard(${route.id}, event)" title="Edit details">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                                <path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                            </svg>
                        </button>
                        <button class="btn-icon" onclick="deleteRouteFromDashboard(${route.id}, '${escapeHTML(route.name)}', event)" title="Delete activity">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');
    
    // Draw miniature canvases
    const canvases = gridContainer.querySelectorAll('.activity-mini-canvas');
    canvases.forEach(canvas => {
        const routeId = parseInt(canvas.getAttribute('data-route-id'), 10);
        const route = filtered.find(r => r.id === routeId);
        if (route && route.simplified_path) {
            drawRouteMiniature(canvas, route.simplified_path);
        }
    });
}

// Draw a glowing neon route path miniature on a card canvas
function drawRouteMiniature(canvas, coords) {
    const ctx = canvas.getContext('2d');
    
    // Set actual canvas size to match layout size
    canvas.width = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
    
    if (!coords || coords.length < 2) {
        // Draw centered placeholder text
        ctx.fillStyle = '#94a3b8';
        ctx.font = '12px Outfit';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('No route path available', canvas.width / 2, canvas.height / 2);
        return;
    }
    
    // Find bounds
    let minLat = Infinity, maxLat = -Infinity;
    let minLon = Infinity, maxLon = -Infinity;
    
    coords.forEach(pt => {
        const lat = pt[0];
        const lon = pt[1];
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
        if (lon < minLon) minLon = lon;
        if (lon > maxLon) maxLon = lon;
    });
    
    const latRange = maxLat - minLat;
    const lonRange = maxLon - minLon;
    
    const padding = 30;
    const w = canvas.width - padding;
    const h = canvas.height - padding;
    
    // Scale factor keeping aspect ratio (correct for mercator aspect ratio approx)
    const latCos = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
    const lonWidth = lonRange * latCos;
    const latHeight = latRange;
    
    const scaleX = w / (lonWidth || 0.0001);
    const scaleY = h / (latHeight || 0.0001);
    const scale = Math.min(scaleX, scaleY);
    
    // Center translation
    const offsetX = (canvas.width - lonWidth * scale) / 2;
    const offsetY = (canvas.height - latHeight * scale) / 2;
    
    // Helper to convert lat/lon to canvas x/y
    function getCanvasCoords(lat, lon) {
        const x = offsetX + (lon - minLon) * latCos * scale;
        // Invert Y axis
        const y = canvas.height - (offsetY + (lat - minLat) * scale);
        return [x, y];
    }
    
    // Draw route path
    ctx.beginPath();
    const startPoint = getCanvasCoords(coords[0][0], coords[0][1]);
    ctx.moveTo(startPoint[0], startPoint[1]);
    
    for (let i = 1; i < coords.length; i++) {
        const pt = getCanvasCoords(coords[i][0], coords[i][1]);
        ctx.lineTo(pt[0], pt[1]);
    }
    
    ctx.strokeStyle = '#00f0ff';
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    
    // High-tech neon glow
    ctx.shadowColor = 'rgba(0, 240, 255, 0.7)';
    ctx.shadowBlur = 6;
    ctx.stroke();
    
    // Reset shadow for dots
    ctx.shadowBlur = 0;
    
    // Draw Start Point (Neon Cyan/White)
    const startXY = getCanvasCoords(coords[0][0], coords[0][1]);
    ctx.beginPath();
    ctx.arc(startXY[0], startXY[1], 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.strokeStyle = '#00f0ff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    
    // Draw End Point (Neon Purple)
    const endXY = getCanvasCoords(coords[coords.length - 1][0], coords[coords.length - 1][1]);
    ctx.beginPath();
    ctx.arc(endXY[0], endXY[1], 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#9333ea';
    ctx.fill();
    ctx.strokeStyle = '#00f0ff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

// Delete route shortcut from dashboard card
async function deleteRouteFromDashboard(routeId, routeName, event) {
    if (event) event.stopPropagation(); // Prevent opening map
    if (!confirm(`Are you sure you want to delete "${routeName}"? This action is permanent.`)) return;

    try {
        const res = await fetch(`/api/routes/${routeId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete route');
        
        // If the currently selected route on map was this one, clear it
        if (currentRoute && currentRoute.id === routeId) {
            deselectRoute();
        } else {
            await loadTags();
            await loadRoutes();
        }
    } catch (err) {
        alert(err.message);
    }
}

// Edit route shortcut from dashboard card
function editRouteFromDashboard(routeId, event) {
    if (event) event.stopPropagation(); // Prevent opening map
    
    // Find route in list
    const route = routesList.find(r => r.id === routeId);
    if (!route) return;
    
    // Temporarily set currentRoute so the existing modal logic works
    currentRoute = route;
    
    openEditRouteModal();
}

// Handle GPX File Upload
async function handleUpload() {
    const form = document.getElementById('upload-form');
    const uploadBtn = document.getElementById('upload-btn');
    const formData = new FormData(form);
    
    uploadBtn.disabled = true;
    uploadBtn.textContent = 'Uploading & Parsing...';

    try {
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        
        if (res.status === 409) {
            const data = await res.json();
            alert(data.error);
            if (data.route_id) {
                selectRoute(data.route_id);
            }
            return;
        }

        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.error || 'Failed to upload route');
        }

        const newRoute = await res.json();
        
        // Reset form
        form.reset();
        document.getElementById('file-name-display').textContent = '';
        
        // Reload details and list
        await loadFolders();
        await loadTags();
        await loadRoutes();
        
        // Select new route
        selectRoute(newRoute.id);
        
    } catch (err) {
        alert(err.message);
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Upload & Parse';
    }
}

// Select a route and load its full details
async function selectRoute(routeId) {
    stopAnimation();
    showDashboardView(false); // Switch to Map Mode

    try {
        const res = await fetch(`/api/routes/${routeId}`);
        if (!res.ok) throw new Error('Could not load route details');
        
        currentRoute = await res.json();
        
        // Update sidebar selection styling
        renderRoutesLedger();
        
        // Show details panel & populate statistics
        document.getElementById('route-details-panel').classList.remove('hidden');
        document.getElementById('route-title').textContent = currentRoute.name;
        document.getElementById('route-desc').textContent = currentRoute.description || 'No description provided.';
        
        // Badges tags
        const metaTags = document.getElementById('route-meta-tags');
        let metaHtml = '';
        if (currentRoute.folder_name) {
            metaHtml += `<span class="folder-badge">${escapeHTML(currentRoute.folder_name)}</span>`;
        }
        if (currentRoute.tags) {
            metaHtml += currentRoute.tags.map(t => `<span class="tag-badge">#${escapeHTML(t)}</span>`).join('');
        }
        metaTags.innerHTML = metaHtml;

        // Stats grid insertion
        document.getElementById('stat-distance').textContent = formatDistance(currentRoute.total_distance);
        document.getElementById('stat-gain').textContent = `${Math.round(currentRoute.elevation_gain)} m`;
        document.getElementById('stat-loss').textContent = `${Math.round(currentRoute.elevation_loss)} m`;
        document.getElementById('stat-duration').textContent = formatDuration(currentRoute.duration);
        document.getElementById('stat-avg-speed').textContent = formatSpeed(currentRoute.avg_speed);
        document.getElementById('stat-avg-moving-speed').textContent = formatSpeed(currentRoute.avg_moving_speed);
        document.getElementById('stat-max-speed').textContent = formatSpeed(currentRoute.max_speed);
        document.getElementById('stat-waypoints').textContent = currentRoute.waypoints_count;
        document.getElementById('stat-tracks').textContent = currentRoute.tracks_count;
        document.getElementById('stat-segments').textContent = currentRoute.segments_count;
        document.getElementById('stat-points').textContent = currentRoute.points_count;

        // Render on Map
        drawRouteOnMap();
        
        // Prepare playback animation points
        prepareAnimationPoints();
        
    } catch (err) {
        alert(err.message);
    }
}

// Render the GPX path and waypoints onto Leaflet
function drawRouteOnMap() {
    // Clear previous layers
    if (routePolyline) map.removeLayer(routePolyline);
    if (animatedPolyline) map.removeLayer(animatedPolyline);
    if (animationMarker) map.removeLayer(animationMarker);
    waypointMarkersGroup.clearLayers();

    const trackCoordinates = [];
    
    // Draw Tracks
    currentRoute.tracks.forEach(track => {
        track.segments.forEach(segment => {
            const segCoords = segment.map(pt => [pt.lat, pt.lon]);
            trackCoordinates.push(...segCoords);
        });
    });

    if (trackCoordinates.length === 0) return;

    // Draw main static route guide polyline in neon cyan (more visible dashed style)
    routePolyline = L.polyline(trackCoordinates, {
        color: '#00f0ff',
        weight: 4,
        opacity: 0.4,
        dashArray: '5, 8'
    }).addTo(map);

    // Initialise empty animated active progress polyline
    animatedPolyline = L.polyline([], {
        color: '#00f0ff',
        weight: 6,
        opacity: 1.0,
        lineCap: 'round',
        lineJoin: 'round'
    }).addTo(map);

    // Draw Waypoints as modern glowing purple markers
    currentRoute.waypoints.forEach(wpt => {
        L.circleMarker([wpt.lat, wpt.lon], {
            radius: 5,
            color: '#00f0ff',
            fillColor: '#9333ea',
            fillOpacity: 0.8,
            weight: 1.5
        })
        .bindPopup(`<strong>${escapeHTML(wpt.name || 'Waypoint')}</strong><br>${escapeHTML(wpt.desc || '')}`)
        .addTo(waypointMarkersGroup);
    });

    // Zoom Map to Route Bounds
    map.fitBounds(routePolyline.getBounds(), { padding: [30, 30] });

    // Show animation controls
    document.getElementById('animation-controller-overlay').classList.remove('hidden');
}

// ----------------------------------------------------
// Playback Animation Logic
// ----------------------------------------------------

function prepareAnimationPoints() {
    animationPoints = [];
    let startTimestamp = null;
    
    // Flatten track coordinates
    currentRoute.tracks.forEach(track => {
        track.segments.forEach(segment => {
            segment.forEach(pt => {
                let elapsed = 0;
                if (pt.time) {
                    const dt = new Date(pt.time);
                    if (startTimestamp === null) {
                        startTimestamp = dt;
                    }
                    elapsed = (dt - startTimestamp) / 1000; // seconds from start
                }
                animationPoints.push({
                    lat: pt.lat,
                    lon: pt.lon,
                    ele: pt.ele,
                    elapsed: elapsed
                });
            });
        });
    });

    // If GPX has no time, space points equally (1s interval)
    const hasTimestamps = animationPoints.some(p => p.elapsed > 0);
    if (!hasTimestamps) {
        animationPoints.forEach((pt, idx) => {
            pt.elapsed = idx * 2.0; // Space points by 2 seconds
        });
    }

    // Smooth the points to reduce animation/camera jitter
    animationPoints = smoothAnimationPoints(animationPoints, 7);

    totalDuration = animationPoints[animationPoints.length - 1].elapsed;
    currentTime = 0;
    
    // Reset scrubber interface
    document.getElementById('animation-scrubber').value = 0;
    updateAnimationState(true);
}

// Moving average coordinate smoothing helper to reduce GPS jitter
function smoothAnimationPoints(points, windowSize = 5) {
    if (points.length < windowSize) return points;
    const half = Math.floor(windowSize / 2);
    const smoothed = [];
    for (let i = 0; i < points.length; i++) {
        let latSum = 0;
        let lonSum = 0;
        let count = 0;
        for (let j = i - half; j <= i + half; j++) {
            if (j >= 0 && j < points.length) {
                latSum += points[j].lat;
                lonSum += points[j].lon;
                count++;
            }
        }
        smoothed.push({
            ...points[i],
            lat: latSum / count,
            lon: lonSum / count
        });
    }
    return smoothed;
}

function toggleAnimation() {
    if (isPlaying) {
        pauseAnimation();
    } else {
        startPlayback();
    }
}

function startPlayback() {
    if (animationPoints.length === 0) return;
    
    isPlaying = true;
    document.getElementById('icon-play').classList.add('hidden');
    document.getElementById('icon-pause').classList.remove('hidden');
    
    lastFrameTime = performance.now();
    animationFrameId = requestAnimationFrame(animationLoop);
}

function pauseAnimation() {
    isPlaying = false;
    document.getElementById('icon-play').classList.remove('hidden');
    document.getElementById('icon-pause').classList.add('hidden');
    
    if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
    }
}

function stopAnimation() {
    pauseAnimation();
    currentTime = 0;
    document.getElementById('animation-scrubber').value = 0;
    updateAnimationState(true);
}

function animationLoop(timestamp) {
    if (!isPlaying) return;

    const dt = (timestamp - lastFrameTime) / 1000; // seconds
    lastFrameTime = timestamp;

    currentTime += dt * speedMultiplier;
    
    if (currentTime >= totalDuration) {
        currentTime = totalDuration;
        pauseAnimation();
    }

    // Update scrubber position
    const pct = (currentTime / totalDuration) * 100;
    document.getElementById('animation-scrubber').value = pct;

    updateAnimationState(false);

    if (isPlaying) {
        animationFrameId = requestAnimationFrame(animationLoop);
    }
}

// Update Polyline and Marker visual state based on currentTime
function updateAnimationState(reset = false) {
    if (animationPoints.length === 0) return;

    // Find the coordinate list up to currentTime
    const pastPoints = animationPoints.filter(p => p.elapsed <= currentTime);
    
    // Interpolate exact active point if playing
    let activePoint = null;
    let heading = 0;
    
    if (pastPoints.length === 0) {
        activePoint = animationPoints[0];
    } else if (pastPoints.length === animationPoints.length) {
        activePoint = animationPoints[animationPoints.length - 1];
    } else {
        // Linearly interpolate between pastPoints.last and nextPoint
        const p1 = pastPoints[pastPoints.length - 1];
        const nextIdx = pastPoints.length;
        const p2 = animationPoints[nextIdx];
        
        const segmentDuration = p2.elapsed - p1.elapsed;
        const ratio = segmentDuration > 0 ? (currentTime - p1.elapsed) / segmentDuration : 0;
        
        activePoint = {
            lat: p1.lat + (p2.lat - p1.lat) * ratio,
            lon: p1.lon + (p2.lon - p1.lon) * ratio
        };

        // Calculate heading bearing for compass control
        const dy = p2.lat - p1.lat;
        const dx = p2.lon - p1.lon;
        heading = Math.atan2(dx, dy) * 180 / Math.PI;
    }

    // Draw active animated path
    const drawCoords = pastPoints.map(p => [p.lat, p.lon]);
    drawCoords.push([activePoint.lat, activePoint.lon]);
    
    if (animatedPolyline) {
        animatedPolyline.setLatLngs(drawCoords);
    }

    // Move Pen marker
    if (animatedPolyline) {
        if (!animationMarker) {
            // Modern neon glowing indicator marker
            animationMarker = L.circleMarker([activePoint.lat, activePoint.lon], {
                radius: 7,
                color: '#00f0ff',
                fillColor: '#ffffff',
                fillOpacity: 1,
                weight: 2
            }).addTo(map);
        } else {
            animationMarker.setLatLng([activePoint.lat, activePoint.lon]);
        }
    }

    // Rotate compass indicator rose
    if (!reset) {
        document.getElementById('map-compass').style.transform = `rotate(${heading}deg)`;
    } else {
        document.getElementById('map-compass').style.transform = `rotate(0deg)`;
    }

    // Pan map dynamically to follow active point
    if (isPlaying && activePoint) {
        map.panTo([activePoint.lat, activePoint.lon], { animate: true, duration: 0.1 });
    }

    // Update timer text
    document.getElementById('animation-time-display').textContent = `${formatDuration(currentTime)} / ${formatDuration(totalDuration)}`;
}

// ----------------------------------------------------
// Video Export Logic (Canvas Render + MediaRecorder)
// ----------------------------------------------------

async function exportVideo() {
    if (!currentRoute || animationPoints.length === 0) return;

    pauseAnimation();

    // Show Progress Dialog
    const modal = document.getElementById('export-modal');
    const fill = document.getElementById('export-progress-fill');
    const statusText = document.getElementById('export-status-text');
    
    modal.classList.remove('hidden');
    fill.style.width = '0%';
    statusText.textContent = 'Preloading map tiles...';

    // Canvas resolution configuration
    const resSelect = document.getElementById('res-select');
    const resVal = resSelect ? resSelect.value : '1080';
    let width = 1920;
    let height = 1080;
    let videoBitrate = 12000000; // 12 Mbps default for 1080p
    
    if (resVal === '720') {
        width = 1280;
        height = 720;
        videoBitrate = 6000000; // 6 Mbps for 720p
    } else if (resVal === '2160') {
        width = 3840;
        height = 2160;
        videoBitrate = 50000000; // 50 Mbps for 4K UHD
    }

    const canvas = document.getElementById('export-canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    
    // Scale factor for drawing vectors and fonts relative to baseline 1080p
    const scaleFactor = height / 1080;

    // Get current map zoom and bounds
    const zoom = map.getZoom();
    const mapBounds = map.getBounds();
    const center = map.getCenter();

    // Web Mercator Formulas
    function lngToX(lng, z) {
        return (lng + 180) / 360 * Math.pow(2, z) * 256;
    }
    function latToY(lat, z) {
        const latRad = lat * Math.PI / 180;
        return (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * Math.pow(2, z) * 256;
    }

    // Define bounds for dynamic camera-following
    let minPxX = 0;
    let minPxY = 0;

    // Collect all tiles covering the entire route path animation to preload
    const tilesSet = new Set();
    animationPoints.forEach(pt => {
        const pxX = lngToX(pt.lon, zoom);
        const pxY = latToY(pt.lat, zoom);
        const minTX = Math.floor((pxX - width / 2) / 256);
        const maxTX = Math.floor((pxX + width / 2) / 256);
        const minTY = Math.floor((pxY - height / 2) / 256);
        const maxTY = Math.floor((pxY + height / 2) / 256);
        for (let tx = minTX; tx <= maxTX; tx++) {
            for (let ty = minTY; ty <= maxTY; ty++) {
                tilesSet.add(`${tx}_${ty}`);
            }
        }
    });

    const tilesToLoad = Array.from(tilesSet).map(key => {
        const [tx, ty] = key.split('_').map(Number);
        return { x: tx, y: ty, z: zoom };
    });

    // Helper to load image
    function loadImage(url) {
        return new Promise((resolve) => {
            const img = new Image();
            img.crossOrigin = 'anonymous'; // Ensure clean origin
            img.onload = () => resolve(img);
            img.onerror = () => resolve(null); // Continue even if tile fails
            img.src = url;
        });
    }

    // Preload tiles via backend CORS-free caching proxy
    const loadedTilesMap = {};
    let loadedCount = 0;
    
    for (const t of tilesToLoad) {
        const tileUrl = `/api/tiles/${t.z}/${t.x}/${t.y}.png`;
        const img = await loadImage(tileUrl);
        if (img) {
            loadedTilesMap[`${t.x}_${t.y}`] = img;
        }
        loadedCount++;
        const progressPct = Math.round((loadedCount / tilesToLoad.length) * 30); // 0-30% progress
        fill.style.width = `${progressPct}%`;
        statusText.textContent = `Loading map background (${loadedCount}/${tilesToLoad.length})`;
    }

    statusText.textContent = 'Rendering animation frames...';

    // Record setup
    const fpsSelect = document.getElementById('fps-select');
    const fps = fpsSelect ? parseInt(fpsSelect.value, 10) : 30;

    const stream = canvas.captureStream(fps);
    let recorder;
    
    // WebM options configured with the resolution-appropriate high bitrate
    try {
        recorder = new MediaRecorder(stream, { 
            mimeType: 'video/webm;codecs=vp9',
            videoBitsPerSecond: videoBitrate
        });
    } catch (e) {
        try {
            recorder = new MediaRecorder(stream, { 
                mimeType: 'video/webm',
                videoBitsPerSecond: videoBitrate
            });
        } catch (e2) {
            alert('MediaRecorder is not supported in this browser.');
            modal.classList.add('hidden');
            return;
        }
    }

    const recordedChunks = [];
    recorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
    };

    recorder.onstop = () => {
        statusText.textContent = 'Saving video file...';
        const rawBlob = new Blob(recordedChunks, { type: 'video/webm' });
        const recordedDuration = Date.now() - recordStartTime;

        const downloadBlob = (blobToDownload) => {
            const url = URL.createObjectURL(blobToDownload);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${currentRoute.name.replace(/[^a-z0-9]/gi, '_').toLowerCase()}-animation.webm`;
            a.click();
            // Hide Modal
            modal.classList.add('hidden');
        };

        if (typeof ysFixWebmDuration === 'function') {
            statusText.textContent = 'Optimizing video duration metadata...';
            ysFixWebmDuration(rawBlob, recordedDuration, (fixedBlob) => {
                downloadBlob(fixedBlob);
            });
        } else {
            downloadBlob(rawBlob);
        }
    };

    // Calculate target video duration based on the activity total duration and the selected speed multiplier
    const targetVideoDuration = totalDuration / speedMultiplier;
    const totalFrames = targetVideoDuration * fps;
    
    const recordStartTime = Date.now();
    recorder.start();

    // Map GPX coords to Canvas pixels
    function latLngToCanvasPx(lat, lng) {
        const pxX = lngToX(lng, zoom);
        const pxY = latToY(lat, zoom);
        return {
            x: pxX - minPxX,
            y: pxY - minPxY
        };
    }

    let currentFrame = 0;
    const startTime = performance.now();

    function drawFrame() {
        const elapsedRealTime = (performance.now() - startTime) / 1000;
        let ratio = elapsedRealTime / targetVideoDuration;
        if (ratio > 1.0) {
            ratio = 1.0;
        }

        const playbackTime = ratio * totalDuration;

        // Calculate current active point on path
        const pastPts = animationPoints.filter(p => p.elapsed <= playbackTime);
        let activePt = null;
        
        if (pastPts.length === 0) {
            activePt = animationPoints[0];
        } else if (pastPts.length === animationPoints.length) {
            activePt = animationPoints[animationPoints.length - 1];
        } else {
            const p1 = pastPts[pastPts.length - 1];
            const p2 = animationPoints[pastPts.length];
            const segDur = p2.elapsed - p1.elapsed;
            const rRatio = segDur > 0 ? (playbackTime - p1.elapsed) / segDur : 0;
            activePt = {
                lat: p1.lat + (p2.lat - p1.lat) * rRatio,
                lon: p1.lon + (p2.lon - p1.lon) * rRatio
            };
        }

        // Update camera position to follow active point (centering on activePt)
        const activePxX = lngToX(activePt.lon, zoom);
        const activePxY = latToY(activePt.lat, zoom);
        minPxX = activePxX - width / 2;
        minPxY = activePxY - height / 2;

        // 1. Draw Map Tiles that cover the current viewport
        ctx.clearRect(0, 0, width, height);
        ctx.save();
        
        const frameTileMinX = Math.floor(minPxX / 256);
        const frameTileMaxX = Math.floor((minPxX + width) / 256);
        const frameTileMinY = Math.floor(minPxY / 256);
        const frameTileMaxY = Math.floor((minPxY + height) / 256);

        for (let tx = frameTileMinX; tx <= frameTileMaxX; tx++) {
            for (let ty = frameTileMinY; ty <= frameTileMaxY; ty++) {
                const img = loadedTilesMap[`${tx}_${ty}`];
                if (img) {
                    const dx = tx * 256 - minPxX;
                    const dy = ty * 256 - minPxY;
                    ctx.drawImage(img, dx, dy);
                }
            }
        }
        ctx.restore();

        // 2. Overlay modern dark radial vignette shading border
        const grad = ctx.createRadialGradient(width/2, height/2, width/3, width/2, height/2, width/1.4);
        grad.addColorStop(0, 'rgba(7, 10, 19, 0.0)');
        grad.addColorStop(1, 'rgba(7, 10, 19, 0.65)');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, width, height);

        // 3. Draw Route Path (guideline & active progress path)
        // Draw modern static route guide polyline in neon cyan (more visible dashed style)
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(0, 240, 255, 0.4)';
        ctx.lineWidth = 4 * scaleFactor;
        ctx.setLineDash([5 * scaleFactor, 8 * scaleFactor]);
        animationPoints.forEach((pt, idx) => {
            const pos = latLngToCanvasPx(pt.lat, pt.lon);
            if (idx === 0) ctx.moveTo(pos.x, pos.y);
            else ctx.lineTo(pos.x, pos.y);
        });
        ctx.stroke();
        ctx.setLineDash([]); // Reset line dash for active path

        // Draw active route path in glowing neon cyan
        ctx.beginPath();
        ctx.strokeStyle = '#00f0ff';
        ctx.lineWidth = 6 * scaleFactor;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        pastPts.forEach((pt, idx) => {
            const pos = latLngToCanvasPx(pt.lat, pt.lon);
            if (idx === 0) ctx.moveTo(pos.x, pos.y);
            else ctx.lineTo(pos.x, pos.y);
        });
        const activePos = latLngToCanvasPx(activePt.lat, activePt.lon);
        if (pastPts.length > 0) {
            ctx.lineTo(activePos.x, activePos.y);
        } else {
            ctx.moveTo(activePos.x, activePos.y);
        }
        ctx.stroke();

        // 4. Draw Waypoints circles (neon purple with cyan border)
        currentRoute.waypoints.forEach(wpt => {
            const pos = latLngToCanvasPx(wpt.lat, wpt.lon);
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, 5 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#9333ea';
            ctx.strokeStyle = '#00f0ff';
            ctx.lineWidth = 1.5 * scaleFactor;
            ctx.fill();
            ctx.stroke();
        });

        // 5. Draw Active Neon Indicator Marker
        ctx.beginPath();
        ctx.arc(activePos.x, activePos.y, 7 * scaleFactor, 0, 2 * Math.PI);
        ctx.fillStyle = '#ffffff';
        ctx.strokeStyle = '#00f0ff';
        ctx.lineWidth = 2 * scaleFactor;
        ctx.fill();
        ctx.stroke();

        // 6. Draw Modern Watermark Corner
        ctx.fillStyle = 'rgba(0, 240, 255, 0.85)';
        ctx.font = `700 ${Math.round(20 * scaleFactor)}px "Outfit", sans-serif`;
        ctx.fillText(currentRoute.name.toUpperCase(), 40 * scaleFactor, height - 65 * scaleFactor);
        ctx.fillStyle = 'rgba(147, 51, 234, 0.85)';
        ctx.font = `600 ${Math.round(11 * scaleFactor)}px "Outfit", sans-serif`;
        ctx.fillText('BLAEU GPX CARTOGRAPHER', 40 * scaleFactor, height - 45 * scaleFactor);

        // Update progress bar
        currentFrame++;
        const totalProgress = 30 + Math.round(ratio * 70); // 30-100% progress
        fill.style.width = `${totalProgress}%`;
        statusText.textContent = `Rendering frame ${currentFrame} at ${(elapsedRealTime).toFixed(1)}s / ${targetVideoDuration}s...`;

        if (elapsedRealTime >= targetVideoDuration) {
            recorder.stop();
            return;
        }

        // Schedule next frame to match the target frame intervals
        const targetNextFrameTime = ((currentFrame + 1) * 1000) / fps;
        const actualElapsedMs = performance.now() - startTime;
        const delay = Math.max(0, targetNextFrameTime - actualElapsedMs);

        setTimeout(drawFrame, delay);
    }

    // Start drawing loops
    drawFrame();
}

// ----------------------------------------------------
// Modals and Dialogue Box Operations
// ----------------------------------------------------

// Folders Management Modal
async function openFoldersModal() {
    const list = document.getElementById('folders-modal-list');
    list.innerHTML = '<li>Loading folders...</li>';
    document.getElementById('folders-modal').classList.remove('hidden');

    await renderFoldersList();
}

async function renderFoldersList() {
    const list = document.getElementById('folders-modal-list');
    try {
        const res = await fetch('/api/folders');
        const folders = await res.json();
        if (folders.length === 0) {
            list.innerHTML = '<li class="modal-list-item">No folders created.</li>';
            return;
        }
        list.innerHTML = folders.map(f => `
            <li class="modal-list-item">
                <span>${escapeHTML(f.name)}</span>
                <button class="btn btn-sm btn-danger" onclick="deleteFolder(${f.id})">Delete</button>
            </li>
        `).join('');
    } catch (e) {
        list.innerHTML = '<li>Error loading folders list</li>';
    }
}

function closeFoldersModal() {
    document.getElementById('folders-modal').classList.add('hidden');
}

async function handleCreateFolder(e) {
    e.preventDefault();
    const input = document.getElementById('new-folder-name');
    const name = input.value.strip ? input.value.strip() : input.value.trim();
    if (!name) return;

    try {
        const res = await fetch('/api/folders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.error || 'Failed to create folder');
        }
        input.value = '';
        await renderFoldersList();
        await loadFolders();
    } catch (err) {
        alert(err.message);
    }
}

async function deleteFolder(folderId) {
    if (!confirm('Are you sure you want to delete this folder? Routes inside it will remain but won\'t be in a folder anymore.')) return;
    try {
        const res = await fetch(`/api/folders/${folderId}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete folder');
        await renderFoldersList();
        await loadFolders();
        await loadRoutes(); // update timeline badges
    } catch (err) {
        alert(err.message);
    }
}

// Edit Route Details Modal
function openEditRouteModal() {
    if (!currentRoute) return;
    
    document.getElementById('edit-name').value = currentRoute.name;
    document.getElementById('edit-desc').value = currentRoute.description || '';
    
    // Select Folder
    const editFolder = document.getElementById('edit-folder');
    editFolder.value = currentRoute.folder_id || '';
    
    // Set Tags
    document.getElementById('edit-tags').value = currentRoute.tags ? currentRoute.tags.join(', ') : '';
    
    document.getElementById('edit-route-modal').classList.remove('hidden');
}

function closeEditRouteModal() {
    document.getElementById('edit-route-modal').classList.add('hidden');
}

async function handleEditRoute(e) {
    e.preventDefault();
    const name = document.getElementById('edit-name').value.trim();
    const description = document.getElementById('edit-desc').value.trim();
    const folder_id = document.getElementById('edit-folder').value || null;
    const tagsRaw = document.getElementById('edit-tags').value;
    const tags = tagsRaw.split(',').map(t => t.trim()).filter(t => t.length > 0);

    try {
        const res = await fetch(`/api/routes/${currentRoute.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description, folder_id, tags })
        });
        if (!res.ok) throw new Error('Failed to update route');
        
        closeEditRouteModal();
        await loadTags();
        await loadRoutes();
        
        // Refresh details if in Map Mode, otherwise clear temp selection
        if (document.getElementById('dashboard-view').classList.contains('hidden')) {
            await selectRoute(currentRoute.id);
        } else {
            currentRoute = null;
        }
    } catch (err) {
        alert(err.message);
    }
}

// Delete Route
async function handleDeleteRoute() {
    if (!currentRoute) return;
    if (!confirm(`Are you sure you want to delete "${currentRoute.name}"? This action is permanent.`)) return;

    try {
        const res = await fetch(`/api/routes/${currentRoute.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete route');
        
        // Clear layers
        if (routePolyline) map.removeLayer(routePolyline);
        if (animatedPolyline) map.removeLayer(animatedPolyline);
        if (animationMarker) map.removeLayer(animationMarker);
        waypointMarkersGroup.clearLayers();
        
        document.getElementById('route-details-panel').classList.add('hidden');
        document.getElementById('animation-controller-overlay').classList.add('hidden');
        currentRoute = null;
        
        await loadTags();
        await loadRoutes();
    } catch (err) {
        alert(err.message);
    }
}

// ----------------------------------------------------
// Utility Functions
// ----------------------------------------------------

function formatDistance(meters) {
    if (meters === null || meters === undefined) return '0.00 km';
    return `${(meters / 1000).toFixed(2)} km`;
}

function formatDuration(seconds) {
    if (!seconds) return '00:00:00';
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    return [
        hrs.toString().padStart(2, '0'),
        mins.toString().padStart(2, '0'),
        secs.toString().padStart(2, '0')
    ].join(':');
}

function formatSpeed(mps) {
    if (!mps) return '0.0 km/h';
    const kmh = mps * 3.6;
    return `${kmh.toFixed(1)} km/h`;
}

function escapeHTML(str) {
    if (!str) return '';
    return str.replace(/[&<>'"]/g, 
        tag => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            "'": '&#39;',
            '"': '&quot;'
        }[tag] || tag)
    );
}
