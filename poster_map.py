import os
import math
import json
import hashlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import osmnx as ox
from shapely.geometry import Point
from pyproj import Transformer
from db import DATA_DIR, update_route_poster_status
import requests
import numpy as np
import matplotlib.colors as mcolors
from matplotlib.font_manager import FontProperties

GEO_CACHE_FILE = os.path.join(ox.settings.cache_folder, 'geocoding_cache.json')

def load_geocoding_cache():
    if os.path.exists(GEO_CACHE_FILE):
        try:
            with open(GEO_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_geocoding_cache(cache):
    try:
        with open(GEO_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def reverse_geocode(lat, lon):
    cache = load_geocoding_cache()
    key = f"{lat:.4f}_{lon:.4f}"
    if key in cache:
        return cache[key]['city'], cache[key]['country']

    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language=en"
    headers = {"User-Agent": "Blaeu-GPX-Cartographer/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            address = data.get("address", {})
            city = address.get("city") or address.get("town") or address.get("village") or address.get("suburb") or address.get("county") or ""
            country = address.get("country") or ""
            cache[key] = {'city': city, 'country': country}
            save_geocoding_cache(cache)
            return city, country
    except Exception as e:
        print(f"Reverse geocoding error: {e}")
    return "", ""

def is_latin_script(text):
    if not text:
        return True
    latin_count = 0
    total_alpha = 0
    for char in text:
        if char.isalpha():
            total_alpha += 1
            if ord(char) < 0x250:
                latin_count += 1
    if total_alpha == 0:
        return True
    return (latin_count / total_alpha) > 0.8

def create_gradient_fade(ax, color, location="bottom", zorder=10):
    vals = np.linspace(0, 1, 256).reshape(-1, 1)
    gradient = np.hstack((vals, vals))

    rgb = mcolors.to_rgb(color)
    my_colors = np.zeros((256, 4))
    my_colors[:, 0] = rgb[0]
    my_colors[:, 1] = rgb[1]
    my_colors[:, 2] = rgb[2]

    if location == "bottom":
        my_colors[:, 3] = np.linspace(1, 0, 256)
        extent_y_start = 0
        extent_y_end = 0.25
    else:
        my_colors[:, 3] = np.linspace(0, 1, 256)
        extent_y_start = 0.75
        extent_y_end = 1.0

    custom_cmap = mcolors.ListedColormap(my_colors)

    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    y_range = ylim[1] - ylim[0]

    y_bottom = ylim[0] + y_range * extent_y_start
    y_top = ylim[0] + y_range * extent_y_end

    ax.imshow(
        gradient,
        extent=[xlim[0], xlim[1], y_bottom, y_top],
        aspect="auto",
        cmap=custom_cmap,
        zorder=zorder,
        origin="lower"
    )



# Set up directories
POSTER_MAPS_DIR = os.path.join(DATA_DIR, 'poster_maps')
os.makedirs(POSTER_MAPS_DIR, exist_ok=True)

# Configure OSMnx settings
ox.settings.use_cache = True
ox.settings.cache_folder = os.path.join(DATA_DIR, 'osmnx_cache')
os.makedirs(ox.settings.cache_folder, exist_ok=True)

# Transformers for EPSG:4326 <-> EPSG:3857
# always_xy=True ensures coordinate ordering is (longitude, latitude) or (x, y)
to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

def generate_poster_background(route_id, lat_min, lat_max, lon_min, lon_max, theme_name, display_city=None, display_country=None, apply_padding=True):
    """
    Generates a minimalist styled background map centered and scaled to the bounding box.
    Returns metadata including bounds and static URL.
    """
    # 1. Project bounding box corners to EPSG:3857
    x_min, y_min = to_3857.transform(lon_min, lat_min)
    x_max, y_max = to_3857.transform(lon_max, lat_max)
    
    # Ensure correct ordering
    x_min, x_max = min(x_min, x_max), max(x_min, x_max)
    y_min, y_max = min(y_min, y_max), max(y_min, y_max)
    
    width_m = x_max - x_min
    height_m = y_max - y_min
    
    # Enforce minimum size of 500m to prevent divide-by-zero or tiny maps
    if width_m < 500:
        center_x = (x_min + x_max) / 2
        x_min = center_x - 250
        x_max = center_x + 250
        width_m = 500
    if height_m < 500:
        center_y = (y_min + y_max) / 2
        y_min = center_y - 250
        y_max = center_y + 250
        height_m = 500
        
    # Add 2000m padding on each side to accommodate camera pans in video exports
    if apply_padding:
        padding_m = 2000
        x_min -= padding_m
        x_max += padding_m
        y_min -= padding_m
        y_max += padding_m
        width_m = x_max - x_min
        height_m = y_max - y_min
    
    # Clamp aspect ratio between 0.5 (tall) and 2.0 (wide) to look nice
    aspect = width_m / height_m
    if aspect < 0.5:
        # Too tall: expand width to achieve aspect = 0.5
        target_width = height_m * 0.5
        diff = target_width - width_m
        x_min -= diff / 2
        x_max += diff / 2
        width_m = target_width
        aspect = 0.5
    elif aspect > 2.0:
        # Too wide: expand height to achieve aspect = 2.0
        target_height = width_m / 2.0
        diff = target_height - height_m
        y_min -= diff / 2
        y_max += diff / 2
        height_m = target_height
        aspect = 2.0
        
    # Calculate final bounding box and center in EPSG:4326
    lon_min_final, lat_min_final = to_4326.transform(x_min, y_min)
    lon_max_final, lat_max_final = to_4326.transform(x_max, y_max)
    center_lon, center_lat = to_4326.transform((x_min + x_max) / 2, (y_min + y_max) / 2)
    
    # Calculate radius for OSM querying (distance from center to furthest corner)
    corner_dist = math.sqrt((width_m / 2)**2 + (height_m / 2)**2)
    
    # Resolve display city/country if not specified
    if display_city is None or display_country is None:
        detected_city, detected_country = reverse_geocode(center_lat, center_lon)
        if display_city is None:
            display_city = detected_city
        if display_country is None:
            display_country = detected_country

    # Generate bbox hash to uniquely identify this crop area and label settings
    label_str = f"{display_city or ''}_{display_country or ''}"
    bbox_str = f"{lat_min:.6f}_{lat_max:.6f}_{lon_min:.6f}_{lon_max:.6f}_{label_str}"
    bbox_hash = hashlib.md5(bbox_str.encode()).hexdigest()[:8]
    filename = f"{route_id}_{theme_name}_{bbox_hash}.png"
    output_path = os.path.join(POSTER_MAPS_DIR, filename)
    
    # Load theme colors
    base_dir = os.path.dirname(os.path.abspath(__file__))
    theme_file = os.path.join(base_dir, 'static', 'themes', f"{theme_name}.json")
    
    if os.path.exists(theme_file):
        try:
            with open(theme_file, 'r', encoding='utf-8') as f:
                theme = json.load(f)
        except Exception:
            theme = None
    else:
        theme = None
        
    if not theme:
        # Default terracotta theme fallback
        theme = {
            "name": "Terracotta",
            "bg": "#F5EDE4",
            "text": "#8B4513",
            "gradient_color": "#F5EDE4",
            "water": "#A8C4C4",
            "parks": "#E8E0D0",
            "road_motorway": "#A0522D",
            "road_primary": "#B8653A",
            "road_secondary": "#C9846A",
            "road_tertiary": "#D9A08A",
            "road_residential": "#E5C4B0",
            "road_default": "#D9A08A",
        }
        
    # Check cache first
    if os.path.exists(output_path):
        return {
            'image_url': f'/api/poster-maps/{filename}',
            'bounds': [[lat_min_final, lon_min_final], [lat_max_final, lon_max_final]],
            'bg_color': theme['bg'],
            'text_color': theme['text'],
            'display_city': display_city or '',
            'display_country': display_country or ''
        }

        
    # Fetch map data
    point = (center_lat, center_lon)
    
    # 1. Fetch street network
    try:
        g = ox.graph_from_point(point, dist=corner_dist, dist_type='bbox', network_type='all', truncate_by_edge=True)
        g_proj = ox.project_graph(g, to_crs='EPSG:3857')
    except Exception as e:
        print(f"Error fetching OSMnx graph: {e}")
        raise e
    finally:
        update_route_poster_status(route_id, {
            'status': 'generating',
            'progress': 2,
            'error': None
        })
        
    # 2. Fetch water features (optional)
    water = None
    try:
        water = ox.features_from_point(point, tags={"natural": ["water", "bay", "strait"], "waterway": "riverbank"}, dist=corner_dist)
    except Exception as e:
        print(f"No water features found or error: {e}")
    finally:
        update_route_poster_status(route_id, {
            'status': 'generating',
            'progress': 3,
            'error': None
        })
        
    # 3. Fetch parks (optional)
    parks = None
    try:
        parks = ox.features_from_point(point, tags={"leisure": "park", "landuse": "grass"}, dist=corner_dist)
    except Exception as e:
        print(f"No parks found or error: {e}")
    finally:
        update_route_poster_status(route_id, {
            'status': 'generating',
            'progress': 4,
            'error': None
        })
        
    # 4. Render the matplotlib figure
    fig_width = 12.0
    fig_height = fig_width / aspect
    
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), facecolor=theme["bg"])
    ax.set_facecolor(theme["bg"])
    ax.set_position((0.0, 0.0, 1.0, 1.0))
    
    # Plot water polygons
    if water is not None and not water.empty:
        water_polys = water[water.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not water_polys.empty:
            try:
                water_polys = water_polys.to_crs("EPSG:3857")
                water_polys.plot(ax=ax, facecolor=theme['water'], edgecolor='none', zorder=1)
            except Exception as e:
                print(f"Error plotting water polygons: {e}")
                
    # Plot park polygons
    if parks is not None and not parks.empty:
        parks_polys = parks[parks.geometry.type.isin(["Polygon", "MultiPolygon"])]
        if not parks_polys.empty:
            try:
                parks_polys = parks_polys.to_crs("EPSG:3857")
                parks_polys.plot(ax=ax, facecolor=theme['parks'], edgecolor='none', zorder=2)
            except Exception as e:
                print(f"Error plotting park polygons: {e}")
                
    # Assign road colors and widths based on highway type
    edge_colors = []
    edge_widths = []
    
    for _u, _v, data in g_proj.edges(data=True):
        highway = data.get('highway', 'unclassified')
        if isinstance(highway, list):
            highway = highway[0] if highway else 'unclassified'
            
        if highway in ["motorway", "motorway_link"]:
            color = theme["road_motorway"]
            width = 1.6
        elif highway in ["trunk", "trunk_link", "primary", "primary_link"]:
            color = theme["road_primary"]
            width = 1.3
        elif highway in ["secondary", "secondary_link"]:
            color = theme["road_secondary"]
            width = 1.0
        elif highway in ["tertiary", "tertiary_link"]:
            color = theme["road_tertiary"]
            width = 0.7
        elif highway in ["residential", "living_street", "unclassified"]:
            color = theme["road_residential"]
            width = 0.5
        else:
            color = theme.get("road_default", theme["road_residential"])
            width = 0.5
            
        edge_colors.append(color)
        edge_widths.append(width)
        
    ox.plot_graph(
        g_proj, ax=ax, bgcolor=theme['bg'],
        node_size=0,
        edge_color=edge_colors,
        edge_linewidth=edge_widths,
        show=False,
        close=False,
    )
    
    # Force coordinates to exactly match our cropped Web Mercator bounding box
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    
    # Render poster gradients and text if either display_city or display_country is provided
    if display_city or display_country:
        create_gradient_fade(ax, theme['bg'], location='bottom', zorder=10)
        create_gradient_fade(ax, theme['bg'], location='top', zorder=10)
        
        scale_factor = min(fig_height, fig_width) / 12.0
        
        base_sub = 22
        base_coords = 14
        
        # City text
        if display_city:
            if is_latin_script(display_city):
                spaced_city = "  ".join(list(display_city.upper()))
            else:
                spaced_city = display_city
                
            base_main = 60
            adjusted_font_size = base_main * scale_factor
            city_char_count = len(display_city)
            if city_char_count > 10:
                length_factor = 10 / city_char_count
                adjusted_font_size = max(adjusted_font_size * length_factor, 10 * scale_factor)
                
            font_main = FontProperties(family="sans-serif", weight="bold", size=adjusted_font_size)
            
            ax.text(
                0.5,
                0.14,
                spaced_city,
                transform=ax.transAxes,
                color=theme["text"],
                ha="center",
                fontproperties=font_main,
                zorder=11,
            )
            
        # Country text
        if display_country:
            font_sub = FontProperties(family="sans-serif", weight="normal", size=base_sub * scale_factor)
            ax.text(
                0.5,
                0.10,
                display_country.upper(),
                transform=ax.transAxes,
                color=theme["text"],
                ha="center",
                fontproperties=font_sub,
                zorder=11,
            )
            
        # Divider line
        ax.plot(
            [0.4, 0.6],
            [0.125, 0.125],
            transform=ax.transAxes,
            color=theme["text"],
            linewidth=1 * scale_factor,
            zorder=11,
        )
        
        # Coordinates text
        font_coords = FontProperties(family="sans-serif", size=base_coords * scale_factor)
        coords_str = (
            f"{center_lat:.4f}° N / {center_lon:.4f}° E"
            if center_lat >= 0
            else f"{abs(center_lat):.4f}° S / {center_lon:.4f}° E"
        )
        if center_lon < 0:
            coords_str = coords_str.replace("E", "W")
            
        ax.text(
            0.5,
            0.07,
            coords_str,
            transform=ax.transAxes,
            color=theme["text"],
            alpha=0.7,
            ha="center",
            fontproperties=font_coords,
            zorder=11,
        )

    # Save the figure to file
    plt.savefig(
        output_path,
        facecolor=theme["bg"],
        bbox_inches="tight",
        pad_inches=0.0,
        dpi=300
    )
    plt.close()
    
    return {
        'image_url': f'/api/poster-maps/{filename}',
        'bounds': [[lat_min_final, lon_min_final], [lat_max_final, lon_max_final]],
        'bg_color': theme['bg'],
        'text_color': theme['text'],
        'display_city': display_city or '',
        'display_country': display_country or ''
    }

