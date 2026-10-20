(() => {
'use strict';

// Configuration Constants
const DEFAULT_STATIONARY_RADIUS_METERS = 15;
const DEFAULT_FALLBACK_SPEED_MPS = 5.0;
const DEFAULT_TIME_GAP_THRESHOLD_SECONDS = 120.0;
const DEFAULT_TIME_GAP_RECOVERY_SECONDS = 2.0;

// Blaeu GPX Cartographer Frontend Logic

// Global Application State
let map = null;
let currentRoute = null;
let originalStartPoint = null;
let originalFinishPoint = null;
let routesList = [];
let activitiesList = []; // Workouts without GPS: no route, no coordinates, no map
let currentActivity = null;
let selectionSeq = 0; // Bumped on every selection change, so a stale route fetch cannot reopen the map
let foldersList = [];
let activeFolderFilter = '';
let activeTagFilter = '';
let currentUser = null;
let authMode = 'login';
let registrationOpen = true;
let csrfToken = null;

// Intercept all fetch requests to automatically attach CSRF token for state-changing operations
const originalFetch = window.fetch;
window.fetch = async function(resource, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'DELETE'].includes(method) && csrfToken) {
        options.headers = options.headers || {};
        if (options.headers instanceof Headers) {
            options.headers.set('X-CSRF-Token', csrfToken);
        } else if (Array.isArray(options.headers)) {
            if (!options.headers.some(h => h[0].toLowerCase() === 'x-csrf-token')) {
                options.headers.push(['X-CSRF-Token', csrfToken]);
            }
        } else {
            options.headers['X-CSRF-Token'] = csrfToken;
        }
    }
    return originalFetch(resource, options);
};

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
let tileLayer = null;
let posterMapOverlay = null;
let mapThemes = [];
let currentMapStyle = 'dark';
let labelsLoadedForRouteId = null;
let posterPollingInterval = null;
let currentGarminAutoSync = 'off';
let currentIntervalsAutoSync = 'off';



// Initialize Page
document.addEventListener('DOMContentLoaded', () => {
    initMap();
    initAppEvents();
    checkAuthStatus();
});

// Initialize Leaflet Map
function initMap() {
    // Initial map setup targeting Europe center
    map = L.map('map', {
        zoomControl: true,
        preferCanvas: true
    }).setView([50.0, 10.0], 4);

    // Use our cached tile proxy endpoint
    tileLayer = L.tileLayer('/api/tiles/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
    }).addTo(map);

    waypointMarkersGroup = L.layerGroup().addTo(map);
}

// Mobile navigation helper functions
function closeMobileSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('active');
}

function openMobileSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('active');
}

// Bind DOM and custom event handlers
function initAppEvents() {
    // Register mobile navigation listeners
    const menuToggle = document.getElementById('menu-toggle-btn');
    const menuClose = document.getElementById('menu-close-btn');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (menuToggle) menuToggle.addEventListener('click', openMobileSidebar);
    if (menuClose) menuClose.addEventListener('click', closeMobileSidebar);
    if (backdrop) backdrop.addEventListener('click', closeMobileSidebar);

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
    
    // Save Image
    const saveImageBtn = document.getElementById('save-image-btn');
    if (saveImageBtn) {
        saveImageBtn.addEventListener('click', saveImage);
    }


    // Settings Modal
    document.getElementById('settings-btn').addEventListener('click', openSettingsModal);
    document.getElementById('close-settings-btn').addEventListener('click', saveSettingsModal);
    const cancelSettingsBtn = document.getElementById('cancel-settings-btn');
    if (cancelSettingsBtn) {
        cancelSettingsBtn.addEventListener('click', cancelSettingsModal);
    }
    loadSettingsIntoModal();

    // About Modal
    const aboutBtn = document.getElementById('about-btn');
    if (aboutBtn) {
        aboutBtn.addEventListener('click', openAboutModal);
    }
    const closeAboutModalBtn = document.getElementById('close-about-modal-btn');
    if (closeAboutModalBtn) {
        closeAboutModalBtn.addEventListener('click', closeAboutModal);
    }
    
    // Auth Form Submit
    const loginForm = document.getElementById('login-form');
    if (loginForm) {
        loginForm.addEventListener('submit', handleAuthSubmit);
    }
    const settingsForm = document.getElementById('settings-form');
    if (settingsForm) {
        settingsForm.addEventListener('submit', (e) => e.preventDefault());
    }
    const loginToggleLink = document.getElementById('login-toggle-link');
    if (loginToggleLink) {
        loginToggleLink.addEventListener('click', toggleAuthMode);
    }
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', handleLogout);
    }
    const privacyToggleBtn = document.getElementById('route-privacy-toggle-btn');
    if (privacyToggleBtn) {
        privacyToggleBtn.addEventListener('click', handlePrivacyToggle);
    }

    // Bind HUD Map Style Selector
    const hudMapStyleSelect = document.getElementById('hud-map-style-select');
    if (hudMapStyleSelect) {
        hudMapStyleSelect.addEventListener('change', (e) => {
            currentMapStyle = e.target.value;
            applyMapStyle();
        });
    }

    const hudCityInput = document.getElementById('hud-map-style-city');
    const hudCountryInput = document.getElementById('hud-map-style-country');
    const handleHudLabelChange = () => {
        if (currentRoute && currentMapStyle !== 'dark') {
            applyMapStyle();
        }
    };
    if (hudCityInput) {
        hudCityInput.addEventListener('change', handleHudLabelChange);
        hudCityInput.addEventListener('blur', handleHudLabelChange);
    }
    if (hudCountryInput) {
        hudCountryInput.addEventListener('change', handleHudLabelChange);
        hudCountryInput.addEventListener('blur', handleHudLabelChange);
    }


    initSettingsFormats();

    // Back to Dashboard
    document.getElementById('back-to-dashboard-btn').addEventListener('click', deselectRoute);
    document.getElementById('activity-back-btn').addEventListener('click', deselectRoute);
    document.getElementById('delete-activity-btn').addEventListener('click', () => {
        if (currentActivity) deleteActivity(currentActivity.id);
    });

    initSettingsTabs();
    initGarminIntegration();
    initIntervalsIntegration();
    initCoach();
    initWelcome();
    initLibrary();

    // Delegated click listeners to prevent XSS in inline event handlers
    document.addEventListener('click', (e) => {
        // Tag filter toggle
        const tagBadge = e.target.closest('.tag-badge');
        if (tagBadge) {
            if (tagBadge.closest('.activity-card')) {
                e.stopPropagation();
            }
            const tagName = tagBadge.getAttribute('data-tag-name');
            if (tagName) {
                toggleTagFilter(tagName);
            }
            return;
        }

        // Delete route from dashboard
        const deleteRouteBtn = e.target.closest('.btn-delete-route');
        if (deleteRouteBtn) {
            e.stopPropagation();
            const routeId = parseInt(deleteRouteBtn.getAttribute('data-route-id'), 10);
            const routeName = deleteRouteBtn.getAttribute('data-route-name');
            deleteRouteFromDashboard(routeId, routeName, e);
            return;
        }

        // Delete map-less activity from dashboard
        const deleteActivityBtn = e.target.closest('.btn-delete-activity');
        if (deleteActivityBtn) {
            e.stopPropagation();
            const activityId = parseInt(deleteActivityBtn.getAttribute('data-activity-id'), 10);
            if (activityId) {
                deleteActivity(activityId);
            }
            return;
        }

        // Import Garmin activity
        const importGarminBtn = e.target.closest('.btn-import-single');
        if (importGarminBtn) {
            const activityId = importGarminBtn.getAttribute('data-id');
            importSingleGarminActivity(activityId, importGarminBtn);
            return;
        }

        // Import Intervals activity
        const importIntervalsBtn = e.target.closest('.btn-import-single-intervals');
        if (importIntervalsBtn) {
            const activityId = importIntervalsBtn.getAttribute('data-id');
            const activityName = importIntervalsBtn.getAttribute('data-name');
            const startTime = importIntervalsBtn.getAttribute('data-start-time');
            importSingleIntervalsActivity(activityId, activityName, startTime, importIntervalsBtn);
            return;
        }

        // Delete user account
        const deleteUserBtn = e.target.closest('.btn-delete-user');
        if (deleteUserBtn) {
            const userId = parseInt(deleteUserBtn.getAttribute('data-user-id'), 10);
            const username = deleteUserBtn.getAttribute('data-username');
            handleDeleteUser(userId, username);
            return;
        }

        // Edit route from dashboard
        const editRouteBtn = e.target.closest('.btn-edit-route');
        if (editRouteBtn) {
            e.stopPropagation();
            const routeId = parseInt(editRouteBtn.getAttribute('data-route-id'), 10);
            if (routeId) {
                editRouteFromDashboard(routeId, e);
            }
            return;
        }

        // Delete folder from modal
        const deleteFolderBtn = e.target.closest('.btn-delete-folder');
        if (deleteFolderBtn) {
            const folderId = parseInt(deleteFolderBtn.getAttribute('data-folder-id'), 10);
            if (folderId) {
                deleteFolder(folderId);
            }
            return;
        }

        // Select route from timeline-item
        const timelineItem = e.target.closest('.timeline-item');
        if (timelineItem) {
            const activityId = parseInt(timelineItem.getAttribute('data-activity-id'), 10);
            if (activityId) {
                selectActivity(activityId);
                return;
            }
            const routeId = parseInt(timelineItem.getAttribute('data-route-id'), 10);
            if (routeId) {
                selectRoute(routeId);
            }
            return;
        }

        // Select route from activity-card
        const activityCard = e.target.closest('.activity-card');
        if (activityCard) {
            if (e.target.closest('.activity-card-actions') || e.target.closest('.tag-badge')) {
                return;
            }
            const activityId = parseInt(activityCard.getAttribute('data-activity-id'), 10);
            if (activityId) {
                selectActivity(activityId);
                return;
            }
            const routeId = parseInt(activityCard.getAttribute('data-route-id'), 10);
            if (routeId) {
                selectRoute(routeId);
            }
            return;
        }
    });
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
            return `<span class="tag-badge ${activeClass}" data-tag-name="${escapeHTML(tag.name)}">#${escapeHTML(tag.name)}</span>`;
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
        const [res] = await Promise.all([fetch('/api/routes'), loadActivities()]);
        routesList = await res.json();
        if (currentActivity && !activitiesList.some(a => a.id === currentActivity.id)) {
            currentActivity = null;
        }
        renderRoutesLedger();
        renderDashboardGrid();
        loadWelcome();
        
        if (currentRoute) {
            showDashboardView(false);
        } else if (currentActivity) {
            showActivityView();
        } else {
            showDashboardView(true);
        }
        
        checkAndStartPolling();
    } catch (err) {
        console.error("Error loading routes", err);
    }
}

// While maps are waiting in the render queue, poll its (small) status and reload the routes
// only when a render moved on, so cards show progress and the rendered-map badge
function checkAndStartPolling() {
    const pending = routesList.some(r => r.poster_status && ['queued', 'generating'].includes(r.poster_status.status));
    if (pending && !posterPollingInterval) refreshMapQueue();
}

const MAP_QUEUE_POLL_MS = 3000;
let mapQueueStatus = null;
let mapQueueSeq = 0;

function mapQueueSignature(status) {
    if (!status) return '';
    const current = status.current || {};
    return [status.theme, status.rendered, status.queued, status.done, status.failed, current.route_id, current.progress].join('|');
}

async function refreshMapQueue(options = {}) {
    clearTimeout(posterPollingInterval);
    posterPollingInterval = null;
    if (!currentUser) return;
    const seq = ++mapQueueSeq;
    try {
        const res = options.fill || options.retryFailed
            ? await fetch('/api/map-queue', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ retry_failed: !!options.retryFailed })
            })
            : await fetch('/api/map-queue');
        if (!res.ok || seq !== mapQueueSeq) return;
        const status = await res.json();
        const changed = mapQueueSignature(status) !== mapQueueSignature(mapQueueStatus);
        const hadStatus = !!mapQueueStatus;
        mapQueueStatus = status;
        renderMapQueue(status);
        if (changed && hadStatus) {
            const routesRes = await fetch('/api/routes');
            if (routesRes.ok) {
                routesList = await routesRes.json();
                renderRoutesLedger();
                renderDashboardGrid();
            }
        }
        if (status.queued > 0 && seq === mapQueueSeq) {
            posterPollingInterval = setTimeout(refreshMapQueue, MAP_QUEUE_POLL_MS);
        }
    } catch (err) {
        console.error('Could not load the map render queue', err);
    }
}

const MAP_RENDER_STEPS = ['Starting', 'Loading street networks', 'Analyzing water bodies', 'Querying parks & greenery', 'Rendering poster layout'];

function renderMapQueue(status) {
    const box = document.getElementById('welcome-map-queue');
    if (!box) return;
    if (!status || !status.routes) {
        box.classList.add('hidden');
        return;
    }
    box.classList.remove('hidden');
    const icon = mapRenderedIconSvg(14);

    if (!status.theme) {
        box.innerHTML = `
            <div class="welcome-map-queue-head"><span class="welcome-map-queue-title">${icon} Map rendering</span></div>
            <div class="welcome-map-queue-detail">Your maps use the live dark style, so there is nothing to render. Pick a poster style as your default map style to render a map for every activity.</div>`;
        return;
    }

    const theme = (mapThemes.find(t => t.id === status.theme) || {}).name || status.theme;
    const percent = Math.round(100 * status.rendered / status.routes);
    let detail;
    let warning = false;
    if (status.current) {
        const step = Math.min(status.current.progress || 0, MAP_RENDER_STEPS.length - 1);
        detail = `Rendering “${escapeHTML(status.current.name || 'activity')}”: ${MAP_RENDER_STEPS[step].toLowerCase()} (step ${step + 1} of ${MAP_RENDER_STEPS.length})`;
        const waiting = status.queued - 1;
        if (waiting > 0) detail += ` · ${waiting} waiting`;
    } else if (status.retry_at) {
        warning = true;
        detail = `Paused: OpenStreetMap could not be reached. Retrying ${escapeHTML(formatRetryTime(status.retry_at))}, ${status.queued} waiting`;
    } else if (status.queued > 0) {
        detail = `${status.queued} waiting to render`;
    } else if (status.rendered === status.routes) {
        detail = `Every activity has a ${escapeHTML(theme)} map`;
    } else {
        detail = 'Nothing waiting to render';
    }
    if (status.failed > 0) {
        detail += ` · ${status.failed} could not be rendered <button type="button" class="welcome-link" id="welcome-map-queue-retry">Retry</button>`;
    }

    box.innerHTML = `
        <div class="welcome-map-queue-head">
            <span class="welcome-map-queue-title">${icon} Map rendering</span>
            <span class="welcome-map-queue-count">${status.rendered} / ${status.routes}</span>
        </div>
        <div class="welcome-map-queue-bar" role="progressbar" aria-label="Activities with a rendered ${escapeHTML(theme)} map"
             aria-valuemin="0" aria-valuemax="${status.routes}" aria-valuenow="${status.rendered}"><i style="width: ${percent}%"></i></div>
        <div class="welcome-map-queue-detail${warning ? ' warning' : ''}">${detail}</div>`;
    const retry = document.getElementById('welcome-map-queue-retry');
    if (retry) retry.addEventListener('click', () => refreshMapQueue({ retryFailed: true }));
}

function formatRetryTime(stamp) {
    const ts = parseUtcTimestamp(stamp);
    if (ts === null) return 'soon';
    const minutes = Math.ceil((ts - Date.now()) / 60000);
    return minutes <= 1 ? 'in a minute' : `in ${minutes} min`;
}

function mapRenderedIconSvg(size) {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"></polygon><line x1="8" y1="2" x2="8" y2="18"></line><line x1="16" y1="6" x2="16" y2="22"></line></svg>`;
}

function renderRoutesLedger() {
    const listContainer = document.getElementById('routes-timeline');
    const items = getLedgerItems(getFilteredRoutes());

    if (items.length === 0) {
        listContainer.innerHTML = '<div class="timeline-empty">No routes match the filters.</div>';
        return;
    }

    listContainer.innerHTML = items.map(item => {
        if (item.kind === 'activity') {
            return renderMaplessTimelineItem(item.data);
        }
        const route = item.data;
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
            formattedDate += ` ${escapeHTML(route.timezone_abbr)}`;
        }

        const privacyIcon = route.is_public ? 
            `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor" title="Public"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.53c-.26-.81-1-1.4-1.9-1.4h-1v-3c0-.55-.45-1-1-1h-6v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.4z"/></svg>` :
            `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor" title="Private"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>`;

        return `
            <div class="timeline-item ${activeClass}" data-route-id="${route.id}">
                <div class="timeline-date">${formattedDate}</div>
                <h4 class="timeline-title">${escapeHTML(route.name)}</h4>
                <p class="timeline-desc">${escapeHTML(route.description || 'No description provided.')}</p>
                <div class="timeline-meta">
                    <span style="opacity: 0.7; display: inline-flex; align-items: center; margin-right: 8px;">${privacyIcon}</span>
                    ${folderBadge}
                    <span class="tag-badge btn-sm">${distanceStr}</span>
                    <span class="tag-badge btn-sm">${durationStr}</span>
                </div>
            </div>
        `;
    }).join('');
}

// ----------------------------------------------------
// Workouts Without GPS (indoor rides, treadmill runs, strength sessions)
// They have no route and no coordinates, so they are listed next to routes but
// never become currentRoute: map, animation, poster and export code only see routes.
// ----------------------------------------------------

async function loadActivities() {
    try {
        const res = await fetch('/api/activities');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        activitiesList = Array.isArray(data) ? data.filter(a => a && parseUtcTimestamp(a.start_time) !== null) : [];
    } catch (err) {
        console.warn('Could not load activities without GPS', err);
        activitiesList = [];
    }
}

function getFilteredRoutes() {
    let filtered = routesList;
    if (activeFolderFilter) {
        filtered = filtered.filter(r => r.folder_id == activeFolderFilter);
    }
    if (activeTagFilter) {
        filtered = filtered.filter(r => r.tags && r.tags.includes(activeTagFilter));
    }
    return filtered;
}

function getVisibleActivities() {
    // Folders and tags only exist for routes
    if (activeFolderFilter || activeTagFilter) return [];
    return activitiesList;
}

// Parses a 'YYYY-MM-DD HH:MM:SS' UTC timestamp; returns milliseconds or null
function parseUtcTimestamp(value) {
    if (!value) return null;
    const ts = Date.parse(String(value).replace(' ', 'T') + 'Z');
    return Number.isNaN(ts) ? null : ts;
}

// Routes and map-less activities as one list, newest first
function getLedgerItems(routes) {
    const items = routes.map(r => ({ kind: 'route', data: r, time: parseUtcTimestamp(r.created_at) || 0 }));
    getVisibleActivities().forEach(a => {
        items.push({ kind: 'activity', data: a, time: parseUtcTimestamp(a.start_time) });
    });
    return items.sort((a, b) => b.time - a.time);
}

const SPORT_ICON_PATHS = {
    ride: '<circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><circle cx="15" cy="5" r="1"/><path d="M12 17.5V14l-3-3 4-3 2 3h2"/>',
    run: '<circle cx="15" cy="4.5" r="2"/><path d="M5 20l4-5 3 2 1 4"/><path d="M7 11l3-3 4 1 2 3h3"/><path d="M10 8l2 5-3 2"/>',
    strength: '<path d="M6.5 6.5l11 11"/><path d="M21 21l-1-1"/><path d="M3 3l1 1"/><path d="M18 22l4-4"/><path d="M2 6l4-4"/><path d="M3 10l7-7"/><path d="M14 21l7-7"/>',
    swim: '<circle cx="12" cy="5" r="2"/><path d="M2 13c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2"/><path d="M2 19c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2"/>',
    other: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'
};

function getSportCategory(sportType) {
    const t = (sportType || '').toLowerCase();
    if (t.includes('ride') || t.includes('cycl') || t.includes('bike')) return 'ride';
    if (t.includes('run')) return 'run';
    if (t.includes('weight') || t.includes('strength') || t.includes('crossfit')) return 'strength';
    if (t.includes('swim')) return 'swim';
    return 'other';
}

function formatSportLabel(sportType) {
    if (!sportType) return 'Workout';
    return String(sportType).replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2');
}

function sportIconSvg(sportType, size) {
    const paths = SPORT_ICON_PATHS[getSportCategory(sportType)];
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

function formatActivityDate(activity) {
    const options = { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false };
    const date = new Date(parseUtcTimestamp(activity.start_time));
    if (activity.timezone) {
        try {
            return date.toLocaleDateString(undefined, { ...options, timeZone: activity.timezone });
        } catch (e) {
            // Unknown time zone name: fall through
        }
    }
    // Without a usable time zone, show the workout's own wall-clock time when known
    const local = parseUtcTimestamp(activity.start_time_local);
    if (local !== null) {
        return new Date(local).toLocaleDateString(undefined, { ...options, timeZone: 'UTC' });
    }
    return date.toLocaleDateString(undefined, options);
}

function hasValue(v) {
    return v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v));
}

// All stats an activity has, in display order
function getActivityStats(activity) {
    const stats = [];
    const category = getSportCategory(activity.sport_type);
    if (hasValue(activity.duration)) stats.push({ key: 'duration', label: 'Duration', value: formatDuration(activity.duration) });
    if (hasValue(activity.moving_time)) stats.push({ key: 'moving_time', label: 'Moving Time', value: formatDuration(activity.moving_time) });
    if (hasValue(activity.distance) && activity.distance > 0) {
        stats.push({ key: 'distance', label: 'Distance', value: formatDistance(activity.distance) });
        const time = activity.moving_time || activity.duration;
        if (time > 0) {
            const mps = activity.distance / time;
            if (category === 'ride') {
                stats.push({ key: 'speed', label: 'Avg Speed', value: formatSpeed(mps) });
            } else {
                stats.push({ key: 'pace', label: 'Avg Pace', value: formatPace(mps) });
            }
        }
    }
    if (hasValue(activity.avg_hr)) stats.push({ key: 'avg_hr', label: 'Avg HR', value: `${Math.round(activity.avg_hr)} bpm` });
    if (hasValue(activity.max_hr)) stats.push({ key: 'max_hr', label: 'Max HR', value: `${Math.round(activity.max_hr)} bpm` });
    if (hasValue(activity.avg_power)) stats.push({ key: 'avg_power', label: 'Avg Power', value: `${Math.round(activity.avg_power)} W` });
    if (hasValue(activity.normalized_power)) stats.push({ key: 'normalized_power', label: 'Norm Power', value: `${Math.round(activity.normalized_power)} W` });
    if (hasValue(activity.avg_cadence)) stats.push({ key: 'avg_cadence', label: 'Cadence', value: `${Math.round(activity.avg_cadence)}` });
    if (hasValue(activity.training_load)) stats.push({ key: 'training_load', label: 'Load', value: `${Math.round(activity.training_load)}` });
    if (hasValue(activity.calories)) stats.push({ key: 'calories', label: 'Calories', value: `${Math.round(activity.calories)} kcal` });
    if (hasValue(activity.rpe)) stats.push({ key: 'rpe', label: 'RPE', value: `${Math.round(activity.rpe)}/10` });
    return stats;
}

// The four stats shown on a card, preferring the ones most workouts have
function getActivityCardStats(activity) {
    const stats = getActivityStats(activity);
    const order = ['duration', 'distance', 'avg_hr', 'avg_power', 'training_load', 'calories', 'moving_time', 'max_hr'];
    const picked = [];
    order.forEach(key => {
        const stat = stats.find(s => s.key === key);
        if (stat && picked.length < 4) picked.push(stat);
    });
    while (picked.length < 4) picked.push({ label: '—', value: '—' });
    return picked;
}

function getActivitySubtitle(activity) {
    return `${formatSportLabel(activity.sport_type)} · ${activity.is_indoor ? 'Indoor' : 'No GPS'}`;
}

function renderMaplessTimelineItem(activity) {
    const activeClass = currentActivity && currentActivity.id === activity.id ? 'active' : '';
    const distanceBadge = activity.distance > 0 ? `<span class="activity-stat-badge">${formatDistance(activity.distance)}</span>` : '';
    return `
        <div class="timeline-item timeline-item-mapless ${activeClass}" data-activity-id="${activity.id}">
            <div class="timeline-date">${escapeHTML(formatActivityDate(activity))}</div>
            <h4 class="timeline-title">${escapeHTML(activity.name || formatSportLabel(activity.sport_type))}</h4>
            <p class="timeline-desc">${escapeHTML(getActivitySubtitle(activity))}</p>
            <div class="timeline-meta">
                <span class="sport-badge">${sportIconSvg(activity.sport_type, 11)}${escapeHTML(formatSportLabel(activity.sport_type))}</span>
                ${distanceBadge}
                <span class="activity-stat-badge">${formatDuration(activity.duration || activity.moving_time)}</span>
            </div>
        </div>
    `;
}

function renderMaplessCard(activity) {
    const name = activity.name || formatSportLabel(activity.sport_type);
    const statsHtml = getActivityCardStats(activity).map(stat => `
        <div class="activity-card-stat">
            <span class="stat-label">${escapeHTML(stat.label)}</span>
            <span class="stat-value">${escapeHTML(stat.value)}</span>
        </div>
    `).join('');
    const username = (currentUser && currentUser.username) || 'You';

    return `
        <div class="activity-card activity-card-mapless" data-activity-id="${activity.id}">
            <div class="activity-card-user-banner" style="background: rgba(255,255,255,0.02); padding: 6px 16px; font-size: 0.78em; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; font-family: 'Outfit', sans-serif; letter-spacing: 0.5px;">
                <span>
                    <svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px; opacity: 0.8;" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                    <span style="opacity: 0.6; margin-right: 2px;">BY</span> <strong style="color: #fff; font-weight: 700;">${escapeHTML(username)}</strong>
                </span>
                <span style="color: #94a3b8; font-size: 0.9em; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;">${activity.is_indoor ? 'Indoor' : 'No GPS'}</span>
            </div>
            <div class="activity-card-preview mapless-preview">
                <div class="mapless-preview-icon">${sportIconSvg(activity.sport_type, 34)}</div>
                <span class="mapless-preview-label">${escapeHTML(formatSportLabel(activity.sport_type))}</span>
                <span class="mapless-preview-note">No map for this workout</span>
            </div>
            <div class="activity-card-body">
                <div class="activity-card-time">
                    <span>${escapeHTML(formatActivityDate(activity))}</span>
                </div>
                <h3 class="activity-card-title" title="${escapeHTML(name)}">${escapeHTML(name)}</h3>
                <p class="activity-card-desc">${escapeHTML(getActivitySubtitle(activity))}</p>
                <div class="activity-card-stats-grid">
                    ${statsHtml}
                </div>
            </div>
            <div class="activity-card-footer">
                <div class="activity-card-tags">
                    <span class="sport-badge">${sportIconSvg(activity.sport_type, 11)}${escapeHTML(formatSportLabel(activity.sport_type))}</span>
                </div>
                <div class="activity-card-actions">
                    <button class="btn-icon btn-delete-activity" data-activity-id="${activity.id}" title="Delete activity">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            </div>
        </div>
    `;
}

