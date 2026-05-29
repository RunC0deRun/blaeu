import os
import io
import time
import pytest
import threading
from app import app
import db
import tempfile
import shutil

# Fixture to start the Flask app in a background thread
@pytest.fixture(scope="module")
def server():
    # Setup temporary directory and database for the server
    temp_db_fd, temp_db_path = tempfile.mkstemp()
    temp_data_dir = tempfile.mkdtemp()
    
    # Override paths using environment variables
    os.environ["DATA_DIR"] = temp_data_dir
    
    # Import and configure inside the scope to apply env overrides
    import db as app_db
    app_db.DB_PATH = temp_db_path
    app_db.DATA_DIR = temp_data_dir
    app_db.init_db()
    
    # Ensure static/upload directories exist
    os.makedirs(os.path.join(temp_data_dir, 'gpx'), exist_ok=True)
    os.makedirs(os.path.join(temp_data_dir, 'tiles_cache'), exist_ok=True)
    
    # Add a mock local tile so the test doesn't need to fetch from internet
    # tile path: /api/tiles/4/8/5.png (for Europe view)
    tile_dir = os.path.join(temp_data_dir, 'tiles_cache', '4', '8')
    os.makedirs(tile_dir, exist_ok=True)
    # Write a 1x1 transparent PNG
    transparent_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82'
    with open(os.path.join(tile_dir, '5.png'), 'wb') as f:
        f.write(transparent_png)

    # Start Flask server
    server_thread = threading.Thread(target=lambda: app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False))
    server_thread.daemon = True
    server_thread.start()
    
    # Give the server a moment to start
    time.sleep(1.0)
    
    yield "http://127.0.0.1:5001"
    
    # Cleanup temp resources
    os.close(temp_db_fd)
    os.unlink(temp_db_path)
    shutil.rmtree(temp_data_dir, ignore_errors=True)

# Helper to write a test GPX file
@pytest.fixture
def test_gpx_path():
    gpx_content = """<?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="Mock" xmlns="http://www.topografix.com/GPX/1/1">
      <trk>
        <name>Integration Run</name>
        <trkseg>
          <trkpt lat="52.5200" lon="13.4050"><ele>34.0</ele><time>2026-05-25T13:00:00Z</time></trkpt>
          <trkpt lat="52.5210" lon="13.4060"><ele>36.0</ele><time>2026-05-25T13:01:00Z</time></trkpt>
        </trkseg>
      </trk>
    </gpx>
    """
    fd, path = tempfile.mkstemp(suffix=".gpx")
    with os.fdopen(fd, 'w') as f:
        f.write(gpx_content)
    yield path
    os.unlink(path)

# Integration Test using Playwright
def test_end_to_end_flow(server, test_gpx_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed. Skipping browser integration test.")

    with sync_playwright() as p:
        # Use headless browser
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        page.on("console", lambda msg: print(f"CONSOLE: {msg.text}"))
        page.on("pageerror", lambda exc: print(f"PAGE ERROR: {exc}"))
        
        # 1. Open home page
        page.goto(server)
        page.wait_for_selector('.app-title')
        assert page.text_content('.app-title').strip().upper() == 'BLAEU'
        
        # Log in
        page.fill('#login-username', 'admin')
        page.fill('#login-password', 'password123')
        page.click('#login-submit-btn')
        page.wait_for_selector('#login-modal', state="hidden")
        
        # 2. Upload file
        # Set file input directly
        page.set_input_files('#file-input', test_gpx_path)
        
        # Click upload button
        page.click('#upload-btn')
        
        # Wait for route details panel to reveal
        page.wait_for_selector('#route-details-panel:not(.hidden)', timeout=5000)
        
        # Check title and statistics display
        assert page.text_content('#route-title') == 'Integration Run'
        assert "km" in page.text_content('#stat-distance')
        assert "00:01:00" in page.text_content('#stat-duration')
        
        # 3. Test Animation Controls
        # Scrubber starts at 0
        assert page.locator('#animation-scrubber').input_value() == '0'
        
        # Click play button
        page.click('#play-pause-btn')
        
        # Let it play for a bit
        page.wait_for_timeout(100)
        
        # Verify that play status is active (shows pause icon, scrubber moved)
        assert page.locator('#icon-pause:not(.hidden)').count() == 1
        current_scrub = float(page.locator('#animation-scrubber').input_value())
        assert current_scrub > 0
        
        # Click pause button
        page.click('#play-pause-btn')
        assert page.locator('#icon-play:not(.hidden)').count() == 1
        
        # 4. Test Video Export
        # Intercept download dialog
        with page.expect_download(timeout=15000) as download_info:
            page.click('#export-video-btn')
            
        download = download_info.value
        assert download.suggested_filename.endswith('.webm')
        assert 'integration_run' in download.suggested_filename
        
        browser.close()


def test_privacy_zone_cropping(server, test_gpx_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed. Skipping browser integration test.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context().new_page()
        
        # 1. Open home page
        page.goto(server)
        page.wait_for_selector('.app-title')
        
        # Log in
        page.fill('#login-username', 'admin')
        page.fill('#login-password', 'password123')
        page.click('#login-submit-btn')
        page.wait_for_selector('#login-modal', state="hidden")
        
        # 2. Upload file
        page.set_input_files('#file-input', test_gpx_path)
        page.click('#upload-btn')
        page.wait_for_selector('#route-details-panel:not(.hidden)', timeout=5000)
        
        # Check initial state: privacy distance should be default '0'
        assert page.evaluate("localStorage.getItem('blaeu_privacy_distance')") is None or page.evaluate("localStorage.getItem('blaeu_privacy_distance')") == '0'
        
        # 3. Open settings modal
        page.click('#settings-btn')
        page.wait_for_selector('#settings-modal:not(.hidden)')
        
        # 4. Select privacy zone range of 1000m
        page.select_option('#privacy-select', '1000')
        
        # Verify the privacy setting is persisted in localStorage
        privacy_val = page.evaluate("localStorage.getItem('blaeu_privacy_distance')")
        assert privacy_val == '1000'
        
        # Turn off privacy zone to test animation play
        page.select_option('#privacy-select', '0')
        privacy_val = page.evaluate("localStorage.getItem('blaeu_privacy_distance')")
        assert privacy_val == '0'

        # Close settings
        page.click('#close-settings-btn')
        page.wait_for_selector('#settings-modal', state="hidden")
        
        # Verify that we can still play/scrub the animation without errors
        page.click('#play-pause-btn')
        page.wait_for_timeout(200)
        assert page.locator('#icon-pause:not(.hidden)').count() == 1
        
        browser.close()
