"""
Text extraction and chunking for the training literature library.

extract() turns a PDF, EPUB, Markdown or text file into segments ({'text', 'page', 'section'}),
keeping page numbers and chapter titles so the coach can cite where a passage comes from.
chunk() splits those segments into overlapping passages of a size that embeds well.
"""
import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser

from defusedxml import ElementTree as ET

ALLOWED_TYPES = {'.pdf': 'pdf', '.epub': 'epub', '.md': 'markdown', '.markdown': 'markdown', '.txt': 'text'}

# A chunk of about 900 characters is roughly 200-250 tokens of English prose: long enough to
# carry a complete idea, small enough to be about one topic, so searches hit precisely and
# a handful of passages fit in the coach's prompt
TARGET_CHARS = 900
MAX_CHARS = 1400
OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 80
# Below this many characters for the whole document, there's no usable text (e.g. a scan)
MIN_DOCUMENT_CHARS = 200


class ExtractionError(ValueError):
    """The file can't be turned into text; the message is shown to the user."""


def file_type_for(filename):
    ext = posixpath.splitext((filename or '').lower())[1]
    return ALLOWED_TYPES.get(ext)


def check_signature(data, file_type):
    """Rejects files whose content doesn't match their extension."""
    if file_type == 'pdf' and not data.startswith(b'%PDF'):
        raise ExtractionError('This file is not a valid PDF.')
    if file_type == 'epub' and not data.startswith(b'PK'):
        raise ExtractionError('This file is not a valid EPUB.')
    if file_type in ('markdown', 'text') and b'\x00' in data[:4096]:
        raise ExtractionError('This file does not look like a text file.')


