import os
import sqlite3
import json
from datetime import datetime, timezone as tz
from zoneinfo import ZoneInfo

def get_timezone_abbr(created_at_str, timezone_name):
    if not created_at_str or not timezone_name:
        return None
    try:
        dt_utc = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz.utc)
        dt_local = dt_utc.astimezone(ZoneInfo(timezone_name))
        return dt_local.tzname()
    except Exception:
        return None

DATA_DIR = os.getenv('DATA_DIR', './data')
DB_PATH = os.path.join(DATA_DIR, 'blaeu.db')

def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Create Users Table first
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        default_map_style TEXT DEFAULT 'dark'
    );
    """)
    
    # 2. Create Folders Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        user_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        UNIQUE(name, user_id)
    );
    """)
    
    # Create Routes Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        filename TEXT NOT NULL,
        file_hash TEXT NOT NULL UNIQUE,
        file_path TEXT NOT NULL,
        folder_id INTEGER,
        user_id INTEGER,
        is_public INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        timezone TEXT,
        simplified_path TEXT,
        poster_status TEXT,
        total_distance REAL,
        elevation_gain REAL,
        elevation_loss REAL,
        duration REAL,
        avg_speed REAL,
        avg_moving_speed REAL,
        max_speed REAL,
        waypoints_count INTEGER,
        tracks_count INTEGER,
        segments_count INTEGER,
        points_count INTEGER,
        FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Ensure user_id and is_public columns exist on routes (migration for existing DBs)
    cursor.execute("PRAGMA table_info(users);")
    user_columns = [row['name'] for row in cursor.fetchall()]
    if 'default_map_style' not in user_columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN default_map_style TEXT DEFAULT 'dark';")
        except sqlite3.OperationalError:
            pass

    cursor.execute("PRAGMA table_info(routes);")
    columns = [row['name'] for row in cursor.fetchall()]
    if 'timezone' not in columns:
        try:
            cursor.execute("ALTER TABLE routes ADD COLUMN timezone TEXT;")
        except sqlite3.OperationalError:
            pass
    if 'simplified_path' not in columns:
        try:
            cursor.execute("ALTER TABLE routes ADD COLUMN simplified_path TEXT;")
        except sqlite3.OperationalError:
            pass
    if 'poster_status' not in columns:
        try:
            cursor.execute("ALTER TABLE routes ADD COLUMN poster_status TEXT;")
        except sqlite3.OperationalError:
            pass
    if 'user_id' not in columns:
        try:
            cursor.execute("ALTER TABLE routes ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;")
        except sqlite3.OperationalError:
            pass
    if 'is_public' not in columns:
        try:
            cursor.execute("ALTER TABLE routes ADD COLUMN is_public INTEGER DEFAULT 0;")
        except sqlite3.OperationalError:
            pass

    # Migrate garmin_connections table if auto_sync_interval is missing
    cursor.execute("PRAGMA table_info(garmin_connections);")
    garmin_columns = [row['name'] for row in cursor.fetchall()]
    if garmin_columns and 'auto_sync_interval' not in garmin_columns:
        try:
            cursor.execute("ALTER TABLE garmin_connections ADD COLUMN auto_sync_interval TEXT DEFAULT 'off';")
        except sqlite3.OperationalError:
            pass

    # Migrate folders table if user_id is missing to make unique(name, user_id)
    cursor.execute("PRAGMA table_info(folders);")
    folder_columns = [row['name'] for row in cursor.fetchall()]
    if 'user_id' not in folder_columns:
        try:
            cursor.execute("DROP TABLE IF EXISTS folders_old;")
            cursor.execute("PRAGMA foreign_keys = OFF;")
            cursor.execute("ALTER TABLE folders RENAME TO folders_old;")
            cursor.execute("""
            CREATE TABLE folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                user_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(name, user_id)
            );
            """)
            cursor.execute("INSERT INTO folders (id, name, created_at) SELECT id, name, created_at FROM folders_old;")
            cursor.execute("DROP TABLE folders_old;")
            cursor.execute("PRAGMA foreign_keys = ON;")
        except Exception as e:
            print(f"Error migrating folders table: {e}")
            cursor.execute("PRAGMA foreign_keys = ON;")
            
    # Repair routes table if it contains references to folders_old (due to SQLite RENAME tracking)
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='routes';")
    routes_sql_row = cursor.fetchone()
    if routes_sql_row:
        routes_sql = routes_sql_row['sql']
        if 'folders_old' in routes_sql.lower():
            try:
                cursor.execute("DROP TABLE IF EXISTS routes_new;")
                cursor.execute("PRAGMA foreign_keys = OFF;")
                cursor.execute("""
                CREATE TABLE routes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    filename TEXT NOT NULL,
                    file_hash TEXT NOT NULL UNIQUE,
                    file_path TEXT NOT NULL,
                    folder_id INTEGER,
                    user_id INTEGER,
                    is_public INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    timezone TEXT,
                    simplified_path TEXT,
                    poster_status TEXT,
                    total_distance REAL,
                    elevation_gain REAL,
                    elevation_loss REAL,
                    duration REAL,
                    avg_speed REAL,
                    avg_moving_speed REAL,
                    max_speed REAL,
                    waypoints_count INTEGER,
                    tracks_count INTEGER,
                    segments_count INTEGER,
                    points_count INTEGER,
                    FOREIGN KEY (folder_id) REFERENCES folders(id) ON DELETE SET NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """)
                # Filter columns that exist in the current routes table to prevent insert failures
                cursor.execute("PRAGMA table_info(routes);")
                curr_cols = [row['name'] for row in cursor.fetchall()]
                common_cols = [
                    'id', 'name', 'description', 'filename', 'file_hash', 'file_path', 
                    'folder_id', 'user_id', 'is_public', 'created_at', 'timezone', 
                    'simplified_path', 'total_distance', 'elevation_gain', 'elevation_loss', 
                    'duration', 'avg_speed', 'avg_moving_speed', 'max_speed', 
                    'waypoints_count', 'tracks_count', 'segments_count', 'points_count',
                    'poster_status'
                ]
                insert_cols = [col for col in common_cols if col in curr_cols]
                cols_str = ", ".join(insert_cols)
                
                cursor.execute(f"INSERT INTO routes_new ({cols_str}) SELECT {cols_str} FROM routes;")
                cursor.execute("DROP TABLE routes;")
                cursor.execute("ALTER TABLE routes_new RENAME TO routes;")
                cursor.execute("PRAGMA foreign_keys = ON;")
                print("Successfully repaired routes table foreign key reference to folders")
            except Exception as e:
                print(f"Error repairing routes table: {e}")
                cursor.execute("PRAGMA foreign_keys = ON;")
    
    # Create Tags Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    """)
    
    # Create Route-Tags Association Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS route_tags (
        route_id INTEGER,
        tag_id INTEGER,
        PRIMARY KEY (route_id, tag_id),
        FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    );
    """)

    # Create Garmin Connections Table (supporting multi-user readiness via user_id)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS garmin_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER DEFAULT 1,
        email TEXT NOT NULL,
        display_name TEXT,
        last_sync TEXT,
        auto_sync_interval TEXT DEFAULT 'off',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # Backfill timezone, start_time, and simplified_path for existing routes where they are NULL
    cursor.execute("SELECT id, file_path, created_at, timezone, simplified_path FROM routes WHERE timezone IS NULL OR simplified_path IS NULL;")
    routes_to_backfill = [dict(row) for row in cursor.fetchall()]
    
    if routes_to_backfill:
        from gpx_parser import parse_gpx
        for r in routes_to_backfill:
            route_id = r['id']
            file_path = r['file_path']
            if file_path and os.path.exists(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        content = f.read()
                    parsed = parse_gpx(content)
                    timezone = parsed.get('timezone') or r['timezone'] or 'UTC'
                    start_time = parsed.get('start_time') or r['created_at']
                    simplified_path = json.dumps(parsed.get('simplified_path', []))
                    
                    cursor.execute("""
                        UPDATE routes 
                        SET timezone = ?, created_at = ?, simplified_path = ? 
                        WHERE id = ?
                    """, (timezone, start_time, simplified_path, route_id))
                except Exception as e:
                    print(f"Error backfilling route {route_id}: {e}")
                    
    conn.commit()
    conn.close()

# Helper Functions for Routes
def add_route(route_metadata, stats):
    conn = get_db()
    cursor = conn.cursor()
    
    timezone = route_metadata.get('timezone')
    created_at = route_metadata.get('created_at')
    if created_at is None:
        from datetime import datetime, timezone as tz
        created_at = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')

    try:
        cursor.execute("""
        INSERT INTO routes (
            name, description, filename, file_hash, file_path, folder_id, user_id, is_public,
            total_distance, elevation_gain, elevation_loss, duration,
            avg_speed, avg_moving_speed, max_speed,
            waypoints_count, tracks_count, segments_count, points_count,
            timezone, created_at, simplified_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            route_metadata['name'],
            route_metadata.get('description'),
            route_metadata['filename'],
            route_metadata['file_hash'],
            route_metadata['file_path'],
            route_metadata.get('folder_id'),
            route_metadata.get('user_id'),
            1 if route_metadata.get('is_public') else 0,
            stats['total_distance'],
            stats['elevation_gain'],
            stats['elevation_loss'],
            stats['duration'],
            stats['avg_speed'],
            stats['avg_moving_speed'],
            stats['max_speed'],
            stats['waypoints_count'],
            stats['tracks_count'],
            stats['segments_count'],
            stats['points_count'],
            timezone,
            created_at,
            route_metadata.get('simplified_path')
        ))
        route_id = cursor.lastrowid
        conn.commit()
        return route_id
    except sqlite3.IntegrityError as e:
        conn.close()
        raise ValueError(f"Route already exists or validation failed: {e}")
    finally:
        conn.close()

