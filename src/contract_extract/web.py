"""Minimal stdlib-only web demo.

Serves a small HTML page (upload a PDF/DOCX, see clauses + review checklist.
No external web framework is required: `python -m contract_extract.web` starts
the demo on http://127.0.0.1:8000/ (override with PORT).

Security notes:
- Uploads are written to a temporary directory and deleted after processing.
- The demo is bound to 127.0.0.1 by default (edit HOST to expose it).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contract_extract.checklist import build_checklist  # noqa: E402
from contract_extract.pipeline import extract_document  # noqa: E402
from contract_extract.schema import DocumentResult  # noqa: E402

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

MAX_UPLOAD = 20 * 1024 * 1024  # 20 MiB


def _render_result(result: DocumentResult) -> str:
    checklist = build_checklist(result)
    rows = [f"<tr><td>{html.escape(c.clause)}</td><td>{c.risk.value}</td>"
            f"<td>p.{c.page}</td><td>{html.escape(c.flag or '')}</td>"
            f"<td>{html.escape(c.quote[:140])}</td></tr>"
            for c in result.clauses]
    return f"""<h2>Result</h2>
<p><strong>{html.escape(result.source)}</strong> —
  format={result.format} pages={result.pages} engine={result.engine}
  overall risk=<span class="risk-{result.risk.value.lower()}">{result.risk.value}</span>
</p>
<table>
<tr><th>field</th><th>risk</th><th>page</th><th>flag</th><th>quote</th></tr>
{''.join(rows) or '<tr><td colspan=5>No clauses found</td></tr>'}
</table>
<pre class="checklist">{html.escape(checklist)}</pre>
<h3>JSON</h3>
<pre>{html.escape(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))}</pre>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path not in ("/", "/favicon.ico"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_PAGE.encode("utf-8"))

    def _do_upload(self):
        content_type = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
        if m is None:
            return "<h2>Bad request</h2><p>Missing multipart boundary.</p>"
        boundary = (m.group(1) or m.group(2)).strip()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return "<h2>Bad request</h2><p>Missing Content-Length.</p>"
        if length > MAX_UPLOAD:
            return "<h2>File too large</h2><p>Max 20 MiB.</p>"
        raw = self.rfile.read(length)
        if len(raw) > MAX_UPLOAD:
            return "<h2>File too large</h2><p>Max 20 MiB.</p>"
        message = BytesParser().parsebytes(
            b"Content-Type: multipart/form-data; boundary=" + boundary.encode() + b"\r\n\r\n" + raw
        )
        item = None
        for part in message.walk():
            if part.get_content_disposition() is None:
                continue
            params = part.get_param("name", header="content-disposition", unquote=True)
            if params == "file":
                item = part
                break
        if item is None:
            return "<h2>No file uploaded</h2><p><a href='/'>back</a></p>"
        disposition = item.get("Content-Disposition") or item.get_content_disposition() or ""
        fname_m = re.search(r'filename="?([^";]+)"?', disposition)
        filename = Path(fname_m.group(1) if fname_m else "upload.bin").name
        if Path(filename).suffix.lower() not in (".pdf", ".docx"):
            return "<h2>Unsupported format</h2><p>Please upload a .pdf or .docx file.</p>"
        payload = item.get_payload(decode=True)
        if payload is None:
            return "<h2>Empty upload</h2><p>The uploaded file was empty.</p>"
        with tempfile.TemporaryDirectory(prefix="contract_extract_") as tmp:
            dest = Path(tmp) / filename
            dest.write_bytes(payload)
            try:
                result = extract_document(str(dest))
            except Exception as exc:
                return f"<h2>Processing failed</h2><p>{html.escape(repr(exc))}</p>"
        return _render_result(result)

    def do_POST(self):  # noqa: N802
        try:
            body = self._do_upload()
        except Exception as exc:  # pragma: no cover - defensive
            body = f"<h2>Error</h2><p>{html.escape(repr(exc))}</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):  # quieter access log
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Contract Extractor — Web demo</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%;margin:1rem 0}
td,th{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;font-size:.9rem}
td{vertical-align:top}
.risk-high{color:#c00;font-weight:700}.risk-medium{color:#c60;font-weight:700}.risk-low{color:#080}
pre{background:#f5f5f5;padding:.8rem;overflow:auto;font-size:.85rem;white-space:pre-wrap}
</style></head><body>
<h1>Contract / Bid risk extractor</h1>
<form action="/" method="post" enctype="multipart/form-data">
  <input type="file" name="file" required accept=".pdf,.docx">
  <button type="submit">Extract</button>
</form>
<p>Local rule-based extraction — no document leaves this machine.</p>
</body></html>
"""


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"contract-extract web demo on http://{HOST}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
