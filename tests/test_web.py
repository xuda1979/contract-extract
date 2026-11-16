"""Web demo tests: GET page, POST pdf/docx upload, unsupported format rejected."""

from __future__ import annotations

import threading
import urllib.request


def _multipart(path, filename):
    from pathlib import Path

    boundary = "----PYTESTBOUNDARY"
    payload = Path(path).read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head + payload + f"\r\n--{boundary}--\r\n".encode(), boundary


def _start_server():
    import contract_extract.web as web

    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def _get(port, path="/"):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return r.status, r.read().decode()


def _post(port, body, boundary):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode()


def test_index_page():
    srv, port = _start_server()
    try:
        status, body = _get(port)
        assert status == 200
        assert "Contract / Bid risk extractor" in body
    finally:
        srv.shutdown()


def test_pdf_upload_renders_result(sample_pdf):
    srv, port = _start_server()
    try:
        body, boundary = _multipart(sample_pdf, sample_pdf.name)
        status, html_body = _post(port, body, boundary)
        assert status == 200
        assert "<h2>Result</h2>" in html_body
        assert f"risk={sample_pdf.name}" not in html_body  # path is temp
    finally:
        srv.shutdown()


def test_docx_upload_renders_result(sample_docx):
    srv, port = _start_server()
    try:
        body, boundary = _multipart(sample_docx, sample_docx.name)
        status, html_body = _post(port, body, boundary)
        assert status == 200
        assert "<h2>Result</h2>" in html_body
    finally:
        srv.shutdown()


def test_unsupported_extension_rejected(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    srv, port = _start_server()
    try:
        body, boundary = _multipart(f, "notes.txt")
        status, html_body = _post(port, body, boundary)
        assert status == 200
        assert "Unsupported format" in html_body
    finally:
        srv.shutdown()