// Show the details panel without map, animation controls or map style HUD
function showActivityView() {
    document.getElementById('dashboard-view').classList.add('hidden');
    document.querySelector('.map-viewport').classList.add('hidden');
    document.getElementById('route-details-panel').classList.add('hidden');
    document.getElementById('animation-controller-overlay').classList.add('hidden');
    const hud = document.getElementById('map-style-hud');
    if (hud) hud.classList.add('hidden');
    document.getElementById('sidebar-ledger-container').classList.remove('hidden');
    document.getElementById('activity-details-panel').classList.remove('hidden');
}

function selectActivity(activityId) {
    const activity = activitiesList.find(a => a.id === activityId);
    if (!activity) return;

    selectionSeq++;
    closeMobileSidebar();
    // Leave map mode entirely; nothing on the map may refer to this activity
    clearRouteFromMap();
    currentRoute = null;
    currentActivity = activity;

    document.getElementById('activity-title').textContent = activity.name || formatSportLabel(activity.sport_type);
    document.getElementById('activity-sport-label').innerHTML =
        `${sportIconSvg(activity.sport_type, 14)}<span>${escapeHTML(formatSportLabel(activity.sport_type))}</span>`;
    document.getElementById('activity-date').textContent = `${formatActivityDate(activity)} · ${activity.is_indoor ? 'Indoor' : 'No GPS'}`;

    const stats = getActivityStats(activity);
    document.getElementById('activity-stats-grid').innerHTML = stats.length ? stats.map(stat => `
        <div class="stat-card">
            <span class="stat-label">${escapeHTML(stat.label)}</span>
            <span class="stat-value">${escapeHTML(stat.value)}</span>
        </div>
    `).join('') : '<div class="timeline-empty">No statistics recorded for this workout.</div>';

    showActivityView();
    renderRoutesLedger();
}

async function deleteActivity(activityId) {
    const activity = activitiesList.find(a => a.id === activityId);
    if (!activity) return;
    const name = activity.name || formatSportLabel(activity.sport_type);
    if (!confirm(`Are you sure you want to delete "${name}"? Future syncs will not import it again.`)) return;

    try {
        const res = await fetch(`/api/activities/${activityId}`, { method: 'DELETE' });
        if (!res.ok) {
            let message = 'Failed to delete activity';
            try {
                message = (await res.json()).error || message;
            } catch (e) {
                // Keep the generic message
            }
            throw new Error(message);
        }
        activitiesList = activitiesList.filter(a => a.id !== activityId);
        if (currentActivity && currentActivity.id === activityId) {
            deselectRoute();
        } else {
            renderRoutesLedger();
            renderDashboardGrid();
        }
    } catch (err) {
        alert(err.message);
    }
}

