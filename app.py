"""Local HTTP entry point for SettleWise."""
import argparse
import json
import mimetypes
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from recon import ConflictError, InputError, confirm, database, import_csv, initialize, reopen, state

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "samples"
MAX_BODY = 2_500_000


class Application:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.paths = {"demo": self.directory / "demo.sqlite", "local": self.directory / "ledger.sqlite"}
        for path in self.paths.values():
            initialize(path)
        with database(self.paths["demo"]) as db:
            present = {row["kind"] for row in db.execute("SELECT DISTINCT kind FROM records")}
        for kind, filename in (("bank", "demo-bank.csv"), ("payouts", "demo-payouts.csv")):
            if kind not in present:
                self.sample(kind, filename)

    def sample(self, kind, filename):
        content = (SAMPLES / filename).read_text(encoding="utf-8")
        return import_csv(self.paths["demo"], kind, filename, content)

    def api(self, method, route, workspace, body=None):
        if workspace not in self.paths:
            raise InputError("unknown workspace")
        path = self.paths[workspace]
        if method == "GET" and route == "/api/state":
            return 200, state(path)
        if method == "POST" and route in ("/api/preview", "/api/import"):
            kind = body.get("kind")
            if kind not in ("bank", "payouts"):
                raise InputError("choose a bank statement or payout report")
            if not isinstance(body.get("name"), str):
                raise InputError("a file name is required")
            return 200, import_csv(
                path, kind, body["name"], body.get("content"), preview=route == "/api/preview",
            )
        if method == "POST" and route in ("/api/confirm", "/api/reopen"):
            payout_id, bank_id = body.get("payout_id"), body.get("bank_id")
            if not isinstance(payout_id, str) or not isinstance(bank_id, str):
                raise InputError("choose a payout and a bank deposit")
            action = confirm if route == "/api/confirm" else reopen
            action(path, payout_id, bank_id)
            return 200, {"updated": True}
        if method == "POST" and workspace == "demo" and route == "/api/demo-late":
            return 200, self.sample("payouts", "demo-late-payout.csv")
        return 404, {"error": "This endpoint does not exist."}


STATIC = {
    "/": ROOT / "web" / "index.html",
    "/style.css": ROOT / "web" / "style.css",
    "/ui.js": ROOT / "web" / "ui.js",
    "/samples/demo-bank.csv": SAMPLES / "demo-bank.csv",
    "/samples/demo-payouts.csv": SAMPLES / "demo-payouts.csv",
    "/samples/demo-late-payout.csv": SAMPLES / "demo-late-payout.csv",
}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, status, data):
        self.reply(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def dispatch(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host")
        if host not in allowed:
            self.json(403, {"error": "Open SettleWise using its local address."})
            return
        url = urlsplit(self.path)
        if self.command == "GET" and url.path in STATIC:
            path = STATIC[url.path]
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            self.reply(200, path.read_bytes(), mime + "; charset=utf-8")
            return
        workspace = parse_qs(url.query).get("workspace", ["demo"])[0]
        try:
            body = None
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host}":
                    self.json(403, {"error": "This request must come from SettleWise."})
                    return
                if self.headers.get_content_type() != "application/json":
                    raise InputError("expected a JSON request")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY:
                    self.json(413, {"error": "The request is empty or exceeds the upload limit."})
                    return
                self.connection.settimeout(15)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise InputError("expected a JSON object")
            self.json(*self.server.app.api(self.command, url.path, workspace, body))
        except (json.JSONDecodeError, UnicodeError):
            self.json(400, {"error": "The request could not be decoded."})
        except ConflictError as error:
            self.json(409, {"error": str(error)})
        except InputError as error:
            self.json(400, {"error": str(error)})
        except (OverflowError, ValueError, TimeoutError):
            self.json(400, {"error": "The request is invalid or incomplete."})
        except sqlite3.OperationalError:
            self.json(503, {"error": "The ledger is temporarily unavailable. Retry the same request."})

    do_GET = dispatch
    do_POST = dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    args = parser.parse_args()
    with Server(("127.0.0.1", args.port), Handler) as server:
        server.app = Application(args.data_dir)
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"SettleWise is running at {url}. Press Ctrl+C to stop.", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
