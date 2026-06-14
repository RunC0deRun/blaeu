import os
import io
import pytest
from flask import json
from unittest.mock import patch, MagicMock
import geopandas as gpd

# Mock the entire osmnx and matplotlib operations to make test fast and network-free
@pytest.fixture
def mock_gis():
    with patch('osmnx.graph_from_bbox') as mock_graph_bbox, \
         patch('osmnx.project_graph') as mock_proj, \
         patch('osmnx.features_from_bbox') as mock_feats, \
         patch('matplotlib.pyplot.savefig') as mock_save:
         
        # Create a real MultiDiGraph with nodes and edges to satisfy graph_to_gdfs
        import networkx as nx
        from shapely.geometry import LineString
        
        g = nx.MultiDiGraph()
        g.graph['crs'] = 'EPSG:3857'
        g.add_node(1, x=0, y=0)
        g.add_node(2, x=1000, y=1000)
        geom = LineString([(0, 0), (1000, 1000)])
        g.add_edge(1, 2, highway='primary', geometry=geom)
        
        mock_graph_bbox.return_value = g
        mock_proj.return_value = g
        
        # Mock features_from_bbox to return empty gdf
        mock_feats.return_value = gpd.GeoDataFrame()
        
        # Mock savefig to actually touch the target output file so it exists
        def side_effect(path, *args, **kwargs):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'dummy')
        mock_save.side_effect = side_effect
        
        yield {
            'graph_from_bbox': mock_graph_bbox,
            'project_graph': mock_proj,
            'features_from_bbox': mock_feats,
            'savefig': mock_save
        }

from tests.test_api import client, GPX_DATA_1

def test_list_map_themes_unauthorized(clean_env):
    unauthed = clean_env.test_client()
    response = unauthed.get('/api/map-themes')
    assert response.status_code == 401
    assert 'Unauthorized' in json.loads(response.data)['error']

def test_list_map_themes(client):
    response = client.get('/api/map-themes')
    assert response.status_code == 200
    themes = json.loads(response.data)
    assert isinstance(themes, list)
    # Check if some default themes are loaded
    theme_ids = [t['id'] for t in themes]
    assert 'noir' in theme_ids
    assert 'blueprint' in theme_ids

def test_generate_poster_map_endpoint(client, mock_gis):
    # 1. Upload a route first to get a valid route ID
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']
    
    # 2. Get poster map
    # Without bounds, let the backend calculate from simplified path
    response = client.get(f'/api/routes/{route_id}/poster-map?theme=noir')
    assert response.status_code == 200
    res_data = json.loads(response.data)
    
    assert 'image_url' in res_data
    assert 'bounds' in res_data
    assert 'bg_color' in res_data
    assert res_data['bg_color'] == '#000000' # Noir background color
    
    # Verify image_url endpoint works
    image_url = res_data['image_url']
    img_res = client.get(image_url)
    assert img_res.status_code == 200
    assert img_res.data == b'dummy' # Matches the mocked file contents

def test_generate_poster_map_with_bounds(client, mock_gis):
    # 1. Upload a route first
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']
    
    # 2. Get poster map with custom bounds
    response = client.get(
        f'/api/routes/{route_id}/poster-map?theme=blueprint&latMin=52.5&latMax=52.6&lonMin=13.4&lonMax=13.5'
    )
    assert response.status_code == 200
    res_data = json.loads(response.data)
    assert 'image_url' in res_data
    assert res_data['bg_color'] == '#1A3A5C' # Blueprint bg color

def test_generate_poster_map_with_custom_labels(client, mock_gis):
    # 1. Upload a route first
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']
    
    # 2. Get poster map with custom labels
    response = client.get(
        f'/api/routes/{route_id}/poster-map?theme=noir&displayCity=Paris&displayCountry=France'
    )
    assert response.status_code == 200
    res_data = json.loads(response.data)
    assert 'image_url' in res_data
    assert res_data['display_city'] == 'Paris'
    assert res_data['display_country'] == 'France'


def test_background_generation_on_upload(client, mock_gis):
    # 1. Update the default map style for the current user to 'noir'
    res = client.put('/api/auth/default-map-style', json={'default_map_style': 'noir'})
    assert res.status_code == 200
    
    # 2. Upload a route
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    with patch('app.run_async_poster_generation') as mock_run:
        upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
        assert upload_res.status_code == 201
        route_id = json.loads(upload_res.data)['id']
        
        # Verify that run_async_poster_generation was called with the correct arguments
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == route_id
        assert args[2] == 'noir'