// Manage transitions between Dashboard Mode and Map Mode
function showDashboardView(show) {
    const dashboardView = document.getElementById('dashboard-view');
    const mapViewport = document.querySelector('.map-viewport');
    const routeDetailsPanel = document.getElementById('route-details-panel');
    const sidebarLedger = document.getElementById('sidebar-ledger-container');
    const animationHUD = document.getElementById('animation-controller-overlay');
    const activityPanel = document.getElementById('activity-details-panel');
    if (activityPanel) activityPanel.classList.add('hidden');
    
    if (show) {
        currentActivity = null;
        dashboardView.classList.remove('hidden');
        mapViewport.classList.add('hidden');
        routeDetailsPanel.classList.add('hidden');
        sidebarLedger.classList.add('hidden');
        animationHUD.classList.add('hidden');
        currentRoute = null;
        // Clean active classes on timeline items
        const activeItems = document.querySelectorAll('.timeline-item.active');
        activeItems.forEach(item => item.classList.remove('active'));
        // The welcome map canvas has no size while the dashboard is hidden
        requestAnimationFrame(drawWelcomeMap);
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

// Remove the selected route's layers, poster overlay and animation from the map
function clearRouteFromMap() {
    stopAnimation();
    if (routePolyline) map.removeLayer(routePolyline);
    if (animatedPolyline) map.removeLayer(animatedPolyline);
    if (animationMarker) map.removeLayer(animationMarker);
    waypointMarkersGroup.clearLayers();
    
    // Clean up poster background overlay and reset map
    const hud = document.getElementById('map-style-hud');
    if (hud) hud.classList.add('hidden');
    
    labelsLoadedForRouteId = null;
    const cityInput = document.getElementById('hud-map-style-city');
    const countryInput = document.getElementById('hud-map-style-country');
    if (cityInput) cityInput.value = '';
    if (countryInput) countryInput.value = '';
    const labelsContainer = document.getElementById('hud-map-poster-labels');
    if (labelsContainer) labelsContainer.classList.add('hidden');

    if (posterMapOverlay) {
        map.removeLayer(posterMapOverlay);
        posterMapOverlay = null;
    }
    if (!map.hasLayer(tileLayer)) {
        tileLayer.addTo(map);
    }
    const mapElement = document.getElementById('map');
    if (mapElement) {
        mapElement.style.backgroundColor = '';
    }
}

// Deselect selected route or map-less activity and return to dashboard
function deselectRoute() {
    selectionSeq++;
    closeMobileSidebar();
    clearRouteFromMap();
    currentRoute = null;
    currentActivity = null;
    showDashboardView(true);
    renderRoutesLedger();
    renderDashboardGrid();
}

// Render Dashboard View grid and aggregate statistics banner
function getStartOfWeek(date, weekStartDayName = 'Monday') {
    const daysOfWeek = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const targetDayIndex = daysOfWeek.indexOf(weekStartDayName);
    
    const d = new Date(date);
    d.setUTCHours(0, 0, 0, 0);
    
    const currentDayIndex = d.getUTCDay();
    let diffIndex = currentDayIndex - targetDayIndex;
    if (diffIndex < 0) {
        diffIndex += 7;
    }
    
    d.setUTCDate(d.getUTCDate() - diffIndex);
    return d;
}

function formatWeekTitle(startDate) {
    const options = { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' };
    return `Week of ${startDate.toLocaleDateString('en-US', options)}`;
}

function renderDashboardGrid() {
    const gridContainer = document.getElementById('dashboard-grid');
    const activityCountEl = document.getElementById('dashboard-activity-count');
    
    // Apply filters
    const filtered = getFilteredRoutes();
    const mapless = getVisibleActivities();
    
    // Only count active user's own activities for summary counts and statistics
    const ownedFiltered = filtered.filter(r => r.is_owner);
    const ownedCountTotal = ownedFiltered.length + mapless.length;
    
    // Update count
    activityCountEl.textContent = ownedCountTotal === 1 ? '1 Activity Loaded' : `${ownedCountTotal} Activities Loaded`;
    
    // Calculate aggregate statistics
    let totalDist = 0;
    let totalGain = 0;
    let totalDur = 0;
    
    ownedFiltered.forEach(r => {
        totalDist += r.total_distance || 0;
        totalGain += r.elevation_gain || 0;
        totalDur += r.duration || 0;
    });
    // Workouts without GPS add their distance and time; indoor "elevation" is virtual, so it is left out
    mapless.forEach(a => {
        totalDist += a.distance || 0;
        totalDur += a.duration || a.moving_time || 0;
    });
    
    document.getElementById('summary-total-distance').textContent = formatDistance(totalDist);
    document.getElementById('summary-total-gain').textContent = `${Math.round(totalGain)} m`;
    document.getElementById('summary-total-duration').textContent = formatDuration(totalDur);
    
    const items = getLedgerItems(filtered);
    if (items.length === 0) {
        gridContainer.innerHTML = '<div class="timeline-empty">No activities match the current filters. Import a GPX file to get started.</div>';
        return;
    }
    
    // Group routes and map-less activities by week
    const weekStartDay = (currentUser && currentUser.week_start_day) || 'Monday';
    const weeksMap = new Map();
    
    items.forEach(item => {
        const dateObj = new Date(item.time);
        const startOfWeek = getStartOfWeek(dateObj, weekStartDay);
        const year = startOfWeek.getUTCFullYear();
        const month = String(startOfWeek.getUTCMonth() + 1).padStart(2, '0');
        const day = String(startOfWeek.getUTCDate()).padStart(2, '0');
        const key = `${year}-${month}-${day}`;
        
        if (!weeksMap.has(key)) {
            weeksMap.set(key, {
                startDate: startOfWeek,
                key: key,
                items: []
            });
        }
        weeksMap.get(key).items.push(item);
    });
    
    // Sort weeks by key DESC (newest first)
    const sortedWeeks = Array.from(weeksMap.values()).sort((a, b) => b.key.localeCompare(a.key));
    
    gridContainer.innerHTML = sortedWeeks.map(week => {
        let weekDist = 0;
        let weekGain = 0;
        let weekDur = 0;
        let ownedCount = 0;
        
        week.items.forEach(item => {
            const r = item.data;
            if (item.kind === 'activity') {
                weekDist += r.distance || 0;
                weekDur += r.duration || r.moving_time || 0;
                ownedCount++;
            } else if (r.is_owner) {
                weekDist += r.total_distance || 0;
                weekGain += r.elevation_gain || 0;
                weekDur += r.duration || 0;
                ownedCount++;
            }
        });
        
        const weekHeaderHtml = `
            <div class="week-header-hud">
                <div class="week-info">
                    <h3 class="week-title-hud">${formatWeekTitle(week.startDate)}</h3>
                    <span class="week-activity-count-hud">${ownedCount === 1 ? '1 Activity' : `${ownedCount} Activities`}</span>
                </div>

                <div class="week-totals-hud">
                    <div class="week-total-item-hud">
                        <span class="week-total-label-hud">Distance</span>
                        <span class="week-total-value-hud">${formatDistance(weekDist)}</span>
                    </div>
                    <div class="week-total-item-hud">
                        <span class="week-total-label-hud">Elev Gain</span>
                        <span class="week-total-value-hud">${Math.round(weekGain)} m</span>
                    </div>
                    <div class="week-total-item-hud">
                        <span class="week-total-label-hud">Duration</span>
                        <span class="week-total-value-hud">${formatDuration(weekDur)}</span>
                    </div>
                </div>
            </div>
        `;
        
        const cardsHtml = week.items.map(item => {
            if (item.kind === 'activity') {
                return renderMaplessCard(item.data);
            }
            const route = item.data;
            const folderBadge = route.folder_name ? `<span class="folder-badge">${escapeHTML(route.folder_name)}</span>` : '';
            const distanceStr = formatDistance(route.total_distance);
            const durationStr = formatDuration(route.duration);
            const gainStr = `${Math.round(route.elevation_gain || 0)} m`;
            const avgPaceStr = formatPace(route.avg_speed);
            
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
                formattedDate += ` ${escapeHTML(route.timezone_abbr)}`;
            }
            
            const tagsHtml = route.tags ? route.tags.map(t => `<span class="tag-badge" data-tag-name="${escapeHTML(t)}">#${escapeHTML(t)}</span>`).join('') : '';
    
            const privacyIcon = route.is_public ? 
                `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor" title="Public"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.53c-.26-.81-1-1.4-1.9-1.4h-1v-3c0-.55-.45-1-1-1h-6v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.4z"/></svg>` :
                `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor" title="Private"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>`;
    
            const mapRenderedBadge = route.map_rendered
                ? `<span class="map-rendered-badge" title="Map rendered" aria-label="Map rendered" role="img">${mapRenderedIconSvg(12)}</span>`
                : '';

            const actionsHtml = route.is_owner ? `
                <div class="activity-card-actions">
                    <button class="btn-icon btn-edit-route" data-route-id="${route.id}" title="Edit details">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                            <path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                        </svg>
                    </button>
                    <button class="btn-icon btn-delete-route" data-route-id="${route.id}" data-route-name="${escapeHTML(route.name)}" title="Delete activity">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            ` : '';
    
            return `
                <div class="activity-card" data-route-id="${route.id}">
                    <div class="activity-card-user-banner" style="background: ${route.is_owner ? 'rgba(255,255,255,0.02)' : 'rgba(0, 240, 255, 0.04)'}; padding: 6px 16px; font-size: 0.78em; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; font-family: 'Outfit', sans-serif; letter-spacing: 0.5px;">
                        <span>
                            <svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px; opacity: 0.8;" fill="currentColor"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>
                            <span style="opacity: 0.6; margin-right: 2px;">BY</span> <strong style="color: ${route.is_owner ? '#fff' : 'var(--primary)'}; font-weight: 700;">${escapeHTML(route.owner_username || 'Unknown')}</strong>
                        </span>
                        ${!route.is_owner ? 
                            `<span style="color: var(--primary); font-size: 0.9em; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; background: rgba(0,240,255,0.1); padding: 2px 8px; border-radius: 4px; text-shadow: 0 0 4px rgba(0,240,255,0.25);">Public</span>` : 
                            `<span style="color: #94a3b8; font-size: 0.9em; font-weight: 600; text-transform: uppercase; letter-spacing: 1px;">Owner</span>`
                        }
                    </div>
                    <div class="activity-card-preview">
                        <canvas class="activity-mini-canvas" data-route-id="${route.id}"></canvas>
                    </div>
                    <div class="activity-card-body">
                        <div class="activity-card-time" style="display: flex; align-items: center; justify-content: space-between;">
                            <span>${formattedDate}</span>
                            <span style="display: inline-flex; align-items: center;">${mapRenderedBadge}<span style="opacity: 0.7; display: inline-flex; align-items: center;">${privacyIcon}</span></span>
                        </div>
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
                                <span class="stat-label">Avg Pace</span>
                                <span class="stat-value">${avgPaceStr}</span>
                            </div>
                        </div>
                    </div>
                    <div class="activity-card-footer">
                        <div class="activity-card-tags">
                            ${folderBadge}
                            ${tagsHtml}
                        </div>
                        ${actionsHtml}
                    </div>
                </div>
            `;
        }).join('');
        
        return `
            <div class="week-section">
                ${weekHeaderHtml}
                <div class="dashboard-grid">
                    ${cardsHtml}
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
            drawRouteMiniature(canvas, route.simplified_path, route.poster_status);
        }
    });
}

// The part of a route outside the start/finish privacy zone, as indexes into coords;
// null when (almost) the whole route lies inside it
function getPrivacyKeepRange(coords) {
    const privacyDistance = getPrivacyDistance();
    let first = 0;
    let last = coords.length - 1;
    if (privacyDistance > 0) {
        const start = coords[0];
        const finish = coords[coords.length - 1];
        while (first < coords.length && getDistanceMeters(coords[first][0], coords[first][1], start[0], start[1]) <= privacyDistance) {
            first++;
        }
        while (last >= first && getDistanceMeters(coords[last][0], coords[last][1], finish[0], finish[1]) <= privacyDistance) {
            last--;
        }
    }
    return first <= last && last - first + 1 >= 2 ? { first, last } : null;
}

// Draw a glowing neon route path miniature on a card canvas
function drawRouteMiniature(canvas, coords, posterStatus) {
    const ctx = canvas.getContext('2d');
    
    // Set actual canvas size to match layout size
    canvas.width = canvas.offsetWidth;
    canvas.height = canvas.offsetHeight;
    
    if (posterStatus && posterStatus.status === 'generating') {
        const progress = posterStatus.progress || 0;
        
        // Draw dark glassmorphic background
        ctx.fillStyle = 'rgba(15, 23, 42, 0.4)';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        
        const centerY = canvas.height / 2 - 5;
        
        // Determine active stage text description
        let activeLabel = "Initializing...";
        if (progress === 1) {
            activeLabel = "Loading street networks...";
        } else if (progress === 2) {
            activeLabel = "Analyzing water bodies...";
        } else if (progress === 3) {
            activeLabel = "Querying parks & greenery...";
        } else if (progress === 4) {
            activeLabel = "Rendering poster layout...";
        }
        
        // Draw header
        ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
        ctx.font = 'bold 9px Outfit, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText("GENERATING MAP BACKGROUND", canvas.width / 2, centerY - 18);
        
        // Draw 5 progress blocks
        const blockW = 16;
        const blockH = 12;
        const gap = 6;
        const totalW = 5 * blockW + 4 * gap;
        const startX = (canvas.width - totalW) / 2;
        
        for (let i = 0; i < 5; i++) {
            const x = startX + i * (blockW + gap);
            // Block i turns green when stage i has completed.
            // Since progress represents the number of completed tasks (1 to 5),
            // block i is green if progress > i.
            const completed = (progress > i);
            
            if (completed) {
                ctx.fillStyle = '#10b981';
                ctx.shadowColor = 'rgba(16, 185, 129, 0.6)';
                ctx.shadowBlur = 6;
                ctx.fillRect(x, centerY, blockW, blockH);
                ctx.shadowBlur = 0;
            } else {
                ctx.fillStyle = 'rgba(255, 255, 255, 0.15)';
                ctx.fillRect(x, centerY, blockW, blockH);
            }
        }
        
        // Draw footer active label
        ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
        ctx.font = '10px Outfit, sans-serif';
        ctx.fillText(activeLabel, canvas.width / 2, centerY + 30);
        return;
    }
    
    if (!coords || coords.length < 2) {
        // Draw centered placeholder text
        ctx.fillStyle = '#94a3b8';
        ctx.font = '12px Outfit';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('No route path available', canvas.width / 2, canvas.height / 2);
        return;
    }
    
    if (getPrivacyDistance() > 0) {
        const keep = getPrivacyKeepRange(coords);
        if (keep) {
            coords = coords.slice(keep.first, keep.last + 1);
        } else {
            ctx.fillStyle = '#94a3b8';
            ctx.font = '12px Outfit';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('Route within privacy zone', canvas.width / 2, canvas.height / 2);
            return;
        }
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
    
    // Draw Start Point (Neon Green)
    const startXY = getCanvasCoords(coords[0][0], coords[0][1]);
    ctx.beginPath();
    ctx.arc(startXY[0], startXY[1], 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#10b981';
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.5;
    ctx.stroke();
    
    // Draw End Point (Neon Red)
    const endXY = getCanvasCoords(coords[coords.length - 1][0], coords[coords.length - 1][1]);
    ctx.beginPath();
    ctx.arc(endXY[0], endXY[1], 4, 0, 2 * Math.PI);
    ctx.fillStyle = '#ef4444';
    ctx.fill();
    ctx.strokeStyle = '#ffffff';
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
        
    } catch (err) {
        alert(err.message);
    } finally {
        uploadBtn.disabled = false;
        uploadBtn.textContent = 'Upload & Parse';
    }
}

// Select a route and load its full details
async function selectRoute(routeId) {
    const seq = ++selectionSeq;
    stopAnimation();
    currentActivity = null;
    showDashboardView(false); // Switch to Map Mode
    closeMobileSidebar();

    try {
        const res = await fetch(`/api/routes/${routeId}`);
        if (!res.ok) throw new Error('Could not load route details');
        
        const route = await res.json();
        // Another route or a map-less activity was selected while this one loaded
        if (seq !== selectionSeq) return;
        currentRoute = route;
        setOriginalStartAndFinishPoints(currentRoute);
        
        // Update sidebar selection styling
        renderRoutesLedger();
        
        // Show details panel & populate statistics
        document.getElementById('route-details-panel').classList.remove('hidden');
        document.getElementById('route-title').textContent = currentRoute.name;
        document.getElementById('route-desc').textContent = currentRoute.description || 'No description provided.';
        
        const privacyStatus = document.getElementById('route-privacy-status');
        const privacyToggleBtn = document.getElementById('route-privacy-toggle-btn');
        const editRouteBtn = document.getElementById('edit-route-btn');
        const deleteRouteBtn = document.getElementById('delete-route-btn');
        
        if (currentRoute.is_owner) {
            document.getElementById('route-privacy-container').style.display = 'inline-flex';
            if (editRouteBtn) editRouteBtn.classList.remove('hidden');
            if (deleteRouteBtn) deleteRouteBtn.classList.remove('hidden');
            
            const lockIcon = `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>`;
            const globeIcon = `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.53c-.26-.81-1-1.4-1.9-1.4h-1v-3c0-.55-.45-1-1-1h-6v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.4z"/></svg>`;
            
            if (currentRoute.is_public) {
                if (privacyStatus) privacyStatus.innerHTML = `${globeIcon} Public`;
                if (privacyToggleBtn) privacyToggleBtn.textContent = 'Make Private';
            } else {
                if (privacyStatus) privacyStatus.innerHTML = `${lockIcon} Private`;
                if (privacyToggleBtn) privacyToggleBtn.textContent = 'Make Public';
            }
            if (privacyToggleBtn) privacyToggleBtn.style.display = 'inline-block';
        } else {
            if (editRouteBtn) editRouteBtn.classList.add('hidden');
            if (deleteRouteBtn) deleteRouteBtn.classList.add('hidden');
            
            const globeIcon = `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.53c-.26-.81-1-1.4-1.9-1.4h-1v-3c0-.55-.45-1-1-1h-6v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.4z"/></svg>`;
            
            if (privacyStatus) privacyStatus.innerHTML = `${globeIcon} Public (by ${escapeHTML(currentRoute.owner_username || 'Unknown')})`;
            if (privacyToggleBtn) privacyToggleBtn.style.display = 'none';
        }
        
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
        document.getElementById('stat-avg-pace').textContent = formatPace(currentRoute.avg_speed);
        document.getElementById('stat-avg-moving-speed').textContent = formatSpeed(currentRoute.avg_moving_speed);
        document.getElementById('stat-avg-moving-pace').textContent = formatPace(currentRoute.avg_moving_speed);
        document.getElementById('stat-max-speed').textContent = formatSpeed(currentRoute.max_speed);
        document.getElementById('stat-waypoints').textContent = currentRoute.waypoints_count;
        document.getElementById('stat-tracks').textContent = currentRoute.tracks_count;
        document.getElementById('stat-segments').textContent = currentRoute.segments_count;
        document.getElementById('stat-points').textContent = currentRoute.points_count;

        // Render on Map
        drawRouteOnMap();
        
        // Load map style preference and show style selector HUD
        const savedStyle = (currentUser && currentUser.default_map_style) || localStorage.getItem('blaeu_map_style') || 'dark';
        currentMapStyle = savedStyle;
        const hudSelect = document.getElementById('hud-map-style-select');
        if (hudSelect) {
            hudSelect.value = currentMapStyle;
        }
        const hud = document.getElementById('map-style-hud');
        if (hud) hud.classList.remove('hidden');
        
        if (currentMapStyle !== 'dark') {
            applyMapStyle();
        }

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
    getFilteredTracks().forEach(track => {
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
    getFilteredWaypoints().forEach(wpt => {
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

    // Start and Finish custom SVG Icons
    const startIcon = L.divIcon({
        html: `
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="12" cy="12" r="9" fill="#064e3b" stroke="#10b981" stroke-width="2" style="filter: drop-shadow(0 0 3px #10b981);"/>
            <path d="M10 8.5L15.5 12L10 15.5V8.5Z" fill="#ffffff"/>
          </svg>
        `,
        className: 'custom-map-marker',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });

    const finishIcon = L.divIcon({
        html: `
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="12" cy="12" r="9" fill="#7f1d1d" stroke="#ef4444" stroke-width="2" style="filter: drop-shadow(0 0 3px #ef4444);"/>
            <rect x="9" y="9" width="3" height="3" fill="#ffffff"/>
            <rect x="12" y="9" width="3" height="3" fill="#111827"/>
            <rect x="9" y="12" width="3" height="3" fill="#111827"/>
            <rect x="12" y="12" width="3" height="3" fill="#ffffff"/>
          </svg>
        `,
        className: 'custom-map-marker',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });

    // Draw Start and Finish Markers
    if (trackCoordinates.length > 0) {
        const startPoint = trackCoordinates[0];
        const finishPoint = trackCoordinates[trackCoordinates.length - 1];

        L.marker(startPoint, { icon: startIcon, zIndexOffset: 1000 })
            .bindPopup('<strong>Start Point</strong>')
            .addTo(waypointMarkersGroup);

        L.marker(finishPoint, { icon: finishIcon, zIndexOffset: 1000 })
            .bindPopup('<strong>Finish Point</strong>')
            .addTo(waypointMarkersGroup);
    }

    // Zoom Map to Route Bounds
    map.fitBounds(routePolyline.getBounds(), { padding: [30, 30] });

    // Show animation controls
    document.getElementById('animation-controller-overlay').classList.remove('hidden');
}

// Helper to calculate distance in meters between two lat/lon coordinates using Haversine formula
// Note: Keep in sync with backend haversine_distance in gpx_parser.py.
// Both use the identical Earth radius constant (6,371,000 meters).
function getDistanceMeters(lat1, lon1, lat2, lon2) {
    const R = 6371000; // Radius of Earth in meters
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
              Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
              Math.sin(dLon / 2) * Math.sin(dLon / 2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return R * c;
}

function getPrivacyDistance() {
    const val = localStorage.getItem('blaeu_privacy_distance');
    return val ? parseInt(val, 10) : 0;
}

// True when a loaded route has at least one track point; map code requires this
function routeHasCoordinates(route) {
    if (!route || !Array.isArray(route.tracks)) return false;
    return route.tracks.some(track => Array.isArray(track.segments) &&
        track.segments.some(segment => Array.isArray(segment) && segment.length > 0));
}

function setOriginalStartAndFinishPoints(route) {
    originalStartPoint = null;
    originalFinishPoint = null;
    if (!route || !route.tracks || route.tracks.length === 0) return;
    
    // Find first point
    for (const track of route.tracks) {
        for (const segment of track.segments) {
            if (segment.length > 0) {
                const pt = segment[0];
                originalStartPoint = { lat: pt.lat, lon: pt.lon };
                break;
            }
        }
        if (originalStartPoint) break;
    }
    
    // Find last point
    for (let i = route.tracks.length - 1; i >= 0; i--) {
        const track = route.tracks[i];
        for (let j = track.segments.length - 1; j >= 0; j--) {
            const segment = track.segments[j];
            if (segment.length > 0) {
                const pt = segment[segment.length - 1];
                originalFinishPoint = { lat: pt.lat, lon: pt.lon };
                break;
            }
        }
        if (originalFinishPoint) break;
    }
}

function getFilteredTracks() {
    if (!currentRoute || !currentRoute.tracks || currentRoute.tracks.length === 0) {
        return [];
    }
    const privacyDistance = getPrivacyDistance();
    if (privacyDistance === 0 || !originalStartPoint || !originalFinishPoint) {
        return currentRoute.tracks;
    }
    
    // Flatten all points chronologically to find the global start and end keep indices
    const allPts = [];
    currentRoute.tracks.forEach(track => {
        track.segments.forEach(segment => {
            segment.forEach(pt => {
                allPts.push(pt);
            });
        });
    });

    if (allPts.length === 0) return [];

    let firstKeepIdx = 0;
    while (firstKeepIdx < allPts.length) {
        const pt = allPts[firstKeepIdx];
        const dStart = getDistanceMeters(pt.lat, pt.lon, originalStartPoint.lat, originalStartPoint.lon);
        if (dStart > privacyDistance) {
            break;
        }
        firstKeepIdx++;
    }

    let lastKeepIdx = allPts.length - 1;
    while (lastKeepIdx >= firstKeepIdx) {
        const pt = allPts[lastKeepIdx];
        const dFinish = getDistanceMeters(pt.lat, pt.lon, originalFinishPoint.lat, originalFinishPoint.lon);
        if (dFinish > privacyDistance) {
            break;
        }
        lastKeepIdx--;
    }

    if (firstKeepIdx > lastKeepIdx) {
        return [];
    }

    // Reconstruct tracks and segments keeping only points within [firstKeepIdx, lastKeepIdx]
    const filteredTracks = [];
    let globalIdx = 0;

    currentRoute.tracks.forEach(track => {
        const filteredSegments = [];
        track.segments.forEach(segment => {
            const filteredSegment = [];
            segment.forEach(pt => {
                if (globalIdx >= firstKeepIdx && globalIdx <= lastKeepIdx) {
                    filteredSegment.push(pt);
                }
                globalIdx++;
            });
            if (filteredSegment.length > 0) {
                filteredSegments.push(filteredSegment);
            }
        });
        if (filteredSegments.length > 0) {
            filteredTracks.push({
                ...track,
                segments: filteredSegments
            });
        }
    });
    return filteredTracks;
}


function getFilteredWaypoints() {
    if (!currentRoute || !currentRoute.waypoints) {
        return [];
    }
    const privacyDistance = getPrivacyDistance();
    if (privacyDistance === 0 || !originalStartPoint || !originalFinishPoint) {
        return currentRoute.waypoints;
    }
    
    return currentRoute.waypoints.filter(wpt => {
        const dStart = getDistanceMeters(wpt.lat, wpt.lon, originalStartPoint.lat, originalStartPoint.lon);
        const dFinish = getDistanceMeters(wpt.lat, wpt.lon, originalFinishPoint.lat, originalFinishPoint.lon);
        return dStart > privacyDistance && dFinish > privacyDistance;
    });
}

// ----------------------------------------------------
// Playback Animation Logic
// ----------------------------------------------------

function prepareAnimationPoints() {
    animationPoints = [];
    let startTimestamp = null;
    
    // Flatten track coordinates
    getFilteredTracks().forEach(track => {
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

    if (animationPoints.length === 0) return;

    // Get selected animation mode (realtime or smooth)
    const modeSelect = document.getElementById('mode-select');
    const animationMode = modeSelect ? modeSelect.value : (localStorage.getItem('blaeu_animation_mode') || 'realtime');

    // If GPX has no time, space points equally (2s interval)
    const hasTimestamps = animationPoints.some(p => p.elapsed > 0);
    if (!hasTimestamps) {
        animationPoints.forEach((pt, idx) => {
            pt.elapsed = idx * 2.0; // Space points by 2 seconds
        });
    } else if (animationMode === 'smooth') {
        // "Smooth" Mode: Filter out stationary periods and play back at constant speed
        const maxStationaryRadius = DEFAULT_STATIONARY_RADIUS_METERS; // meters
        const activePoints = [];
        activePoints.push({ ...animationPoints[0] });

        for (let i = 1; i < animationPoints.length; i++) {
            const lastActive = activePoints[activePoints.length - 1];
            const pt = animationPoints[i];
            const d = getDistanceMeters(lastActive.lat, lastActive.lon, pt.lat, pt.lon);
            if (d >= maxStationaryRadius) {
                activePoints.push({ ...pt });
            }
        }
        
        // Ensure last point is included to reach the exact finish line
        if (animationPoints.length > 1) {
            const lastPt = animationPoints[animationPoints.length - 1];
            const lastActive = activePoints[activePoints.length - 1];
            if (lastActive.lat !== lastPt.lat || lastActive.lon !== lastPt.lon) {
                activePoints.push({ ...lastPt });
            }
        }

        // Calculate cumulative distance and elapsed time based on constant average moving speed
        let cumulativeDistance = 0;
        const distances = [0];
        for (let i = 1; i < activePoints.length; i++) {
            const p1 = activePoints[i - 1];
            const p2 = activePoints[i];
            const d = getDistanceMeters(p1.lat, p1.lon, p2.lat, p2.lon);
            cumulativeDistance += d;
            distances.push(cumulativeDistance);
        }

        let speed = (currentRoute && currentRoute.avg_moving_speed) ? currentRoute.avg_moving_speed : DEFAULT_FALLBACK_SPEED_MPS;
        if (speed <= 0) {
            speed = DEFAULT_FALLBACK_SPEED_MPS;
        }

        for (let i = 0; i < activePoints.length; i++) {
            activePoints[i].elapsed = distances[i] / speed;
        }
        animationPoints = activePoints;
    } else {
        // "Real-Time" Mode: Play back based on actual progression through time, slowing/speeding as recorded
        let adjustedElapsed = 0;
        const processedPoints = [];
        processedPoints.push({ ...animationPoints[0], elapsed: 0 });

        for (let i = 1; i < animationPoints.length; i++) {
            const pt = animationPoints[i];
            const prevPt = animationPoints[i - 1];
            let dt = pt.elapsed - prevPt.elapsed;
            // Cap extreme gaps (e.g. GPS signal lost/restored) to DEFAULT_TIME_GAP_RECOVERY_SECONDS to avoid animation jumps.
            // We set the threshold to DEFAULT_TIME_GAP_THRESHOLD_SECONDS to ensure sparse tracks (e.g. with 60s intervals) are not compressed.
            if (dt > DEFAULT_TIME_GAP_THRESHOLD_SECONDS) {
                dt = DEFAULT_TIME_GAP_RECOVERY_SECONDS;
            }
            adjustedElapsed += dt;
            processedPoints.push({
                ...pt,
                elapsed: adjustedElapsed
            });
        }
        animationPoints = processedPoints;
    }

    // Smooth the points to reduce animation/camera jitter
    animationPoints = smoothAnimationPoints(animationPoints, 7);

    // Update static routePolyline coordinate set to match current animation profile (Real-Time vs Smooth)
    if (routePolyline) {
        const drawCoords = animationPoints.map(p => [p.lat, p.lon]);
        routePolyline.setLatLngs(drawCoords);
    }

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
    const cameraMode = localStorage.getItem('blaeu_camera_mode') || 'follow';
    if (isPlaying && activePoint && cameraMode === 'follow') {
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
    let height = 1080;
    let videoBitrate = 12000000; // 12 Mbps default for 1080p
    
    if (resVal === '720') {
        height = 720;
        videoBitrate = 6000000; // 6 Mbps for 720p
    } else if (resVal === '2160') {
        height = 2160;
        videoBitrate = 50000000; // 50 Mbps for 4K UHD
    }

    const aspectStr = (currentUser && currentUser.default_aspect_ratio) || '16:9';
    const parts = aspectStr.split(':');
    const targetAspect = parseFloat(parts[0]) / parseFloat(parts[1]);
    let width = Math.round(height * targetAspect);
    if (width % 2 !== 0) {
        width += 1;
    }

    const canvas = document.getElementById('export-canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    
    // Scale factor for drawing vectors and fonts relative to baseline 1080p
    const scaleFactor = height / 1080;

    // Calculate optimal zoom level independently of the browser window size/view
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    animationPoints.forEach(pt => {
        if (pt.lat < minLat) minLat = pt.lat;
        if (pt.lat > maxLat) maxLat = pt.lat;
        if (pt.lon < minLon) minLon = pt.lon;
        if (pt.lon > maxLon) maxLon = pt.lon;
    });

    // Helper functions for Web Mercator at zoom 0
    function lngToX0(lng) {
        return (lng + 180) / 360 * 256;
    }
    function latToY0(lat) {
        const latRad = lat * Math.PI / 180;
        return (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * 256;
    }

    const xMin0 = lngToX0(minLon);
    const xMax0 = lngToX0(maxLon);
    const yMin0 = latToY0(maxLat);
    const yMax0 = latToY0(minLat);

    const routeW0 = xMax0 - xMin0;
    const routeH0 = yMax0 - yMin0;

    // Target box with 15% padding on all sides (70% of canvas dimensions)
    const targetW = width * 0.7;
    const targetH = height * 0.7;

    const zWidth = Math.log2(targetW / (routeW0 * scaleFactor));
    const zHeight = Math.log2(targetH / (routeH0 * scaleFactor));
    
    const zoom = Math.max(0, Math.min(18, Math.floor(Math.min(zWidth, zHeight))));
    const center = {
        lat: (minLat + maxLat) / 2,
        lng: (minLon + maxLon) / 2
    };

    // Web Mercator Formulas
    function lngToX(lng, z) {
        return (lng + 180) / 360 * Math.pow(2, z) * 256 * scaleFactor;
    }
    function latToY(lat, z) {
        const latRad = lat * Math.PI / 180;
        return (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * Math.pow(2, z) * 256 * scaleFactor;
    }

    const cameraMode = localStorage.getItem('blaeu_camera_mode') || 'follow';
    const cPxX = lngToX(center.lng, zoom);
    const cPxY = latToY(center.lat, zoom);
    const staticMinPxX = cPxX - width / 2;
    const staticMinPxY = cPxY - height / 2;

    // Define bounds for dynamic camera-following
    let minPxX = 0;
    let minPxY = 0;

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

    let posterMapImg = null;
    const isPosterActive = currentMapStyle !== 'dark' && currentRoute.posterMapUrl;
    const loadedTilesMap = {};

    if (isPosterActive) {
        statusText.textContent = 'Loading map background...';
        posterMapImg = await loadImage(currentRoute.posterMapUrl);
        fill.style.width = '30%';
    } else {
        // Collect all tiles covering the entire route path animation to preload
        const tilesSet = new Set();
        animationPoints.forEach(pt => {
            const pxX = lngToX(pt.lon, zoom);
            const pxY = latToY(pt.lat, zoom);
            const minTX = Math.floor((pxX - width / 2) / (256 * scaleFactor));
            const maxTX = Math.floor((pxX + width / 2) / (256 * scaleFactor));
            const minTY = Math.floor((pxY - height / 2) / (256 * scaleFactor));
            const maxTY = Math.floor((pxY + height / 2) / (256 * scaleFactor));
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

        // Preload tiles via backend CORS-free caching proxy
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
    }

    statusText.textContent = 'Rendering animation frames...';

    // Record setup
    const fpsSelect = document.getElementById('fps-select');
    const fps = fpsSelect ? parseInt(fpsSelect.value, 10) : 30;

    const formatSelect = document.getElementById('format-select');
    const userSelectedFormat = formatSelect ? formatSelect.value : 'video/webm'; // 'video/webm' or 'video/mp4'
    
    let recorderMimeType = 'video/webm';
    if (userSelectedFormat === 'video/mp4') {
        if (MediaRecorder.isTypeSupported('video/mp4;codecs=h264')) {
            recorderMimeType = 'video/mp4;codecs=h264';
        } else if (MediaRecorder.isTypeSupported('video/mp4')) {
            recorderMimeType = 'video/mp4';
        } else {
            // No native MP4 support (Chrome/Firefox): record WebM and convert on server
            if (MediaRecorder.isTypeSupported('video/webm;codecs=vp9')) {
                recorderMimeType = 'video/webm;codecs=vp9';
            } else {
                recorderMimeType = 'video/webm';
            }
        }
    } else { // WebM
        if (MediaRecorder.isTypeSupported('video/webm;codecs=vp9')) {
            recorderMimeType = 'video/webm;codecs=vp9';
        } else if (MediaRecorder.isTypeSupported('video/webm;codecs=vp8')) {
            recorderMimeType = 'video/webm;codecs=vp8';
        } else {
            recorderMimeType = 'video/webm';
        }
    }

    const stream = canvas.captureStream(fps);
    let recorder;
    
    // MediaRecorder configured with the resolution-appropriate high bitrate and resolved format
    try {
        recorder = new MediaRecorder(stream, { 
            mimeType: recorderMimeType,
            videoBitsPerSecond: videoBitrate
        });
    } catch (e) {
        try {
            // fallback
            recorder = new MediaRecorder(stream, { 
                videoBitsPerSecond: videoBitrate
            });
        } catch (e2) {
            alert('MediaRecorder is not supported in this browser.');
            modal.classList.add('hidden');
            return;
        }
    }

    recorder.onerror = (event) => {
        console.error("MediaRecorder error:", event.error);
        alert(`Video recording error: ${event.error.message || event.error.name || 'Unknown error'}`);
        modal.classList.add('hidden');
    };

    const recordedChunks = [];
    recorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
    };

    // Calculate target video duration based on the activity total duration and the selected speed multiplier
    const targetVideoDuration = totalDuration / speedMultiplier;
    const totalFrames = Math.ceil(targetVideoDuration * fps);

    // We always use virtual time to guarantee every frame is captured at the configured FPS.
    // The server-side transcoding fixes timestamps and duration to match real-time playback.
    const useVirtualTime = true;

    recorder.onstop = () => {
        statusText.textContent = 'Saving video file...';
        
        // Determine the actual recorded MIME type
        const actualMimeType = recorder.mimeType || recorderMimeType;
        const isActuallyWebM = actualMimeType.includes('webm');
        
        const rawBlob = new Blob(recordedChunks, { type: isActuallyWebM ? 'video/webm' : 'video/mp4' });

        const downloadBlob = (blobToDownload, ext) => {
            const url = URL.createObjectURL(blobToDownload);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${currentRoute.name.replace(/[^a-z0-9]/gi, '_').toLowerCase()}-animation.${ext}`;
            a.click();
            // Hide Modal
            modal.classList.add('hidden');
        };

        const fallbackDownload = () => {
            if (isActuallyWebM && typeof ysFixWebmDuration === 'function') {
                statusText.textContent = 'Optimizing video duration metadata...';
                ysFixWebmDuration(rawBlob, targetVideoDuration * 1000, (fixedBlob) => {
                    downloadBlob(fixedBlob, 'webm');
                });
            } else {
                downloadBlob(rawBlob, isActuallyWebM ? 'webm' : 'mp4');
            }
        };

        statusText.textContent = 'Processing video on server...';
        const formData = new FormData();
        const ext = userSelectedFormat === 'video/mp4' ? 'mp4' : 'webm';
        formData.append('file', rawBlob, `animation.${isActuallyWebM ? 'webm' : 'mp4'}`);
        formData.append('fps', fps);
        formData.append('format', ext);
        formData.append('bitrate', videoBitrate);
        
        fetch('/api/convert-video', {
            method: 'POST',
            body: formData
        })
        .then(res => {
            if (!res.ok) throw new Error('Transcoding failed');
            return res.blob();
        })
        .then(processedBlob => {
            downloadBlob(processedBlob, ext);
        })
        .catch(err => {
            console.error(err);
            alert('Server processing failed. Downloading original video instead.');
            fallbackDownload();
        });
    };

    const recordStartTime = Date.now();
    recorder.start();

    // Map GPX coords to Canvas pixels
    let latLngToCanvasPx;
    if (isPosterActive && currentRoute.posterMapBounds) {
        const bounds = currentRoute.posterMapBounds;
        const bLatMin = bounds[0][0];
        const bLonMin = bounds[0][1];
        const bLatMax = bounds[1][0];
        const bLonMax = bounds[1][1];
        
        const pMin = map.options.crs.project(L.latLng(bLatMin, bLonMin));
        const pMax = map.options.crs.project(L.latLng(bLatMax, bLonMax));
        
        const xMin = pMin.x;
        const xMax = pMax.x;
        const yMin = pMin.y;
        const yMax = pMax.y;

        latLngToCanvasPx = function(lat, lng) {
            const p = map.options.crs.project(L.latLng(lat, lng));
            return {
                x: ((p.x - xMin) / (xMax - xMin)) * width,
                y: (1.0 - (p.y - yMin) / (yMax - yMin)) * height
            };
        };
    } else {
        latLngToCanvasPx = function(lat, lng) {
            const pxX = lngToX(lng, zoom);
            const pxY = latToY(lat, zoom);
            return {
                x: pxX - minPxX,
                y: pxY - minPxY
            };
        };
    }

    let currentFrame = 0;

    function drawFrame() {
        let ratio = currentFrame / totalFrames;
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

        if (!isPosterActive) {
            if (cameraMode === 'static') {
                minPxX = staticMinPxX;
                minPxY = staticMinPxY;
            } else {
                // Update camera position to follow active point (centering on activePt)
                const activePxX = lngToX(activePt.lon, zoom);
                const activePxY = latToY(activePt.lat, zoom);
                minPxX = activePxX - width / 2;
                minPxY = activePxY - height / 2;
            }
        }

        // 1. Draw Map Tiles or Poster Background that cover the current viewport
        ctx.clearRect(0, 0, width, height);
        ctx.save();
        
        if (isPosterActive) {
            if (posterMapImg) {
                ctx.drawImage(posterMapImg, 0, 0, width, height);
            } else {
                ctx.fillStyle = currentRoute.posterMapBgColor || '#0e1320';
                ctx.fillRect(0, 0, width, height);
            }
        } else {
            const frameTileMinX = Math.floor(minPxX / (256 * scaleFactor));
            const frameTileMaxX = Math.floor((minPxX + width) / (256 * scaleFactor));
            const frameTileMinY = Math.floor(minPxY / (256 * scaleFactor));
            const frameTileMaxY = Math.floor((minPxY + height) / (256 * scaleFactor));

            for (let tx = frameTileMinX; tx <= frameTileMaxX; tx++) {
                for (let ty = frameTileMinY; ty <= frameTileMaxY; ty++) {
                    const img = loadedTilesMap[`${tx}_${ty}`];
                    if (img) {
                        const dx = tx * 256 * scaleFactor - minPxX;
                        const dy = ty * 256 * scaleFactor - minPxY;
                        const ds = 256 * scaleFactor;
                        ctx.drawImage(img, dx, dy, ds, ds);
                    }
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
        getFilteredWaypoints().forEach(wpt => {
            const pos = latLngToCanvasPx(wpt.lat, wpt.lon);
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, 5 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#9333ea';
            ctx.strokeStyle = '#00f0ff';
            ctx.lineWidth = 1.5 * scaleFactor;
            ctx.fill();
            ctx.stroke();
        });

        // 4b. Draw Start and Finish markers on Canvas
        if (animationPoints.length > 0) {
            // Draw Start Marker (Neon Green circle with white play triangle)
            const startPt = animationPoints[0];
            const startPos = latLngToCanvasPx(startPt.lat, startPt.lon);
            
            ctx.beginPath();
            ctx.arc(startPos.x, startPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#064e3b';
            ctx.strokeStyle = '#10b981';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            // Draw Play Triangle
            ctx.beginPath();
            const sz = 3.5 * scaleFactor;
            ctx.moveTo(startPos.x - sz * 0.6, startPos.y - sz);
            ctx.lineTo(startPos.x + sz, startPos.y);
            ctx.lineTo(startPos.x - sz * 0.6, startPos.y + sz);
            ctx.closePath();
            ctx.fillStyle = '#ffffff';
            ctx.fill();

            // Draw Finish Marker (Neon Red circle with 2x2 Checkerboard pattern in center)
            const finishPt = animationPoints[animationPoints.length - 1];
            const finishPos = latLngToCanvasPx(finishPt.lat, finishPt.lon);
            
            ctx.beginPath();
            ctx.arc(finishPos.x, finishPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#7f1d1d';
            ctx.strokeStyle = '#ef4444';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            // Draw 2x2 Checkerboard in center
            const sq = 3 * scaleFactor;
            // Top-left
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x - sq, finishPos.y - sq, sq, sq);
            // Top-right
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x, finishPos.y - sq, sq, sq);
            // Bottom-left
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x - sq, finishPos.y, sq, sq);
            // Bottom-right
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x, finishPos.y, sq, sq);
        }

        // 5. Draw Active Neon Indicator Marker
        ctx.beginPath();
        ctx.arc(activePos.x, activePos.y, 7 * scaleFactor, 0, 2 * Math.PI);
        ctx.fillStyle = '#ffffff';
        ctx.strokeStyle = '#00f0ff';
        ctx.lineWidth = 2 * scaleFactor;
        ctx.fill();
        ctx.stroke();

        // 6. Draw Modern Watermark Corner
        const showWatermark = localStorage.getItem('blaeu_show_watermark') === 'on';
        if (showWatermark) {
            ctx.fillStyle = 'rgba(0, 240, 255, 0.85)';
            ctx.font = `700 ${Math.round(20 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText(currentRoute.name.toUpperCase(), 40 * scaleFactor, height - 65 * scaleFactor);
            ctx.fillStyle = 'rgba(147, 51, 234, 0.85)';
            ctx.font = `600 ${Math.round(11 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText('BLAEU GPX CARTOGRAPHER', 40 * scaleFactor, height - 45 * scaleFactor);
        }

        // Draw Top Stats Overlay
        drawTopStats(ctx, width, scaleFactor);

        // Update progress bar
        currentFrame++;
        const totalProgress = 30 + Math.round(ratio * 70); // 30-100% progress
        fill.style.width = `${totalProgress}%`;
        
        const displayTime = ratio * targetVideoDuration;
        statusText.textContent = `Rendering frame ${currentFrame}/${totalFrames} at ${displayTime.toFixed(1)}s / ${targetVideoDuration.toFixed(1)}s...`;

        if (currentFrame > totalFrames) {
            try {
                if (recorder && recorder.state !== 'inactive') {
                    recorder.stop();
                } else {
                    modal.classList.add('hidden');
                }
            } catch (err) {
                console.error("Error stopping recorder:", err);
                modal.classList.add('hidden');
            }
            return;
        }

        // Schedule next frame to match the target capture rate, preventing frame drops in MediaRecorder
        setTimeout(drawFrame, 1000 / fps);
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
                <button class="btn btn-sm btn-danger btn-delete-folder" data-folder-id="${f.id}">Delete</button>
            </li>
        `).join('');
    } catch (e) {
        list.innerHTML = '<li>Error loading folders list</li>';
    }
}

function closeFoldersModal() {
    document.getElementById('folders-modal').classList.add('hidden');
}

// Settings Modal Operations
function openSettingsModal() {
    closeMobileSidebar();
    loadSettingsIntoModal();
    
    // Reset tabs to General tab
    const tabBtns = document.querySelectorAll('#settings-modal .tab-btn');
    tabBtns.forEach(btn => {
        if (btn.getAttribute('data-tab') === 'settings-tab-general') {
            btn.classList.add('active');
            btn.style.borderBottom = '2px solid var(--primary)';
            btn.style.opacity = '0.95';
        } else {
            btn.classList.remove('active');
            btn.style.borderBottom = '2px solid transparent';
            btn.style.opacity = '0.6';
        }
    });
    const panes = document.querySelectorAll('#settings-modal .settings-tab-pane');
    panes.forEach(pane => {
        if (pane.id === 'settings-tab-general') {
            pane.classList.remove('hidden');
        } else {
            pane.classList.add('hidden');
        }
    });

    document.getElementById('settings-modal').classList.remove('hidden');
    checkGarminStatus();
    checkIntervalsStatus();
    refreshTrainingDataSettings();
    loadLibrary();
    if (currentUser && currentUser.is_admin) {
        const adminSec = document.getElementById('admin-user-section');
        if (adminSec) adminSec.classList.remove('hidden');
        const adminTab = document.getElementById('settings-admin-tab-btn');
        if (adminTab) adminTab.classList.remove('hidden');
        loadAdminUserList();
        document.getElementById('admin-llm-section').classList.remove('hidden');
        loadLlmSettings();
    } else {
        const adminSec = document.getElementById('admin-user-section');
        if (adminSec) adminSec.classList.add('hidden');
        const adminTab = document.getElementById('settings-admin-tab-btn');
        if (adminTab) adminTab.classList.add('hidden');
        document.getElementById('admin-llm-section').classList.add('hidden');
    }
    loadCoachProfile();
}

function loadSettingsIntoModal() {
    const modeSelect = document.getElementById('mode-select');
    if (modeSelect) {
        modeSelect.value = localStorage.getItem('blaeu_animation_mode') || 'realtime';
    }
    const cameraSelect = document.getElementById('camera-select');
    if (cameraSelect) {
        cameraSelect.value = localStorage.getItem('blaeu_camera_mode') || 'follow';
    }
    const aspectSelect = document.getElementById('aspect-ratio-select');
    if (aspectSelect && currentUser) {
        aspectSelect.value = currentUser.default_aspect_ratio || '16:9';
    }
    const resSelect = document.getElementById('res-select');
    if (resSelect) {
        resSelect.value = localStorage.getItem('blaeu_video_res') || '1080';
    }
    const fpsSelect = document.getElementById('fps-select');
    if (fpsSelect) {
        fpsSelect.value = localStorage.getItem('blaeu_video_fps') || '30';
    }
    const formatSelect = document.getElementById('format-select');
    if (formatSelect) {
        formatSelect.value = localStorage.getItem('blaeu_video_format') || 'video/webm';
    }
    const privacySelect = document.getElementById('privacy-select');
    if (privacySelect) {
        privacySelect.value = localStorage.getItem('blaeu_privacy_distance') || '0';
    }
    const defaultStyleSelect = document.getElementById('default-map-style-select');
    if (defaultStyleSelect && currentUser) {
        defaultStyleSelect.value = currentUser.default_map_style || 'dark';
    }
    const weekStartSelect = document.getElementById('week-start-day-select');
    if (weekStartSelect && currentUser) {
        weekStartSelect.value = currentUser.week_start_day || 'Monday';
    }
    const autoSyncSelect = document.getElementById('settings-garmin-auto-sync');
    if (autoSyncSelect) {
        autoSyncSelect.value = currentGarminAutoSync;
    }
    const intervalsAutoSyncSelect = document.getElementById('settings-intervals-auto-sync');
    if (intervalsAutoSyncSelect) {
        intervalsAutoSyncSelect.value = currentIntervalsAutoSync;
    }
    const watermarkSelect = document.getElementById('watermark-select');
    if (watermarkSelect) {
        watermarkSelect.value = localStorage.getItem('blaeu_show_watermark') || 'off';
    }
    const exportStatsSelect = document.getElementById('export-stats-select');
    if (exportStatsSelect) {
        exportStatsSelect.value = localStorage.getItem('blaeu_show_export_stats') || 'on';
    }
}

async function saveSettingsModal() {
    await saveWellnessSetting();
    await saveCoachProfileIfChanged();

    const modeSelect = document.getElementById('mode-select');
    const oldMode = localStorage.getItem('blaeu_animation_mode') || 'realtime';
    const newMode = modeSelect ? modeSelect.value : oldMode;
    localStorage.setItem('blaeu_animation_mode', newMode);

    const cameraSelect = document.getElementById('camera-select');
    const oldCamera = localStorage.getItem('blaeu_camera_mode') || 'follow';
    const newCamera = cameraSelect ? cameraSelect.value : oldCamera;
    localStorage.setItem('blaeu_camera_mode', newCamera);

    const aspectSelect = document.getElementById('aspect-ratio-select');
    if (aspectSelect && currentUser) {
        const newVal = aspectSelect.value;
        const oldVal = currentUser.default_aspect_ratio || '16:9';
        if (newVal !== oldVal) {
            try {
                const res = await fetch('/api/auth/default-aspect-ratio', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ default_aspect_ratio: newVal })
                });
                if (res.ok) {
                    currentUser.default_aspect_ratio = newVal;
                    if (currentRoute && currentMapStyle !== 'dark') {
                        delete currentRoute.posterMapUrl;
                        delete currentRoute.posterMapBounds;
                        delete currentRoute.posterMapBgColor;
                        delete currentRoute.posterMapTextColor;
                        applyMapStyle();
                    }
                } else {
                    console.error('Failed to update default map aspect ratio');
                }
            } catch (err) {
                console.error('Error updating default map aspect ratio:', err);
            }
        }
    }

    const resSelect = document.getElementById('res-select');
    if (resSelect) {
        localStorage.setItem('blaeu_video_res', resSelect.value);
    }

    const fpsSelect = document.getElementById('fps-select');
    if (fpsSelect) {
        localStorage.setItem('blaeu_video_fps', fpsSelect.value);
    }

    const formatSelect = document.getElementById('format-select');
    if (formatSelect) {
        localStorage.setItem('blaeu_video_format', formatSelect.value);
    }

    const privacySelect = document.getElementById('privacy-select');
    const oldPrivacy = localStorage.getItem('blaeu_privacy_distance') || '0';
    const newPrivacy = privacySelect ? privacySelect.value : oldPrivacy;
    localStorage.setItem('blaeu_privacy_distance', newPrivacy);

    const defaultStyleSelect = document.getElementById('default-map-style-select');
    if (defaultStyleSelect && currentUser) {
        const newVal = defaultStyleSelect.value;
        const oldVal = currentUser.default_map_style || 'dark';
        if (newVal !== oldVal) {
            try {
                const res = await fetch('/api/auth/default-map-style', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ default_map_style: newVal })
                });
                if (res.ok) {
                    currentUser.default_map_style = newVal;
                    if (currentRoute) {
                        currentMapStyle = newVal;
                        const hudSelect = document.getElementById('hud-map-style-select');
                        if (hudSelect) {
                            hudSelect.value = newVal;
                        }
                        applyMapStyle();
                    }
                } else {
                    console.error('Failed to update default map style');
                }
            } catch (err) {
                console.error('Error updating default map style:', err);
            }
        }
    }

    const weekStartSelect = document.getElementById('week-start-day-select');
    if (weekStartSelect && currentUser) {
        const newVal = weekStartSelect.value;
        const oldVal = currentUser.week_start_day || 'Monday';
        if (newVal !== oldVal) {
            try {
                const res = await fetch('/api/auth/week-start-day', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ week_start_day: newVal })
                });
                if (res.ok) {
                    currentUser.week_start_day = newVal;
                    renderDashboardGrid();
                } else {
                    console.error('Failed to update week start day');
                }
            } catch (err) {
                console.error('Error updating week start day:', err);
            }
        }
    }

    const autoSyncSelect = document.getElementById('settings-garmin-auto-sync');
    if (autoSyncSelect && !document.getElementById('garmin-connected-section').classList.contains('hidden')) {
        const interval = autoSyncSelect.value;
        if (interval !== currentGarminAutoSync) {
            try {
                const res = await fetch('/api/garmin/auto-sync', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_sync_interval: interval })
                });
                if (!res.ok) {
                    const data = await res.json();
                    throw new Error(data.error || 'Failed to update auto-sync setting');
                }
                currentGarminAutoSync = interval;
            } catch (err) {
                console.error('Error updating Garmin auto-sync:', err);
                alert(err.message);
            }
        }
    }

    const intervalsAutoSyncSelect = document.getElementById('settings-intervals-auto-sync');
    if (intervalsAutoSyncSelect && !document.getElementById('intervals-connected-section').classList.contains('hidden')) {
        const interval = intervalsAutoSyncSelect.value;
        if (interval !== currentIntervalsAutoSync) {
            try {
                const res = await fetch('/api/intervals/auto-sync', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_sync_interval: interval })
                });
                if (!res.ok) {
                    const data = await res.json();
                    throw new Error(data.error || 'Failed to update auto-sync setting');
                }
                currentIntervalsAutoSync = interval;
            } catch (err) {
                console.error('Error updating Intervals.icu auto-sync:', err);
                alert(err.message);
            }
        }
    }

    const watermarkSelect = document.getElementById('watermark-select');
    if (watermarkSelect) {
        localStorage.setItem('blaeu_show_watermark', watermarkSelect.value);
    }
    const exportStatsSelect = document.getElementById('export-stats-select');
    if (exportStatsSelect) {
        localStorage.setItem('blaeu_show_export_stats', exportStatsSelect.value);
    }

    if (newMode !== oldMode || newPrivacy !== oldPrivacy) {
        if (currentRoute) {
            if (newPrivacy !== oldPrivacy) {
                drawRouteOnMap();
                if (currentMapStyle !== 'dark') {
                    applyMapStyle();
                }
            }
            prepareAnimationPoints();
        }
    }

    document.getElementById('settings-modal').classList.add('hidden');
}

function cancelSettingsModal() {
    loadSettingsIntoModal();
    document.getElementById('settings-modal').classList.add('hidden');
}

// About Modal Operations
async function openAboutModal() {
    closeMobileSidebar();
    document.getElementById('about-modal').classList.remove('hidden');
    try {
        const res = await fetch('/api/about');
        const data = await res.json();
        document.getElementById('about-version').textContent = data.version;
        
        // Format the build date if it's in ISO format
        let buildDate = data.build_date;
        if (buildDate && buildDate !== 'Unknown') {
            try {
                const date = new Date(buildDate);
                if (!isNaN(date.getTime())) {
                    buildDate = date.toLocaleString();
                }
            } catch (e) {}
        }
        document.getElementById('about-build-date').textContent = buildDate;
    } catch (err) {
        console.error('Error fetching about details:', err);
        document.getElementById('about-version').textContent = '0.11.0';
        document.getElementById('about-build-date').textContent = 'Unknown';
    }
}

function closeAboutModal() {
    document.getElementById('about-modal').classList.add('hidden');
}


function initSettingsFormats() {
    const formatSelect = document.getElementById('format-select');
    if (!formatSelect) return;
    
    formatSelect.innerHTML = '';
    
    // Always offer WebM (.webm)
    const optWebm = document.createElement('option');
    optWebm.value = 'video/webm';
    optWebm.textContent = 'WebM (.webm)';
    formatSelect.appendChild(optWebm);

    // Always offer MP4 (.mp4)
    const optMp4 = document.createElement('option');
    optMp4.value = 'video/mp4';
    optMp4.textContent = 'MP4 (.mp4)';
    formatSelect.appendChild(optMp4);
    
    // Restore saved format preference
    const savedFormat = localStorage.getItem('blaeu_video_format');
    if (savedFormat) {
        const hasOption = Array.from(formatSelect.options).some(opt => opt.value === savedFormat);
        if (hasOption) {
            formatSelect.value = savedFormat;
        }
    }
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


function hexToRgba(hex, alpha) {
    let c = hex.substring(1);
    if (c.length === 3) {
        c = c[0] + c[0] + c[1] + c[1] + c[2] + c[2];
    }
    const r = parseInt(c.substring(0, 2), 16);
    const g = parseInt(c.substring(2, 4), 16);
    const b = parseInt(c.substring(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function drawIcon(ctx, type, x, y, size, color) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5 * (size / 20); // Scale line width proportionally
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    if (type === 'duration') {
        // Clock icon
        ctx.beginPath();
        ctx.arc(x, y, size / 2, 0, 2 * Math.PI);
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x, y - size / 3);
        ctx.moveTo(x, y);
        ctx.lineTo(x + size / 4, y + size / 8);
        ctx.stroke();
    } else if (type === 'distance') {
        // Map Pin / Marker icon
        ctx.beginPath();
        ctx.arc(x, y - size / 6, size / 3, 0, 2 * Math.PI);
        ctx.stroke();
        
        ctx.beginPath();
        ctx.moveTo(x - size / 3, y - size / 6);
        ctx.bezierCurveTo(x - size / 3, y + size / 6, x, y + size / 2, x, y + size / 2);
        ctx.bezierCurveTo(x, y + size / 2, x + size / 3, y + size / 6, x + size / 3, y - size / 6);
        ctx.stroke();
        
        ctx.beginPath();
        ctx.arc(x, y - size / 6, size / 8, 0, 2 * Math.PI);
        ctx.stroke();
    } else if (type === 'pace') {
        // Fast-forward / Speedometer icon
        ctx.beginPath();
        ctx.arc(x, y + size / 8, size / 2, 0.8 * Math.PI, 2.2 * Math.PI); // Open circle
        ctx.stroke();
        
        ctx.beginPath();
        ctx.moveTo(x, y + size / 8);
        ctx.lineTo(x + size / 4, y - size / 8); // Hand
        ctx.stroke();
        
        // Dash at top
        ctx.beginPath();
        ctx.moveTo(x - size / 6, y - size / 3);
        ctx.lineTo(x + size / 6, y - size / 3);
        ctx.stroke();
    } else if (type === 'gain') {
        // Mountains icon
        ctx.beginPath();
        // Left small peak
        ctx.moveTo(x - size / 2, y + size / 3);
        ctx.lineTo(x - size / 6, y - size / 4);
        ctx.lineTo(x + size / 6, y + size / 3);
        // Right big peak
        ctx.moveTo(x - size / 4, y + size / 3);
        ctx.lineTo(x + size / 6, y - size / 2);
        ctx.lineTo(x + size / 2, y + size / 3);
        ctx.stroke();
    }

    ctx.restore();
}

function drawTopStats(ctx, width, scaleFactor) {
    if (!currentRoute) return;

    const showStats = localStorage.getItem('blaeu_show_export_stats') !== 'off';
    if (!showStats) return;

    const isPosterActive = currentMapStyle !== 'dark' && currentRoute.posterMapUrl;
    const statsColor = isPosterActive ? (currentRoute.posterMapTextColor || '#8B4513') : '#00f0ff';
    const baseColor = isPosterActive ? (currentRoute.posterMapBgColor || '#F5EDE4') : '#070a19';

    // Draw top linear gradient
    const topGrad = ctx.createLinearGradient(0, 0, 0, 110 * scaleFactor);
    topGrad.addColorStop(0, hexToRgba(baseColor, 0.85));
    topGrad.addColorStop(1, hexToRgba(baseColor, 0.0));
    ctx.fillStyle = topGrad;
    ctx.fillRect(0, 0, width, 110 * scaleFactor);

    // Prepare stats data
    const stats = [
        { type: 'distance', value: formatDistance(currentRoute.total_distance) },
        { type: 'duration', value: formatDuration(currentRoute.duration) },
        { type: 'pace', value: formatPace(currentRoute.avg_speed) },
        { type: 'gain', value: `${Math.round(currentRoute.elevation_gain)} m` }
    ];

    // Draw each stat distributed horizontally
    ctx.save();
    ctx.textAlign = 'center';
    
    stats.forEach((stat, idx) => {
        const x = (width / 4) * (idx + 0.5);
        
        // Draw Icon
        const iconSize = 20 * scaleFactor;
        const iconY = 35 * scaleFactor;
        drawIcon(ctx, stat.type, x, iconY, iconSize, statsColor);
        
        // Draw Value
        ctx.fillStyle = statsColor;
        ctx.font = `700 ${Math.round(20 * scaleFactor)}px "Outfit", sans-serif`;
        ctx.fillText(stat.value, x, 75 * scaleFactor);
    });
    
    ctx.restore();
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

function formatPace(mps) {
    if (!mps || mps <= 0.1) return '--:-- m/km';
    const totalSecondsPerKm = 1000 / mps;
    const minutes = Math.floor(totalSecondsPerKm / 60);
    const seconds = Math.round(totalSecondsPerKm % 60);
    let displayMins = minutes;
    let displaySecs = seconds;
    if (displaySecs === 60) {
        displayMins += 1;
        displaySecs = 0;
    }
    return `${displayMins}:${displaySecs.toString().padStart(2, '0')} m/km`;
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

// ----------------------------------------------------
// Garmin Connect API Integration Functions
// ----------------------------------------------------
let garminActivities = []; // Store fetched activities

function initGarminIntegration() {
    // Check Garmin status on startup
    checkGarminStatus();

    // Bind event listeners
    const connectBtn = document.getElementById('garmin-connect-btn');
    if (connectBtn) {
        connectBtn.addEventListener('click', connectGarmin);
    }

    const disconnectBtn = document.getElementById('garmin-disconnect-btn');
    if (disconnectBtn) {
        disconnectBtn.addEventListener('click', disconnectGarmin);
    }

    const syncBtn = document.getElementById('sidebar-garmin-sync-btn');
    if (syncBtn) {
        syncBtn.addEventListener('click', syncGarminActivities);
    }



    const closeActivitiesBtn = document.getElementById('close-garmin-activities-btn');
    if (closeActivitiesBtn) {
        closeActivitiesBtn.addEventListener('click', () => {
            document.getElementById('garmin-activities-modal').classList.add('hidden');
        });
    }

    const selectAllCheckbox = document.getElementById('garmin-select-all');
    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', (e) => {
            const checkboxes = document.querySelectorAll('.garmin-activity-checkbox');
            checkboxes.forEach(cb => {
                if (!cb.disabled) {
                    cb.checked = e.target.checked;
                }
            });
        });
    }

    const importSelectedBtn = document.getElementById('garmin-import-selected-btn');
    if (importSelectedBtn) {
        importSelectedBtn.addEventListener('click', importSelectedGarminActivities);
    }
}

async function checkGarminStatus() {
    try {
        const res = await fetch('/api/garmin/status');
        const data = await res.json();
        
        const disconnectedSection = document.getElementById('garmin-disconnected-section');
        const connectedSection = document.getElementById('garmin-connected-section');
        
        const sidebarBtn = document.getElementById('sidebar-garmin-sync-btn');
        if (data.status === 'connected') {
            disconnectedSection.classList.add('hidden');
            connectedSection.classList.remove('hidden');
            if (sidebarBtn) sidebarBtn.classList.remove('hidden');
            
            document.getElementById('garmin-connected-name').textContent = data.display_name || 'Garmin Connected';
            document.getElementById('garmin-connected-email').textContent = data.email;
            
            const autoSyncSelect = document.getElementById('settings-garmin-auto-sync');
            if (autoSyncSelect) {
                currentGarminAutoSync = data.auto_sync_interval || 'off';
                autoSyncSelect.value = currentGarminAutoSync;
            }
        } else {
            disconnectedSection.classList.remove('hidden');
            connectedSection.classList.add('hidden');
            if (sidebarBtn) sidebarBtn.classList.add('hidden');
            
            document.getElementById('garmin-email').value = '';
            document.getElementById('garmin-password').value = '';
            const mfaInput = document.getElementById('garmin-mfa');
            if (mfaInput) mfaInput.value = '';
            document.getElementById('garmin-mfa-container').classList.add('hidden');
            
            const connectBtn = document.getElementById('garmin-connect-btn');
            if (connectBtn) {
                connectBtn.disabled = false;
                connectBtn.textContent = 'Connect Garmin';
            }
        }
    } catch (err) {
        console.error("Error checking Garmin status", err);
    }
}

async function connectGarmin() {
    const email = document.getElementById('garmin-email').value.trim();
    const password = document.getElementById('garmin-password').value.trim();
    const mfaContainer = document.getElementById('garmin-mfa-container');
    const mfaInput = document.getElementById('garmin-mfa');
    const mfaCode = mfaContainer.classList.contains('hidden') ? null : mfaInput.value.trim();
    const connectBtn = document.getElementById('garmin-connect-btn');

    if (!email || !password) {
        alert('Please enter both your Garmin email and password.');
        return;
    }

    if (!mfaContainer.classList.contains('hidden') && !mfaCode) {
        alert('Please enter the MFA verification code sent to your phone/email.');
        return;
    }

    connectBtn.disabled = true;
    connectBtn.textContent = mfaCode ? 'Verifying MFA Code...' : 'Connecting...';

    try {
        const payload = { email, password };
        if (mfaCode) {
            payload.mfa_code = mfaCode;
        }

        const res = await fetch('/api/garmin/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json();

        if (res.status === 401 || !res.ok) {
            throw new Error(data.error || 'Connection failed');
        }

        if (data.status === 'mfa_required') {
            mfaContainer.classList.remove('hidden');
            mfaInput.focus();
            connectBtn.disabled = false;
            connectBtn.textContent = 'Verify MFA Code';
            alert('Multi-Factor Authentication is required. Please enter the MFA code sent by Garmin.');
        } else if (data.status === 'connected') {
            alert(`Successfully connected as ${data.display_name || email}!`);
            await checkGarminStatus();
        }
    } catch (err) {
        alert(err.message);
        connectBtn.disabled = false;
        connectBtn.textContent = mfaCode ? 'Verify MFA Code' : 'Connect Garmin';
    }
}

async function disconnectGarmin() {
    if (!confirm('Are you sure you want to disconnect your Garmin account? All local session tokens will be deleted.')) return;
    
    try {
        const res = await fetch('/api/garmin/disconnect', { method: 'POST' });
        if (!res.ok) throw new Error('Failed to disconnect');
        await checkGarminStatus();
        alert('Garmin disconnected successfully.');
    } catch (err) {
        alert(err.message);
    }
}

async function syncGarminActivities() {
    const syncBtn = document.getElementById('sidebar-garmin-sync-btn');
    const originalText = syncBtn.innerHTML;
    
    syncBtn.disabled = true;
    syncBtn.textContent = 'Syncing...';
    
    try {
        const res = await fetch('/api/garmin/activities');
        const data = await res.json();
        
        if (data.status === 'needs_reauthentication') {
            alert('Your Garmin session has expired. Please reconnect your account.');
            await fetch('/api/garmin/disconnect', { method: 'POST' });
            await checkGarminStatus();
            return;
        }
        
        if (!res.ok || data.status !== 'success') {
            throw new Error(data.error || 'Failed to sync activities');
        }
        
        garminActivities = data.activities;
        renderGarminActivitiesList();
        
        document.getElementById('garmin-activities-modal').classList.remove('hidden');
    } catch (err) {
        alert(err.message);
    } finally {
        syncBtn.disabled = false;
        syncBtn.innerHTML = originalText;
    }
}

function renderGarminActivitiesList() {
    const listBody = document.getElementById('garmin-activities-list');
    const selectAllCheckbox = document.getElementById('garmin-select-all');
    if (selectAllCheckbox) selectAllCheckbox.checked = false;
    
    if (!garminActivities || garminActivities.length === 0) {
        listBody.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 20px; opacity: 0.6;">No activities found on Garmin Connect.</td></tr>';
        return;
    }
    
    listBody.innerHTML = garminActivities.map(act => {
        const dateObj = new Date(act.startTimeLocal);
        const dateStr = dateObj.toLocaleDateString(undefined, {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit', hour12: false
        });
        
        const distKm = act.distance ? (act.distance / 1000).toFixed(2) + ' km' : '0.00 km';
        const durStr = formatDuration(act.duration);
        const typeStr = act.activityType ? act.activityType.charAt(0).toUpperCase() + act.activityType.slice(1) : 'Unknown';
        
        return `
            <tr id="garmin-row-${act.activityId}">
                <td style="text-align: center; padding: 12px 10px;">
                    <input type="checkbox" class="garmin-activity-checkbox" data-id="${act.activityId}">
                </td>
                <td style="padding: 12px 10px;">${dateStr}</td>
                <td style="padding: 12px 10px; font-weight: 500;" title="${escapeHTML(act.activityName)}">${escapeHTML(act.activityName)}</td>
                <td style="padding: 12px 10px;"><span class="tag-badge btn-sm">${escapeHTML(typeStr)}</span></td>
                <td style="padding: 12px 10px;">${distKm}</td>
                <td style="padding: 12px 10px;">${durStr}</td>
                <td style="padding: 12px 10px; text-align: right;">
                    <button type="button" class="btn btn-sm btn-primary btn-import-single" data-id="${act.activityId}">Import</button>
                </td>
            </tr>
        `;
    }).join('');
}

async function importSingleGarminActivity(activityId, btn) {
    btn.disabled = true;
    btn.textContent = 'Importing...';
    
    const row = document.getElementById(`garmin-row-${activityId}`);
    if (row) {
        const cb = row.querySelector('.garmin-activity-checkbox');
        if (cb) cb.disabled = true;
    }
    
    try {
        const res = await fetch('/api/garmin/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activityId })
        });
        
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || 'Import failed');
        }
        
        btn.textContent = 'Imported';
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-outline');
        btn.style.borderColor = '#10b981';
        btn.style.color = '#10b981';
        
        await loadTags();
        await loadRoutes();
    } catch (err) {
        alert(err.message);
        btn.disabled = false;
        btn.textContent = 'Import';
        if (row) {
            const cb = row.querySelector('.garmin-activity-checkbox');
            if (cb) cb.disabled = false;
        }
    }
}

async function importSelectedGarminActivities() {
    const checkboxes = document.querySelectorAll('.garmin-activity-checkbox:checked');
    const selectedIds = Array.from(checkboxes).map(cb => cb.getAttribute('data-id'));
    
    if (selectedIds.length === 0) {
        alert('Please select at least one activity to import.');
        return;
    }
    
    const importBtn = document.getElementById('garmin-import-selected-btn');
    const originalText = importBtn.textContent;
    importBtn.disabled = true;
    importBtn.textContent = `Importing (${selectedIds.length})...`;
    
    let successCount = 0;
    let failCount = 0;
    
    for (const activityId of selectedIds) {
        const row = document.getElementById(`garmin-row-${activityId}`);
        const btnSingle = row ? row.querySelector('.btn-import-single') : null;
        
        if (btnSingle && btnSingle.textContent === 'Imported') {
            continue;
        }
        
        if (btnSingle) {
            btnSingle.disabled = true;
            btnSingle.textContent = 'Importing...';
        }
        
        try {
            const res = await fetch('/api/garmin/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ activityId })
            });
            
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.error || 'Import failed');
            }
            
            successCount++;
            if (btnSingle) {
                btnSingle.textContent = 'Imported';
                btnSingle.classList.remove('btn-primary');
                btnSingle.classList.add('btn-outline');
                btnSingle.style.borderColor = '#10b981';
                btnSingle.style.color = '#10b981';
            }
            const cb = row ? row.querySelector('.garmin-activity-checkbox') : null;
            if (cb) {
                cb.checked = false;
                cb.disabled = true;
            }
        } catch (err) {
            console.error(`Failed to import activity ${activityId}:`, err);
            failCount++;
            if (btnSingle) {
                btnSingle.disabled = false;
                btnSingle.textContent = 'Import';
            }
        }
    }
    
    await loadTags();
    await loadRoutes();
    
    importBtn.disabled = false;
    importBtn.textContent = originalText;
    
    let msg = `Import process finished.\nSuccessfully imported: ${successCount} activities.`;
    if (failCount > 0) {
        msg += `\nFailed imports: ${failCount} activities.`;
    }
    alert(msg);
    
    if (failCount === 0) {
        document.getElementById('garmin-activities-modal').classList.add('hidden');
    }
}