def get_routes(user_id, folder_id=None, sort_by=None, sort_order=None):
    conn = get_db()
    cursor = conn.cursor()
    
    query = """
        SELECT r.*, f.name AS folder_name, u.username AS owner_username
        FROM routes r
        LEFT JOIN folders f ON r.folder_id = f.id
        LEFT JOIN users u ON r.user_id = u.id
        WHERE (r.user_id = ? OR r.is_public = 1)
    """
    params = [user_id]
    if folder_id is not None:
        query += " AND r.folder_id = ?"
        params.append(folder_id)
        
    # Whitelist sort parameters to prevent SQL injection
    ALLOWED_SORT_COLUMNS = {
        'name': 'r.name',
        'date': 'r.created_at',
        'created_at': 'r.created_at',
        'distance': 'r.total_distance',
        'total_distance': 'r.total_distance',
        'duration': 'r.duration'
    }
    ALLOWED_SORT_ORDERS = {'ASC', 'DESC'}

    sort_col = 'r.created_at'
    sort_dir = 'DESC'

    if sort_by in ALLOWED_SORT_COLUMNS:
        sort_col = ALLOWED_SORT_COLUMNS[sort_by]
    if sort_order and sort_order.upper() in ALLOWED_SORT_ORDERS:
        sort_dir = sort_order.upper()

    query += f" ORDER BY {sort_col} {sort_dir}, r.id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    routes = []
    for row in rows:
        route = dict(row)
        # Deserialize simplified_path
        if 'simplified_path' in route and route['simplified_path']:
            try:
                route['simplified_path'] = json.loads(route['simplified_path'])
            except Exception:
                route['simplified_path'] = []
        else:
            route['simplified_path'] = []
            
        # Deserialize poster_status
        if 'poster_status' in route and route['poster_status']:
            try:
                route['poster_status'] = json.loads(route['poster_status'])
            except Exception:
                route['poster_status'] = None
        else:
            route['poster_status'] = None
        # Fetch tags
        cursor.execute("""
            SELECT t.name FROM tags t
            JOIN route_tags rt ON t.id = rt.tag_id
            WHERE rt.route_id = ?
        """, (route['id'],))
        route['tags'] = [tag_row['name'] for tag_row in cursor.fetchall()]
        route['timezone_abbr'] = get_timezone_abbr(route.get('created_at'), route.get('timezone'))
        routes.append(route)
        
    conn.close()
    return routes

