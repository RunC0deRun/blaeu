import defusedxml.ElementTree as ET
from datetime import datetime
import math
from typing import Optional, Dict, Any, List

def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    # Clean Z suffix or standard ISO string format
    cleaned = dt_str.strip()
    if cleaned.endswith('Z'):
        cleaned = cleaned[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        # Fallback parse formats
        for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
    return None

def haversine_distance(p1: Dict[str, float], p2: Dict[str, float]) -> float:
    lat1, lon1 = math.radians(p1['lat']), math.radians(p1['lon'])
    lat2, lon2 = math.radians(p2['lat']), math.radians(p2['lon'])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371000 * c

def simplify_coords(tracks: List[Dict[str, Any]], max_points: int = 100) -> List[List[float]]:
    # Extract all coordinate pairs in sequence from all tracks/segments
    coords = []
    for track in tracks:
        for segment in track.get('segments', []):
            for pt in segment:
                coords.append([pt['lat'], pt['lon']])
                
    if not coords:
        return []
        
    n = len(coords)
    if n <= max_points:
        return coords
        
    # Downsample keeping start and end points
    step = (n - 1) / (max_points - 1)
    simplified = []
    for i in range(max_points):
        idx = int(round(i * step))
        if idx < n:
            simplified.append(coords[idx])
    return simplified

def parse_gpx(file_content: Any) -> Dict[str, Any]:
    """
    Parses GPX XML content (can be string or bytes) and returns statistics and coordinates.
    """
    if isinstance(file_content, bytes):
        file_content = file_content.decode('utf-8', errors='ignore')
    
    try:
        root = ET.fromstring(file_content)
    except Exception as e:
        raise ValueError(f"Invalid XML syntax: {e}")
    
    # Strip namespaces for easy tag parsing
    for elem in root.iter():
        if '}' in elem.tag:
            elem.tag = elem.tag.split('}', 1)[1]

    # Find start coordinates and time for timezone lookup and activity start time
    start_lat = None
    start_lon = None
    start_time_str = None

    # Find first trackpoint with valid time
    for trkpt in root.findall('.//trkpt'):
        try:
            lat = float(trkpt.get('lat'))
            lon = float(trkpt.get('lon'))
            if start_lat is None:
                start_lat = lat
                start_lon = lon
            time_elem = trkpt.find('time')
            if time_elem is not None and time_elem.text:
                val = time_elem.text.strip()
                if parse_iso_datetime(val) is not None:
                    start_time_str = val
                    # If we found coordinates and time, we can stop
                    break
        except (TypeError, ValueError):
            continue

    # Fallback to waypoints if no start time/coords found
    if start_lat is None or start_time_str is None:
        for wpt in root.findall('.//wpt'):
            try:
                lat = float(wpt.get('lat'))
                lon = float(wpt.get('lon'))
                if start_lat is None:
                    start_lat = lat
                    start_lon = lon
                time_elem = wpt.find('time')
                if time_elem is not None and time_elem.text:
                    val = time_elem.text.strip()
                    if parse_iso_datetime(val) is not None:
                        start_time_str = val
                        break
            except (TypeError, ValueError):
                continue

    # Timezone lookup
    timezone_name = None
    if start_lat is not None and start_lon is not None:
        try:
            from timezonefinder import TimezoneFinder
            tf = TimezoneFinder()
            timezone_name = tf.timezone_at(lng=start_lon, lat=start_lat)
        except Exception:
            pass

    # Standardize start_time_str to UTC formatted string if valid
    start_time_utc_str = None
    if start_time_str:
        dt = parse_iso_datetime(start_time_str)
        if dt:
            if dt.tzinfo is not None:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc)
            start_time_utc_str = dt.strftime('%Y-%m-%d %H:%M:%S')

    # Counts
    waypoints_count = len(root.findall('.//wpt'))
    tracks_count = len(root.findall('.//trk'))
    segments_count = len(root.findall('.//trkseg'))
    points_count = len(root.findall('.//trkpt'))
    
    # Extract Waypoints
    waypoints_data = []
    for wpt in root.findall('.//wpt'):
        try:
            lat = float(wpt.get('lat'))
            lon = float(wpt.get('lon'))
        except (TypeError, ValueError):
            continue
        name_elem = wpt.find('name')
        desc_elem = wpt.find('desc')
        name = name_elem.text if name_elem is not None else ""
        desc = desc_elem.text if desc_elem is not None else ""
        waypoints_data.append({
            'lat': lat,
            'lon': lon,
            'name': name,
            'desc': desc
        })
        
    # Extract Tracks and compute stats
    tracks_data = []
    total_distance = 0.0
    elevation_gain = 0.0
    elevation_loss = 0.0
    total_duration = 0.0
    
    moving_distance = 0.0
    moving_duration = 0.0
    max_speed = 0.0
    
    has_time = False
    has_ele = False
    
    for trk in root.findall('.//trk'):
        trk_name_elem = trk.find('name')
        trk_desc_elem = trk.find('desc')
        trk_name = trk_name_elem.text if trk_name_elem is not None else ""
        trk_desc = trk_desc_elem.text if trk_desc_elem is not None else ""
        
        segments = []
        for trkseg in trk.findall('.//trkseg'):
            seg_points = []
            for trkpt in trkseg.findall('.//trkpt'):
                try:
                    lat = float(trkpt.get('lat'))
                    lon = float(trkpt.get('lon'))
                except (TypeError, ValueError):
                    continue
                
                ele_elem = trkpt.find('ele')
                ele = float(ele_elem.text) if (ele_elem is not None and ele_elem.text is not None) else None
                if ele is not None:
                    has_ele = True
                    
                time_elem = trkpt.find('time')
                time_str = time_elem.text if (time_elem is not None and time_elem.text is not None) else None
                time_val = parse_iso_datetime(time_str)
                if time_val is not None:
                    has_time = True
                    
                seg_points.append({
                    'lat': lat,
                    'lon': lon,
                    'ele': ele,
                    'time': time_str,
                    'datetime': time_val
                })
            
            # Compute stats for this segment
            seg_dist = 0.0
            seg_gain = 0.0
            seg_loss = 0.0
            seg_dur = 0.0
            
            for i in range(len(seg_points) - 1):
                p1 = seg_points[i]
                p2 = seg_points[i+1]
                
                # Distance
                d = haversine_distance(p1, p2)
                seg_dist += d
                
                # Elevation
                if p1['ele'] is not None and p2['ele'] is not None:
                    diff = p2['ele'] - p1['ele']
                    if diff > 0:
                        seg_gain += diff
                    else:
                        seg_loss += abs(diff)
                        
                # Time / Speed
                if p1['datetime'] is not None and p2['datetime'] is not None:
                    dt = (p2['datetime'] - p1['datetime']).total_seconds()
                    if dt > 0:
                        seg_dur += dt
                        speed = d / dt
                        
                        # Filter out extreme noise speed outliers (e.g. > 100 m/s)
                        if speed < 100.0:
                            max_speed = max(max_speed, speed)
                            if speed >= 0.5: # 0.5 m/s threshold for active moving
                                moving_distance += d
                                moving_duration += dt
            
            total_distance += seg_dist
            elevation_gain += seg_gain
            elevation_loss += seg_loss
            total_duration += seg_dur
            
            # Clean datetime objects before returning/saving
            for pt in seg_points:
                pt.pop('datetime', None)
                
            segments.append(seg_points)
            
        tracks_data.append({
            'name': trk_name,
            'desc': trk_desc,
            'segments': segments
        })
        
    avg_speed = (total_distance / total_duration) if (has_time and total_duration > 0) else 0.0
    avg_moving_speed = (moving_distance / moving_duration) if (has_time and moving_duration > 0) else 0.0
    
    return {
        'statistics': {
            'total_distance': total_distance,
            'elevation_gain': elevation_gain if has_ele else 0.0,
            'elevation_loss': elevation_loss if has_ele else 0.0,
            'duration': total_duration if has_time else 0.0,
            'avg_speed': avg_speed,
            'avg_moving_speed': avg_moving_speed,
            'max_speed': max_speed if has_time else 0.0,
            'waypoints_count': waypoints_count,
            'tracks_count': tracks_count,
            'segments_count': segments_count,
            'points_count': points_count
        },
        'tracks': tracks_data,
        'waypoints': waypoints_data,
        'timezone': timezone_name,
        'start_time': start_time_utc_str,
        'simplified_path': simplify_coords(tracks_data)
    }
