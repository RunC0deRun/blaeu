import logging

from flask import Blueprint, current_app, jsonify, request

from db import get_user_by_id
from utils import get_current_user_id, rate_limit
from coach import library, llm
from coach.settings import get_llm_config, is_library_configured

logger = logging.getLogger('blaeu.library')
library_bp = Blueprint('library', __name__)

MAX_QUERY_CHARS = 500


def _current_user():
    user_id = get_current_user_id()
    if not user_id:
        return None, (jsonify({'error': 'Unauthorized. Please log in.'}), 401)
    return get_user_by_id(user_id), None


def _serialize(document, user):
    return {
        'id': document['id'],
        'title': document['title'],
        'filename': document['filename'],
        'file_type': document['file_type'],
        'file_size': document['file_size'],
        'shared': document['owner_user_id'] is None,
        'status': document['status'],
        'error': document['error'],
        'pages': document['pages'],
        'chunk_count': document['chunk_count'],
        'embedded_count': document['embedded_count'],
        'created_at': document['created_at'],
        'can_manage': library.can_manage(document, user['id'], bool(user['is_admin'])),
    }


@library_bp.route('/api/library', methods=['GET'])
def list_library():
    user, error = _current_user()
    if error:
        return error
    config = get_llm_config()
    return jsonify({
        'enabled': is_library_configured(config),
        'embedding_model': config['embedding_model'] or None,
        'can_share': bool(user['is_admin']),
        'max_file_mb': library.MAX_FILE_BYTES // (1024 * 1024),
        'documents': [_serialize(d, user) for d in library.list_documents(user['id'])],
    })


@library_bp.route('/api/library', methods=['POST'])
@rate_limit(limit=20, period=60)
def upload_document():
    user, error = _current_user()
    if error:
        return error
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'Choose a file to upload.'}), 400
    shared = request.form.get('shared') == 'true'
    if shared and not user['is_admin']:
        return jsonify({'error': 'Only administrators can add documents to the shared library.'}), 403
    try:
        document_id = library.add_document(
            user['id'], upload.filename, upload.read(library.MAX_FILE_BYTES + 1),
            shared=shared, title=request.form.get('title')
        )
    except library.UploadError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        logger.exception("Failed to store library upload")
        return jsonify({'error': 'Failed to store the document: An unexpected server error occurred.'}), 500
    library.start_indexing_soon(current_app._get_current_object())
    return jsonify(_serialize(library.get_document(document_id), user)), 201


def _managed_document(document_id):
    user, error = _current_user()
    if error:
        return None, None, error
    document = library.get_document(document_id)
    if not library.can_see(document, user['id']):
        return None, None, (jsonify({'error': 'Document not found.'}), 404)
    if not library.can_manage(document, user['id'], bool(user['is_admin'])):
        return None, None, (jsonify({'error': 'Only an administrator can change the shared library.'}), 403)
    return user, document, None


@library_bp.route('/api/library/<int:document_id>', methods=['DELETE'])
def delete_document(document_id):
    _, document, error = _managed_document(document_id)
    if error:
        return error
    library.delete_document(document['id'])
    return jsonify({'success': True})


@library_bp.route('/api/library/<int:document_id>/reindex', methods=['POST'])
def reindex_document(document_id):
    user, document, error = _managed_document(document_id)
    if error:
        return error
    library.reindex_document(document['id'])
    library.start_indexing_soon(current_app._get_current_object())
    return jsonify(_serialize(library.get_document(document['id']), user))


@library_bp.route('/api/library/<int:document_id>/preview', methods=['GET'])
def preview_document(document_id):
    user, error = _current_user()
    if error:
        return error
    document = library.get_document(document_id)
    if not library.can_see(document, user['id']):
        return jsonify({'error': 'Document not found.'}), 404
    return jsonify({'title': document['title'], 'chunks': library.preview(document_id)})


@library_bp.route('/api/library/search', methods=['POST'])
@rate_limit(limit=30, period=60)
def search_library():
    """Lets users check what the coach would find for a question."""
    user, error = _current_user()
    if error:
        return error
    query = ((request.json or {}).get('query') or '').strip()
    if not query:
        return jsonify({'error': 'Enter something to search for.'}), 400
    if len(query) > MAX_QUERY_CHARS:
        return jsonify({'error': f'The search is too long (at most {MAX_QUERY_CHARS} characters).'}), 400
    config = get_llm_config()
    if not is_library_configured(config):
        return jsonify({'error': 'Library search is not set up. Ask an administrator to choose an embedding model.'}), 503
    try:
        results = library.search(user['id'], query, config)
    except llm.LLMError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({'results': results})