def _clean(text):
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('­', '')
    # Join words hyphenated across line breaks, a common PDF artefact
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
    text = re.sub(r'[ \t ]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


HEADING_LINE = re.compile(r'^[A-Z0-9][^\n]{2,70}$')


def _mark_pdf_headings(text):
    """
    PDF text often separates paragraphs with single line breaks only. A short line that starts
    with a capital and doesn't end like a sentence is most likely a heading: surround it with
    blank lines so chunks preferably start there.
    """
    lines = text.split('\n')
    out = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_heading = (HEADING_LINE.match(stripped) and not stripped.endswith(('.', ',', ';', ':', '?', '!'))
                      and len(stripped.split()) <= 10 and i + 1 < len(lines))
        out.append(f"\n{stripped}\n" if is_heading else line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(out)).strip()


def _extract_pdf(data):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(''):
                    raise ExtractionError('This PDF is password protected.')
            except Exception:
                raise ExtractionError('This PDF is password protected.')
        title = None
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip() or None
        segments = []
        for number, page in enumerate(reader.pages, start=1):
            text = _mark_pdf_headings(_clean(page.extract_text() or ''))
            if text:
                segments.append({'text': text, 'page': number, 'section': None})
        return title, segments, len(reader.pages)
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise ExtractionError(f'This PDF could not be read ({type(e).__name__}).')


class _HTMLText(HTMLParser):
    """Collects readable text from (X)HTML, with line breaks at block elements."""
    BLOCKS = {'p', 'div', 'br', 'li', 'tr', 'section', 'article', 'blockquote', 'pre',
              'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'ul', 'ol', 'dd', 'dt'}
    SKIP = {'script', 'style', 'head', 'title', 'nav'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.heading, self._skip, self._in_heading = [], None, 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCKS:
            self.parts.append('\n\n' if tag.startswith('h') or tag == 'p' else '\n')
        if tag in ('h1', 'h2') and self.heading is None:
            self._in_heading = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCKS:
            self.parts.append('\n\n' if tag.startswith('h') or tag == 'p' else '\n')
        if tag in ('h1', 'h2'):
            self._in_heading = False

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._in_heading and data.strip():
            self.heading = ((self.heading or '') + ' ' + data.strip()).strip()


def _extract_epub(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        container = ET.fromstring(archive.read('META-INF/container.xml'))
        rootfile = next(el for el in container.iter() if el.tag.endswith('rootfile'))
        opf_path = rootfile.get('full-path')
        opf = ET.fromstring(archive.read(opf_path))
    except (KeyError, StopIteration, zipfile.BadZipFile, ET.ParseError):
        raise ExtractionError('This EPUB could not be read.')

    base = posixpath.dirname(opf_path)
    title = next((el.text.strip() for el in opf.iter() if el.tag.endswith('}title') and el.text), None)
    manifest = {el.get('id'): el.get('href') for el in opf.iter() if el.tag.endswith('}item')}
    spine = [el.get('idref') for el in opf.iter() if el.tag.endswith('}itemref')]

    segments = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        path = posixpath.normpath(posixpath.join(base, href.split('#')[0]))
        try:
            html = archive.read(path).decode('utf-8', errors='replace')
        except KeyError:
            continue
        parser = _HTMLText()
        parser.feed(html)
        text = _clean(''.join(parser.parts))
        if text:
            segments.append({'text': text, 'page': None, 'section': parser.heading})
    return title, segments, None


def _extract_text(data, markdown):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    text = _clean(text)
    title, segments, section, current = None, [], None, []
    for line in text.split('\n'):
        heading = re.match(r'^(#{1,3})\s+(.+?)\s*#*$', line) if markdown else None
        if heading:
            if current:
                segments.append({'text': '\n'.join(current).strip(), 'page': None, 'section': section})
            section, current = heading.group(2), []
            if title is None and len(heading.group(1)) == 1:
                title = section
        else:
            current.append(line)
    if current:
        segments.append({'text': '\n'.join(current).strip(), 'page': None, 'section': section})
    return title, [s for s in segments if s['text']], None


def extract(data, file_type):
    """Returns (title or None, segments, page count or None)."""
    check_signature(data, file_type)
    if file_type == 'pdf':
        result = _extract_pdf(data)
    elif file_type == 'epub':
        result = _extract_epub(data)
    else:
        result = _extract_text(data, markdown=file_type == 'markdown')
    if sum(len(s['text']) for s in result[1]) < MIN_DOCUMENT_CHARS:
        raise ExtractionError(
            'No readable text was found. Scanned books (images of pages) are not supported.'
        )
    return result


def _split_long(paragraph):
    """Splits a paragraph that is longer than a chunk, preferably at sentence ends."""
    sentences = re.split(r'(?<=[.!?])\s+', paragraph)
    parts, current = [], ''
    for sentence in sentences:
        while len(sentence) > MAX_CHARS:  # no sentence ends at all: cut hard
            parts.append(sentence[:TARGET_CHARS])
            sentence = sentence[TARGET_CHARS:]
        if current and len(current) + len(sentence) + 1 > TARGET_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def chunk(segments):
    """
    Splits segments into passages of about TARGET_CHARS, along paragraph boundaries, with a
    little overlap so an idea that spans two passages isn't lost. Text flows on across pages
    but never across a chapter boundary; a chunk keeps the page where it starts.
    """
    paragraphs = []
    for segment in segments:
        for paragraph in re.split(r'\n\s*\n', segment['text']):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            for part in (_split_long(paragraph) if len(paragraph) > MAX_CHARS else [paragraph]):
                paragraphs.append((part, segment['page'], segment['section']))

    def is_heading(text):
        return len(text) <= 80 and not text.rstrip().endswith(('.', '!', '?', ',', ';', ':'))

    chunks, current, page, section = [], [], None, object()

    def flush():
        chunks.append({'text': '\n\n'.join(current), 'page': page, 'section': section})

    for text, para_page, para_section in paragraphs:
        length = sum(len(p) + 2 for p in current)
        too_long = length + len(text) > TARGET_CHARS and length >= TARGET_CHARS // 3
        if current and (para_section != section or too_long):
            # A heading at the end belongs to the text that follows it
            carried = [current.pop()] if len(current) > 1 and is_heading(current[-1]) else []
            flush()
            if para_section == section and not carried:
                tail = current[-1][-OVERLAP_CHARS:] if current else ''
                # Start the overlap at a word boundary
                tail = tail[tail.find(' ') + 1:] if ' ' in tail else tail
                current = [tail, text] if tail else [text]
            else:
                current = carried + [text]
            page, section = para_page, para_section
        elif current:
            current.append(text)
        else:
            current, page, section = [text], para_page, para_section
    if current:
        flush()

    # Fold fragments that are too small to be useful into the previous chunk of the same section
    merged = []
    for c in chunks:
        if merged and len(c['text']) < MIN_CHUNK_CHARS and merged[-1]['section'] == c['section'] \
                and len(merged[-1]['text']) + len(c['text']) <= MAX_CHARS:
            merged[-1]['text'] += '\n\n' + c['text']
        else:
            merged.append(dict(c))
    return merged
