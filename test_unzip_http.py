import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from unzip_http import RemoteZipFile, list_files


def _make_zip(entries):
    """Build a zip in memory. entries is a list of (name, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class _RangeHandler(BaseHTTPRequestHandler):
    """Serves one zip and honours Range requests, like a real HTTP server."""

    payload = b''

    def log_message(self, *args):
        pass

    def _head(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Length', str(len(self.payload)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

    def do_HEAD(self):
        self._head()

    def do_GET(self):
        rng = self.headers.get('Range')
        if not rng:
            self._head()
            self.wfile.write(self.payload)
            return
        start, end = rng.replace('bytes=', '').split('-')
        start = int(start)
        end = int(end) if end else len(self.payload) - 1
        chunk = self.payload[start:end + 1]
        self.send_response(206)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Length', str(len(chunk)))
        self.send_header('Content-Range', f'bytes {start}-{end}/{len(self.payload)}')
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        self.wfile.write(chunk)


ENTRIES = [
    ('hello.txt', b'hello from the zip'),
    ('nested/data.bin', bytes(range(256)) * 4),
    ('empty.txt', b''),
]


@pytest.fixture
def server():
    payload = _make_zip(ENTRIES)

    class Handler(_RangeHandler):
        pass

    Handler.payload = payload
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    try:
        yield f'http://{host}:{port}/archive.zip', payload
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_namelist_lists_every_entry(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        names = rz.namelist()
    assert 'hello.txt' in names
    assert 'nested/data.bin' in names
    assert 'empty.txt' in names


def test_infolist_reports_sizes(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        infos = {i.filename: i for i in rz.infolist()}
    assert infos['hello.txt'].file_size == len(b'hello from the zip')
    assert infos['empty.txt'].file_size == 0


def test_files_is_a_name_to_info_mapping(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        files = rz.files
    assert 'hello.txt' in files
    assert files['hello.txt'].file_size == len(b'hello from the zip')


def test_open_streams_the_real_bytes(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        with rz.open('hello.txt') as f:
            data = f.read()
    assert data == b'hello from the zip'


def test_open_round_trips_binary_content(server):
    url, _ = server
    expected = bytes(range(256)) * 4
    with RemoteZipFile(url) as rz:
        with rz.open('nested/data.bin') as f:
            data = f.read()
    assert data == expected


def test_open_empty_member(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        with rz.open('empty.txt') as f:
            data = f.read()
    assert data == b''


def test_open_text_decodes(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        with rz.open_text('hello.txt') as f:
            text = f.read()
    assert text == 'hello from the zip'


def test_extract_writes_the_file_to_disk(server, tmp_path):
    url, _ = server
    with RemoteZipFile(url) as rz:
        rz.extract('hello.txt', path=tmp_path)
    assert (tmp_path / 'hello.txt').read_bytes() == b'hello from the zip'


def test_extract_creates_nested_directories(server, tmp_path):
    url, _ = server
    with RemoteZipFile(url) as rz:
        rz.extract('nested/data.bin', path=tmp_path)
    assert (tmp_path / 'nested' / 'data.bin').read_bytes() == bytes(range(256)) * 4


def test_extractall_writes_everything(server, tmp_path):
    url, _ = server
    with RemoteZipFile(url) as rz:
        rz.extractall(path=tmp_path)
    assert (tmp_path / 'hello.txt').exists()
    assert (tmp_path / 'nested' / 'data.bin').exists()
    assert (tmp_path / 'empty.txt').exists()


def test_matching_files_supports_globs(server):
    url, _ = server
    with RemoteZipFile(url) as rz:
        matched = [f.filename for f in rz.matching_files('*.txt')]
    assert 'hello.txt' in matched
    assert 'empty.txt' in matched
    assert 'nested/data.bin' not in matched


def test_zip_size_is_known_after_listing(server):
    url, payload = server
    with RemoteZipFile(url) as rz:
        rz.namelist()
        assert rz.zip_size == len(payload)


def test_list_files_prints_to_stderr(server, capsys):
    url, _ = server
    with RemoteZipFile(url) as rz:
        list_files(rz)
    err = capsys.readouterr().err
    assert 'hello.txt' in err