def test_garmin_auto_sync(client):
    # 1. Connect Garmin mock connection first
    from db import save_garmin_connection
    save_garmin_connection(1, "test@example.com", "Test User")
    
    # 2. Get status, should show auto_sync_interval as 'off' by default
    res = client.get('/api/garmin/status')
    assert res.status_code == 200
    status_data = json.loads(res.data)
    assert status_data['status'] == 'connected'
    assert status_data['auto_sync_interval'] == 'off'
    
    # 3. Update auto-sync to '3h'
    res = client.put('/api/garmin/auto-sync', json={'auto_sync_interval': '3h'})
    assert res.status_code == 200
    res_data = json.loads(res.data)
    assert res_data['status'] == 'success'
    assert res_data['auto_sync_interval'] == '3h'
    
    # 4. Get status again, should be '3h'
    res = client.get('/api/garmin/status')
    assert res.status_code == 200
    status_data = json.loads(res.data)
    assert status_data['auto_sync_interval'] == '3h'
    
    # 5. Test invalid interval
    res = client.put('/api/garmin/auto-sync', json={'auto_sync_interval': 'invalid'})
    assert res.status_code == 400


def test_reverse_geocode_input_validation():
    from poster_map import reverse_geocode
    
    # Verify invalid inputs return empty immediately without triggering external requests
    assert reverse_geocode("invalid", 13.4) == ("", "")
    assert reverse_geocode(52.5, "invalid") == ("", "")
    assert reverse_geocode(91.0, 13.4) == ("", "")
    assert reverse_geocode(-91.0, 13.4) == ("", "")
    assert reverse_geocode(52.5, 181.0) == ("", "")
    assert reverse_geocode(52.5, -181.0) == ("", "")
    assert reverse_geocode(float('nan'), 13.4) == ("", "")
    assert reverse_geocode(52.5, float('inf')) == ("", "")


def test_generate_poster_map_with_corrupted_cache(client, mock_gis, tmp_path):
    import json
    from poster_map import generate_poster_background, ox
    
    # 1. Setup a temp cache folder and write corrupted files
    cache_dir = tmp_path / "osmnx_cache"
    cache_dir.mkdir()
    
    # Write a 0-byte file
    zero_byte_file = cache_dir / "empty.json"
    zero_byte_file.touch()
    
    # Write an invalid JSON file
    corrupted_file = cache_dir / "corrupted.json"
    corrupted_file.write_text("invalid json data here")
    
    # Write a valid JSON file
    valid_file = cache_dir / "valid.json"
    valid_file.write_text('{"status": "ok"}')
    
    # Temporarily override ox.settings.cache_folder
    original_cache_folder = ox.settings.cache_folder
    ox.settings.cache_folder = str(cache_dir)
    
    # Setup mock graph_from_bbox side effect
    call_count = 0
    original_side_effect = mock_gis['graph_from_bbox'].side_effect
    original_return = mock_gis['graph_from_bbox'].return_value
    
    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise json.JSONDecodeError("Expecting value", "", 0)
        # On second call, return the mock graph
        return original_return
        
    mock_gis['graph_from_bbox'].side_effect = side_effect
    mock_gis['graph_from_bbox'].return_value = None
    
    try:
        # Generate poster map
        res = generate_poster_background(
            route_id=1,
            lat_min=52.5, lat_max=52.6,
            lon_min=13.4, lon_max=13.5,
            theme_name="noir",
            display_city="Paris",
            display_country="France"
        )
        
        # Verify call count is 2 (first failed, second succeeded)
        assert call_count == 2
        
        # Verify the corrupted cache files were removed
        assert not zero_byte_file.exists()
        assert not corrupted_file.exists()
        
        # Verify the valid cache file is still present
        assert valid_file.exists()
        
        # Verify result format
        assert 'image_url' in res
        assert res['display_city'] == 'Paris'
        assert res['display_country'] == 'France'
        
    finally:
        # Restore mock state and ox cache folder
        ox.settings.cache_folder = original_cache_folder
        mock_gis['graph_from_bbox'].side_effect = original_side_effect
        mock_gis['graph_from_bbox'].return_value = original_return