def get_route(route_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.*, f.name AS folder_name, u.username AS owner_username
        FROM routes r
        LEFT JOIN folders f ON r.folder_id = f.id
        LEFT JOIN users u ON r.user_id = u.id
        WHERE r.id = ?
    """, (route_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    
    route = dict(row)
    # Deserialize simplified_path
    if 'simplified_path' in route and route['simplified_path']:
        try:
            route['simplified_path'] = json.loads(route['simplified_path'])
        except Exception:
            route['simplified_path'] = []
    else:
        route['simplified_path'] = []
        
    # Deserialize poster_status
    if 'poster_status' in route and route['poster_status']:
        try:
            route['poster_status'] = json.loads(route['poster_status'])
        except Exception:
            route['poster_status'] = None
    else:
        route['poster_status'] = None
    # Fetch tags
    cursor.execute("""
        SELECT t.name FROM tags t
        JOIN route_tags rt ON t.id = rt.tag_id
        WHERE rt.route_id = ?
    """, (route_id,))
    route['tags'] = [tag_row['name'] for tag_row in cursor.fetchall()]
    route['timezone_abbr'] = get_timezone_abbr(route.get('created_at'), route.get('timezone'))
    conn.close()
    return route

def update_route(route_id, name=None, description=None, folder_id=None, tags=None, is_public=None):
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # Update details
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if is_public is not None:
            updates.append("is_public = ?")
            params.append(1 if is_public else 0)
        # We explicitly allow folder_id to be set to None/null to remove folder grouping
        if folder_id is not None:
            if folder_id == -1 or folder_id == 'null' or folder_id == '':
                updates.append("folder_id = NULL")
            else:
                updates.append("folder_id = ?")
                params.append(folder_id)
                
        if updates:
            params.append(route_id)
            cursor.execute(f"UPDATE routes SET {', '.join(updates)} WHERE id = ?", params)
            
        # Update tags if provided
        if tags is not None:
            # Clear current tags
            cursor.execute("DELETE FROM route_tags WHERE route_id = ?", (route_id,))
            for tag_name in tags:
                tag_name = tag_name.strip().lower()
                if not tag_name:
                    continue
                # Ensure tag exists
                cursor.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
                cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
                tag_id = cursor.fetchone()['id']
                # Associate
                cursor.execute("INSERT OR IGNORE INTO route_tags (route_id, tag_id) VALUES (?, ?)", (route_id, tag_id))
                
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def delete_route(route_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT file_path FROM routes WHERE id = ?", (route_id,))
        row = cursor.fetchone()
        file_path = row['file_path'] if row else None
        
        cursor.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        conn.commit()
        
        # Try to delete the physical file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Error removing route file {file_path}: {e}")
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for Folders
def create_folder(name, user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO folders (name, user_id) VALUES (?, ?)", (name.strip(), user_id))
        folder_id = cursor.lastrowid
        conn.commit()
        return folder_id
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError("Folder already exists")
    finally:
        conn.close()

def get_folders(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM folders WHERE user_id = ? ORDER BY name ASC", (user_id,))
    rows = cursor.fetchall()
    folders = [dict(row) for row in rows]
    conn.close()
    return folders

def delete_folder(folder_id, user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM folders WHERE id = ? AND user_id = ?", (folder_id, user_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for Tags
def get_all_tags(user_id=None):
    conn = get_db()
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("""
            SELECT DISTINCT t.id, t.name 
            FROM tags t
            JOIN route_tags rt ON t.id = rt.tag_id
            JOIN routes r ON rt.route_id = r.id
            WHERE r.user_id = ?
            ORDER BY t.name ASC
        """, (user_id,))
    else:
        cursor.execute("SELECT * FROM tags ORDER BY name ASC")
    rows = cursor.fetchall()
    tags = [dict(row) for row in rows]
    conn.close()
    return tags

# Helper Functions for Garmin Connections
def save_garmin_connection(user_id, email, display_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Check if there is an existing auto_sync_interval to preserve
        cursor.execute("SELECT auto_sync_interval FROM garmin_connections WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        existing_interval = row['auto_sync_interval'] if row else 'off'
        
        cursor.execute("DELETE FROM garmin_connections WHERE user_id = ?", (user_id,))
        cursor.execute("""
            INSERT INTO garmin_connections (user_id, email, display_name, auto_sync_interval)
            VALUES (?, ?, ?, ?)
        """, (user_id, email, display_name, existing_interval))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_garmin_connection(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM garmin_connections WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_garmin_connection(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM garmin_connections WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_garmin_last_sync(user_id, last_sync_time):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE garmin_connections SET last_sync = ? WHERE user_id = ?", (last_sync_time, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for Users
def add_user(username, password_hash, is_admin=0):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (username, password_hash, is_admin)
            VALUES (?, ?, ?)
        """, (username.strip(), password_hash, is_admin))
        user_id = cursor.lastrowid
        conn.commit()
        return user_id
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError("Username already exists")
    finally:
        conn.close()

