import logging
from flask import Blueprint, request, jsonify
from db import create_folder, get_folders, delete_folder
from utils import get_current_user_id

logger = logging.getLogger('blaeu.folders')
folders_bp = Blueprint('folders', __name__)

@folders_bp.route('/api/folders', methods=['GET', 'POST'])
def handle_folders():
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    if request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        if not name or not name.strip():
            return jsonify({'error': 'Folder name required'}), 400
        try:
            folder_id = create_folder(name, user_id)
            return jsonify({'id': folder_id, 'name': name.strip()}), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 409
        except Exception as e:
            logger.exception("Unexpected error during folder creation")
            return jsonify({'error': 'Failed to create folder: An unexpected server error occurred.'}), 500
    else:
        return jsonify(get_folders(user_id))


@folders_bp.route('/api/folders/<int:folder_id>', methods=['DELETE'])
def remove_folder(folder_id):
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Unauthorized. Please log in.'}), 401
        
    try:
        delete_folder(folder_id, user_id)
        return jsonify({'success': True})
    except Exception as e:
        logger.exception("Unexpected error deleting folder")
        return jsonify({'error': 'Could not delete folder: An unexpected server error occurred.'}), 500
