import io
import os
import re
import zipfile
from unittest.mock import patch

import math
import pytest

import db
from coach import documents, embeddings, library, llm
from coach import settings as coach_settings


# --- Test files ----------------------------------------------------------------------

def make_pdf(pages):
    """A minimal valid PDF with one text line per page."""
    objects = ['<< /Type /Catalog /Pages 2 0 R >>', None,
               '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b'%PDF-1.4\n')
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n{body}\nendobj\n".encode('latin-1'))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_epub(title, chapters):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''')
        items = ''.join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(len(chapters)))
        spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        z.writestr('OEBPS/content.opf', f'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>
  <manifest>{items}</manifest><spine>{spine}</spine>
</package>''')
        for i, (heading, body) in enumerate(chapters):
            z.writestr(f'OEBPS/c{i}.xhtml', f'''<html><head><title>x</title><style>p {{}}</style></head>
<body><h1>{heading}</h1><p>{body}</p><script>alert(1)</script></body></html>''')
    return buffer.getvalue()


LONG_TEXT = ' '.join(['Easy runs build the aerobic base and should feel conversational.'] * 6)


# --- Fake embeddings -----------------------------------------------------------------

def fake_embed(config, texts):
    """Bag of words hashed into 64 dimensions: texts sharing words are similar."""
    vectors = []
    for text in texts:
        vector = [0.0] * 64
        for word in re.findall(r'\w+', text.lower()):
            vector[sum(map(ord, word)) % 64] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        vectors.append([x / norm for x in vector])
    return vectors


@pytest.fixture
def user_id(authed_client):
    return db.get_user_by_username('test_user')['id']


@pytest.fixture
def configured(user_id, monkeypatch):
    coach_settings.save_llm_settings({'base_url': 'http://ollama:11434/v1', 'embedding_model': 'fake-embed'})
    monkeypatch.setattr(embeddings, 'embed', fake_embed)
    monkeypatch.setattr(library, 'start_indexing_soon', lambda app: None)
    return coach_settings.get_llm_config()


def _second_user():
    from werkzeug.security import generate_password_hash
    return db.add_user('other_user', generate_password_hash('Password123', method='pbkdf2:sha256'), 0)


def _md(text):
    return text.encode()


# --- Extraction ----------------------------------------------------------------------

def test_extract_markdown_sections_and_title():
    title, segments, pages = documents.extract(
        _md(f"# Running Basics\n\n{LONG_TEXT}\n\n## Recovery\n\n{LONG_TEXT}"), 'markdown')
    assert title == 'Running Basics' and pages is None
    assert [s['section'] for s in segments] == ['Running Basics', 'Recovery']


def test_extract_pdf_keeps_page_numbers():
    data = make_pdf([f'Page one about easy runs. {LONG_TEXT[:150]}', f'Page two about intervals. {LONG_TEXT[:150]}'])
    title, segments, pages = documents.extract(data, 'pdf')
    assert pages == 2
    assert [s['page'] for s in segments] == [1, 2]
    assert 'intervals' in segments[1]['text']


def test_extract_epub_follows_spine_and_skips_scripts():
    data = make_epub('Training Book', [('Base Building', LONG_TEXT), ('Tapering', LONG_TEXT)])
    title, segments, _ = documents.extract(data, 'epub')
    assert title == 'Training Book'
    assert [s['section'] for s in segments] == ['Base Building', 'Tapering']
    assert 'alert' not in ' '.join(s['text'] for s in segments)


def test_extract_rejects_mismatched_and_empty_files():
    with pytest.raises(documents.ExtractionError, match='not a valid PDF'):
        documents.extract(b'hello', 'pdf')
    with pytest.raises(documents.ExtractionError, match='No readable text'):
        documents.extract(make_pdf(['']), 'pdf')
    with pytest.raises(documents.ExtractionError, match='text file'):
        documents.extract(b'\x00\x01binary', 'text')


def test_chunks_have_target_size_overlap_and_respect_chapters():
    paragraphs = '\n\n'.join(f'Paragraph {i}. ' + 'Tempo runs improve lactate threshold. ' * 10 for i in range(10))
    segments = [{'text': paragraphs, 'page': 1, 'section': 'Tempo'},
                {'text': 'Rest days matter. ' * 30, 'page': 9, 'section': 'Recovery'}]
    chunks = documents.chunk(segments)
    tempo = [c for c in chunks if c['section'] == 'Tempo']
    assert len(tempo) > 1
    assert all(len(c['text']) <= documents.MAX_CHARS for c in chunks)
    # The start of each following chunk repeats the end of the previous one
    assert tempo[0]['text'][-60:].split()[-1] in tempo[1]['text'][:documents.OVERLAP_CHARS + 50]
    recovery = [c for c in chunks if c['section'] == 'Recovery']
    assert len(recovery) == 1 and recovery[0]['page'] == 9 and 'Tempo' not in recovery[0]['text']


def test_chunks_flow_across_pdf_pages():
    segments = [{'text': 'Short page one text about pacing.', 'page': 1, 'section': None},
                {'text': 'Short page two text about pacing.', 'page': 2, 'section': None}]
    chunks = documents.chunk(segments)
    assert len(chunks) == 1 and chunks[0]['page'] == 1


# --- Storage and indexing ------------------------------------------------------------

def test_upload_validation(configured, user_id):
    with pytest.raises(library.UploadError, match='PDF, EPUB'):
        library.add_document(user_id, 'notes.docx', b'x')
    with pytest.raises(library.UploadError, match='empty'):
        library.add_document(user_id, 'notes.md', b'')
    with pytest.raises(library.UploadError, match='not a valid PDF'):
        library.add_document(user_id, 'book.pdf', b'not a pdf')
    library.add_document(user_id, 'notes.md', _md(LONG_TEXT))
    with pytest.raises(library.UploadError, match='already in the library'):
        library.add_document(user_id, 'copy.md', _md(LONG_TEXT))


def test_indexing_makes_document_searchable(configured, user_id):
    doc_id = library.add_document(user_id, 'guide.md', _md(
        f"# Guide\n\n## Easy running\n\n{LONG_TEXT}\n\n## Intervals\n\n"
        + ' '.join(['VO2max intervals of four minutes with equal recovery jog.'] * 6)))
    library.run_pending_indexing()
    doc = library.get_document(doc_id)
    assert doc['status'] == 'ready'
    assert doc['title'] == 'Guide'  # the title from the file replaces the file name
    assert doc['chunk_count'] == doc['embedded_count'] >= 2

    results = library.search(user_id, 'VO2max intervals recovery jog', configured)
    assert results and results[0]['section'] == 'Intervals'
    assert results[0]['title'] == 'Guide'
    assert results[0]['similarity'] > 0.5


def test_search_respects_personal_and_shared_documents(configured, user_id):
    other = _second_user()
    library.add_document(other, 'private.md', _md('Secret marathon plan. ' * 20))
    library.add_document(user_id, 'shared.md', _md('Shared marathon taper advice. ' * 20), shared=True)
    library.run_pending_indexing()
    titles = {r['title'] for r in library.search(user_id, 'marathon', configured)}
    assert titles == {'shared'}
    titles = {r['title'] for r in library.search(other, 'marathon', configured)}
    assert titles == {'private', 'shared'}
    assert {d['title'] for d in library.list_documents(user_id)} == {'shared'}


def test_unrelated_query_finds_nothing(configured, user_id):
    library.add_document(user_id, 'guide.md', _md(LONG_TEXT))
    library.run_pending_indexing()
    assert library.search(user_id, 'carbohydrate loading nutrition', configured) == []


def test_keyword_search_ignores_filler_words():
    assert library._fts_query('How do I recover after the race?') == '"recover" OR "race"'
    assert library._fts_query('Hoe herstel ik na een wedstrijd?') == '"herstel" OR "wedstrijd"'
    assert library._fts_query('a an') == ''


def test_transient_embedding_error_retries_later(configured, user_id, monkeypatch):
    doc_id = library.add_document(user_id, 'guide.md', _md(LONG_TEXT))
    monkeypatch.setattr(embeddings, 'embed', lambda c, t: (_ for _ in ()).throw(llm.LLMError('Cannot reach')))
    library.run_pending_indexing()
    doc = library.get_document(doc_id)
    assert doc['status'] == 'indexing' and doc['attempts'] == 1
    assert doc['next_attempt_at'] is not None and 'Cannot reach' in doc['error']
    assert doc['chunk_count'] > 0  # extraction isn't repeated

    monkeypatch.setattr(embeddings, 'embed', fake_embed)
    db_conn = db.get_db()
    db_conn.execute("UPDATE library_documents SET next_attempt_at = NULL WHERE id = ?", (doc_id,))
    db_conn.commit()
    db_conn.close()
    library.run_pending_indexing()
    assert library.get_document(doc_id)['status'] == 'ready'


def test_unreadable_document_fails_with_message(configured, user_id):
    doc_id = library.add_document(user_id, 'scan.pdf', make_pdf(['']))
    library.run_pending_indexing()
    doc = library.get_document(doc_id)
    assert doc['status'] == 'failed' and 'Scanned books' in doc['error']


def test_changing_embedding_model_reindexes(configured, user_id):
    doc_id = library.add_document(user_id, 'guide.md', _md(LONG_TEXT))
    library.run_pending_indexing()
    coach_settings.save_llm_settings({'embedding_model': 'other-embed'})
    config = coach_settings.get_llm_config()
    assert library.search(user_id, 'easy runs aerobic', config) == []  # old vectors aren't mixed in
    library.run_pending_indexing()
    assert library.get_document(doc_id)['status'] == 'ready'
    assert library.search(user_id, 'easy runs aerobic', config)


def test_nothing_is_indexed_without_embedding_model(user_id, monkeypatch):
    called = []
    monkeypatch.setattr(embeddings, 'embed', lambda c, t: called.append(1))
    doc_id = library.add_document(user_id, 'guide.md', _md(LONG_TEXT))
    library.run_pending_indexing()
    assert library.get_document(doc_id)['status'] == 'pending' and not called


def test_delete_document_removes_file_and_index(configured, user_id):
    doc_id = library.add_document(user_id, 'guide.md', _md(LONG_TEXT))
    library.run_pending_indexing()
    path = library.get_document(doc_id)['file_path']
    assert os.path.exists(path)
    library.delete_document(doc_id)
    assert not os.path.exists(path)
    conn = db.get_db()
    assert conn.execute("SELECT COUNT(*) FROM library_chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM library_chunks_fts").fetchone()[0] == 0
    conn.close()


def test_deleting_user_removes_personal_library_only(configured, user_id):
    other = _second_user()
    library.add_document(other, 'private.md', _md('Secret plan. ' * 30))
    shared_id = library.add_document(other, 'shared.md', _md('Shared plan. ' * 30), shared=True)
    library.run_pending_indexing()
    db.delete_user(other)
    conn = db.get_db()
    titles = [r['title'] for r in conn.execute("SELECT title FROM library_documents")]
    conn.close()
    assert titles == ['shared']
    assert library.get_document(shared_id)['uploaded_by'] is None


# --- API -----------------------------------------------------------------------------

def _upload(client, name, data, **form):
    return client.post('/api/library', data={'file': (io.BytesIO(data), name), **form},
                       content_type='multipart/form-data')


def test_library_api_flow(authed_client, configured):
    listing = authed_client.get('/api/library').get_json()
    assert listing['enabled'] is True and listing['can_share'] is True and listing['documents'] == []

    resp = _upload(authed_client, 'guide.md', _md(f"# Guide\n\n{LONG_TEXT}"), title='My guide')
    assert resp.status_code == 201
    doc = resp.get_json()
    assert doc['title'] == 'My guide' and doc['status'] == 'pending' and doc['shared'] is False

    library.run_pending_indexing()
    preview = authed_client.get(f"/api/library/{doc['id']}/preview").get_json()
    assert preview['chunks'] and 'aerobic' in preview['chunks'][0]['text']
    assert library.get_document(doc['id'])['title'] == 'My guide'  # a chosen title is kept

    results = authed_client.post('/api/library/search', json={'query': 'easy runs aerobic base'}).get_json()['results']
    assert results and results[0]['title'] == 'My guide'

    assert authed_client.post(f"/api/library/{doc['id']}/reindex").get_json()['status'] == 'pending'
    assert authed_client.delete(f"/api/library/{doc['id']}").status_code == 200
    assert authed_client.get('/api/library').get_json()['documents'] == []


def test_library_api_permissions(authed_client, configured):
    shared = _upload(authed_client, 'shared.md', _md(LONG_TEXT), shared='true').get_json()
    assert shared['shared'] is True
    personal = _upload(authed_client, 'mine.md', _md('Personal notes about tempo. ' * 20)).get_json()

    authed_client.post('/api/auth/logout')
    authed_client.post('/api/auth/register', json={'username': 'second_user', 'password': 'Password123'})
    listing = authed_client.get('/api/library').get_json()
    assert [d['title'] for d in listing['documents']] == ['shared']
    assert listing['documents'][0]['can_manage'] is False and listing['can_share'] is False
    assert _upload(authed_client, 'x.md', _md('Other text. ' * 30), shared='true').status_code == 403
    assert authed_client.delete(f"/api/library/{shared['id']}").status_code == 403
    assert authed_client.delete(f"/api/library/{personal['id']}").status_code == 404
    assert authed_client.get(f"/api/library/{personal['id']}/preview").status_code == 404


def test_library_api_validation(authed_client, user_id):
    assert authed_client.post('/api/library', data={}, content_type='multipart/form-data').status_code == 400
    assert _upload(authed_client, 'book.docx', b'data').status_code == 400
    assert authed_client.post('/api/library/search', json={'query': ''}).status_code == 400
    assert authed_client.post('/api/library/search', json={'query': 'tempo'}).status_code == 503  # no model
    assert authed_client.get('/api/library').get_json()['enabled'] is False


def test_library_requires_login(client):
    assert client.get('/api/library').status_code == 401
    assert client.post('/api/library/search', json={'query': 'x'}).status_code == 401


def test_admin_test_reports_embeddings(authed_client, configured):
    with patch('coach.llm.test_connection', return_value={'ok': True}):
        data = authed_client.post('/api/admin/llm/test', json={'base_url': 'http://ollama:11434/v1'}).get_json()
    assert data['embeddings'] == {'ok': True, 'dimensions': 64, 'error': None}


def test_separate_embedding_endpoint_never_gets_chat_key(user_id, monkeypatch):
    monkeypatch.delenv('BLAEU_LLM_API_KEY', raising=False)
    config = coach_settings.save_llm_settings({
        'base_url': 'https://api.example.com/v1', 'api_key': 'sk-secret',
        'embedding_model': 'bge-m3', 'embedding_base_url': 'http://athena:11434/v1',
    })
    emb = coach_settings.embedding_config(config)
    assert emb['base_url'] == 'http://athena:11434/v1' and emb['api_key'] == ''
    assert coach_settings.is_library_configured(config)
