import pytest
from gpx_parser import parse_gpx, haversine_distance

# Mock GPX files
GPX_SINGLE_TRACK = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MockCreator" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="52.5200" lon="13.4050">
    <name>Start Point</name>
    <desc>This is the start</desc>
  </wpt>
  <trk>
    <name>Morning Run</name>
    <desc>Nice and easy</desc>
    <trkseg>
      <trkpt lat="52.5200" lon="13.4050">
        <ele>34.0</ele>
        <time>2026-05-25T13:00:00Z</time>
      </trkpt>
      <trkpt lat="52.5210" lon="13.4060">
        <ele>36.0</ele>
        <time>2026-05-25T13:01:00Z</time>
      </trkpt>
      <trkpt lat="52.5220" lon="13.4070">
        <ele>35.0</ele>
        <time>2026-05-25T13:02:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

GPX_MULTI_TRACK = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MockCreator" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Track 1</name>
    <trkseg>
      <trkpt lat="52.5200" lon="13.4050">
        <ele>10.0</ele>
        <time>2026-05-25T13:00:00Z</time>
      </trkpt>
      <trkpt lat="52.5210" lon="13.4060">
        <ele>15.0</ele>
        <time>2026-05-25T13:01:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
  <trk>
    <name>Track 2</name>
    <trkseg>
      <trkpt lat="52.5210" lon="13.4060">
        <ele>15.0</ele>
        <time>2026-05-25T13:02:00Z</time>
      </trkpt>
      <trkpt lat="52.5220" lon="13.4070">
        <ele>12.0</ele>
        <time>2026-05-25T13:03:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

GPX_NO_DATA = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="MockCreator" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>No Data Route</name>
    <trkseg>
      <trkpt lat="52.5200" lon="13.4050">
      </trkpt>
      <trkpt lat="52.5210" lon="13.4060">
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

def test_haversine_distance():
    p1 = {'lat': 52.5200, 'lon': 13.4050}
    p2 = {'lat': 52.5210, 'lon': 13.4060}
    dist = haversine_distance(p1, p2)
    assert dist > 0
    # Expected distance is around 130 meters
    assert 120 < dist < 140

def test_parse_single_track():
    result = parse_gpx(GPX_SINGLE_TRACK)
    stats = result['statistics']
    
    assert stats['waypoints_count'] == 1
    assert stats['tracks_count'] == 1
    assert stats['segments_count'] == 1
    assert stats['points_count'] == 3
    
    assert stats['elevation_gain'] == 2.0
    assert stats['elevation_loss'] == 1.0
    assert stats['duration'] == 120.0
    
    total_dist = stats['total_distance']
    assert 240 < total_dist < 280
    
    # Speed validation
    assert stats['avg_speed'] == total_dist / 120.0
    assert stats['avg_moving_speed'] == total_dist / 120.0
    assert stats['max_speed'] > 0
    
    # Structure verification
    assert len(result['tracks']) == 1
    assert result['tracks'][0]['name'] == "Morning Run"
    assert result['tracks'][0]['desc'] == "Nice and easy"
    assert len(result['tracks'][0]['segments']) == 1
    assert len(result['tracks'][0]['segments'][0]) == 3
    
    assert len(result['waypoints']) == 1
    assert result['waypoints'][0]['name'] == "Start Point"
    
    assert result['timezone'] == 'Europe/Berlin'
    assert result['start_time'] == '2026-05-25 13:00:00'

def test_parse_multi_track():
    result = parse_gpx(GPX_MULTI_TRACK)
    stats = result['statistics']
    
    assert stats['tracks_count'] == 2
    assert stats['segments_count'] == 2
    assert stats['points_count'] == 4
    
    assert stats['elevation_gain'] == 5.0
    assert stats['elevation_loss'] == 3.0
    assert stats['duration'] == 120.0 # 60s segment 1 + 60s segment 2
    
    assert len(result['tracks']) == 2
    assert result['tracks'][0]['name'] == "Track 1"
    assert result['tracks'][1]['name'] == "Track 2"

def test_parse_no_data():
    result = parse_gpx(GPX_NO_DATA)
    stats = result['statistics']
    
    assert stats['tracks_count'] == 1
    assert stats['points_count'] == 2
    assert stats['elevation_gain'] == 0.0
    assert stats['elevation_loss'] == 0.0
    assert stats['duration'] == 0.0
    assert stats['avg_speed'] == 0.0
    assert stats['avg_moving_speed'] == 0.0
    assert stats['max_speed'] == 0.0
    assert result['timezone'] == 'Europe/Berlin'
    assert result['start_time'] is None

def test_parse_gpx_billion_laughs():
    malicious_gpx = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
      <!ENTITY lol "lol">
      <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
      <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
    ]>
    <gpx version="1.1" creator="MockCreator" xmlns="http://www.topografix.com/GPX/1/1">
      <trk>
        <name>&lol3;</name>
      </trk>
    </gpx>
    """
    
    with pytest.raises(ValueError) as excinfo:
        parse_gpx(malicious_gpx)
        
    assert "EntitiesForbidden" in str(excinfo.value) or "entities are forbidden" in str(excinfo.value)

