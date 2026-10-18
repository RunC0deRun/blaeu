"""
The training literature library: storing uploaded documents, indexing them in the background
and searching them for the coach.

Indexing is a background job like the history backfill: a document is extracted and chunked
once, then its chunks are embedded in batches. Progress is stored after every batch, so a
restart continues where it stopped, and a lease keeps two workers off the same document.

Search is hybrid: semantic (embedding similarity, computed by sqlite-vec over the chunks the
user may see) plus keyword (SQLite FTS5), merged with reciprocal rank fusion. Each user
searches the shared library plus their own documents, never someone else's.
"""
import hashlib
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

import sqlite_vec

import db
from coach import documents, embeddings, llm
from coach.settings import get_llm_config, is_library_configured

logger = logging.getLogger('blaeu.coach.library')

# Below the app's 50 MB request limit, leaving room for the rest of the upload form
MAX_FILE_BYTES = 40 * 1024 * 1024
LEASE_MINUTES = 10
# Embedding work per run; the background worker continues in the next run (every minute)
RUN_SECONDS = 45
MAX_ATTEMPTS = 6
RETRY_BASE_MINUTES = 2

# Search
CANDIDATES = 30
RRF_K = 60
# Passages found only semantically must be at least this similar to the query. Measured with
# bge-m3: related passages score about 0.6-0.8, unrelated ones 0.4-0.5.
MIN_SIMILARITY = 0.5
# Passages found only by keyword must still be somewhat related in meaning
MIN_KEYWORD_SIMILARITY = 0.4
# Filler words (English and Dutch) that would make the keyword search match almost anything
STOPWORDS = set('''
the and for are but not you all any can had her was one our out has his how its may new now
own see two who why did get him let put say she too use what when where which while with
this that from they them then than there their these those have been were will would could
should about after before into over under more most some such only also very just your
een het van met voor naar maar ook als dan wat wie hoe waar wanneer niet geen bij uit door
over onder deze dit die dat zijn was waren wordt worden heb hebt heeft hebben kan kun kunnen
moet moeten mijn jouw zijn haar onze hun wel nog meer veel zoals
'''.split())

STATUS_LABELS = ('pending', 'indexing', 'ready', 'failed')


def _vector_db():
    """A database connection with the sqlite-vec functions loaded."""
    conn = db.get_db()
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return conn


def library_dir():
    return os.path.join(db.DATA_DIR, 'library')


def _now():
    return datetime.now(timezone.utc)


def _fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


# --- Documents -----------------------------------------------------------------------

class UploadError(ValueError):
    pass


def visible_condition(alias='d'):
    """SQL condition (with one parameter: user id) for documents a user may see and search."""
    return f"({alias}.owner_user_id IS NULL OR {alias}.owner_user_id = ?)"