// Load available map themes from backend
async function loadMapThemes() {
    try {
        const res = await fetch('/api/map-themes');
        if (!res.ok) throw new Error('Failed to load map themes');
        mapThemes = await res.json();
        
        const select = document.getElementById('hud-map-style-select');
        if (select) {
            select.innerHTML = '<option value="dark">Dark Matter (Default)</option>';
            mapThemes.forEach(theme => {
                const opt = document.createElement('option');
                opt.value = theme.id;
                opt.textContent = theme.name;
                select.appendChild(opt);
            });
        }
        
        const defaultStyleSelect = document.getElementById('default-map-style-select');
        if (defaultStyleSelect) {
            defaultStyleSelect.innerHTML = '<option value="dark">Dark Matter (Default)</option>';
            mapThemes.forEach(theme => {
                const opt = document.createElement('option');
                opt.value = theme.id;
                opt.textContent = theme.name;
                defaultStyleSelect.appendChild(opt);
            });
            if (currentUser) {
                defaultStyleSelect.value = currentUser.default_map_style || 'dark';
            }
        }
    } catch (err) {
        console.error('Error loading map themes:', err);
    }
}

// Apply current map style (Dark Matter vs minimalist poster overlay)
async function applyMapStyle() {
    // Posters need a track: never request one for a route (or anything else) without coordinates
    if (!currentRoute || !routeHasCoordinates(currentRoute)) return;
    
    // Remove existing poster overlay if present
    if (posterMapOverlay) {
        map.removeLayer(posterMapOverlay);
        posterMapOverlay = null;
    }
    
    const mapElement = document.getElementById('map');
    
    // Save preference
    localStorage.setItem('blaeu_map_style', currentMapStyle);
    
    if (currentMapStyle === 'dark') {
        // Restore default tile layer
        if (!map.hasLayer(tileLayer)) {
            tileLayer.addTo(map);
        }
        if (mapElement) {
            mapElement.style.backgroundColor = '';
        }
        
        // Remove background values stored on currentRoute
        delete currentRoute.posterMapUrl;
        delete currentRoute.posterMapBounds;
        delete currentRoute.posterMapBgColor;

        map.setMaxBounds(null);

        const labelsContainer = document.getElementById('hud-map-poster-labels');
        if (labelsContainer) {
            labelsContainer.classList.add('hidden');
        }
        const cityInput = document.getElementById('hud-map-style-city');
        const countryInput = document.getElementById('hud-map-style-country');
        if (cityInput) cityInput.value = '';
        if (countryInput) countryInput.value = '';
        labelsLoadedForRouteId = null;
    } else {
        // Poster map style active
        // Hide default tiles
        if (map.hasLayer(tileLayer)) {
            map.removeLayer(tileLayer);
        }
        
        const select = document.getElementById('hud-map-style-select');
        const cityInput = document.getElementById('hud-map-style-city');
        const countryInput = document.getElementById('hud-map-style-country');
        const labelsContainer = document.getElementById('hud-map-poster-labels');
        
        if (labelsContainer) {
            labelsContainer.classList.remove('hidden');
        }

        // Check if there is completed poster map data matching our active style in database-cached status
        let cachedData = null;
        const currentAspectRatio = (currentUser && currentUser.default_aspect_ratio) || '16:9';
        if (currentRoute.poster_status && 
            currentRoute.poster_status.status === 'completed' && 
            currentRoute.poster_status.theme === currentMapStyle && 
            currentRoute.poster_status.aspect_ratio === currentAspectRatio &&
            labelsLoadedForRouteId !== currentRoute.id) {
            cachedData = currentRoute.poster_status;
        }

        if (cachedData) {
            // Apply cached poster map immediately (no fetch, no loader status text changes!)
            if (mapElement) {
                mapElement.style.backgroundColor = cachedData.bg_color;
            }
            posterMapOverlay = L.imageOverlay(cachedData.image_url, cachedData.bounds, {
                opacity: 1.0,
                zIndex: 1
            }).addTo(map);
            posterMapOverlay.bringToBack();
            map.setMaxBounds(posterMapOverlay.getBounds());

            currentRoute.posterMapUrl = cachedData.image_url;
            currentRoute.posterMapBounds = cachedData.bounds;
            currentRoute.posterMapBgColor = cachedData.bg_color;
            currentRoute.posterMapTextColor = cachedData.text_color;

            if (labelsLoadedForRouteId !== currentRoute.id) {
                if (cityInput) cityInput.value = cachedData.display_city || '';
                if (countryInput) countryInput.value = cachedData.display_country || '';
                labelsLoadedForRouteId = currentRoute.id;
            }
        } else {
            // Cache miss or manually editing labels/switching theme: fetch from backend
            let originalText = '';
            if (select && select.selectedIndex >= 0) {
                originalText = select.options[select.selectedIndex].text;
                select.options[select.selectedIndex].text = 'Loading Map...';
            }

            if (labelsLoadedForRouteId !== currentRoute.id) {
                if (cityInput) cityInput.value = '';
                if (countryInput) countryInput.value = '';
            }

            const fetchRouteId = currentRoute.id;
            try {
                let url = `/api/routes/${fetchRouteId}/poster-map?theme=${currentMapStyle}&aspectRatio=${encodeURIComponent(currentAspectRatio)}`;
                if (labelsLoadedForRouteId === fetchRouteId) {
                    const cityVal = cityInput ? cityInput.value : '';
                    const countryVal = countryInput ? countryInput.value : '';
                    url += `&displayCity=${encodeURIComponent(cityVal)}&displayCountry=${encodeURIComponent(countryVal)}`;
                }

                const res = await fetch(url);
                if (!res.ok) throw new Error('Could not generate poster map');
                const data = await res.json();

                // If user deselected the route or switched to a different route during the fetch, abort
                if (!currentRoute || currentRoute.id !== fetchRouteId) {
                    return;
                }

                if (mapElement) {
                    mapElement.style.backgroundColor = data.bg_color;
                }
                posterMapOverlay = L.imageOverlay(data.image_url, data.bounds, {
                    opacity: 1.0,
                    zIndex: 1
                }).addTo(map);
                posterMapOverlay.bringToBack();
                map.setMaxBounds(posterMapOverlay.getBounds());

                currentRoute.posterMapUrl = data.image_url;
                currentRoute.posterMapBounds = data.bounds;
                currentRoute.posterMapBgColor = data.bg_color;
                currentRoute.posterMapTextColor = data.text_color;

                if (labelsLoadedForRouteId !== currentRoute.id) {
                    if (cityInput) cityInput.value = data.display_city || '';
                    if (countryInput) countryInput.value = data.display_country || '';
                    labelsLoadedForRouteId = currentRoute.id;
                }
            } catch (err) {
                console.error(err);
                alert('Map style generation failed: ' + err.message);
                currentMapStyle = 'dark';
                if (select) select.value = 'dark';
                if (!map.hasLayer(tileLayer)) tileLayer.addTo(map);
                if (mapElement) mapElement.style.backgroundColor = '';
            } finally {
                if (select && select.selectedIndex >= 0) {
                    const activeTheme = mapThemes.find(t => t.id === currentMapStyle);
                    select.options[select.selectedIndex].text = activeTheme ? activeTheme.name : 'Dark Matter (Default)';
                }
            }
        }
    }
}

