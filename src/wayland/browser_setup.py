"""Loopback-only browser onboarding; secrets never pass through the terminal UI."""

import html
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .credentials import save_api_key


class BrowserSetup:
    def __init__(self, runtime):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.lock = threading.Lock()
        self.expires = 0.0
        from .broker.portal import PortalConnection

        self.portal = PortalConnection(runtime)

    def start(self, provider):
        if provider not in ("openai", "ibkr"):
            raise ValueError("Unknown connection provider")
        with self.lock:
            self.expires = time.monotonic() + 1800
            if self.server is None:
                owner = self

                class Handler(BaseHTTPRequestHandler):
                    def setup(self):
                        super().setup()
                        self.connection.settimeout(15)

                    def log_message(self, *_):
                        pass  # Paths contain the capability token; form bodies contain secrets.

                    def valid(self):
                        return (
                            self.headers.get("Host") == owner.address
                            and self.path in (f"/{owner.token}/openai", f"/{owner.token}/ibkr")
                            and time.monotonic() < owner.expires
                        )

                    def page(self, body, status=200):
                        content = (
                            "<!doctype html><html lang='en'><meta charset='utf-8'>"
                            "<meta name='viewport' content='width=device-width'>"
                            "<title>Wayland connections</title><style>"
                            "body{background:#141c1b;color:#b8e9c0;font:18px system-ui;max-width:650px;margin:8vh auto;padding:24px}"
                            "input,button{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0;background:#243530;color:#d9fbe2;border:1px solid #638f79}"
                            "a{color:#89d9ee}p{line-height:1.6}</style>"
                            "<h1>WAYLAND · Connections</h1>" + body + "</html>"
                        ).encode()
                        self.send_response(status)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(content)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.send_header("X-Frame-Options", "DENY")
                        self.send_header(
                            "Content-Security-Policy",
                            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                        )
                        self.end_headers()
                        self.wfile.write(content)

                    def do_GET(self):
                        if not self.valid():
                            self.page(
                                "<p>Connection link expired. Open Connections in Wayland again.</p>", 403
                            )
                            return
                        if self.path.endswith("/ibkr"):
                            state = owner.portal.snapshot()
                            if state.get("url"):
                                url = html.escape(state["url"], quote=True)
                                self.page(
                                    f"<meta http-equiv='refresh' content='3;url={url}'><h2>IBKR paper login</h2><p>Use your paper trading login. Wayland refuses live accounts.</p><p>The local IBKR gateway uses a self-signed certificate; your browser may ask you to continue to localhost.</p><p><a href='{url}' rel='noreferrer'>Continue to IBKR login</a></p><p>Wayland checks the connection automatically after login.</p>"
                                )
                            else:
                                self.page(
                                    "<meta http-equiv='refresh' content='3'><h2>Preparing IBKR login</h2><p>"
                                    + html.escape(state["status"])
                                    + "</p><p>This page updates automatically.</p>"
                                )
                            return
                        model = html.escape(owner.runtime.settings().openai_model, quote=True)
                        self.page(
                            "<h2>Connect OpenAI API</h2><p>1. <a href='https://platform.openai.com/api-keys' target='_blank' rel='noopener noreferrer'>Sign in to OpenAI and create an API key ↗</a></p><p>2. Paste the key below. API billing is separate from a ChatGPT subscription. The key stays on this computer, outside chat and audit logs.</p>"
                            f"<form method='post'><label>API key<input type='password' name='key' autocomplete='off' required maxlength='512'></label><label>API model<input name='model' value='{model}' required maxlength='100' placeholder='Your enabled API model ID'></label><button>Verify and connect</button></form>"
                        )

                    def do_POST(self):
                        if not self.valid() or self.headers.get("Origin") != "http://" + owner.address:
                            self.page("<p>Invalid connection request.</p>", 403)
                            return
                        if not self.path.endswith("/openai"):
                            self.page("<p>Unsupported request.</p>", 405)
                            return
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                            if not 0 < length <= 2048:
                                raise ValueError()
                            data = parse_qs(self.rfile.read(length).decode(), max_num_fields=4)
                            key, model = data["key"][0].strip(), data["model"][0].strip()
                            if not key or len(key) > 512 or not model or len(model) > 100:
                                raise ValueError()
                            owner.connect_openai(key, model)
                        except Exception:  # noqa: BLE001 -- never echo SDK errors or submitted secrets
                            self.page(
                                "<h2>Could not connect</h2><p>Check the API key, model access and network, then go back and retry. No key was added to chat or logs.</p>",
                                400,
                            )
                            return
                        self.page(
                            "<h2>OpenAI connected</h2><p>Credentials and model access verified. Return to Wayland; all roles use this connection immediately. A successful analysis is still required to verify Responses API compatibility.</p>"
                        )

                self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                self.server.daemon_threads = True
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            if provider == "ibkr":
                self.portal.start()
            return {"url": f"http://{self.address}/{self.token}/{provider}"}

    @property
    def address(self):
        assert self.server is not None
        return "127.0.0.1:" + str(self.server.server_port)

    def connect_openai(self, key, model):
        from openai import OpenAI

        with OpenAI(api_key=key, timeout=8, max_retries=0) as client:
            client.models.retrieve(model)
        with self.runtime.lock:
            self.runtime.dispatch("trading_settings", patch={"openai_model": model})
            save_api_key(self.runtime.root, key)
            with self.runtime.store() as store:
                store.set("ui:provider", {"status": "Connected · credentials and model verified"})

    def close(self):
        self.portal.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