def test_generate_poster_map_osm_timeout(client, mock_gis):
    # 1. Upload a route first
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']
    
    # 2. Make graph_from_bbox raise requests.exceptions.Timeout
    import requests
    original_side_effect = mock_gis['graph_from_bbox'].side_effect
    original_return = mock_gis['graph_from_bbox'].return_value
    
    mock_gis['graph_from_bbox'].side_effect = requests.exceptions.Timeout("Connection timed out")
    mock_gis['graph_from_bbox'].return_value = None
    
    try:
        response = client.get(f'/api/routes/{route_id}/poster-map?theme=noir')
        assert response.status_code == 503
        res_data = json.loads(response.data)
        assert 'error' in res_data
        assert 'timed out or failed' in res_data['error']
    finally:
        # Restore mock state
        mock_gis['graph_from_bbox'].side_effect = original_side_effect
        mock_gis['graph_from_bbox'].return_value = original_return


def test_generate_poster_map_with_aspect_ratio(client, mock_gis):
    # 1. Upload a route first
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']
    
    # 2. Get poster map with default aspect ratio
    response_default = client.get(f'/api/routes/{route_id}/poster-map?theme=noir')
    assert response_default.status_code == 200
    url_default = json.loads(response_default.data)['image_url']
    
    # 3. Get poster map with 3:4 aspect ratio
    response_custom = client.get(f'/api/routes/{route_id}/poster-map?theme=noir&aspectRatio=3:4')
    assert response_custom.status_code == 200
    url_custom = json.loads(response_custom.data)['image_url']
    
    # The image URLs should be different because the aspect ratio is part of the file hash
    assert url_default != url_custom


def test_generate_poster_background_safe_zone(client, mock_gis):
    from poster_map import generate_poster_background
    
    # 1. Upload a route first to get a valid route ID and initialize the DB
    data = {
        'file': (io.BytesIO(GPX_DATA_1.encode('utf-8')), 'test_route.gpx')
    }
    upload_res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert upload_res.status_code == 201
    route_id = json.loads(upload_res.data)['id']

    # 2. No labels/gradients:
    res_no_labels = generate_poster_background(
        route_id=route_id,
        lat_min=52.5, lat_max=52.6,
        lon_min=13.4, lon_max=13.5,
        theme_name="noir",
        display_city="", display_country="",  # falsy => no gradients
        aspect_ratio="1:1"
    )
    bounds_no_labels = res_no_labels['bounds']
    lat_min_no, lon_min_no = bounds_no_labels[0]
    lat_max_no, lon_max_no = bounds_no_labels[1]
    
    # 3. With labels/gradients:
    res_labels = generate_poster_background(
        route_id=route_id,
        lat_min=52.5, lat_max=52.6,
        lon_min=13.4, lon_max=13.5,
        theme_name="noir",
        display_city="Berlin", display_country="Germany",  # truthy => gradients active
        aspect_ratio="1:1"
    )
    bounds_labels = res_labels['bounds']
    lat_min_l, lon_min_l = bounds_labels[0]
    lat_max_l, lon_max_l = bounds_labels[1]
    
    # With gradients/labels active and aspect_ratio 1:1, the height must be significantly larger (taller) 
    # than when they are inactive, because of the safe zone requirement.
    height_no_labels = lat_max_no - lat_min_no
    height_labels = lat_max_l - lat_min_l
    
    assert height_labels > height_no_labels
    
    # Also verify that the original route latitude bounds [52.5, 52.6] are strictly within 
    # the middle 50% of the returned bounds_labels [lat_min_l, lat_max_l].
    from pyproj import Transformer
    to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    _, y_route_min = to_3857.transform(13.4, 52.5)
    _, y_route_max = to_3857.transform(13.5, 52.6)
    
    _, y_min_l = to_3857.transform(lon_min_l, lat_min_l)
    _, y_max_l = to_3857.transform(lon_max_l, lat_max_l)
    height_m_l = y_max_l - y_min_l
    
    assert y_route_min >= y_min_l + 0.25 * height_m_l - 1.0
    assert y_route_max <= y_max_l - 0.25 * height_m_l + 1.0