// Save static high-definition image of the entire track map
async function saveImage() {
    if (!currentRoute || animationPoints.length === 0) return;

    pauseAnimation();

    // Show Progress Dialog
    const modal = document.getElementById('export-modal');
    const fill = document.getElementById('export-progress-fill');
    const statusText = document.getElementById('export-status-text');
    const titleText = document.getElementById('export-status-title');
    
    if (titleText) titleText.textContent = 'Generating Image';
    modal.classList.remove('hidden');
    fill.style.width = '0%';
    statusText.textContent = 'Preloading map background...';

    // Helper to load image
    function loadImage(url) {
        return new Promise((resolve) => {
            const img = new Image();
            img.crossOrigin = 'anonymous';
            img.onload = () => resolve(img);
            img.onerror = () => resolve(null);
            img.src = url;
        });
    }

    const canvas = document.getElementById('export-canvas');
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';

    const latitudes = animationPoints.map(p => p.lat);
    const longitudes = animationPoints.map(p => p.lon);
    const latMin = Math.min(...latitudes);
    const latMax = Math.max(...latitudes);
    const lonMin = Math.min(...longitudes);
    const lonMax = Math.max(...longitudes);
    
    const centerLat = (latMin + latMax) / 2;
    const centerLon = (lonMin + lonMax) / 2;

    const isPosterActive = currentMapStyle !== 'dark' && currentRoute.posterMapUrl;

    if (isPosterActive) {
        // Load the background poster image
        statusText.textContent = 'Loading poster background...';
        const posterMapImg = await loadImage(currentRoute.posterMapUrl);
        if (!posterMapImg) {
            alert('Could not load poster background image.');
            modal.classList.add('hidden');
            return;
        }

        fill.style.width = '50%';
        statusText.textContent = 'Rendering high-resolution poster...';

        const width = posterMapImg.naturalWidth;
        const height = posterMapImg.naturalHeight;
        canvas.width = width;
        canvas.height = height;

        // Scale factor for drawing vectors relative to baseline 1080p height
        const scaleFactor = height / 1080;

        // Draw poster background image (takes up 100% of the canvas)
        ctx.clearRect(0, 0, width, height);
        ctx.drawImage(posterMapImg, 0, 0, width, height);

        // Project coordinate bounds of the poster
        const bounds = currentRoute.posterMapBounds;
        const bLatMin = bounds[0][0];
        const bLonMin = bounds[0][1];
        const bLatMax = bounds[1][0];
        const bLonMax = bounds[1][1];
        
        const pMin = map.options.crs.project(L.latLng(bLatMin, bLonMin));
        const pMax = map.options.crs.project(L.latLng(bLatMax, bLonMax));
        
        const xMin = pMin.x;
        const xMax = pMax.x;
        const yMin = pMin.y;
        const yMax = pMax.y;

        // Map GPX coords to Poster Canvas pixels
        function latLngToCanvasPx(lat, lng) {
            const p = map.options.crs.project(L.latLng(lat, lng));
            return {
                x: ((p.x - xMin) / (xMax - xMin)) * width,
                y: (1.0 - (p.y - yMin) / (yMax - yMin)) * height
            };
        }

        // Draw Route Path
        ctx.beginPath();
        ctx.strokeStyle = '#00f0ff';
        ctx.lineWidth = 6 * scaleFactor;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        animationPoints.forEach((pt, idx) => {
            const pos = latLngToCanvasPx(pt.lat, pt.lon);
            if (idx === 0) ctx.moveTo(pos.x, pos.y);
            else ctx.lineTo(pos.x, pos.y);
        });
        ctx.stroke();

        // Draw Waypoints
        getFilteredWaypoints().forEach(wpt => {
            const pos = latLngToCanvasPx(wpt.lat, wpt.lon);
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, 5 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#9333ea';
            ctx.strokeStyle = '#00f0ff';
            ctx.lineWidth = 1.5 * scaleFactor;
            ctx.fill();
            ctx.stroke();
        });

        // Draw Start/Finish Markers
        if (animationPoints.length > 0) {
            const startPt = animationPoints[0];
            const startPos = latLngToCanvasPx(startPt.lat, startPt.lon);
            ctx.beginPath();
            ctx.arc(startPos.x, startPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#064e3b';
            ctx.strokeStyle = '#10b981';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            ctx.beginPath();
            const sz = 3.5 * scaleFactor;
            ctx.moveTo(startPos.x - sz * 0.6, startPos.y - sz);
            ctx.lineTo(startPos.x + sz, startPos.y);
            ctx.lineTo(startPos.x - sz * 0.6, startPos.y + sz);
            ctx.closePath();
            ctx.fillStyle = '#ffffff';
            ctx.fill();

            const finishPt = animationPoints[animationPoints.length - 1];
            const finishPos = latLngToCanvasPx(finishPt.lat, finishPt.lon);
            ctx.beginPath();
            ctx.arc(finishPos.x, finishPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#7f1d1d';
            ctx.strokeStyle = '#ef4444';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            const sq = 3 * scaleFactor;
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x - sq, finishPos.y - sq, sq, sq);
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x, finishPos.y - sq, sq, sq);
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x - sq, finishPos.y, sq, sq);
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x, finishPos.y, sq, sq);
        }

        // Draw Watermark
        const showWatermark = localStorage.getItem('blaeu_show_watermark') === 'on';
        if (showWatermark) {
            ctx.fillStyle = 'rgba(0, 240, 255, 0.85)';
            ctx.font = `700 ${Math.round(20 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText(currentRoute.name.toUpperCase(), 40 * scaleFactor, height - 65 * scaleFactor);
            ctx.fillStyle = 'rgba(147, 51, 234, 0.85)';
            ctx.font = `600 ${Math.round(11 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText('BLAEU GPX CARTOGRAPHER', 40 * scaleFactor, height - 45 * scaleFactor);
        }

        // Draw Top Stats Overlay
        drawTopStats(ctx, width, scaleFactor);

    } else {
        // Dark Matter Style
        // Canvas resolution configuration
        const resSelect = document.getElementById('res-select');
        const resVal = resSelect ? resSelect.value : '1080';
        let height = 1080;
        
        if (resVal === '720') {
            height = 720;
        } else if (resVal === '2160') {
            height = 2160;
        }

        const aspectStr = (currentUser && currentUser.default_aspect_ratio) || '16:9';
        const parts = aspectStr.split(':');
        const targetAspect = parseFloat(parts[0]) / parseFloat(parts[1]);
        let width = Math.round(height * targetAspect);
        if (width % 2 !== 0) {
            width += 1;
        }

        canvas.width = width;
        canvas.height = height;

        const scaleFactor = height / 1080;

        // Use fitZoom to make sure the whole track is visible
        const pBounds = L.latLngBounds(L.latLng(latMin, lonMin), L.latLng(latMax, lonMax));
        const fitZoom = map.getBoundsZoom(pBounds);
        const zoom = fitZoom;

        // Web Mercator Formulas
        function lngToX(lng, z) {
            return (lng + 180) / 360 * Math.pow(2, z) * 256 * scaleFactor;
        }
        function latToY(lat, z) {
            const latRad = lat * Math.PI / 180;
            return (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * Math.pow(2, z) * 256 * scaleFactor;
        }

        const centerPxX = lngToX(centerLon, zoom);
        const centerPxY = latToY(centerLat, zoom);
        const minPxX = centerPxX - width / 2;
        const minPxY = centerPxY - height / 2;

        // Collect all tiles covering the viewport centered on the track
        const tilesSet = new Set();
        const frameTileMinX = Math.floor(minPxX / (256 * scaleFactor));
        const frameTileMaxX = Math.floor((minPxX + width) / (256 * scaleFactor));
        const frameTileMinY = Math.floor(minPxY / (256 * scaleFactor));
        const frameTileMaxY = Math.floor((minPxY + height) / (256 * scaleFactor));

        for (let tx = frameTileMinX; tx <= frameTileMaxX; tx++) {
            for (let ty = frameTileMinY; ty <= frameTileMaxY; ty++) {
                tilesSet.add(`${tx}_${ty}`);
            }
        }

        const tilesToLoad = Array.from(tilesSet).map(key => {
            const [tx, ty] = key.split('_').map(Number);
            return { x: tx, y: ty, z: zoom };
        });

        const loadedTilesMap = {};
        let loadedCount = 0;
        for (const t of tilesToLoad) {
            const tileUrl = `/api/tiles/${t.z}/${t.x}/${t.y}.png`;
            const img = await loadImage(tileUrl);
            if (img) {
                loadedTilesMap[`${t.x}_${t.y}`] = img;
            }
            loadedCount++;
            const progressPct = Math.round((loadedCount / tilesToLoad.length) * 50);
            fill.style.width = `${progressPct}%`;
            statusText.textContent = `Loading map background (${loadedCount}/${tilesToLoad.length})`;
        }

        statusText.textContent = 'Rendering image...';
        fill.style.width = '80%';

        // Map GPX coords to Canvas pixels
        function latLngToCanvasPx(lat, lng) {
            const pxX = lngToX(lng, zoom);
            const pxY = latToY(lat, zoom);
            return {
                x: pxX - minPxX,
                y: pxY - minPxY
            };
        }

        // Draw Map Tiles
        ctx.clearRect(0, 0, width, height);
        ctx.save();
        for (let tx = frameTileMinX; tx <= frameTileMaxX; tx++) {
            for (let ty = frameTileMinY; ty <= frameTileMaxY; ty++) {
                const img = loadedTilesMap[`${tx}_${ty}`];
                if (img) {
                    const dx = tx * 256 * scaleFactor - minPxX;
                    const dy = ty * 256 * scaleFactor - minPxY;
                    const ds = 256 * scaleFactor;
                    ctx.drawImage(img, dx, dy, ds, ds);
                }
            }
        }
        ctx.restore();

        // 2. Overlay vignette
        const grad = ctx.createRadialGradient(width/2, height/2, width/3, width/2, height/2, width/1.4);
        grad.addColorStop(0, 'rgba(7, 10, 19, 0.0)');
        grad.addColorStop(1, 'rgba(7, 10, 19, 0.65)');
        ctx.fillStyle = grad;
        ctx.fillRect(0, 0, width, height);

        // 3. Draw Route Path
        ctx.beginPath();
        ctx.strokeStyle = '#00f0ff';
        ctx.lineWidth = 6 * scaleFactor;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        animationPoints.forEach((pt, idx) => {
            const pos = latLngToCanvasPx(pt.lat, pt.lon);
            if (idx === 0) ctx.moveTo(pos.x, pos.y);
            else ctx.lineTo(pos.x, pos.y);
        });
        ctx.stroke();

        // 4. Draw Waypoints
        getFilteredWaypoints().forEach(wpt => {
            const pos = latLngToCanvasPx(wpt.lat, wpt.lon);
            ctx.beginPath();
            ctx.arc(pos.x, pos.y, 5 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#9333ea';
            ctx.strokeStyle = '#00f0ff';
            ctx.lineWidth = 1.5 * scaleFactor;
            ctx.fill();
            ctx.stroke();
        });

        // 5. Draw Start/Finish Markers
        if (animationPoints.length > 0) {
            const startPt = animationPoints[0];
            const startPos = latLngToCanvasPx(startPt.lat, startPt.lon);
            ctx.beginPath();
            ctx.arc(startPos.x, startPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#064e3b';
            ctx.strokeStyle = '#10b981';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            ctx.beginPath();
            const sz = 3.5 * scaleFactor;
            ctx.moveTo(startPos.x - sz * 0.6, startPos.y - sz);
            ctx.lineTo(startPos.x + sz, startPos.y);
            ctx.lineTo(startPos.x - sz * 0.6, startPos.y + sz);
            ctx.closePath();
            ctx.fillStyle = '#ffffff';
            ctx.fill();

            const finishPt = animationPoints[animationPoints.length - 1];
            const finishPos = latLngToCanvasPx(finishPt.lat, finishPt.lon);

            ctx.beginPath();
            ctx.arc(finishPos.x, finishPos.y, 9 * scaleFactor, 0, 2 * Math.PI);
            ctx.fillStyle = '#7f1d1d';
            ctx.strokeStyle = '#ef4444';
            ctx.lineWidth = 2 * scaleFactor;
            ctx.fill();
            ctx.stroke();
            
            const sq = 3 * scaleFactor;
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x - sq, finishPos.y - sq, sq, sq);
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x, finishPos.y - sq, sq, sq);
            ctx.fillStyle = '#111827';
            ctx.fillRect(finishPos.x - sq, finishPos.y, sq, sq);
            ctx.fillStyle = '#ffffff';
            ctx.fillRect(finishPos.x, finishPos.y, sq, sq);
        }

        // 6. Draw Watermark
        const showWatermark = localStorage.getItem('blaeu_show_watermark') === 'on';
        if (showWatermark) {
            ctx.fillStyle = 'rgba(0, 240, 255, 0.85)';
            ctx.font = `700 ${Math.round(20 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText(currentRoute.name.toUpperCase(), 40 * scaleFactor, height - 65 * scaleFactor);
            ctx.fillStyle = 'rgba(147, 51, 234, 0.85)';
            ctx.font = `600 ${Math.round(11 * scaleFactor)}px "Outfit", sans-serif`;
            ctx.fillText('BLAEU GPX CARTOGRAPHER', 40 * scaleFactor, height - 45 * scaleFactor);
        }

        // Draw Top Stats Overlay
        drawTopStats(ctx, width, scaleFactor);
    }

    // Save/Download
    fill.style.width = '100%';
    statusText.textContent = 'Saving image file...';
    
    setTimeout(() => {
        const dataUrl = canvas.toDataURL('image/png');
        const a = document.createElement('a');
        a.href = dataUrl;
        a.download = `${currentRoute.name.replace(/[^a-z0-9]/gi, '_').toLowerCase()}.png`;
        a.click();
        
        // Hide Modal & Restore title
        modal.classList.add('hidden');
        if (titleText) titleText.textContent = 'Generating Video';
    }, 500);
}

// ----------------------------------------------------
// Authentication & User Management Logic
// ----------------------------------------------------

async function checkAuthStatus() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        const loginModal = document.getElementById('login-modal');
        
        if (data.logged_in) {
            currentUser = data.user;
            csrfToken = data.csrf_token;
            if (loginModal) loginModal.style.display = 'none';
            
            // Set up UI header for logout visibility, etc.
            const logoutBtn = document.getElementById('logout-btn');
            if (logoutBtn) {
                logoutBtn.style.display = 'flex';
            }
            
            // Load app data
            await loadFolders();
            await loadTags();
            await loadRoutes();
            await loadMapThemes();
            checkGarminStatus();
            checkDataPrompt();
            checkCoachAvailability();
        } else {
            currentUser = null;
            csrfToken = data.csrf_token;
            registrationOpen = data.registration_open;
            if (loginModal) loginModal.style.display = 'flex';
            showAuthModal(data.no_users_exist, data.registration_open);
        }
    } catch (err) {
        console.error("Error checking auth status:", err);
    }
}

function toggleAuthMode(e) {
    if (e) e.preventDefault();
    if (!registrationOpen) {
        authMode = 'login';
        showAuthModal(false, false);
        return;
    }
    if (authMode === 'login') {
        authMode = 'register';
    } else {
        authMode = 'login';
    }
    showAuthModal(false, true);
}

function showAuthModal(noUsersExist, registrationOpen) {
    const loginTitle = document.getElementById('login-title');
    const submitBtn = document.getElementById('login-submit-btn');
    const toggleText = document.getElementById('login-toggle-text');
    const toggleLink = document.getElementById('login-toggle-link');
    const errorMsg = document.getElementById('login-error-msg');
    
    if (errorMsg) errorMsg.style.display = 'none';
    
    if (noUsersExist) {
        authMode = 'admin_setup';
        if (loginTitle) loginTitle.textContent = 'Create Admin Account';
        if (submitBtn) submitBtn.textContent = 'Create Admin';
        if (toggleText) toggleText.parentElement.style.display = 'none';
    } else {
        if (authMode === 'register' && registrationOpen) {
            if (loginTitle) loginTitle.textContent = 'Register';
            if (submitBtn) submitBtn.textContent = 'Register';
            if (toggleText) toggleText.textContent = "Already have an account?";
            if (toggleLink) toggleLink.textContent = "Log In";
            if (toggleText) toggleText.parentElement.style.display = 'block';
        } else {
            authMode = 'login';
            if (loginTitle) loginTitle.textContent = 'Log In';
            if (submitBtn) submitBtn.textContent = 'Log In';
            if (toggleText) {
                if (registrationOpen) {
                    toggleText.textContent = "Don't have an account?";
                    if (toggleLink) toggleLink.textContent = "Register";
                    toggleText.parentElement.style.display = 'block';
                } else {
                    toggleText.parentElement.style.display = 'none';
                }
            }
        }
    }
}

async function handleAuthSubmit(e) {
    if (e) e.preventDefault();
    
    const usernameInput = document.getElementById('login-username');
    const passwordInput = document.getElementById('login-password');
    const errorMsg = document.getElementById('login-error-msg');
    const submitBtn = document.getElementById('login-submit-btn');
    
    if (!usernameInput || !passwordInput) return;
    
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    
    if (!username || !password) return;
    
    if (errorMsg) errorMsg.style.display = 'none';
    if (submitBtn) submitBtn.disabled = true;
    
    const url = (authMode === 'register' || authMode === 'admin_setup') ? '/api/auth/register' : '/api/auth/login';
    
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        
        if (!res.ok) {
            throw new Error(data.error || 'Authentication failed');
        }
        
        // Clear forms
        usernameInput.value = '';
        passwordInput.value = '';
        
        // Auth success
        currentUser = data.user;
        csrfToken = data.csrf_token;
        const loginModal = document.getElementById('login-modal');
        if (loginModal) loginModal.style.display = 'none';
        
        // Load user data
        await loadFolders();
        await loadTags();
        await loadRoutes();
        await loadMapThemes();
        checkGarminStatus();
        checkDataPrompt();
        checkCoachAvailability();
    } catch (err) {
        if (errorMsg) {
            errorMsg.textContent = err.message;
            errorMsg.style.display = 'block';
        }
    } finally {
        if (submitBtn) submitBtn.disabled = false;
    }
}

async function handleLogout() {
    try {
        const res = await fetch('/api/auth/logout', { method: 'POST' });
        if (!res.ok) throw new Error('Logout failed');
        currentUser = null;
        csrfToken = null;
        routesList = [];
        foldersList = [];
        deselectRoute();
        resetCoach();
        resetWelcome();
        const adminSec = document.getElementById('admin-user-section');
        if (adminSec) adminSec.classList.add('hidden');
        await checkAuthStatus();
    } catch (err) {
        console.error("Logout failed:", err);
    }
}

async function handlePrivacyToggle() {
    if (!currentRoute) return;
    const newStatus = !currentRoute.is_public;
    const privacyToggleBtn = document.getElementById('route-privacy-toggle-btn');
    if (privacyToggleBtn) privacyToggleBtn.disabled = true;
    
    try {
        const res = await fetch(`/api/routes/${currentRoute.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_public: newStatus })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to update privacy status');
        
        currentRoute.is_public = newStatus;
        
        const privacyStatus = document.getElementById('route-privacy-status');
        const lockIcon = `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"/></svg>`;
        const globeIcon = `<svg viewBox="0 0 24 24" width="12" height="12" style="vertical-align: middle; margin-right: 4px;" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.53c-.26-.81-1-1.4-1.9-1.4h-1v-3c0-.55-.45-1-1-1h-6v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.4z"/></svg>`;
        
        if (newStatus) {
            if (privacyStatus) privacyStatus.innerHTML = `${globeIcon} Public`;
            if (privacyToggleBtn) privacyToggleBtn.textContent = 'Make Private';
        } else {
            if (privacyStatus) privacyStatus.innerHTML = `${lockIcon} Private`;
            if (privacyToggleBtn) privacyToggleBtn.textContent = 'Make Public';
        }
        
        await loadRoutes();
    } catch (err) {
        alert(err.message);
    } finally {
        if (privacyToggleBtn) privacyToggleBtn.disabled = false;
    }
}

async function loadAdminUserList() {
    const listBody = document.getElementById('admin-user-list');
    if (!listBody) return;
    listBody.innerHTML = '<tr><td colspan="3" style="text-align: center; padding: 10px;">Loading users...</td></tr>';
    
    try {
        const res = await fetch('/api/auth/users');
        if (!res.ok) throw new Error('Failed to fetch user list');
        const users = await res.json();
        
        listBody.innerHTML = users.map(user => {
            const role = user.is_admin ? '<span style="color: var(--primary);">Admin</span>' : 'User';
            const isSelf = currentUser && currentUser.id === user.id;
            const deleteBtn = isSelf ? 
                '<span style="opacity: 0.5; font-size: 0.9em; padding: 4px 8px;">(You)</span>' :
                `<button type="button" class="btn btn-sm btn-outline btn-delete-user" data-user-id="${user.id}" data-username="${escapeHTML(user.username)}" style="border-color: #ef4444; color: #ef4444; padding: 2px 8px;">Delete</button>`;
            
            return `
                <tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <td style="padding: 8px 0; font-weight: 500;">${escapeHTML(user.username)}</td>
                    <td style="padding: 8px 0;">${role}</td>
                    <td style="padding: 8px 0; text-align: right;">${deleteBtn}</td>
                </tr>
            `;
        }).join('');
    } catch (err) {
        listBody.innerHTML = `<tr><td colspan="3" style="text-align: center; color: #ef4444; padding: 10px;">${escapeHTML(err.message)}</td></tr>`;
    }
}

async function handleDeleteUser(userId, username) {
    if (!confirm(`Are you sure you want to delete the user account "${username}"? All their activities and folders will be deleted permanently.`)) return;
    
    try {
        const res = await fetch(`/api/auth/users/${userId}`, {
            method: 'DELETE'
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to delete user');
        
        alert(`User "${username}" has been successfully deleted.`);
        loadAdminUserList();
    } catch (err) {
        alert(err.message);
    }
}



// Intervals.icu API Integration Functions
// ----------------------------------------------------
let intervalsActivities = []; // Store fetched activities

function initIntervalsIntegration() {
    // Check status on startup
    checkIntervalsStatus();

    // Bind event listeners
    const connectBtn = document.getElementById('intervals-connect-btn');
    if (connectBtn) {
        connectBtn.addEventListener('click', connectIntervals);
    }

    const disconnectBtn = document.getElementById('intervals-disconnect-btn');
    if (disconnectBtn) {
        disconnectBtn.addEventListener('click', disconnectIntervals);
    }

    const syncBtn = document.getElementById('sidebar-intervals-sync-btn');
    if (syncBtn) {
        syncBtn.addEventListener('click', syncIntervalsActivities);
    }

    const closeActivitiesBtn = document.getElementById('close-intervals-activities-btn');
    if (closeActivitiesBtn) {
        closeActivitiesBtn.addEventListener('click', () => {
            document.getElementById('intervals-activities-modal').classList.add('hidden');
        });
    }

    const selectAllCheckbox = document.getElementById('intervals-select-all');
    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', (e) => {
            const checkboxes = document.querySelectorAll('.intervals-activity-checkbox');
            checkboxes.forEach(cb => {
                if (!cb.disabled) {
                    cb.checked = e.target.checked;
                }
            });
        });
    }

    const importSelectedBtn = document.getElementById('intervals-import-selected-btn');
    if (importSelectedBtn) {
        importSelectedBtn.addEventListener('click', importSelectedIntervalsActivities);
    }

    const historyStartBtn = document.getElementById('intervals-history-start-btn');
    if (historyStartBtn) historyStartBtn.addEventListener('click', startHistoryImport);
    const historyCancelBtn = document.getElementById('intervals-history-cancel-btn');
    if (historyCancelBtn) historyCancelBtn.addEventListener('click', cancelHistoryImport);
    const promptSaveBtn = document.getElementById('data-prompt-save-btn');
    if (promptSaveBtn) promptSaveBtn.addEventListener('click', saveDataPrompt);
    const promptLaterBtn = document.getElementById('data-prompt-later-btn');
    if (promptLaterBtn) {
        promptLaterBtn.addEventListener('click', () => {
            document.getElementById('data-prompt-modal').classList.add('hidden');
        });
    }
}

