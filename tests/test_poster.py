import os
import io
import pytest
from flask import json
from unittest.mock import patch, MagicMock
import geopandas as gpd

# Mock the entire osmnx and matplotlib operations to make test fast and network-free
@pytest.fixture
def mock_gis():
    with patch('osmnx.graph_from_point') as mock_graph_pt, \
         patch('osmnx.project_graph') as mock_proj, \
         patch('osmnx.features_from_point') as mock_feats, \
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
        
        mock_graph_pt.return_value = g
        mock_proj.return_value = g
        
        # Mock features_from_point to return empty gdf
        mock_feats.return_value = gpd.GeoDataFrame()
        
        # Mock savefig to actually touch the target output file so it exists
        def side_effect(path, *args, **kwargs):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'dummy')
        mock_save.side_effect = side_effect
        
        yield {
            'graph_from_point': mock_graph_pt,
            'project_graph': mock_proj,
            'features_from_point': mock_feats,
            'savefig': mock_save
        }

from tests.test_api import client, GPX_DATA_1

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