def get_user_by_username(username):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username.strip(),))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_id(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, is_admin, created_at FROM users ORDER BY username ASC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def delete_user(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Delete user connection token directories on disk
        token_dir = os.path.join(DATA_DIR, 'garmin_tokens', str(user_id))
        if os.path.exists(token_dir):
            import shutil
            shutil.rmtree(token_dir, ignore_errors=True)
            
        # Delete user routes files from disk before removing from DB
        cursor.execute("SELECT file_path FROM routes WHERE user_id = ?", (user_id,))
        for row in cursor.fetchall():
            file_path = row['file_path']
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                    
        # Delete from DB
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        cursor.execute("DELETE FROM garmin_connections WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def backfill_ownerless_data(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE routes SET user_id = ? WHERE user_id IS NULL", (user_id,))
        cursor.execute("UPDATE folders SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def count_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM users")
    row = cursor.fetchone()
    conn.close()
    return row['count']

def update_user_default_map_style(user_id, default_map_style):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET default_map_style = ? WHERE id = ?", (default_map_style, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_route_poster_status(route_id, poster_status_dict):
    conn = get_db()
    cursor = conn.cursor()
    try:
        status_json = json.dumps(poster_status_dict) if poster_status_dict else None
        cursor.execute("UPDATE routes SET poster_status = ? WHERE id = ?", (status_json, route_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_all_active_garmin_connections():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM garmin_connections WHERE auto_sync_interval IS NOT NULL AND auto_sync_interval != 'off'")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_garmin_auto_sync_interval(user_id, auto_sync_interval):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE garmin_connections SET auto_sync_interval = ? WHERE user_id = ?", (auto_sync_interval, user_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def attempt_garmin_sync_lock(user_id, now_str, last_sync_str, seconds):
    conn = get_db()
    cursor = conn.cursor()
    try:
        if last_sync_str is None:
            cursor.execute("""
                UPDATE garmin_connections 
                SET last_sync = ? 
                WHERE user_id = ? AND last_sync IS NULL
            """, (now_str, user_id))
        else:
            cursor.execute("""
                UPDATE garmin_connections 
                SET last_sync = ? 
                WHERE user_id = ? AND last_sync = ?
            """, (now_str, user_id, last_sync_str))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()