async function checkIntervalsStatus() {
    try {
        const res = await fetch('/api/intervals/status');
        const data = await res.json();
        
        const disconnectedSection = document.getElementById('intervals-disconnected-section');
        const connectedSection = document.getElementById('intervals-connected-section');
        const sidebarBtn = document.getElementById('sidebar-intervals-sync-btn');
        
        if (data.status === 'connected') {
            disconnectedSection.classList.add('hidden');
            connectedSection.classList.remove('hidden');
            if (sidebarBtn) sidebarBtn.classList.remove('hidden');
            
            document.getElementById('intervals-connected-athlete-id').textContent = `Athlete ID: ${data.athlete_id}`;
            
            const autoSyncSelect = document.getElementById('settings-intervals-auto-sync');
            if (autoSyncSelect) {
                currentIntervalsAutoSync = data.auto_sync_interval || 'off';
                autoSyncSelect.value = currentIntervalsAutoSync;
            }
        } else {
            disconnectedSection.classList.remove('hidden');
            connectedSection.classList.add('hidden');
            if (sidebarBtn) sidebarBtn.classList.add('hidden');
            
            document.getElementById('intervals-athlete-id').value = '';
            document.getElementById('intervals-api-key').value = '';
            
            const connectBtn = document.getElementById('intervals-connect-btn');
            if (connectBtn) {
                connectBtn.disabled = false;
                connectBtn.textContent = 'Connect Intervals.icu';
            }
        }
    } catch (err) {
        console.error("Error checking Intervals.icu status", err);
    }
}

async function connectIntervals() {
    const athleteId = document.getElementById('intervals-athlete-id').value.trim();
    const apiKey = document.getElementById('intervals-api-key').value.trim();
    const connectBtn = document.getElementById('intervals-connect-btn');

    if (!apiKey) {
        alert('Please enter your Intervals.icu API Key.');
        return;
    }

    if (connectBtn) {
        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting...';
    }

    try {
        const res = await fetch('/api/intervals/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ athlete_id: athleteId, api_key: apiKey })
        });
        const data = await res.json();
        if (res.ok) {
            alert('Intervals.icu connected successfully! Your training history is now imported in the background.');
            checkIntervalsStatus();
            refreshTrainingDataSettings();
            checkDataPrompt();
        } else {
            alert(data.error || 'Failed to connect to Intervals.icu.');
            if (connectBtn) {
                connectBtn.disabled = false;
                connectBtn.textContent = 'Connect Intervals.icu';
            }
        }
    } catch (err) {
        console.error("Error connecting to Intervals.icu", err);
        alert('An error occurred during connection setup.');
        if (connectBtn) {
            connectBtn.disabled = false;
            connectBtn.textContent = 'Connect Intervals.icu';
        }
    }
}

async function disconnectIntervals() {
    if (!confirm('Are you sure you want to disconnect Intervals.icu?')) return;
    try {
        const res = await fetch('/api/intervals/disconnect', { method: 'POST' });
        if (res.ok) {
            alert('Intervals.icu disconnected.');
            checkIntervalsStatus();
        } else {
            alert('Failed to disconnect Intervals.icu.');
        }
    } catch (err) {
        console.error("Error disconnecting Intervals.icu", err);
    }
}

async function syncIntervalsActivities() {
    const modal = document.getElementById('intervals-activities-modal');
    const listContainer = document.getElementById('intervals-activities-list');
    const selectAllCheckbox = document.getElementById('intervals-select-all');
    
    if (selectAllCheckbox) selectAllCheckbox.checked = false;
    
    if (listContainer) {
        listContainer.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 20px;">Fetching activities from Intervals.icu...</td></tr>';
    }
    if (modal) {
        modal.classList.remove('hidden');
    }

    try {
        const res = await fetch('/api/intervals/activities');
        const data = await res.json();
        
        if (data.status === 'success') {
            intervalsActivities = data.activities;
            renderIntervalsActivities(data.activities);
        } else if (data.status === 'needs_reauthentication') {
            alert(data.error || 'Authentication expired. Please reconnect.');
            if (modal) modal.classList.add('hidden');
            openSettingsModal();
            const integrationsTab = document.querySelector('#settings-modal .tab-btn[data-tab="settings-tab-integrations"]');
            if (integrationsTab) integrationsTab.click();
        } else {
            if (listContainer) {
                listContainer.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #ef4444; padding: 20px;">${data.error || 'Failed to fetch activities'}</td></tr>`;
            }
        }
    } catch (err) {
        console.error("Error fetching intervals activities", err);
        if (listContainer) {
            listContainer.innerHTML = '<tr><td colspan="7" style="text-align: center; color: #ef4444; padding: 20px;">Failed to fetch activities. An unexpected error occurred.</td></tr>';
        }
    }
}

function renderIntervalsActivities(activities) {
    const listContainer = document.getElementById('intervals-activities-list');
    if (!listContainer) return;
    
    if (activities.length === 0) {
        listContainer.innerHTML = '<tr><td colspan="7" style="text-align: center; padding: 20px;">No recent activities found on Intervals.icu.</td></tr>';
        return;
    }

    fetch('/api/routes')
        .then(res => res.json())
        .then(routes => {
            const importedIds = new Set();
            routes.forEach(r => {
                const fn = r.filename || '';
                if (fn.startsWith('intervals_') && fn.endsWith('.gpx')) {
                    try {
                        const actId = fn.split('_')[1];
                        importedIds.add(actId);
                    } catch(e) {}
                }
            });

            listContainer.innerHTML = '';
            activities.forEach(act => {
                const isImported = importedIds.has(String(act.activityId));
                const tr = document.createElement('tr');
                if (isImported) {
                    tr.style.opacity = '0.5';
                }
                
                const dateStr = act.startTimeLocal ? act.startTimeLocal.replace('T', ' ').substring(0, 16) : 'Unknown';
                const distKm = act.distance ? (act.distance / 1000).toFixed(2) + ' km' : '0.00 km';
                const durStr = formatDuration(act.duration);
                
                tr.innerHTML = `
                    <td style="text-align: center;">
                        <input type="checkbox" class="intervals-activity-checkbox" data-id="${act.activityId}" data-name="${escapeHTML(act.activityName)}" data-start-time="${act.startTimeUTC}" ${isImported ? 'disabled' : ''}>
                    </td>
                    <td>${dateStr}</td>
                    <td style="font-weight: 500;">${escapeHTML(act.activityName)}</td>
                    <td><span class="activity-type-badge">${escapeHTML(act.activityType)}</span></td>
                    <td>${distKm}</td>
                    <td>${durStr}</td>
                    <td style="text-align: right;">
                        ${isImported ? 
                            '<span style="color: var(--secondary); font-size: 0.85em; font-weight: bold;">Imported</span>' : 
                            `<button class="btn btn-sm btn-primary btn-import-single-intervals" data-id="${act.activityId}" data-name="${escapeHTML(act.activityName)}" data-start-time="${act.startTimeUTC}">Import</button>`
                        }
                    </td>
                `;
                listContainer.appendChild(tr);
            });
        })
        .catch(err => {
            console.error("Error checking imported routes", err);
        });
}

async function importSingleIntervalsActivity(activityId, activityName, startTime, btn) {
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Importing...';
    }

    try {
        const res = await fetch('/api/intervals/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activityId, activityName, startTimeLocal: startTime })
        });
        const data = await res.json();
        
        if (res.ok) {
            alert(`Activity "${data.name}" imported successfully!`);
            syncIntervalsActivities();
            loadRoutes();
        } else {
            alert(data.error || 'Failed to import activity.');
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Import';
            }
        }
    } catch (err) {
        console.error("Error importing activity", err);
        alert('An error occurred during activity import.');
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Import';
        }
    }
}

async function importSelectedIntervalsActivities() {
    const checkboxes = document.querySelectorAll('.intervals-activity-checkbox:checked:not(:disabled)');
    if (checkboxes.length === 0) {
        alert('Please select at least one activity to import.');
        return;
    }

    const importSelectedBtn = document.getElementById('intervals-import-selected-btn');
    if (importSelectedBtn) {
        importSelectedBtn.disabled = true;
        importSelectedBtn.textContent = `Importing (${checkboxes.length})...`;
    }

    let successCount = 0;
    let failCount = 0;

    for (const cb of checkboxes) {
        const activityId = cb.getAttribute('data-id');
        const activityName = cb.getAttribute('data-name');
        const startTime = cb.getAttribute('data-start-time');
        
        try {
            const res = await fetch('/api/intervals/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ activityId, activityName, startTimeLocal: startTime })
            });
            if (res.ok) {
                successCount++;
            } else {
                failCount++;
            }
        } catch (err) {
            console.error(`Failed to import activity ${activityId}`, err);
            failCount++;
        }
    }

    alert(`Import complete! Successfully imported: ${successCount} activities. Failed: ${failCount}.`);
    
    if (importSelectedBtn) {
        importSelectedBtn.disabled = false;
        importSelectedBtn.textContent = 'Import Selected';
    }

    syncIntervalsActivities();
    loadRoutes();
}

function initSettingsTabs() {
    const tabBtns = document.querySelectorAll('#settings-modal .tab-btn');
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            tabBtns.forEach(b => {
                b.classList.remove('active');
                b.style.borderBottom = '2px solid transparent';
                b.style.opacity = '0.6';
            });
            
            btn.classList.add('active');
            btn.style.borderBottom = '2px solid var(--primary)';
            btn.style.opacity = '0.95';
            
            const panes = document.querySelectorAll('#settings-modal .settings-tab-pane');
            panes.forEach(pane => pane.classList.add('hidden'));
            
            const targetTab = btn.getAttribute('data-tab');
            document.getElementById(targetTab).classList.remove('hidden');
        });
    });
}






// ----------------------------------------------------
// Training Data Preferences (history import & wellness opt-in)
// ----------------------------------------------------
let dataPreferences = null;
let historyPollTimer = null;
const HISTORY_POLL_MS = 15000;

async function fetchDataPreferences() {
    const res = await fetch('/api/auth/data-preferences');
    if (!res.ok) throw new Error('Failed to load training data preferences.');
    dataPreferences = await res.json();
    return dataPreferences;
}

async function putDataPreferences(payload) {
    const res = await fetch('/api/auth/data-preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to save your choices.');
    dataPreferences = data;
    return data;
}

// One-time prompt: history import for existing connections, and the wellness opt-in
async function checkDataPrompt() {
    try {
        const prefs = await fetchDataPreferences();
        if (prefs.prompt && prefs.prompt.show) showDataPrompt(prefs);
    } catch (err) {
        console.error('Error checking training data preferences', err);
    }
}

function showDataPrompt(prefs) {
    const modal = document.getElementById('data-prompt-modal');
    if (!modal) return;
    document.getElementById('data-prompt-history-section').classList.toggle('hidden', !prefs.prompt.offer_history);
    document.getElementById('data-prompt-wellness-section').classList.toggle('hidden', !prefs.prompt.ask_wellness);
    document.getElementById('data-prompt-history').checked = true;
    document.getElementById('data-prompt-history-months').value = String(prefs.history_default_months || 12);
    document.getElementById('data-prompt-wellness').checked = false;  // opt-in: never pre-checked
    document.getElementById('data-prompt-error').classList.add('hidden');
    modal.classList.remove('hidden');
}