def add_document(user_id, filename, data, shared=False, title=None):
    """Stores an uploaded file and queues it for indexing. Returns the document id."""
    file_type = documents.file_type_for(filename)
    if not file_type:
        raise UploadError('Upload a PDF, EPUB, Markdown or text file.')
    if not data:
        raise UploadError('The file is empty.')
    if len(data) > MAX_FILE_BYTES:
        raise UploadError(f'The file is too large (at most {MAX_FILE_BYTES // (1024 * 1024)} MB).')
    try:
        documents.check_signature(data, file_type)
    except documents.ExtractionError as e:
        raise UploadError(str(e))

    owner = None if shared else user_id
    file_hash = hashlib.sha256(data).hexdigest()
    conn = db.get_db()
    try:
        duplicate = conn.execute(
            "SELECT title FROM library_documents WHERE file_hash = ? AND owner_user_id IS ?",
            (file_hash, owner)
        ).fetchone()
    finally:
        conn.close()
    if duplicate:
        raise UploadError(f'This file is already in the library as "{duplicate["title"]}".')

    os.makedirs(library_dir(), exist_ok=True)
    ext = os.path.splitext(filename)[1].lower()
    file_path = os.path.join(library_dir(), f"{uuid.uuid4().hex}{ext}")
    with open(file_path, 'wb') as f:
        f.write(data)

    display_title = (title or '').strip() or os.path.splitext(os.path.basename(filename))[0]
    conn = db.get_db()
    try:
        cursor = conn.execute("""
            INSERT INTO library_documents
                (owner_user_id, uploaded_by, title, filename, file_path, file_hash, file_size, file_type, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (owner, user_id, display_title[:200], os.path.basename(filename)[:255], file_path,
              file_hash, len(data), file_type))
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        if os.path.exists(file_path):
            os.remove(file_path)
        raise
    finally:
        conn.close()


def get_document(document_id):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM library_documents WHERE id = ?", (document_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_documents(user_id):
    conn = db.get_db()
    try:
        rows = conn.execute(f"""
            SELECT * FROM library_documents d WHERE {visible_condition()}
            ORDER BY d.owner_user_id IS NOT NULL, d.title COLLATE NOCASE
        """, (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def can_see(document, user_id):
    return document is not None and document['owner_user_id'] in (None, user_id)


def can_manage(document, user_id, is_admin):
    """Owners manage their own documents; admins manage the shared library."""
    if document is None:
        return False
    return document['owner_user_id'] == user_id or (document['owner_user_id'] is None and is_admin)


def _clear_chunks(conn, document_id):
    conn.execute(
        "DELETE FROM library_chunks_fts WHERE rowid IN (SELECT id FROM library_chunks WHERE document_id = ?)",
        (document_id,)
    )
    conn.execute("DELETE FROM library_chunks WHERE document_id = ?", (document_id,))


def delete_document(document_id):
    document = get_document(document_id)
    if not document:
        return False
    conn = db.get_db()
    try:
        _clear_chunks(conn, document_id)
        conn.execute("DELETE FROM library_documents WHERE id = ?", (document_id,))
        conn.commit()
    finally:
        conn.close()
    if document['file_path'] and os.path.exists(document['file_path']):
        try:
            os.remove(document['file_path'])
        except OSError:
            logger.warning(f"Could not remove library file {document['file_path']}")
    return True


def reindex_document(document_id):
    """Extracts and embeds a document again from scratch."""
    conn = db.get_db()
    try:
        _clear_chunks(conn, document_id)
        conn.execute("""
            UPDATE library_documents SET status = 'pending', error = NULL, chunk_count = 0, embedded_count = 0,
                attempts = 0, next_attempt_at = NULL, locked_until = NULL, updated_at = ?
            WHERE id = ?
        """, (_fmt(_now()), document_id))
        conn.commit()
    finally:
        conn.close()


def preview(document_id, limit=5):
    conn = db.get_db()
    try:
        rows = conn.execute(
            "SELECT seq, page, section, text FROM library_chunks WHERE document_id = ? ORDER BY seq LIMIT ?",
            (document_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Indexing ------------------------------------------------------------------------

def _update(document_id, **fields):
    fields['updated_at'] = _fmt(_now())
    conn = db.get_db()
    try:
        conn.execute(
            f"UPDATE library_documents SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), document_id)
        )
        conn.commit()
    finally:
        conn.close()


def _claim(document_id, now, lease_until):
    conn = db.get_db()
    try:
        cursor = conn.execute("""
            UPDATE library_documents SET locked_until = ?
            WHERE id = ? AND status IN ('pending', 'indexing')
              AND (locked_until IS NULL OR locked_until <= ?)
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        """, (lease_until, document_id, now, now))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _extract_and_chunk(document):
    with open(document['file_path'], 'rb') as f:
        data = f.read()
    title, segments, pages = documents.extract(data, document['file_type'])
    chunks = documents.chunk(segments)
    conn = db.get_db()
    try:
        _clear_chunks(conn, document['id'])
        for seq, c in enumerate(chunks):
            cursor = conn.execute(
                "INSERT INTO library_chunks (document_id, seq, page, section, text) VALUES (?, ?, ?, ?, ?)",
                (document['id'], seq, c['page'], (c['section'] or '')[:200] or None, c['text'])
            )
            conn.execute("INSERT INTO library_chunks_fts (rowid, text) VALUES (?, ?)", (cursor.lastrowid, c['text']))
        fields = {'status': 'indexing', 'chunk_count': len(chunks), 'embedded_count': 0, 'pages': pages}
        # Prefer the title inside the file when the user didn't choose one (the default is the file name)
        if title and document['title'] == os.path.splitext(document['filename'])[0]:
            fields['title'] = title[:200]
        conn.execute(
            f"UPDATE library_documents SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*fields.values(), document['id'])
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _embed_pending_chunks(document_id, config, deadline):
    """Embeds chunks that have no vector (from the current model) yet, batch by batch."""
    model = config['embedding_model']
    while datetime.now(timezone.utc) < deadline:
        conn = db.get_db()
        try:
            rows = conn.execute("""
                SELECT id, text FROM library_chunks
                WHERE document_id = ? AND (embedding IS NULL OR embedding_model IS NOT ?)
                ORDER BY seq LIMIT ?
            """, (document_id, model, embeddings.BATCH_SIZE)).fetchall()
        finally:
            conn.close()
        if not rows:
            return True
        vectors = embeddings.embed(config, [r['text'] for r in rows])
        conn = db.get_db()
        try:
            conn.executemany(
                "UPDATE library_chunks SET embedding = ?, embedding_model = ? WHERE id = ?",
                [(embeddings.to_blob(v), model, r['id']) for r, v in zip(rows, vectors)]
            )
            done = conn.execute(
                "SELECT COUNT(*) FROM library_chunks WHERE document_id = ? AND embedding_model = ?",
                (document_id, model)
            ).fetchone()[0]
            conn.execute("UPDATE library_documents SET embedded_count = ?, updated_at = ? WHERE id = ?",
                         (done, _fmt(_now()), document_id))
            conn.commit()
        finally:
            conn.close()
    return False


def _mark_stale_documents(model):
    """Documents embedded with another model must be embedded again (e.g. after a model change)."""
    conn = db.get_db()
    try:
        conn.execute("""
            UPDATE library_documents SET status = 'indexing', embedded_count = 0
            WHERE status = 'ready' AND EXISTS (
                SELECT 1 FROM library_chunks c WHERE c.document_id = library_documents.id
                  AND (c.embedding IS NULL OR c.embedding_model IS NOT ?)
            )
        """, (model,))
        conn.commit()
    finally:
        conn.close()


def index_document(document_id, config, deadline):
    """Advances one claimed document: extraction (once), then embedding until the deadline."""
    document = get_document(document_id)
    if not document:
        return
    try:
        if document['status'] == 'pending':
            _extract_and_chunk(document)
        finished = _embed_pending_chunks(document_id, config, deadline)
        if finished:
            _update(document_id, status='ready', attempts=0, error=None, locked_until=None)
            logger.info(f"Library document {document_id} indexed")
        else:
            _update(document_id, locked_until=None)
    except documents.ExtractionError as e:
        _update(document_id, status='failed', error=str(e), locked_until=None)
    except (llm.LLMError, OSError) as e:
        attempts = document['attempts'] + 1
        if attempts >= MAX_ATTEMPTS:
            _update(document_id, status='failed', attempts=attempts, locked_until=None,
                    error=f'Indexing failed repeatedly: {e}')
        else:
            delay = RETRY_BASE_MINUTES * (2 ** (attempts - 1))
            logger.warning(f"Library document {document_id}: retrying in {delay} min ({e})")
            _update(document_id, attempts=attempts, error=f'Retrying after a temporary problem: {e}',
                    next_attempt_at=_fmt(_now() + timedelta(minutes=delay)), locked_until=None)
    except Exception as e:
        logger.exception(f"Unexpected error indexing library document {document_id}")
        _update(document_id, status='failed', error=f'Unexpected error: {e}', locked_until=None)


_run_lock = threading.Lock()


def run_pending_indexing():
    """Called from the background worker (and after an upload): indexes waiting documents."""
    config = get_llm_config()
    if not is_library_configured(config):
        return
    if not _run_lock.acquire(blocking=False):
        return  # this process is already indexing
    try:
        _mark_stale_documents(config['embedding_model'])
        deadline = _now() + timedelta(seconds=RUN_SECONDS)
        conn = db.get_db()
        try:
            ids = [r['id'] for r in conn.execute(
                "SELECT id FROM library_documents WHERE status IN ('pending', 'indexing') ORDER BY id"
            ).fetchall()]
        finally:
            conn.close()
        for document_id in ids:
            now = _now()
            if now >= deadline:
                break
            if _claim(document_id, _fmt(now), _fmt(now + timedelta(minutes=LEASE_MINUTES))):
                index_document(document_id, config, deadline)
    finally:
        _run_lock.release()


def start_indexing_soon(app):
    """Starts indexing in the background right after an upload, instead of waiting for the worker."""
    def run():
        with app.app_context():
            try:
                run_pending_indexing()
            except Exception:
                logger.exception("Background library indexing failed")
    threading.Thread(target=run, name='library-indexing', daemon=True).start()


# --- Search --------------------------------------------------------------------------

def _fts_query(query):
    """Turns free text into a safe FTS5 query: the words, any of which may match."""
    words = [w for w in re.findall(r'\w+', query.lower()) if len(w) >= 3 and w not in STOPWORDS]
    return ' OR '.join(f'"{w}"' for w in words[:20])


def search(user_id, query, config=None, limit=5):
    """
    Returns up to `limit` passages for a query, best first, from the documents the user may
    see: [{'chunk_id', 'document_id', 'title', 'page', 'section', 'text', 'similarity', 'score'}].
    """
    config = config or get_llm_config()
    if not query or not query.strip() or not is_library_configured(config):
        return []
    model = config['embedding_model']
    query_blob = embeddings.to_blob(embeddings.embed(config, [query.strip()])[0])

    conn = _vector_db()
    try:
        similarity_sql = "1 - vec_distance_cosine(c.embedding, ?)"
        rows = conn.execute(f"""
            SELECT c.id, {similarity_sql} AS similarity FROM library_chunks c
            JOIN library_documents d ON d.id = c.document_id
            WHERE {visible_condition()} AND c.embedding_model = ? AND c.embedding IS NOT NULL
            ORDER BY similarity DESC LIMIT ?
        """, (query_blob, user_id, model, CANDIDATES)).fetchall()
        similarities = {r['id']: r['similarity'] for r in rows}
        semantic = [r['id'] for r in rows if r['similarity'] >= MIN_SIMILARITY]

        keyword = []
        fts = _fts_query(query)
        if fts:
            keyword = [r['id'] for r in conn.execute(f"""
                SELECT c.id FROM library_chunks_fts f
                JOIN library_chunks c ON c.id = f.rowid
                JOIN library_documents d ON d.id = c.document_id
                WHERE library_chunks_fts MATCH ? AND {visible_condition()}
                ORDER BY bm25(library_chunks_fts) LIMIT ?
            """, (fts, user_id, CANDIDATES)).fetchall()]
            # Keyword matches outside the semantic top must still be somewhat related in meaning
            missing = [i for i in keyword if i not in similarities]
            if missing:
                similarities.update({r['id']: r['similarity'] for r in conn.execute(f"""
                    SELECT c.id, {similarity_sql} AS similarity FROM library_chunks c
                    WHERE c.embedding_model = ? AND c.embedding IS NOT NULL
                      AND c.id IN ({', '.join('?' for _ in missing)})
                """, (query_blob, model, *missing)).fetchall()})
            keyword = [i for i in keyword if similarities.get(i, 0.0) >= MIN_KEYWORD_SIMILARITY]

        # Reciprocal rank fusion: passages ranked well by both methods come first
        fused = {}
        for ranking in (semantic, keyword):
            for rank, chunk_id in enumerate(ranking):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        best = sorted(fused, key=fused.get, reverse=True)[:limit]
        if not best:
            return []
        details = {r['chunk_id']: dict(r) for r in conn.execute(f"""
            SELECT c.id AS chunk_id, c.document_id, c.page, c.section, c.text, d.title
            FROM library_chunks c JOIN library_documents d ON d.id = c.document_id
            WHERE c.id IN ({', '.join('?' for _ in best)})
        """, best).fetchall()}
    finally:
        conn.close()
    results = []
    for chunk_id in best:
        item = details[chunk_id]
        item['similarity'] = round(similarities.get(chunk_id, 0.0), 3)
        item['score'] = round(fused[chunk_id], 5)
        results.append(item)
    return results
