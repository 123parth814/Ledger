"""Run the Ledger web app on localhost."""

import argparse
import io
import json
import re
import struct
import threading
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ledger import Classifier, LABELS, MAX_FILE, MAX_TEXT, ROOT, ROUTER_NAME

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_PDF_PAGES = 40
MAX_DOCX_XML = 4 * 1024 * 1024


def extract_pdf(body):
    try:
        reader = PdfReader(io.BytesIO(body))
    except (PyPdfError, KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError, OSError, struct.error, zlib.error) as error:
        raise ValueError("The PDF could not be read. Try another file or paste the text.") from error
    if reader.is_encrypted:
        raise ValueError("Unlock the PDF before uploading it.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"Upload a PDF with at most {MAX_PDF_PAGES} pages.")
    extracted = []
    length = 0
    for page in reader.pages:
        try:
            part = page.extract_text() or ""
        except (PyPdfError, KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError, OSError, struct.error, zlib.error) as error:
            raise ValueError("The PDF could not be read. Try another file or paste the text.") from error
        length += len(part) + (1 if extracted else 0)
        if length > MAX_TEXT:
            raise ValueError("The extracted text exceeds 40,000 characters. Paste the relevant section.")
        extracted.append(part)
    return "\n".join(extracted)


def _paragraph_text(paragraph):
    parts = []
    for node in paragraph.iter():
        if node.tag == WORD_NAMESPACE + "t" and node.text:
            parts.append(node.text)
        elif node.tag == WORD_NAMESPACE + "tab":
            parts.append("\t")
        elif node.tag in (WORD_NAMESPACE + "br", WORD_NAMESPACE + "cr"):
            parts.append("\n")
    return "".join(parts)


def extract_docx(body):
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            document = archive.getinfo("word/document.xml")
            if document.file_size > MAX_DOCX_XML:
                raise ValueError("The DOCX is too large to extract. Paste the relevant section.")
            with archive.open(document) as stream:
                xml = stream.read(MAX_DOCX_XML + 1)
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError, OSError, zlib.error) as error:
        raise ValueError("The DOCX could not be read. Try another file or paste the text.") from error
    if len(xml) > MAX_DOCX_XML:
        raise ValueError("The DOCX is too large to extract. Paste the relevant section.")
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", xml, flags=re.I):
        raise ValueError("The DOCX contains unsupported XML declarations.")
    try:
        root = ElementTree.fromstring(xml)
    except (ElementTree.ParseError, ValueError) as error:
        raise ValueError("The DOCX could not be read. Try another file or paste the text.") from error
    return "\n".join(_paragraph_text(paragraph) for paragraph in root.iter(WORD_NAMESPACE + "p"))


def extract_document(filename, body):
    extension = Path(filename).suffix.casefold()
    if extension == ".pdf":
        text = extract_pdf(body)
    elif extension == ".docx":
        text = extract_docx(body)
    elif extension == ".txt":
        try:
            encoding = "utf-16" if body.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            text = body.decode(encoding)
        except UnicodeDecodeError as error:
            raise ValueError("Save the TXT as UTF-8 or UTF-16 and try again.") from error
    else:
        raise ValueError("Choose a TXT, PDF or DOCX file.")
    if not text.strip():
        raise ValueError("No readable text was found. For a scanned PDF, run OCR or paste the text.")
    return text


class LedgerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_content(self, status, mime, body, *, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        )
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_json(self, status, data, *, head_only=False):
        self.send_content(status, "application/json; charset=utf-8", json.dumps(data).encode(), head_only=head_only)

    def _handle_get(self, *, head_only=False):
        route = urlsplit(self.path).path
        if route == "/api/model":
            self.send_json(200, self.server.model_info, head_only=head_only)
        elif route in STATIC:
            file, mime = STATIC[route]
            self.send_content(200, mime, (ROOT / "web" / file).read_bytes(), head_only=head_only)
        else:
            self.send_json(404, {"error": "Page not found."}, head_only=head_only)

    def do_GET(self):
        self._handle_get(head_only=False)

    def do_HEAD(self):
        self._handle_get(head_only=True)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/classify":
            self.close_connection = True
            self.send_json(404, {"error": "Page not found."})
            return
        if not self.allowed_origin():
            self.close_connection = True
            self.send_json(403, {"error": "Open Ledger at its localhost address."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("Add a complaint before classifying.")
        except ValueError:
            self.close_connection = True
            self.send_json(400, {"error": "Add a complaint before classifying."})
            return
        if length > MAX_FILE:
            self.close_connection = True
            self.send_json(413, {"error": "Choose a file smaller than 8 MB."})
            return

        content_type = self.headers.get_content_type()
        if content_type not in ("application/octet-stream", "application/json"):
            self.close_connection = True
            self.send_json(415, {"error": "Send text or a supported document."})
            return

        body = self.rfile.read(length)
        try:
            if content_type == "application/octet-stream":
                filename = unquote(self.headers.get("X-Filename", ""))
                text = extract_document(filename, body)
            else:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                    raise ValueError("Provide the complaint as text.")
                text = payload["text"]
            if len(text) > MAX_TEXT:
                raise ValueError("Use up to 40,000 characters. Paste the relevant section.")
            result = self.server.classifier.predict(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "The submitted text could not be read."})
        except ValueError as error:
            self.send_json(422, {"error": str(error)})
        else:
            self.send_json(200, result)

    def allowed_origin(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return (host is not None and host.casefold() in hosts) and (
            origin is None or origin.casefold() in {f"http://{item}" for item in hosts}
        )


def serve(port, open_browser=False):
    classifier = Classifier()
    report = json.loads((ROOT / "analysis" / "metrics.json").read_text(encoding="utf-8"))
    with ThreadingHTTPServer(("127.0.0.1", port), LedgerHandler) as server:
        server.classifier = classifier
        server.model_info = {
            "model": ROUTER_NAME,
            "labels": LABELS,
            "test_count": report["data"]["splits"]["test"],
            "macro_f1": report["ensemble"]["macro_f1"],
        }
        address = f"http://127.0.0.1:{server.server_port}"
        print(f"Ledger is ready at {address}", flush=True)
        if open_browser:
            timer = threading.Timer(0.15, webbrowser.open, args=(address,))
            timer.daemon = True
            timer.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open Ledger in the default browser.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Use a port between 0 and 65535.")
    try:
        serve(args.port, args.open)
    except FileNotFoundError as error:
        parser.exit(1, f"Missing project file: {error.filename}. Extract the complete project before running Ledger.\n")
    except OSError as error:
        parser.exit(1, f"Could not start Ledger on port {args.port}: {error}. Try --port 8766.\n")