async function saveDataPrompt() {
    const prompt = (dataPreferences && dataPreferences.prompt) || {};
    const saveBtn = document.getElementById('data-prompt-save-btn');
    const errorEl = document.getElementById('data-prompt-error');
    const payload = { prompt_answered: true };
    if (prompt.offer_history) {
        payload.import_history = document.getElementById('data-prompt-history').checked;
        payload.history_months = parseInt(document.getElementById('data-prompt-history-months').value, 10);
    }
    if (prompt.ask_wellness) {
        payload.wellness_opt_in = document.getElementById('data-prompt-wellness').checked;
    }

    if (saveBtn) saveBtn.disabled = true;
    errorEl.classList.add('hidden');
    try {
        const prefs = await putDataPreferences(payload);
        if (currentUser) currentUser.wellness_opt_in = prefs.wellness_opt_in;
        document.getElementById('data-prompt-modal').classList.add('hidden');
        renderTrainingDataSettings(prefs);
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.classList.remove('hidden');
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

// Settings > Integrations: wellness toggle and the history import panel
async function refreshTrainingDataSettings() {
    try {
        renderTrainingDataSettings(await fetchDataPreferences());
    } catch (err) {
        console.error('Error loading training data settings', err);
        const statusEl = document.getElementById('intervals-history-status');
        if (statusEl) statusEl.textContent = 'Could not load the history import status.';
    }
}

function renderTrainingDataSettings(prefs) {
    const wellnessCheckbox = document.getElementById('settings-wellness-opt-in');
    if (wellnessCheckbox) wellnessCheckbox.checked = prefs.wellness_opt_in === true;
    renderHistoryJob(prefs.history);
}

function formatHistoryDate(isoDate) {
    if (!isoDate) return '';
    const d = new Date(`${isoDate}T00:00:00`);
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

function renderHistoryJob(job) {
    const statusEl = document.getElementById('intervals-history-status');
    const progressEl = document.getElementById('intervals-history-progress');
    const fillEl = document.getElementById('intervals-history-progress-fill');
    const startBtn = document.getElementById('intervals-history-start-btn');
    const cancelBtn = document.getElementById('intervals-history-cancel-btn');
    const monthsSelect = document.getElementById('intervals-history-months');
    if (!statusEl) return;

    const active = job && (job.status === 'pending' || job.status === 'running');
    const counts = job ? `${job.activities_new} new, ${job.activities_enriched} updated, ${job.wellness_days} days of wellness data.` : '';
    let text;
    if (!job) {
        text = 'Import your past activities and wellness data. It runs slowly in the background (about a minute per month) to stay within Intervals.icu\'s limits.';
    } else if (active) {
        text = job.windows_done === 0
            ? 'Importing history: starting shortly...'
            : `Importing history: ${job.percent}% done, back to ${formatHistoryDate(job.processed_until)}. ${counts}`;
        if (job.last_error) text += ` Retrying after a temporary problem: ${job.last_error}`;
    } else if (job.status === 'completed') {
        text = `History imported back to ${formatHistoryDate(job.range_start)}. ${counts}`;
    } else if (job.status === 'paused') {
        text = job.last_error
            ? `History import paused: ${job.last_error}`
            : 'History import paused after repeated errors. Resume it when the problem is solved.';
    } else if (job.status === 'failed') {
        text = `History import stopped because of an error: ${job.last_error || 'unknown error.'}`;
    } else {
        text = `History import stopped at ${job.percent}%. ${counts}`;
    }
    statusEl.textContent = text;

    progressEl.classList.toggle('hidden', !active);
    if (active) {
        fillEl.style.width = `${job.percent}%`;
        progressEl.setAttribute('aria-valuenow', String(job.percent));
    }
    cancelBtn.classList.toggle('hidden', !active);
    startBtn.classList.toggle('hidden', active);
    monthsSelect.classList.toggle('hidden', active || (job && job.status === 'paused'));
    startBtn.textContent = !job ? 'Import history'
        : job.status === 'paused' ? 'Resume import'
        : job.status === 'failed' ? 'Try again'
        : 'Import again';

    scheduleHistoryPoll(active);
}

function scheduleHistoryPoll(active) {
    clearTimeout(historyPollTimer);
    const settingsModal = document.getElementById('settings-modal');
    if (active && settingsModal && !settingsModal.classList.contains('hidden')) {
        historyPollTimer = setTimeout(refreshTrainingDataSettings, HISTORY_POLL_MS);
    }
}

async function startHistoryImport() {
    const startBtn = document.getElementById('intervals-history-start-btn');
    const months = parseInt(document.getElementById('intervals-history-months').value, 10);
    if (startBtn) startBtn.disabled = true;
    try {
        const res = await fetch('/api/intervals/backfill', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ months })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to start the history import.');
        renderHistoryJob(data.job);
    } catch (err) {
        alert(err.message);
    } finally {
        if (startBtn) startBtn.disabled = false;
    }
}

async function cancelHistoryImport() {
    if (!confirm('Stop the history import? Activities imported so far are kept, and you can start it again later.')) return;
    try {
        const res = await fetch('/api/intervals/backfill/cancel', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to stop the history import.');
        renderHistoryJob(data.job);
    } catch (err) {
        alert(err.message);
    }
}

async function saveWellnessSetting() {
    const checkbox = document.getElementById('settings-wellness-opt-in');
    if (!checkbox || !dataPreferences) return;
    const current = dataPreferences.wellness_opt_in === true;
    // Leaving an unanswered choice unchecked keeps it unanswered, so the prompt can still ask
    if (checkbox.checked === current || (dataPreferences.wellness_opt_in === null && !checkbox.checked)) return;
    try {
        const prefs = await putDataPreferences({ wellness_opt_in: checkbox.checked });
        if (currentUser) currentUser.wellness_opt_in = prefs.wellness_opt_in;
    } catch (err) {
        console.error('Error updating wellness setting', err);
        alert(err.message);
    }
}

// ----------------------------------------------------
// Welcome screen
// ----------------------------------------------------
let welcomeData = null;
let welcomeSeq = 0;
let welcomeNotePoll = null;
let welcomeResizeTimer = null;

const WELCOME_NOTE_POLL_MS = 10000;
const HR_ZONE_COLORS = ['#64748b', '#38bdf8', '#34d399', '#fbbf24', '#f97316', '#f43f5e', '#be123c'];

function initWelcome() {
    const tabs = [document.getElementById('dashboard-tab-welcome'), document.getElementById('dashboard-tab-activities')];
    tabs[0].addEventListener('click', () => showDashboardTab('welcome'));
    tabs[1].addEventListener('click', () => showDashboardTab('activities'));
    tabs.forEach((tab, i) => tab.addEventListener('keydown', e => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        e.preventDefault();
        const next = tabs[(i + 1) % tabs.length];
        next.click();
        next.focus();
    }));
    document.getElementById('welcome-all-activities-btn').addEventListener('click', () => showDashboardTab('activities'));
    document.getElementById('welcome-play-btn').addEventListener('click', playWelcomeRoute);
    document.getElementById('welcome-open-btn').addEventListener('click', () => {
        if (welcomeData && welcomeData.latest) openWelcomeActivity(welcomeData.latest);
    });
    document.getElementById('welcome-coach-followup-btn').addEventListener('click', discussCoachNote);
    document.getElementById('welcome-coach-refresh-btn').addEventListener('click', () => loadCoachNote(true));
    window.addEventListener('resize', () => {
        clearTimeout(welcomeResizeTimer);
        welcomeResizeTimer = setTimeout(drawWelcomeMap, 150);
    });
}

function showDashboardTab(name) {
    const welcome = name === 'welcome';
    document.getElementById('welcome-view').classList.toggle('hidden', !welcome);
    document.getElementById('activities-overview').classList.toggle('hidden', welcome);
    [['dashboard-tab-welcome', welcome], ['dashboard-tab-activities', !welcome]].forEach(([id, active]) => {
        const tab = document.getElementById(id);
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
    });
    if (welcome) {
        drawWelcomeMap();
    } else {
        // Canvases only get their size once the grid is visible
        renderDashboardGrid();
    }
}

// The browser's own date: the server uses it as "today" for the athlete
function localDateISO() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function loadWelcome() {
    const seq = ++welcomeSeq;
    renderWelcomeGreeting();
    try {
        const res = await fetch(`/api/welcome?date=${localDateISO()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (seq !== welcomeSeq) return;
        welcomeData = data;
        renderWelcome(data);
    } catch (err) {
        console.error('Could not load the welcome screen', err);
    }
    loadCoachNote();
    // Queues every activity that has no rendered map yet, then tracks the queue
    refreshMapQueue({ fill: true });
}

function resetWelcome() {
    welcomeSeq++;
    welcomeData = null;
    clearTimeout(welcomeNotePoll);
    document.getElementById('welcome-coach-note').classList.add('hidden');
    showDashboardTab('welcome');
}

function renderWelcomeGreeting() {
    const now = new Date();
    const hour = now.getHours();
    const part = hour < 5 ? 'evening' : hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
    const name = currentUser && currentUser.username ? `, ${currentUser.username}` : '';
    document.getElementById('welcome-greeting').textContent = `Good ${part}${name}`;
    document.getElementById('welcome-date').textContent = now.toLocaleDateString(undefined, {
        weekday: 'short', day: 'numeric', month: 'short', year: 'numeric'
    });
}

function renderWelcome(data) {
    const hasLatest = !!data.latest;
    document.getElementById('welcome-empty').classList.toggle('hidden', hasLatest);
    document.getElementById('welcome-spotlight').classList.toggle('hidden', !hasLatest);
    if (hasLatest) renderWelcomeSpotlight(data.latest, data.comparison);
    renderWelcomeRail(data);
    renderWelcomeRecent(data.recent || []);
    renderWelcomeEvents(data.events || []);
}

function shortDuration(seconds) {
    if (!seconds) return '—';
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

function hoursMinutes(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}`;
}

function paceValue(mps) {
    const perKm = Math.round(1000 / mps);
    return `${Math.floor(perKm / 60)}:${String(perKm % 60).padStart(2, '0')}`;
}

// The headline numbers for the spotlight, as [label, value, unit]
function welcomeSpotStats(a) {
    const stats = [];
    const time = a.moving_time || a.duration;
    if (hasValue(a.distance) && a.distance > 0) stats.push(['Distance', (a.distance / 1000).toFixed(1), 'km']);
    if (time) stats.push(['Time', shortDuration(time), '']);
    if (a.distance > 0 && time > 0) {
        const mps = a.distance / time;
        if (getSportCategory(a.sport_type) === 'ride') stats.push(['Speed', (mps * 3.6).toFixed(1), 'km/h']);
        else if (mps > 0.1) stats.push(['Pace', paceValue(mps), '/km']);
    }
    if (hasValue(a.avg_hr)) stats.push(['Avg HR', Math.round(a.avg_hr), 'bpm']);
    if (hasValue(a.avg_power)) stats.push(['Avg power', Math.round(a.avg_power), 'W']);
    if (hasValue(a.training_load)) stats.push(['Load', Math.round(a.training_load), '', true]);
    if (hasValue(a.aerobic_te)) stats.push(['Aerobic TE', Number(a.aerobic_te).toFixed(1), '']);
    if (hasValue(a.elevation_gain) && a.elevation_gain > 0 && !a.is_indoor) stats.push(['Elevation', Math.round(a.elevation_gain), 'm']);
    if (hasValue(a.calories)) stats.push(['Calories', Math.round(a.calories), 'kcal']);
    return stats.slice(0, 6);
}

function renderWelcomeSpotlight(a, comparison) {
    const route = a.route_id ? routesList.find(r => r.id === a.route_id) : null;
    const hasMap = !!(route && Array.isArray(route.simplified_path) && route.simplified_path.length > 1);

    document.getElementById('welcome-spot-meta').textContent = `${formatActivityDate(a)} · ${formatSportLabel(a.sport_type)}`;
    document.getElementById('welcome-spot-title').textContent = a.name || formatSportLabel(a.sport_type);
    document.getElementById('welcome-spot-stats').innerHTML = welcomeSpotStats(a).map(([label, value, unit, glow]) => `
        <div>
            <div class="welcome-label">${escapeHTML(label)}</div>
            <div class="welcome-big${glow ? ' glow' : ''}">${escapeHTML(String(value))}${unit ? `<small>${escapeHTML(unit)}</small>` : ''}</div>
        </div>`).join('');

    const zones = document.getElementById('welcome-zones');
    const zoneTimes = a.hr_zone_times || [];
    const zoneTotal = zoneTimes.reduce((sum, t) => sum + t, 0);
    zones.classList.toggle('hidden', !zoneTotal);
    if (zoneTotal) {
        const color = i => HR_ZONE_COLORS[Math.min(i, HR_ZONE_COLORS.length - 1)];
        const label = zoneTimes.map((t, i) => t ? `zone ${i + 1} ${Math.round(100 * t / zoneTotal)}%` : '').filter(Boolean).join(', ');
        zones.innerHTML = `
            <div class="welcome-label">Time in HR zones</div>
            <div class="welcome-zone-bar" role="img" aria-label="${escapeHTML(label)}">
                ${zoneTimes.map((t, i) => t ? `<i style="width:${(100 * t / zoneTotal).toFixed(1)}%;background:${color(i)}"></i>` : '').join('')}
            </div>
            <div class="welcome-zone-legend">
                ${zoneTimes.map((t, i) => t ? `<span style="--zone-color:${color(i)}">Z${i + 1} ${shortDuration(t)}</span>` : '').join('')}
            </div>`;
    }

    const compare = document.getElementById('welcome-compare');
    const sentence = comparisonSentence(comparison);
    compare.classList.toggle('hidden', !sentence);
    compare.innerHTML = sentence;

    document.getElementById('welcome-map-canvas').classList.toggle('hidden', !hasMap);
    document.getElementById('welcome-play-btn').classList.toggle('hidden', !hasMap);
    const mapless = document.getElementById('welcome-mapless');
    mapless.classList.toggle('hidden', hasMap);
    if (!hasMap) {
        mapless.innerHTML = `${sportIconSvg(a.sport_type, 48)}<span>${a.is_indoor ? 'Indoor workout' : 'No GPS recorded'}</span>`;
    }
    const privacy = getPrivacyDistance();
    const chip = document.getElementById('welcome-privacy-chip');
    chip.classList.toggle('hidden', !hasMap || !privacy);
    chip.textContent = `PRIVACY ZONE ${privacy} m`;
    drawWelcomeMap();
}

function comparisonSentence(c) {
    if (!c) return '';
    const others = `your last ${c.count} similar workouts`;
    let main;
    if (c.metric === 'pace') {
        const s = Math.round(Math.abs(c.diff));
        main = s < 2 ? `<b>The same pace</b> as ${others}` : `<b>${s} s/km ${c.diff < 0 ? 'faster' : 'slower'}</b> than ${others}`;
    } else {
        const kmh = Math.abs(c.diff).toFixed(1);
        main = Math.abs(c.diff) < 0.3 ? `<b>The same speed</b> as ${others}` : `<b>${kmh} km/h ${c.diff > 0 ? 'faster' : 'slower'}</b> than ${others}`;
    }
    if (c.hr_diff === null || c.hr_diff === undefined) return `${main}.`;
    if (Math.abs(c.hr_diff) <= 1) return `${main}, at the same average heart rate.`;
    return `${main}, at a ${Math.abs(c.hr_diff)} bpm ${c.hr_diff < 0 ? 'lower' : 'higher'} average heart rate.`;
}

function drawWelcomeMap() {
    const latest = welcomeData && welcomeData.latest;
    const canvas = document.getElementById('welcome-map-canvas');
    if (!latest || !latest.route_id || canvas.classList.contains('hidden') || !canvas.offsetWidth) return;
    const route = routesList.find(r => r.id === latest.route_id);
    if (route && route.simplified_path) drawWelcomeRoute(canvas, route);
}

// Marker spacing on the welcome map: every 5 km, wider on long rides so there are at most six
const WELCOME_MARKER_STEPS_KM = [5, 10, 25, 50, 100];

// The latest route, drawn at the screen's full resolution: a cyan-to-purple gradient over a
// soft glow, start and finish dots in the gradient's colours, and distance markers
function drawWelcomeRoute(canvas, route) {
    const coords = route.simplified_path;
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const keep = getPrivacyKeepRange(coords);
    if (!keep) {
        ctx.fillStyle = '#94a3b8';
        ctx.font = '12px Outfit, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Route within privacy zone', width / 2, height / 2);
        return;
    }

    // Distance along the simplified path, scaled to the recorded distance so markers land
    // where the kilometres really are
    const along = [0];
    for (let i = 1; i < coords.length; i++) {
        along.push(along[i - 1] + getDistanceMeters(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]));
    }
    const pathLength = along[along.length - 1] || 1;
    const scale = route.total_distance > 0 ? route.total_distance / pathLength : 1;

    // Equirectangular projection, fitted to the visible part
    const visible = coords.slice(keep.first, keep.last + 1);
    const lats = visible.map(p => p[0]);
    const lons = visible.map(p => p[1]);
    const minLat = Math.min(...lats), maxLat = Math.max(...lats);
    const minLon = Math.min(...lons), maxLon = Math.max(...lons);
    const latCos = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
    const pad = 44;
    const fit = Math.min((width - pad * 2) / (((maxLon - minLon) * latCos) || 1e-4), (height - pad * 2) / ((maxLat - minLat) || 1e-4));
    const offsetX = (width - (maxLon - minLon) * latCos * fit) / 2;
    const offsetY = (height - (maxLat - minLat) * fit) / 2;
    const project = p => [offsetX + (p[1] - minLon) * latCos * fit, height - (offsetY + (p[0] - minLat) * fit)];
    const points = visible.map(project);

    const xs = points.map(p => p[0]);
    const gradient = ctx.createLinearGradient(Math.min(...xs), 0, Math.max(...xs), 0);
    gradient.addColorStop(0, '#00f0ff');
    gradient.addColorStop(1, '#9333ea');
    const trace = () => {
        ctx.beginPath();
        points.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    };
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = gradient;

    ctx.save();
    ctx.filter = 'blur(4px)';
    ctx.globalAlpha = 0.45;
    ctx.lineWidth = 7;
    trace();
    ctx.stroke();
    ctx.restore();

    ctx.lineWidth = 2.5;
    trace();
    ctx.stroke();

    const dot = (p, fill, r) => {
        ctx.beginPath();
        ctx.arc(p[0], p[1], r, 0, 2 * Math.PI);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#070a13';
        ctx.stroke();
    };

    // Distance markers, only where the route is visible
    const totalKm = pathLength * scale / 1000;
    const step = WELCOME_MARKER_STEPS_KM.find(s => totalKm / s <= 6) || WELCOME_MARKER_STEPS_KM[WELCOME_MARKER_STEPS_KM.length - 1];
    ctx.font = '500 10px "JetBrains Mono", monospace';
    ctx.textBaseline = 'alphabetic';
    // A marker right at the finish would sit on the finish dot
    for (let km = step; km < totalKm - step * 0.3; km += step) {
        const target = km * 1000 / scale;
        const i = along.findIndex(d => d >= target);
        if (i < 1 || i - 1 < keep.first || i > keep.last) continue;
        const t = (target - along[i - 1]) / ((along[i] - along[i - 1]) || 1);
        const p = project([
            coords[i - 1][0] + (coords[i][0] - coords[i - 1][0]) * t,
            coords[i - 1][1] + (coords[i][1] - coords[i - 1][1]) * t
        ]);
        ctx.beginPath();
        ctx.arc(p[0], p[1], 3.5, 0, 2 * Math.PI);
        ctx.fillStyle = '#070a13';
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = '#cbd5e1';
        ctx.stroke();
        const label = `${km} km`;
        const labelWidth = ctx.measureText(label).width;
        // Keep the label inside the map
        const lx = p[0] + 8 + labelWidth > width - 6 ? p[0] - 8 - labelWidth : p[0] + 8;
        ctx.fillStyle = 'rgba(7, 10, 19, 0.75)';
        ctx.fillRect(lx - 3, p[1] - 17, labelWidth + 6, 14);
        ctx.fillStyle = '#cbd5e1';
        ctx.fillText(label, lx, p[1] - 6);
    }

    dot(points[0], '#00f0ff', 5);
    dot(points[points.length - 1], '#9333ea', 5);
}

async function openWelcomeActivity(activity) {
    if (activity.route_id) {
        await selectRoute(activity.route_id);
    } else if (activitiesList.some(a => a.id === activity.id)) {
        selectActivity(activity.id);
    }
}

async function playWelcomeRoute() {
    const latest = welcomeData && welcomeData.latest;
    if (!latest || !latest.route_id) return;
    await selectRoute(latest.route_id);
    if (currentRoute && currentRoute.id === latest.route_id && !isPlaying) toggleAnimation();
}

// A small trend line; the viewBox is stretched to the tile's width
function sparklineSvg(series) {
    const values = (series || []).map(p => p.value).filter(v => v !== null && v !== undefined);
    if (values.length < 2) return '';
    const W = 200, H = 30, pad = 3;
    const min = Math.min(...values), max = Math.max(...values);
    const x = i => (i / (values.length - 1)) * W;
    const y = v => pad + (1 - (v - min) / ((max - min) || 1)) * (H - pad * 2);
    const line = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        <path d="${line} L${W} ${H} L0 ${H} Z" fill="rgba(0,240,255,0.08)"/>
        <path d="${line}" fill="none" stroke="#00f0ff" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
    </svg>`;
}

// Form zones as Intervals.icu names them
function formZone(form) {
    if (form > 25) return ['Transition', 'warn'];
    if (form > 5) return ['Fresh', 'info'];
    if (form >= -10) return ['Grey zone', 'info'];
    if (form >= -30) return ['Optimal', 'good'];
    return ['High risk', 'bad'];
}

function signed(n, digits = 0) {
    const rounded = Number(n.toFixed(digits));
    return `${rounded > 0 ? '+' : rounded < 0 ? '−' : ''}${Math.abs(rounded).toFixed(digits)}`;
}

function renderWelcomeRail(data) {
    const tiles = [];
    const f = data.fitness;
    if (f) {
        const [zone, cls] = formZone(f.form);
        tiles.push(`<span class="welcome-label">Form</span>
            <span class="welcome-big glow">${signed(f.form)}</span>
            <span class="welcome-pill ${cls}">${zone}</span>`);
        const ramp = hasValue(f.ramp_rate) ? `Ramp ${signed(f.ramp_rate, 1)} per week` : `Fatigue ${Math.round(f.atl)}`;
        tiles.push(`<span class="welcome-label">Fitness</span>
            <span class="welcome-big">${Math.round(f.ctl)}</span>
            ${sparklineSvg(f.ctl_series)}
            <span class="welcome-sub">${ramp}</span>`);
    }
    const r = data.recovery;
    [['hrv', 'HRV', 'ms'], ['resting_hr', 'Resting HR', 'bpm']].forEach(([key, label, unit]) => {
        const m = r && r[key];
        if (!m) return;
        const diff = Math.round(m.value - m.avg7);
        const vs = diff ? `${diff > 0 ? '▲' : '▼'} ${Math.abs(diff)} vs 7-day avg` : 'Same as 7-day avg';
        tiles.push(`<span class="welcome-label">${label}</span>
            <span class="welcome-big">${Math.round(m.value)}<small>${unit}</small></span>
            ${sparklineSvg(m.series)}
            <span class="welcome-sub">${vs}</span>`);
    });
    const week = data.last_seven_days;
    if (week && week.sessions) {
        tiles.push(`<span class="welcome-label">Last 7 days</span>
            <span class="welcome-big">${Math.round(week.load) || week.sessions}<small>${week.load ? 'load' : 'sessions'}</small></span>
            <span class="welcome-sub">${week.sessions} ${week.sessions === 1 ? 'session' : 'sessions'} · ${hoursMinutes(week.duration)} h</span>`);
    }
    document.getElementById('welcome-rail').innerHTML = tiles.map(t => `<div class="welcome-panel">${t}</div>`).join('');
}

function welcomeActivityMeta(a) {
    const parts = [formatActivityDate(a)];
    if (a.distance > 0) parts.push(`${(a.distance / 1000).toFixed(1)} km`);
    const time = a.moving_time || a.duration;
    if (time) parts.push(shortDuration(time));
    if (hasValue(a.avg_hr)) parts.push(`${Math.round(a.avg_hr)} bpm`);
    return parts.join(' · ');
}

function renderWelcomeRecent(activities) {
    const box = document.getElementById('welcome-recent');
    if (!activities.length) {
        box.innerHTML = '<p class="welcome-none">No earlier workouts yet.</p>';
        return;
    }
    box.innerHTML = activities.map((a, i) => `
        <button type="button" class="welcome-row" data-index="${i}">
            <span class="welcome-sport ${getSportCategory(a.sport_type)}">${sportIconSvg(a.sport_type, 18)}</span>
            <span style="min-width:0">
                <span class="welcome-row-name" style="display:block">${escapeHTML(a.name || formatSportLabel(a.sport_type))}</span>
                <span class="welcome-row-meta" style="display:block">${escapeHTML(welcomeActivityMeta(a))}</span>
            </span>
            <span class="welcome-row-load">${hasValue(a.training_load) ? `${Math.round(a.training_load)}<small>LOAD</small>` : ''}</span>
        </button>`).join('');
    box.querySelectorAll('.welcome-row').forEach(row => {
        row.addEventListener('click', () => openWelcomeActivity(activities[Number(row.dataset.index)]));
    });
}

function formatEventTime(stamp) {
    const ts = parseUtcTimestamp(stamp);
    if (ts === null) return '';
    const minutes = Math.round((Date.now() - ts) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 24 * 60) return `${Math.round(minutes / 60)} h ago`;
    const date = new Date(ts);
    if (minutes < 6 * 24 * 60) return date.toLocaleDateString(undefined, { weekday: 'short' });
    return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

function renderWelcomeEvents(events) {
    const box = document.getElementById('welcome-events');
    if (!events.length) {
        box.innerHTML = '<p class="welcome-none">Syncs, library indexing and coach conversations will show up here.</p>';
        return;
    }
    box.innerHTML = events.map(e => `
        <div class="welcome-event">
            <span class="welcome-event-dot ${escapeHTML(e.kind)}"></span>
            <div style="min-width:0">
                <div class="welcome-event-title">${escapeHTML(e.title)}</div>
                <div class="welcome-event-detail">${escapeHTML(e.detail)}</div>
            </div>
            <span class="welcome-event-time">${escapeHTML(formatEventTime(e.time))}</span>
        </div>`).join('');
}

// --- The coach's note ---

async function loadCoachNote(refresh = false) {
    clearTimeout(welcomeNotePoll);
    if (!currentUser) return;
    const seq = welcomeSeq;
    try {
        const res = refresh
            ? await fetch('/api/coach/daily-note/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ date: localDateISO() })
            })
            : await fetch(`/api/coach/daily-note?date=${localDateISO()}`);
        const note = await res.json();
        if (seq !== welcomeSeq) return;
        if (!res.ok) throw new Error(note.error || `HTTP ${res.status}`);
        renderCoachNote(note);
        if (note.status === 'generating') {
            welcomeNotePoll = setTimeout(() => loadCoachNote(), WELCOME_NOTE_POLL_MS);
        }
    } catch (err) {
        console.error('Could not load the coach note', err);
    }
}

function renderCoachNote(note) {
    const panel = document.getElementById('welcome-coach-note');
    const text = document.getElementById('welcome-coach-text');
    const followup = document.getElementById('welcome-coach-followup-btn');
    const refresh = document.getElementById('welcome-coach-refresh-btn');
    panel.classList.toggle('hidden', note.status === 'unavailable');
    text.classList.toggle('pending', note.status === 'generating');
    text.classList.toggle('failed', note.status === 'failed');
    followup.classList.toggle('hidden', note.status !== 'ready');
    refresh.classList.toggle('hidden', note.status === 'generating' || note.status === 'unavailable');
    refresh.textContent = note.status === 'failed' ? 'Try again' : 'Write a new note';
    if (note.status === 'generating') {
        text.textContent = 'Your coach is going through your latest training to write today\'s note. This can take a few minutes.';
    } else if (note.status === 'failed') {
        text.textContent = `The coach couldn't write today's note. ${note.error || ''}`.trim();
    } else if (note.status === 'ready') {
        text.innerHTML = renderCoachMarkdown(note.content);
    }
}

async function discussCoachNote() {
    try {
        const res = await fetch('/api/coach/daily-note/discuss', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date: localDateISO() })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not open the conversation.');
        await openCoachPanel();
        await openCoachConversation(data.id);
    } catch (err) {
        alert(err.message);
    }
}

// ----------------------------------------------------
// AI Coach
// ----------------------------------------------------
let coachAvailable = false;
let coachConversationId = null;
let coachAbortController = null;
let coachProfileLoaded = null;

const COACH_PROFILE_INPUTS = {
    goals: 'coach-profile-goals',
    target_events: 'coach-profile-target-events',
    availability: 'coach-profile-availability',
    injuries: 'coach-profile-injuries',
    notes: 'coach-profile-notes'
};

const COACH_SUGGESTIONS = [
    'How did my training go this week?',
    'Plan my training for next week.',
    'Am I recovering well, or should I take it easy?'
];

// Renders the coach's Markdown answer. All text is HTML-escaped first and only fixed tags
// without attributes are added, so model output can never inject markup or scripts.
function sourceName(source) {
    return [source.title, source.section, source.page ? `p. ${source.page}` : null].filter(Boolean).join(' · ');
}

// Turns [n] markers for known sources into superscript citations. Runs on rendered (escaped)
// HTML; the only inserted text is the escaped source name.
function linkCoachCitations(html, sources) {
    if (!sources || !sources.length) return html;
    const byNumber = new Map(sources.map(s => [s.n, s]));
    return html.replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (match, numbers) => {
        const list = numbers.split(',').map(n => parseInt(n, 10));
        if (!list.every(n => byNumber.has(n))) return match;
        const title = list.map(n => `[${n}] ${sourceName(byNumber.get(n))}`).join('\n');
        return `<sup class="coach-cite" title="${escapeHTML(title)}">[${list.join(', ')}]</sup>`;
    });
}

function renderCoachMarkdown(markdown) {
    const lines = escapeHTML(markdown || '').split('\n');
    const inline = text => text
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/__([^_]+)__/g, '<strong>$1</strong>')
        .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>');
    const isTableRow = line => /^\s*\|.*\|\s*$/.test(line);
    const isTableSeparator = line => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
    const isBlockStart = line => /^```/.test(line) || /^#{1,6}\s/.test(line)
        || /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line) || isTableRow(line);
    const cells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => inline(c.trim()));

    const out = [];
    let i = 0;
    while (i < lines.length) {
        const line = lines[i];
        if (/^```/.test(line)) {
            const code = [];
            i++;
            while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
            i++;
            out.push(`<pre><code>${code.join('\n')}</code></pre>`);
        } else if (/^#{1,6}\s/.test(line)) {
            const level = line.match(/^#+/)[0].length;
            const tag = level <= 2 ? 'h3' : level === 3 ? 'h4' : 'h5';
            out.push(`<${tag}>${inline(line.replace(/^#+\s+/, ''))}</${tag}>`);
            i++;
        } else if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
            const ordered = /^\s*\d+[.)]\s+/.test(line);
            const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
            const items = [];
            while (i < lines.length && pattern.test(lines[i])) {
                items.push(`<li>${inline(lines[i].replace(pattern, ''))}</li>`);
                i++;
            }
            const tag = ordered ? 'ol' : 'ul';
            out.push(`<${tag}>${items.join('')}</${tag}>`);
        } else if (isTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
            const head = cells(line).map(c => `<th>${c}</th>`).join('');
            const rows = [];
            i += 2;
            while (i < lines.length && isTableRow(lines[i])) {
                rows.push(`<tr>${cells(lines[i]).map(c => `<td>${c}</td>`).join('')}</tr>`);
                i++;
            }
            out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table>`);
        } else if (!line.trim()) {
            i++;
        } else {
            const para = [];
            while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i]))) {
                para.push(inline(lines[i]));
                i++;
            }
            out.push(`<p>${para.join('<br>')}</p>`);
        }
    }
    return out.join('');
}

function initCoach() {
    document.getElementById('coach-open-btn').addEventListener('click', openCoachPanel);
    document.getElementById('coach-close-btn').addEventListener('click', closeCoachPanel);
    document.getElementById('coach-new-btn').addEventListener('click', startNewCoachConversation);
    document.getElementById('coach-history-btn').addEventListener('click', toggleCoachHistory);
    document.getElementById('coach-stop-btn').addEventListener('click', () => {
        if (coachAbortController) coachAbortController.abort();
    });
    document.getElementById('coach-composer').addEventListener('submit', e => {
        e.preventDefault();
        submitCoachInput();
    });
    document.getElementById('coach-input').addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            submitCoachInput();
        }
    });
    document.getElementById('coach-panel').addEventListener('keydown', e => {
        if (e.key === 'Escape') closeCoachPanel();
    });

    document.getElementById('admin-llm-test-btn').addEventListener('click', testLlmSettings);
    document.getElementById('admin-llm-save-btn').addEventListener('click', saveLlmSettings);
    document.getElementById('admin-llm-base-url').addEventListener('input', updateLlmExternalWarning);
    document.getElementById('admin-llm-embedding-base-url').addEventListener('input', updateLlmExternalWarning);
}

async function checkCoachAvailability() {
    try {
        const res = await fetch('/api/coach/status');
        if (!res.ok) throw new Error('Status unavailable');
        const data = await res.json();
        coachAvailable = data.available;
        document.getElementById('coach-open-btn').classList.toggle('hidden', !coachAvailable);
        const modelLabel = document.getElementById('coach-panel-model');
        modelLabel.textContent = data.model || '';
        modelLabel.title = data.model || '';
        document.getElementById('coach-disclaimer').textContent = data.is_external
            ? 'Answers come from an external AI provider that receives your training data. The coach can make mistakes and is no substitute for medical advice.'
            : 'The coach can make mistakes and is no substitute for medical advice.';
        if (!coachAvailable) closeCoachPanel();
    } catch (err) {
        console.error('Error checking AI coach status', err);
    }
}

function resetCoach() {
    if (coachAbortController) coachAbortController.abort();
    closeCoachPanel();
    coachConversationId = null;
    coachAvailable = false;
    document.getElementById('coach-open-btn').classList.add('hidden');
    document.getElementById('coach-messages').innerHTML = '';
    document.getElementById('coach-conversation-list').innerHTML = '';
}

async function openCoachPanel() {
    document.getElementById('coach-panel').classList.remove('hidden');
    document.getElementById('coach-open-btn').setAttribute('aria-expanded', 'true');
    if (coachConversationId === null) renderCoachEmptyState();
    document.getElementById('coach-input').focus();
}

function closeCoachPanel() {
    const panel = document.getElementById('coach-panel');
    const wasOpen = !panel.classList.contains('hidden');
    panel.classList.add('hidden');
    const openBtn = document.getElementById('coach-open-btn');
    openBtn.setAttribute('aria-expanded', 'false');
    if (wasOpen && !openBtn.classList.contains('hidden')) openBtn.focus();
}

function toggleCoachHistory() {
    const list = document.getElementById('coach-conversation-list');
    const btn = document.getElementById('coach-history-btn');
    const show = list.classList.contains('hidden');
    list.classList.toggle('hidden', !show);
    btn.setAttribute('aria-expanded', String(show));
    if (show) loadCoachConversations();
}

function hideCoachHistory() {
    document.getElementById('coach-conversation-list').classList.add('hidden');
    document.getElementById('coach-history-btn').setAttribute('aria-expanded', 'false');
}

function formatCoachDate(stamp) {
    if (!stamp) return '';
    const d = new Date(stamp.replace(' ', 'T') + 'Z');
    return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

async function loadCoachConversations() {
    const list = document.getElementById('coach-conversation-list');
    try {
        const res = await fetch('/api/coach/conversations');
        if (!res.ok) throw new Error('Failed to load conversations.');
        const conversations = await res.json();
        list.innerHTML = '';
        if (!conversations.length) {
            list.innerHTML = '<p class="coach-empty" style="padding: 8px;">No conversations yet.</p>';
            return;
        }
        conversations.forEach(c => {
            const title = c.title || 'New conversation';
            const item = document.createElement('div');
            item.className = 'coach-conversation-item' + (c.id === coachConversationId ? ' active' : '');
            item.setAttribute('role', 'listitem');
            const open = document.createElement('button');
            open.type = 'button';
            open.className = 'coach-conversation-open';
            open.innerHTML = `${escapeHTML(title)}<small>${escapeHTML(formatCoachDate(c.updated_at))}</small>`;
            open.addEventListener('click', () => openCoachConversation(c.id));
            const del = document.createElement('button');
            del.type = 'button';
            del.className = 'coach-conversation-delete';
            del.textContent = 'Delete';
            del.setAttribute('aria-label', `Delete conversation: ${title}`);
            del.addEventListener('click', () => deleteCoachConversation(c.id));
            item.append(open, del);
            list.appendChild(item);
        });
    } catch (err) {
        list.innerHTML = `<p class="coach-empty" style="padding: 8px;">${escapeHTML(err.message)}</p>`;
    }
}

function renderCoachEmptyState() {
    const container = document.getElementById('coach-messages');
    container.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'coach-empty';
    empty.innerHTML = '<p>Ask about your training, recovery or plans. The coach knows your recent workouts, thresholds and the goals in your coach profile (Settings &gt; Coach).</p>';
    const suggestions = document.createElement('div');
    suggestions.className = 'coach-suggestions';
    COACH_SUGGESTIONS.forEach(text => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'coach-suggestion';
        btn.textContent = text;
        btn.addEventListener('click', () => sendCoachMessage(text));
        suggestions.appendChild(btn);
    });
    empty.appendChild(suggestions);
    container.appendChild(empty);
}

function startNewCoachConversation() {
    if (coachAbortController) return;
    coachConversationId = null;
    renderCoachEmptyState();
    hideCoachHistory();
    document.getElementById('coach-input').focus();
}

async function openCoachConversation(id) {
    if (coachAbortController) return;
    try {
        const res = await fetch(`/api/coach/conversations/${id}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load the conversation.');
        coachConversationId = data.id;
        const container = document.getElementById('coach-messages');
        container.innerHTML = '';
        if (!data.messages.length) renderCoachEmptyState();
        data.messages.forEach(m => appendCoachMessage(m.role, m.content, m.reasoning, m.status, m.sources, m.steps));
        const last = data.messages[data.messages.length - 1];
        if (last && last.role === 'user') appendCoachError('This message has no answer yet.', true);
        hideCoachHistory();
        container.scrollTop = container.scrollHeight;
    } catch (err) {
        alert(err.message);
    }
}

async function deleteCoachConversation(id) {
    if (!confirm('Delete this conversation? This cannot be undone.')) return;
    try {
        const res = await fetch(`/api/coach/conversations/${id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error('Failed to delete the conversation.');
        if (id === coachConversationId) {
            coachConversationId = null;
            renderCoachEmptyState();
        }
        loadCoachConversations();
    } catch (err) {
        alert(err.message);
    }
}

function coachStepText(step) {
    if (step.label) {
        // Running steps end with "..."; finished ones show their result
        return step.summary === undefined ? `${step.label}...` : `${step.label} · ${step.summary}`;
    }
    // Steps stored before steps had labels (library searches only)
    const label = step.automatic ? 'Searched the library for your question' : `Searched the library: "${step.query}"`;
    if (step.found === undefined) return `${label}...`;
    return `${label} · ${step.found ? `${step.found} passage${step.found === 1 ? '' : 's'} from ${step.titles.join(', ')}` : 'nothing relevant found'}`;
}

function renderCoachSteps(stepsEl, steps) {
    stepsEl.innerHTML = '';
    stepsEl.hidden = !steps.length;
    steps.forEach(step => {
        const item = document.createElement('div');
        const running = step.label ? step.summary === undefined : step.found === undefined;
        item.className = `coach-step ${step.kind === 'data' ? 'data' : 'search'}` + (running ? ' active' : '');
        item.textContent = coachStepText(step);
        stepsEl.appendChild(item);
    });
}

// Lists the cited sources; passages the coach read but didn't cite are folded away
function renderCoachSources(sourcesEl, sources, answer) {
    sourcesEl.innerHTML = '';
    sourcesEl.hidden = !sources.length;
    if (!sources.length) return;
    const cited = citedNumbers(answer);
    const renderSource = source => {
        const details = document.createElement('details');
        details.className = 'coach-source';
        const summary = document.createElement('summary');
        summary.textContent = `[${source.n}] ${sourceName(source)}`;
        const text = document.createElement('div');
        text.className = 'coach-source-text';
        text.textContent = source.text;
        details.append(summary, text);
        return details;
    };
    const used = sources.filter(s => cited.has(s.n));
    const other = sources.filter(s => !cited.has(s.n));
    if (used.length) {
        const heading = document.createElement('div');
        heading.className = 'coach-sources-heading';
        heading.textContent = 'Sources';
        sourcesEl.appendChild(heading);
        used.forEach(s => sourcesEl.appendChild(renderSource(s)));
    }
    if (other.length) {
        const more = document.createElement('details');
        more.className = 'coach-sources-more';
        const summary = document.createElement('summary');
        summary.textContent = `${used.length ? 'Also consulted' : 'Consulted'}: ${other.length} passage${other.length === 1 ? '' : 's'}`;
        more.appendChild(summary);
        other.forEach(s => more.appendChild(renderSource(s)));
        sourcesEl.appendChild(more);
    }
}

function appendCoachMessage(role, content, reasoning, status, sources = [], steps = []) {
    const container = document.getElementById('coach-messages');
    const empty = container.querySelector('.coach-empty');
    if (empty) empty.remove();
    const el = document.createElement('div');
    el.className = `coach-message ${role}`;
    let reasoningText = null;
    let reasoningSummary = null;
    if (role === 'assistant') {
        const details = document.createElement('details');
        details.className = 'coach-reasoning';
        details.innerHTML = '<summary>Reasoning</summary>';
        reasoningSummary = details.querySelector('summary');
        reasoningText = document.createElement('div');
        reasoningText.className = 'coach-reasoning-text';
        reasoningText.textContent = reasoning || '';
        details.appendChild(reasoningText);
        details.hidden = !reasoning;
        el.appendChild(details);
    }
    let stepsEl = null;
    if (role === 'assistant') {
        stepsEl = document.createElement('div');
        stepsEl.className = 'coach-steps';
        renderCoachSteps(stepsEl, steps);
        el.appendChild(stepsEl);
    }
    const body = document.createElement('div');
    body.className = 'coach-message-body';
    if (role === 'assistant') {
        body.innerHTML = linkCoachCitations(renderCoachMarkdown(content), sources);
    } else {
        body.textContent = content;
    }
    el.appendChild(body);
    let sourcesEl = null;
    if (role === 'assistant') {
        sourcesEl = document.createElement('div');
        sourcesEl.className = 'coach-sources';
        renderCoachSources(sourcesEl, sources, content);
        el.appendChild(sourcesEl);
    }
    if (status === 'interrupted') {
        const note = document.createElement('span');
        note.className = 'coach-message-note';
        note.textContent = 'This answer was cut short.';
        el.appendChild(note);
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return { el, body, reasoningText, reasoningSummary, stepsEl, sourcesEl };
}

function appendCoachError(message, canRetry) {
    const container = document.getElementById('coach-messages');
    const el = document.createElement('div');
    el.className = 'coach-message error';
    el.setAttribute('role', 'alert');
    el.textContent = message;
    if (canRetry) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-sm btn-outline coach-retry-btn';
        retry.textContent = 'Try again';
        retry.addEventListener('click', () => {
            el.remove();
            sendCoachMessage(null, true);
        });
        el.appendChild(document.createElement('br'));
        el.appendChild(retry);
    }
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
}

function setCoachBusy(busy) {
    document.getElementById('coach-send-btn').classList.toggle('hidden', busy);
    document.getElementById('coach-stop-btn').classList.toggle('hidden', !busy);
    document.getElementById('coach-input').disabled = busy;
    document.getElementById('coach-new-btn').disabled = busy;
    document.getElementById('coach-messages').setAttribute('aria-busy', String(busy));
}

function submitCoachInput() {
    const input = document.getElementById('coach-input');
    const text = input.value.trim();
    if (!text || coachAbortController) return;
    input.value = '';
    sendCoachMessage(text);
}

function formatThinkingTime(seconds) {
    return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

async function sendCoachMessage(text, retry = false) {
    if (coachAbortController) return;
    let thinkingStarted = null;
    let thinkingTimer = null;
    const stopThinkingTimer = () => {
        if (thinkingTimer) clearInterval(thinkingTimer);
        thinkingTimer = null;
        if (thinkingStarted !== null && view) {
            const seconds = Math.round((Date.now() - thinkingStarted) / 1000);
            view.reasoningSummary.textContent = `Reasoning (${formatThinkingTime(seconds)})`;
        }
    };
    coachAbortController = new AbortController();
    setCoachBusy(true);
    let answer = '';
    let reasoning = '';
    let view = null;
    let finished = false;
    try {
        if (coachConversationId === null) {
            const res = await fetch('/api/coach/conversations', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Failed to start a conversation.');
            coachConversationId = data.id;
        }
        if (!retry) appendCoachMessage('user', text);
        view = appendCoachMessage('assistant', '');
        view.body.classList.add('coach-typing');

        const res = await fetch(`/api/coach/conversations/${coachConversationId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(retry ? { retry: true } : { content: text }),
            signal: coachAbortController.signal
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.error || 'The coach could not answer.');
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        const container = document.getElementById('coach-messages');
        let buffer = '';
        let errorMessage = null;
        let doneStatus = null;
        const steps = [];
        let sources = [];
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let boundary;
            while ((boundary = buffer.indexOf('\n\n')) >= 0) {
                const raw = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                const event = (raw.match(/^event: (.*)$/m) || [])[1];
                const dataLine = (raw.match(/^data: (.*)$/m) || [])[1];
                const data = dataLine ? JSON.parse(dataLine) : {};
                const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
                if (event === 'content') {
                    if (thinkingTimer) stopThinkingTimer();
                    answer += data.text;
                    view.body.innerHTML = renderCoachMarkdown(answer);
                } else if (event === 'reasoning') {
                    if (thinkingStarted === null && !answer) {
                        thinkingStarted = Date.now();
                        const tick = () => {
                            const seconds = Math.round((Date.now() - thinkingStarted) / 1000);
                            view.reasoningSummary.textContent = `Thinking... ${formatThinkingTime(seconds)}`;
                        };
                        tick();
                        thinkingTimer = setInterval(tick, 1000);
                    }
                    reasoning += data.text;
                    view.reasoningText.textContent = reasoning;
                    view.reasoningText.parentElement.hidden = false;
                } else if (event === 'step') {
                    steps.push(data);
                    renderCoachSteps(view.stepsEl, steps);
                } else if (event === 'step_result') {
                    steps[steps.length - 1] = data;
                    renderCoachSteps(view.stepsEl, steps);
                } else if (event === 'sources') {
                    sources = data.sources;
                    view.body.innerHTML = linkCoachCitations(renderCoachMarkdown(answer), sources);
                    renderCoachSources(view.sourcesEl, sources, answer);
                } else if (event === 'error') {
                    errorMessage = data.message;
                } else if (event === 'done') {
                    finished = true;
                    doneStatus = data.status;
                }
                if (nearBottom) container.scrollTop = container.scrollHeight;
            }
        }
        view.body.classList.remove('coach-typing');
        if (errorMessage) {
            if (!answer) view.el.remove();
            appendCoachError(errorMessage, !answer);
        } else if (!finished) {
            appendCoachError('The connection to the coach was interrupted.', !answer);
        } else if (doneStatus === 'interrupted') {
            const note = document.createElement('span');
            note.className = 'coach-message-note';
            note.textContent = 'This answer was cut short.';
            view.el.appendChild(note);
        }
    } catch (err) {
        if (view) view.body.classList.remove('coach-typing');
        if (view && !answer) view.el.remove();
        if (err.name === 'AbortError') {
            if (view && answer) {
                const note = document.createElement('span');
                note.className = 'coach-message-note';
                note.textContent = 'Stopped.';
                view.el.appendChild(note);
            }
        } else {
            appendCoachError(err.message, coachConversationId !== null && !answer);
        }
    } finally {
        if (thinkingTimer) stopThinkingTimer();
        coachAbortController = null;
        setCoachBusy(false);
        document.getElementById('coach-input').focus();
    }
}

// Settings > Coach: the athlete's profile
async function loadCoachProfile() {
    try {
        const res = await fetch('/api/coach/profile');
        if (!res.ok) throw new Error('Failed to load the coach profile.');
        const profile = await res.json();
        coachProfileLoaded = {};
        Object.entries(COACH_PROFILE_INPUTS).forEach(([key, id]) => {
            coachProfileLoaded[key] = profile[key] || '';
            document.getElementById(id).value = coachProfileLoaded[key];
        });
    } catch (err) {
        console.error('Error loading coach profile', err);
        coachProfileLoaded = null;
    }
}

async function saveCoachProfileIfChanged() {
    if (!coachProfileLoaded) return;
    const values = {};
    let changed = false;
    Object.entries(COACH_PROFILE_INPUTS).forEach(([key, id]) => {
        values[key] = document.getElementById(id).value.trim();
        if (values[key] !== coachProfileLoaded[key]) changed = true;
    });
    if (!changed) return;
    try {
        const res = await fetch('/api/coach/profile', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(values)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to save the coach profile.');
        coachProfileLoaded = values;
    } catch (err) {
        alert(err.message);
    }
}

// Settings > Admin: the LLM endpoint
function isExternalLlmUrl(url) {
    let host;
    try {
        host = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, '');
    } catch (e) {
        return false;
    }
    if (!host) return false;
    if (host === 'localhost' || host === '::1' || (!host.includes('.') && !host.includes(':'))) return false;
    if (/^(127\.|10\.|192\.168\.|169\.254\.)/.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host)) return false;
    if (host.includes(':') && /^(fc|fd|fe80)/.test(host)) return false;
    return !/\.(local|lan|home|internal|home\.arpa|localhost)$/.test(host);
}

function updateLlmExternalWarning() {
    const urls = ['admin-llm-base-url', 'admin-llm-embedding-base-url']
        .map(id => document.getElementById(id).value.trim()).filter(Boolean);
    document.getElementById('admin-llm-external-warning').classList.toggle('hidden', !urls.some(isExternalLlmUrl));
}

async function loadLlmSettings() {
    try {
        const res = await fetch('/api/admin/llm');
        if (!res.ok) throw new Error('Failed to load the AI coach settings.');
        renderLlmSettings(await res.json());
    } catch (err) {
        console.error('Error loading LLM settings', err);
    }
}

function renderLlmSettings(config) {
    document.getElementById('admin-llm-enabled').checked = config.enabled;
    document.getElementById('admin-llm-base-url').value = config.base_url || '';
    document.getElementById('admin-llm-model').value = config.model || '';
    document.getElementById('admin-llm-context-window').value = config.context_window;
    document.getElementById('admin-llm-temperature').value = config.temperature;
    document.getElementById('admin-llm-reasoning').value = config.reasoning || '';
    document.getElementById('admin-llm-embedding-model').value = config.embedding_model || '';
    document.getElementById('admin-llm-library-search').value = config.library_search || 'auto';
    document.getElementById('admin-llm-data-tools').value = config.data_tools || 'auto';
    document.getElementById('admin-llm-embedding-base-url').value = config.embedding_base_url || '';
    const keyInput = document.getElementById('admin-llm-api-key');
    keyInput.value = '';
    keyInput.disabled = config.api_key_from_env;
    document.getElementById('admin-llm-api-key-status').textContent = config.api_key_from_env
        ? 'Set through the BLAEU_LLM_API_KEY environment variable.'
        : config.api_key_set ? 'A key is stored. Leave empty to keep it.' : 'No key stored.';
    updateLlmExternalWarning();
}

function readLlmForm() {
    const payload = {
        enabled: document.getElementById('admin-llm-enabled').checked,
        base_url: document.getElementById('admin-llm-base-url').value.trim(),
        model: document.getElementById('admin-llm-model').value.trim(),
        context_window: parseInt(document.getElementById('admin-llm-context-window').value, 10),
        temperature: parseFloat(document.getElementById('admin-llm-temperature').value),
        reasoning: document.getElementById('admin-llm-reasoning').value,
        embedding_model: document.getElementById('admin-llm-embedding-model').value.trim(),
        library_search: document.getElementById('admin-llm-library-search').value,
        data_tools: document.getElementById('admin-llm-data-tools').value,
        embedding_base_url: document.getElementById('admin-llm-embedding-base-url').value.trim()
    };
    const key = document.getElementById('admin-llm-api-key').value.trim();
    if (key) payload.api_key = key;
    return payload;
}

function showLlmTestResult(html, ok) {
    const box = document.getElementById('admin-llm-test-result');
    box.innerHTML = html;
    box.classList.remove('hidden', 'ok', 'failed');
    if (ok !== null) box.classList.add(ok ? 'ok' : 'failed');
}

async function testLlmSettings() {
    const btn = document.getElementById('admin-llm-test-btn');
    btn.disabled = true;
    showLlmTestResult('Testing the connection. A local model may need a minute to load...', null);
    try {
        const res = await fetch('/api/admin/llm/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(readLlmForm())
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'The test failed.');

        const options = document.getElementById('admin-llm-model-options');
        options.innerHTML = '';
        data.models.forEach(name => {
            const option = document.createElement('option');
            option.value = name;
            options.appendChild(option);
        });
        const lines = [data.is_ollama ? 'Ollama server detected.' : 'OpenAI-compatible endpoint.'];
        if (data.models.length) lines.push(`${data.models.length} model(s) available; pick one in the Model field.`);
        if (data.model_found === false) lines.push('The chosen model is not in the list of available models.');
        lines.push(data.ok
            ? `The model answered in ${data.seconds} s: "${escapeHTML(data.reply)}"`
            : `<strong>Problem:</strong> ${escapeHTML(data.error)}`);
        if (data.embeddings) {
            lines.push(data.embeddings.ok
                ? `Embeddings work (${data.embeddings.dimensions} dimensions).`
                : `<strong>Embedding problem:</strong> ${escapeHTML(data.embeddings.error)}`);
        }
        const ok = data.ok && (!data.embeddings || data.embeddings.ok);
        showLlmTestResult(lines.join('<br>'), ok);
    } catch (err) {
        showLlmTestResult(`<strong>Problem:</strong> ${escapeHTML(err.message)}`, false);
    } finally {
        btn.disabled = false;
    }
}

async function saveLlmSettings() {
    const btn = document.getElementById('admin-llm-save-btn');
    btn.disabled = true;
    try {
        const res = await fetch('/api/admin/llm', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(readLlmForm())
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to save the AI coach settings.');
        renderLlmSettings(data);
        showLlmTestResult('Settings saved.', true);
        checkCoachAvailability();
    } catch (err) {
        showLlmTestResult(`<strong>Problem:</strong> ${escapeHTML(err.message)}`, false);
    } finally {
        btn.disabled = false;
    }
}

// ----------------------------------------------------
// Training literature library (Settings > Library)
// ----------------------------------------------------
let libraryPollTimer = null;
const LIBRARY_POLL_MS = 5000;

function initLibrary() {
    document.getElementById('library-upload-btn').addEventListener('click', uploadLibraryDocument);
    document.getElementById('library-search-btn').addEventListener('click', searchLibrary);
    document.getElementById('library-search-input').addEventListener('keydown', e => {
        // Enter searches instead of submitting the surrounding settings form
        if (e.key === 'Enter') {
            e.preventDefault();
            searchLibrary();
        }
    });
}

function formatFileSize(bytes) {
    if (!bytes) return '';
    return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} kB`;
}

function libraryStatusText(doc, enabled) {
    if (doc.status === 'failed') return `Failed: ${doc.error || 'unknown error'}`;
    if (doc.status === 'pending') {
        return enabled ? 'Waiting to be indexed...' : 'Waiting for an administrator to set up library search.';
    }
    if (doc.status === 'indexing') {
        let text = doc.chunk_count
            ? `Indexing: ${doc.embedded_count} of ${doc.chunk_count} passages`
            : 'Reading the document...';
        if (doc.error) text += ` (${doc.error})`;
        return text;
    }
    const parts = [`Ready: ${doc.chunk_count} passages`];
    if (doc.pages) parts.push(`${doc.pages} pages`);
    parts.push(formatFileSize(doc.file_size));
    return parts.filter(Boolean).join(' · ');
}

async function loadLibrary() {
    const container = document.getElementById('library-documents');
    try {
        const res = await fetch('/api/library');
        if (!res.ok) throw new Error('Failed to load the library.');
        renderLibrary(await res.json());
    } catch (err) {
        container.innerHTML = `<p class="data-choice-help">${escapeHTML(err.message)}</p>`;
    }
}

function renderLibrary(data) {
    document.getElementById('library-disabled-note').classList.toggle('hidden', data.enabled);
    document.getElementById('library-shared-label').classList.toggle('hidden', !data.can_share);
    const container = document.getElementById('library-documents');
    container.innerHTML = '';
    if (!data.documents.length) {
        container.innerHTML = '<p class="data-choice-help">No documents yet.</p>';
    }
    data.documents.forEach(doc => {
        const item = document.createElement('div');
        item.className = 'library-document';

        const header = document.createElement('div');
        header.className = 'library-document-header';
        const info = document.createElement('div');
        const title = document.createElement('div');
        title.className = 'library-document-title';
        title.textContent = doc.title;
        const badge = document.createElement('span');
        badge.className = 'library-badge' + (doc.shared ? '' : ' personal');
        badge.textContent = doc.shared ? 'Shared' : 'Personal';
        title.appendChild(badge);
        const status = document.createElement('div');
        status.className = 'library-document-status' + (doc.status === 'failed' ? ' failed' : '');
        status.textContent = libraryStatusText(doc, data.enabled);
        info.append(title, status);

        const actions = document.createElement('div');
        actions.className = 'library-document-actions';
        const addAction = (label, handler, aria) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline';
            btn.textContent = label;
            btn.setAttribute('aria-label', `${aria}: ${doc.title}`);
            btn.addEventListener('click', handler);
            actions.appendChild(btn);
            return btn;
        };
        let previewBox = null;
        if (doc.chunk_count) {
            addAction('Preview', async () => {
                if (previewBox) {
                    previewBox.remove();
                    previewBox = null;
                    return;
                }
                previewBox = document.createElement('div');
                previewBox.className = 'library-preview';
                previewBox.textContent = 'Loading...';
                item.appendChild(previewBox);
                await loadLibraryPreview(doc.id, previewBox);
            }, 'Preview the extracted text');
        }
        if (doc.can_manage) {
            if (doc.status === 'failed' || doc.status === 'ready') {
                addAction('Re-index', () => reindexLibraryDocument(doc.id), 'Index again');
            }
            addAction('Delete', () => deleteLibraryDocument(doc), 'Delete');
        }
        header.append(info, actions);
        item.appendChild(header);

        if (doc.status === 'indexing' && doc.chunk_count) {
            const percent = Math.round(100 * doc.embedded_count / doc.chunk_count);
            const bar = document.createElement('div');
            bar.className = 'progress-bar';
            bar.setAttribute('role', 'progressbar');
            bar.setAttribute('aria-label', `Indexing ${doc.title}`);
            bar.setAttribute('aria-valuemin', '0');
            bar.setAttribute('aria-valuemax', '100');
            bar.setAttribute('aria-valuenow', String(percent));
            bar.innerHTML = `<div class="progress-fill" style="width: ${percent}%"></div>`;
            item.appendChild(bar);
        }
        container.appendChild(item);
    });

    // Keep the list fresh while documents are being indexed and the settings are open
    clearTimeout(libraryPollTimer);
    const busy = data.enabled && data.documents.some(d => d.status === 'pending' || d.status === 'indexing');
    const settingsModal = document.getElementById('settings-modal');
    if (busy && !settingsModal.classList.contains('hidden')) {
        libraryPollTimer = setTimeout(loadLibrary, LIBRARY_POLL_MS);
    }
}

async function loadLibraryPreview(documentId, box) {
    try {
        const res = await fetch(`/api/library/${documentId}/preview`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to load the preview.');
        box.innerHTML = '';
        const note = document.createElement('p');
        note.className = 'data-choice-help';
        note.textContent = 'The first passages as the coach will read them. Garbled text here means the file could not be read well.';
        box.appendChild(note);
        data.chunks.forEach(c => box.appendChild(renderLibraryPassage(c.text, sourceLabel(null, c.page, c.section))));
    } catch (err) {
        box.textContent = err.message;
    }
}

function sourceLabel(title, page, section) {
    return [title, section, page ? `p. ${page}` : null].filter(Boolean).join(' · ');
}

function renderLibraryPassage(text, source) {
    const passage = document.createElement('div');
    passage.className = 'library-passage';
    if (source) {
        const label = document.createElement('span');
        label.className = 'library-passage-source';
        label.textContent = source;
        passage.appendChild(label);
    }
    passage.appendChild(document.createTextNode(text));
    return passage;
}

async function uploadLibraryDocument() {
    const fileInput = document.getElementById('library-file');
    const status = document.getElementById('library-upload-status');
    const btn = document.getElementById('library-upload-btn');
    const file = fileInput.files[0];
    if (!file) {
        status.textContent = 'Choose a file first.';
        return;
    }
    const form = new FormData();
    form.append('file', file);
    form.append('title', document.getElementById('library-title').value.trim());
    form.append('shared', document.getElementById('library-shared').checked ? 'true' : 'false');
    btn.disabled = true;
    status.textContent = `Uploading ${file.name}...`;
    try {
        const res = await fetch('/api/library', { method: 'POST', body: form });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || (res.status === 413 ? 'The file is too large.' : 'The upload failed.'));
        status.textContent = `Added "${data.title}". It's indexed in the background.`;
        fileInput.value = '';
        document.getElementById('library-title').value = '';
        document.getElementById('library-shared').checked = false;
        loadLibrary();
    } catch (err) {
        status.textContent = err.message;
    } finally {
        btn.disabled = false;
    }
}

