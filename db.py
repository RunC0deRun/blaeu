import os
import sqlite3
import json
import logging

logger = logging.getLogger('blaeu.db')
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

def add_column_if_missing(cursor, table_name, column_name, column_def):
    cursor.execute(f"PRAGMA table_info({table_name});")
    columns = [row['name'] for row in cursor.fetchall()]
    if column_name not in columns:
        try:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def};")
            logger.info(f"Database migration: Added column {column_name} to table {table_name}")
        except sqlite3.OperationalError as e:
            logger.error(f"Failed to add column {column_name} to table {table_name}: {e}")

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
        default_map_style TEXT DEFAULT 'dark',
        week_start_day TEXT DEFAULT 'Monday',
        default_aspect_ratio TEXT DEFAULT '16:9'
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

    # Ensure columns exist (migration for existing DBs)
    add_column_if_missing(cursor, 'users', 'default_map_style', "TEXT DEFAULT 'dark'")
    add_column_if_missing(cursor, 'users', 'week_start_day', "TEXT DEFAULT 'Monday'")
    add_column_if_missing(cursor, 'users', 'default_aspect_ratio', "TEXT DEFAULT '16:9'")
    add_column_if_missing(cursor, 'routes', 'timezone', "TEXT")
    add_column_if_missing(cursor, 'routes', 'simplified_path', "TEXT")
    add_column_if_missing(cursor, 'routes', 'poster_status', "TEXT")
    add_column_if_missing(cursor, 'routes', 'user_id', "INTEGER REFERENCES users(id) ON DELETE CASCADE")
    add_column_if_missing(cursor, 'routes', 'is_public', "INTEGER DEFAULT 0")


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
            logger.error(f"Error migrating folders table: {e}", exc_info=True)
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
                logger.info("Successfully repaired routes table foreign key reference to folders")
            except Exception as e:
                logger.error(f"Error repairing routes table: {e}", exc_info=True)
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
    add_column_if_missing(cursor, 'garmin_connections', 'auto_sync_interval', "TEXT DEFAULT 'off'")

    # Create Intervals.icu Connections Table (supporting multi-user readiness via user_id)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS intervals_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER DEFAULT 1,
        athlete_id TEXT NOT NULL DEFAULT '0',
        api_key TEXT NOT NULL,
        last_sync TEXT,
        auto_sync_interval TEXT DEFAULT 'off',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Wellness import is opt-in: NULL = not asked yet, 0 = declined, 1 = accepted
    add_column_if_missing(cursor, 'users', 'wellness_opt_in', "INTEGER")
    # When the user answered the training-data prompt (history import / wellness)
    add_column_if_missing(cursor, 'users', 'data_prompt_answered_at', "TEXT")

    # Create Activities Table: the training log. Every workout gets a row, with or
    # without GPS; workouts with a map link to their route via route_id.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        route_id INTEGER,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        name TEXT,
        sport_type TEXT,
        start_time TEXT,
        start_time_local TEXT,
        timezone TEXT,
        duration REAL,
        moving_time REAL,
        distance REAL,
        elevation_gain REAL,
        avg_hr REAL,
        max_hr REAL,
        avg_power REAL,
        normalized_power REAL,
        avg_cadence REAL,
        calories REAL,
        training_load REAL,
        intensity REAL,
        trimp REAL,
        aerobic_te REAL,
        anaerobic_te REAL,
        rpe REAL,
        feel INTEGER,
        hr_zone_times TEXT,
        power_zone_times TEXT,
        pace_zone_times TEXT,
        has_gps INTEGER DEFAULT 0,
        is_indoor INTEGER DEFAULT 0,
        hidden INTEGER DEFAULT 0,
        merged_into INTEGER,
        raw_summary TEXT,
        enriched_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE SET NULL,
        UNIQUE(user_id, source, source_id)
    );
    """)

    # Create Daily Wellness Table: one row per user, date and source; merged on read
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_wellness (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        source TEXT NOT NULL,
        resting_hr REAL,
        hrv REAL,
        hrv_sdnn REAL,
        sleep_secs REAL,
        sleep_score REAL,
        sleep_quality REAL,
        weight REAL,
        body_fat REAL,
        readiness REAL,
        body_battery_high REAL,
        body_battery_low REAL,
        stress_avg REAL,
        spo2 REAL,
        respiration REAL,
        steps INTEGER,
        vo2max REAL,
        ctl REAL,
        atl REAL,
        ramp_rate REAL,
        soreness REAL,
        fatigue REAL,
        mood REAL,
        motivation REAL,
        raw TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        UNIQUE(user_id, date, source)
    );
    """)

    add_column_if_missing(cursor, 'daily_wellness', 'vo2max', "REAL")
    # The same workout imported from two sources: the less complete row points at the other
    add_column_if_missing(cursor, 'activities', 'merged_into', "INTEGER")

    # Create Sync Jobs Table: long-running, resumable imports such as the history backfill.
    # Work is done in small steps; cursor marks how far the job got, so a restart resumes.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sync_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        range_start TEXT NOT NULL,
        range_end TEXT NOT NULL,
        cursor TEXT,
        windows_total INTEGER DEFAULT 0,
        windows_done INTEGER DEFAULT 0,
        activities_new INTEGER DEFAULT 0,
        activities_enriched INTEGER DEFAULT 0,
        activities_skipped INTEGER DEFAULT 0,
        duplicates_merged INTEGER DEFAULT 0,
        wellness_days INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        last_error TEXT,
        next_attempt_at TEXT,
        locked_until TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status);")

    # Create App Settings Table: instance-wide key/value settings managed by admins (e.g. the LLM endpoint)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Create Coach Profiles Table: the athlete's goals and context for the AI coach
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS coach_profiles (
        user_id INTEGER PRIMARY KEY,
        goals TEXT,
        target_events TEXT,
        availability TEXT,
        injuries TEXT,
        notes TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Create Chat Tables: coach conversations and their messages
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        reasoning TEXT,
        model TEXT,
        status TEXT DEFAULT 'complete',
        sources TEXT,
        steps TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
    );
    """)
    # Library passages an answer cited, and the searches made for it (JSON)
    add_column_if_missing(cursor, 'chat_messages', 'sources', "TEXT")
    add_column_if_missing(cursor, 'chat_messages', 'steps', "TEXT")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_conversations_user ON chat_conversations(user_id, updated_at);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id);")

    # Create Library Tables: training literature for the AI coach. A document without an
    # owner belongs to the shared library; otherwise only its owner can see and search it.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS library_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER,
        uploaded_by INTEGER,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_hash TEXT NOT NULL,
        file_size INTEGER,
        file_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT,
        pages INTEGER,
        chunk_count INTEGER DEFAULT 0,
        embedded_count INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        next_attempt_at TEXT,
        locked_until TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
    );
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS library_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        page INTEGER,
        section TEXT,
        text TEXT NOT NULL,
        embedding BLOB,
        embedding_model TEXT,
        FOREIGN KEY (document_id) REFERENCES library_documents(id) ON DELETE CASCADE
    );
    """)
    # Keyword index over the chunk texts; rowid is the chunk id
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS library_chunks_fts USING fts5(
        text, tokenize = 'unicode61 remove_diacritics 2'
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_documents_owner ON library_documents(owner_user_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_chunks_document ON library_chunks(document_id, seq);")

    # Create Athlete Thresholds Table: per sport and source (e.g. Run/Ride from Intervals.icu)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS athlete_thresholds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        sport TEXT NOT NULL,
        source TEXT NOT NULL,
        ftp REAL,
        lthr REAL,
        max_hr REAL,
        resting_hr REAL,
        threshold_pace REAL,
        hr_zones TEXT,
        power_zones TEXT,
        pace_zones TEXT,
        raw TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        UNIQUE(user_id, sport, source)
    );
    """)

    # Create Indexes for Query Optimization
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_routes_user_id ON routes(user_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_routes_folder_id ON routes(folder_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_routes_created_at ON routes(created_at);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_folders_user_id ON folders(user_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_route_tags_tag_id ON route_tags(tag_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_user_start ON activities(user_id, start_time);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_activities_route_id ON activities(route_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_wellness_user_date ON daily_wellness(user_id, date);")

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
                    logger.error(f"Error backfilling route {route_id}: {e}", exc_info=True)

    # Give every existing route a training-log entry. Rows created here have no
    # raw_summary yet; the enrichment job fills in source metrics later.
    cursor.execute("""
        SELECT r.* FROM routes r
        LEFT JOIN activities a ON a.route_id = r.id
        WHERE a.id IS NULL AND r.user_id IS NOT NULL
    """)
    for route in [dict(row) for row in cursor.fetchall()]:
        try:
            _insert_route_activity(cursor, route)
        except sqlite3.Error as e:
            logger.error(f"Error creating activity for route {route['id']}: {e}", exc_info=True)

    conn.commit()
    conn.close()

# Helper Functions for Activities
def route_activity_identity(filename, file_hash):
    """Derives (source, source_id) for a route from its stored filename."""
    filename = filename or ''
    for source in ('garmin', 'intervals'):
        prefix = f"{source}_"
        if filename.startswith(prefix):
            source_id = filename[len(prefix):].split('_')[0].split('.')[0]
            if source_id:
                return source, source_id
    return 'upload', file_hash

def _insert_route_activity(cursor, route):
    """Creates a minimal activity row for a route (no-op if one already exists)."""
    source, source_id = route_activity_identity(route.get('filename'), route.get('file_hash'))
    cursor.execute("""
        INSERT INTO activities (
            user_id, route_id, source, source_id, name, start_time, timezone,
            duration, distance, elevation_gain, has_gps
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id, source, source_id) DO UPDATE SET
            route_id = COALESCE(activities.route_id, excluded.route_id),
            has_gps = 1
    """, (
        route['user_id'], route['id'], source, source_id, route.get('name'),
        route.get('created_at'), route.get('timezone'), route.get('duration'),
        route.get('total_distance'), route.get('elevation_gain')
    ))

ACTIVITY_FIELDS = (
    'name', 'sport_type', 'start_time', 'start_time_local', 'timezone',
    'duration', 'moving_time', 'distance', 'elevation_gain',
    'avg_hr', 'max_hr', 'avg_power', 'normalized_power', 'avg_cadence', 'calories',
    'training_load', 'intensity', 'trimp', 'aerobic_te', 'anaerobic_te', 'rpe', 'feel',
    'hr_zone_times', 'power_zone_times', 'pace_zone_times', 'is_indoor', 'raw_summary'
)

def upsert_activity(user_id, source, source_id, fields, route_id=None):
    """
    Inserts or updates an activity by (user_id, source, source_id) and returns its id.
    Fields that are None never overwrite stored values, so a partial summary cannot
    erase data from an earlier, richer one. Zone lists are stored as JSON.
    """
    values = {k: fields.get(k) for k in ACTIVITY_FIELDS}
    for key in ('hr_zone_times', 'power_zone_times', 'pace_zone_times', 'raw_summary'):
        if values[key] is not None and not isinstance(values[key], str):
            values[key] = json.dumps(values[key])
    if values['is_indoor'] is not None:
        values['is_indoor'] = 1 if values['is_indoor'] else 0
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    enriched_at = now if values['raw_summary'] is not None else None

    columns = ', '.join(ACTIVITY_FIELDS)
    placeholders = ', '.join('?' for _ in ACTIVITY_FIELDS)
    updates = ',\n'.join(f"{k} = COALESCE(excluded.{k}, activities.{k})" for k in ACTIVITY_FIELDS)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT INTO activities (
                user_id, source, source_id, route_id, has_gps, enriched_at, updated_at, {columns}
            ) VALUES (?, ?, ?, ?, ?, ?, ?, {placeholders})
            ON CONFLICT(user_id, source, source_id) DO UPDATE SET
                route_id = COALESCE(excluded.route_id, activities.route_id),
                has_gps = MAX(excluded.has_gps, activities.has_gps),
                enriched_at = COALESCE(excluded.enriched_at, activities.enriched_at),
                updated_at = excluded.updated_at,
                {updates}
        """, (
            user_id, source, str(source_id), route_id, 1 if route_id else 0, enriched_at, now,
            *[values[k] for k in ACTIVITY_FIELDS]
        ))
        cursor.execute(
            "SELECT id FROM activities WHERE user_id = ? AND source = ? AND source_id = ?",
            (user_id, source, str(source_id))
        )
        activity_id = cursor.fetchone()['id']
        conn.commit()
        return activity_id
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_activity_by_source(user_id, source, source_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM activities WHERE user_id = ? AND source = ? AND source_id = ?",
        (user_id, source, str(source_id))
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_activity_source_ids(user_id, source):
    """Returns the source ids already stored for a user, including hidden ones."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT source_id FROM activities WHERE user_id = ? AND source = ?", (user_id, source))
    ids = {row['source_id'] for row in cursor.fetchall()}
    conn.close()
    return ids

def set_activity_hidden(activity_id, hidden):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE activities SET hidden = ? WHERE id = ?", (1 if hidden else 0, activity_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_activity(activity_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM activities WHERE id = ?", (activity_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_activity(activity_id):
    """
    Removes an activity from the training log. Uploaded activities are deleted; synced
    ones are kept as hidden tombstones so the next sync or backfill does not import them
    again (the same convention as delete_route).
    """
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM activities WHERE id = ? AND source = 'upload'", (activity_id,))
        cursor.execute("UPDATE activities SET hidden = 1 WHERE id = ?", (activity_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_latest_activity_start(user_id, source):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(start_time) AS latest FROM activities WHERE user_id = ? AND source = ?",
        (user_id, source)
    )
    row = cursor.fetchone()
    conn.close()
    return row['latest'] if row else None

def get_activities(user_id, start=None, end=None, include_hidden=False):
    """Returns a user's activities ordered by start time (newest first)."""
    query = "SELECT * FROM activities WHERE user_id = ?"
    params = [user_id]
    if not include_hidden:
        query += " AND hidden = 0 AND merged_into IS NULL"
    if start:
        query += " AND start_time >= ?"
        params.append(start)
    if end:
        query += " AND start_time < ?"
        params.append(end)
    query += " ORDER BY start_time DESC"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def mark_user_data_prompt_answered(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET data_prompt_answered_at = ? WHERE id = ?",
            (datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S'), user_id)
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_user_wellness_opt_in(user_id):
    user = get_user_by_id(user_id)
    return user.get('wellness_opt_in') if user else None

def update_user_wellness_opt_in(user_id, opt_in):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET wellness_opt_in = ? WHERE id = ?", (1 if opt_in else 0, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

WELLNESS_FIELDS = (
    'resting_hr', 'hrv', 'hrv_sdnn', 'sleep_secs', 'sleep_score', 'sleep_quality',
    'weight', 'body_fat', 'readiness', 'body_battery_high', 'body_battery_low',
    'stress_avg', 'spo2', 'respiration', 'steps', 'vo2max', 'ctl', 'atl', 'ramp_rate',
    'soreness', 'fatigue', 'mood', 'motivation'
)

def upsert_daily_wellness(user_id, date, source, fields, raw=None):
    columns = ', '.join(WELLNESS_FIELDS)
    placeholders = ', '.join('?' for _ in WELLNESS_FIELDS)
    updates = ',\n'.join(f"{k} = COALESCE(excluded.{k}, daily_wellness.{k})" for k in WELLNESS_FIELDS)
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT INTO daily_wellness (user_id, date, source, raw, updated_at, {columns})
            VALUES (?, ?, ?, ?, ?, {placeholders})
            ON CONFLICT(user_id, date, source) DO UPDATE SET
                raw = COALESCE(excluded.raw, daily_wellness.raw),
                updated_at = excluded.updated_at,
                {updates}
        """, (
            user_id, date, source, json.dumps(raw) if raw is not None else None, now,
            *[fields.get(k) for k in WELLNESS_FIELDS]
        ))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_daily_wellness(user_id, start=None, end=None):
    query = "SELECT * FROM daily_wellness WHERE user_id = ?"
    params = [user_id]
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    query += " ORDER BY date DESC, source"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

THRESHOLD_FIELDS = ('ftp', 'lthr', 'max_hr', 'resting_hr', 'threshold_pace', 'hr_zones', 'power_zones', 'pace_zones')

def upsert_athlete_thresholds(user_id, sport, source, fields, raw=None):
    values = {k: fields.get(k) for k in THRESHOLD_FIELDS}
    for key in ('hr_zones', 'power_zones', 'pace_zones'):
        if values[key] is not None and not isinstance(values[key], str):
            values[key] = json.dumps(values[key])
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Thresholds are a snapshot: the latest values replace the old ones entirely
        cursor.execute(f"""
            INSERT OR REPLACE INTO athlete_thresholds (user_id, sport, source, raw, updated_at, {', '.join(THRESHOLD_FIELDS)})
            VALUES (?, ?, ?, ?, ?, {', '.join('?' for _ in THRESHOLD_FIELDS)})
        """, (
            user_id, sport, source, json.dumps(raw) if raw is not None else None, now,
            *[values[k] for k in THRESHOLD_FIELDS]
        ))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_athlete_thresholds(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM athlete_thresholds WHERE user_id = ? ORDER BY sport, source", (user_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

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
        if route_metadata.get('user_id') is not None:
            _insert_route_activity(cursor, {
                'id': route_id,
                'user_id': route_metadata['user_id'],
                'filename': route_metadata['filename'],
                'file_hash': route_metadata['file_hash'],
                'name': route_metadata['name'],
                'created_at': created_at,
                'timezone': timezone,
                'duration': stats['duration'],
                'total_distance': stats['total_distance'],
                'elevation_gain': stats['elevation_gain'],
            })
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

        # Uploaded activities go with their route. Synced ones are kept as hidden
        # tombstones so the next sync or backfill does not import them again.
        cursor.execute("DELETE FROM activities WHERE route_id = ? AND source = 'upload'", (route_id,))
        cursor.execute("UPDATE activities SET hidden = 1, has_gps = 0 WHERE route_id = ?", (route_id,))
        cursor.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        conn.commit()
        
        # Try to delete the physical file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Error removing route file {file_path}: {e}")
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
        token_dir = os.path.join(DATA_DIR, 'garmin_tokens', str(int(user_id)))
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
                    
        # Delete the user's personal library: files, keyword index and (by cascade) chunks
        cursor.execute("SELECT id, file_path FROM library_documents WHERE owner_user_id = ?", (user_id,))
        for row in cursor.fetchall():
            cursor.execute(
                "DELETE FROM library_chunks_fts WHERE rowid IN (SELECT id FROM library_chunks WHERE document_id = ?)",
                (row['id'],)
            )
            if row['file_path'] and os.path.exists(row['file_path']):
                try:
                    os.remove(row['file_path'])
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

def update_user_week_start_day(user_id, week_start_day):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET week_start_day = ? WHERE id = ?", (week_start_day, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_user_default_aspect_ratio(user_id, default_aspect_ratio):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET default_aspect_ratio = ? WHERE id = ?", (default_aspect_ratio, user_id))
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

# Helper Functions for Intervals.icu Connections
def save_intervals_connection(user_id, athlete_id, api_key):
    conn = get_db()
    cursor = conn.cursor()
    try:
        # Check if there is an existing auto_sync_interval to preserve
        cursor.execute("SELECT auto_sync_interval FROM intervals_connections WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        existing_interval = row['auto_sync_interval'] if row else 'off'
        
        cursor.execute("DELETE FROM intervals_connections WHERE user_id = ?", (user_id,))
        cursor.execute("""
            INSERT INTO intervals_connections (user_id, athlete_id, api_key, auto_sync_interval)
            VALUES (?, ?, ?, ?)
        """, (user_id, athlete_id, api_key, existing_interval))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_intervals_connection(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM intervals_connections WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_intervals_connection(user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM intervals_connections WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def update_intervals_last_sync(user_id, last_sync_time):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE intervals_connections SET last_sync = ? WHERE user_id = ?", (last_sync_time, user_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_all_active_intervals_connections():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM intervals_connections WHERE auto_sync_interval IS NOT NULL AND auto_sync_interval != 'off'")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_intervals_auto_sync_interval(user_id, auto_sync_interval):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE intervals_connections SET auto_sync_interval = ? WHERE user_id = ?", (auto_sync_interval, user_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def attempt_intervals_sync_lock(user_id, now_str, last_sync_str, seconds):
    conn = get_db()
    cursor = conn.cursor()
    try:
        if last_sync_str is None:
            cursor.execute("""
                UPDATE intervals_connections 
                SET last_sync = ? 
                WHERE user_id = ? AND last_sync IS NULL
            """, (now_str, user_id))
        else:
            cursor.execute("""
                UPDATE intervals_connections 
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



def find_duplicate_activities(user_id, source, start_time, duration=None, tolerance_secs=120):
    """
    Finds visible activities from other sources that are the same workout: started within
    tolerance_secs and, when both durations are known, lasting within 10% of each other.
    """
    if not start_time:
        return []
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM activities
        WHERE user_id = ? AND source != ? AND hidden = 0 AND merged_into IS NULL
          AND start_time IS NOT NULL
          AND ABS(strftime('%s', start_time) - strftime('%s', ?)) <= ?
    """, (user_id, source, start_time, tolerance_secs))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    matches = []
    for row in rows:
        if duration and row.get('duration'):
            longest = max(duration, row['duration'])
            if abs(duration - row['duration']) > max(0.1 * longest, tolerance_secs):
                continue
        matches.append(row)
    return matches

def merge_activities(duplicate_ids, primary_id):
    """Marks activities as duplicates of primary_id; they stay stored but are not shown."""
    if not duplicate_ids:
        return
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.executemany(
            "UPDATE activities SET merged_into = ? WHERE id = ? AND id != ?",
            [(primary_id, dup_id, primary_id) for dup_id in duplicate_ids]
        )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for Sync Jobs
SYNC_JOB_ACTIVE_STATUSES = ('pending', 'running', 'paused')
SYNC_JOB_UPDATABLE = {
    'status', 'cursor', 'windows_done', 'activities_new', 'activities_enriched', 'activities_skipped',
    'duplicates_merged', 'wellness_days', 'attempts', 'last_error', 'next_attempt_at',
    'locked_until', 'completed_at',
}

def create_sync_job(user_id, source, kind, range_start, range_end, windows_total):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO sync_jobs (user_id, source, kind, status, range_start, range_end, cursor, windows_total)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
        """, (user_id, source, kind, range_start, range_end, range_end, windows_total))
        job_id = cursor.lastrowid
        conn.commit()
        return job_id
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_sync_job(job_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_latest_sync_job(user_id, source, kind):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM sync_jobs WHERE user_id = ? AND source = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (user_id, source, kind)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_runnable_sync_jobs(now_str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM sync_jobs
        WHERE status IN ('pending', 'running')
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
          AND (locked_until IS NULL OR locked_until <= ?)
        ORDER BY id
    """, (now_str, now_str))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def claim_sync_job(job_id, now_str, lease_until_str):
    """Atomically leases a job so only one process (gunicorn worker) works on it."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE sync_jobs SET locked_until = ?, status = 'running', updated_at = ?
            WHERE id = ? AND status IN ('pending', 'running')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (locked_until IS NULL OR locked_until <= ?)
        """, (lease_until_str, now_str, job_id, now_str, now_str))
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()

def update_sync_job(job_id, **fields):
    unknown = set(fields) - SYNC_JOB_UPDATABLE
    if unknown:
        raise ValueError(f"Unknown sync job fields: {sorted(unknown)}")
    fields['updated_at'] = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    assignments = ', '.join(f"{k} = ?" for k in fields)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"UPDATE sync_jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


# Helper Functions for App Settings
def get_app_settings(prefix=''):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM app_settings WHERE key LIKE ?", (f"{prefix}%",))
    rows = {row['key']: row['value'] for row in cursor.fetchall()}
    conn.close()
    return rows

def set_app_settings(values):
    """Stores settings; a value of None removes the key."""
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        for key, value in values.items():
            if value is None:
                cursor.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, str(value), now)
                )
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for the Coach Profile
COACH_PROFILE_FIELDS = ('goals', 'target_events', 'availability', 'injuries', 'notes')

def get_coach_profile(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM coach_profiles WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {'user_id': user_id, **dict.fromkeys(COACH_PROFILE_FIELDS), 'updated_at': None}

def save_coach_profile(user_id, fields):
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    values = [fields.get(k) for k in COACH_PROFILE_FIELDS]
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT OR REPLACE INTO coach_profiles (user_id, {', '.join(COACH_PROFILE_FIELDS)}, updated_at)
            VALUES (?, {', '.join('?' for _ in COACH_PROFILE_FIELDS)}, ?)
        """, (user_id, *values, now))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# Helper Functions for Coach Conversations
def create_conversation(user_id, title):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO chat_conversations (user_id, title) VALUES (?, ?)", (user_id, title))
        conversation_id = cursor.lastrowid
        conn.commit()
        return conversation_id
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_conversation(conversation_id, user_id):
    """Returns the conversation only if it belongs to user_id."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM chat_conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_conversations(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM chat_conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC", (user_id,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def delete_conversation(conversation_id, user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM chat_conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def get_chat_messages(conversation_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def add_chat_message(conversation_id, role, content, reasoning=None, model=None, status='complete',
                     sources=None, steps=None):
    now = datetime.now(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO chat_messages (conversation_id, role, content, reasoning, model, status, sources, steps, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (conversation_id, role, content, reasoning, model, status,
              json.dumps(sources) if sources else None, json.dumps(steps) if steps else None, now))
        message_id = cursor.lastrowid
        cursor.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        conn.commit()
        return message_id
    except sqlite3.Error as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