async function deleteLibraryDocument(doc) {
    const where = doc.shared ? 'the shared library (for all users)' : 'your library';
    if (!confirm(`Delete "${doc.title}" from ${where}? The coach can no longer use it.`)) return;
    try {
        const res = await fetch(`/api/library/${doc.id}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Failed to delete the document.');
        loadLibrary();
    } catch (err) {
        alert(err.message);
    }
}

async function reindexLibraryDocument(documentId) {
    try {
        const res = await fetch(`/api/library/${documentId}/reindex`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Failed to start indexing.');
        loadLibrary();
    } catch (err) {
        alert(err.message);
    }
}

async function searchLibrary() {
    const query = document.getElementById('library-search-input').value.trim();
    const box = document.getElementById('library-search-results');
    if (!query) return;
    box.innerHTML = '<p class="data-choice-help">Searching...</p>';
    try {
        const res = await fetch('/api/library/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'The search failed.');
        box.innerHTML = '';
        if (!data.results.length) {
            box.innerHTML = '<p class="data-choice-help">Nothing relevant found.</p>';
            return;
        }
        data.results.forEach(r => {
            const source = `${sourceLabel(r.title, r.page, r.section)} · relevance ${Math.round(r.similarity * 100)}%`;
            box.appendChild(renderLibraryPassage(r.text, source));
        });
    } catch (err) {
        box.innerHTML = `<p class="data-choice-help">${escapeHTML(err.message)}</p>`;
    }
}

})();
