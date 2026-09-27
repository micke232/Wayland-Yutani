"""Managed IBKR browser gateway. Authentication/inspection only; no order API."""

import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

GATEWAY_URL = "https://download2.interactivebrokers.com/portal/clientportal.gw.zip"


def unpack_gateway(data: bytes, destination: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 250_000_000:
            raise ValueError("Gateway archive exceeds size limit")
        for item in archive.infolist():
            target = destination / item.filename
            if (
                not target.resolve().is_relative_to(destination.resolve())
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe gateway archive")
        archive.extractall(destination)


def java_binary(progress):
    for candidate in (
        "/opt/homebrew/opt/openjdk@21/bin/java",
        "/usr/local/opt/openjdk@21/bin/java",
        shutil.which("java"),
    ):
        if candidate and Path(candidate).is_file():
            result = subprocess.run([candidate, "-version"], capture_output=True, timeout=10, check=False)
            if result.returncode == 0:
                return Path(candidate)
    brew = shutil.which("brew") or next(
        (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if Path(p).is_file()), None
    )
    if not brew:
        raise RuntimeError("Java is missing; automatic installation requires Homebrew")
    progress("Installing Java for IBKR · this may take several minutes")
    subprocess.run(
        [brew, "install", "openjdk@21"],
        env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=1200,
        check=True,
    )
    prefix = subprocess.check_output([brew, "--prefix", "openjdk@21"], timeout=10, text=True).strip()
    return Path(prefix) / "bin/java"


class PortalConnection:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.thread = None
        self.process = None
        self.lock = threading.Lock()
        self.state = {"status": "Not connected", "connected": False}
        self.url = ""
        self.opener = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def publish(self, status, **extra):
        with self.lock:
            self.state = {"status": status, "connected": False, **extra}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.publish("Preparing IBKR browser login")
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def prepare(self):
        self.publish("Checking Java")
        java = java_binary(self.publish)
        if self.stop.is_set():
            return
        folder = self.runtime.root / "connections/ibkr-gateway"
        folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (folder / "bin/run.sh").exists():
            self.publish("Downloading the official IBKR Client Portal Gateway")
            with urllib.request.urlopen(GATEWAY_URL, timeout=60) as response:
                data = response.read(50_000_001)
            if len(data) > 50_000_000:
                raise ValueError("Gateway download exceeds limit")
            temporary = folder.with_name("ibkr-install")
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(mode=0o700)
            unpack_gateway(data, temporary)
            temporary.rename(folder)
        folder.chmod(0o700)
        # Vendor debug logging includes session/configuration data. Disable it before any login.
        (folder / "root/logback.xml").write_text('<configuration><root level="OFF"/></configuration>')
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.url = f"https://localhost:{port}"
        # Generate an app-local certificate. Python trusts only this certificate; no verify=False.
        cert, keystore = folder / "root/wayland.pem", folder / "root/wayland.jks"
        password_file = folder / "root/cert-password"
        if not cert.exists() or not keystore.exists() or not password_file.exists():
            cert.unlink(missing_ok=True)
            keystore.unlink(missing_ok=True)
            import secrets

            password = secrets.token_urlsafe(24)
            descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(password)
            common = [
                str(java.with_name("keytool")),
                "-keystore",
                str(keystore),
                "-storepass:file",
                str(password_file),
            ]
            subprocess.run(
                [
                    *common,
                    "-genkeypair",
                    "-storetype",
                    "JKS",
                    "-alias",
                    "wayland",
                    "-keyalg",
                    "RSA",
                    "-validity",
                    "3650",
                    "-dname",
                    "CN=localhost",
                    "-ext",
                    "SAN=dns:localhost,ip:127.0.0.1",
                    "-keypass:file",
                    str(password_file),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                [*common, "-exportcert", "-alias", "wayland", "-rfc", "-file", str(cert)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            keystore.chmod(0o600)
        password = password_file.read_text()
        config = folder / "root/wayland.yaml"
        config.write_text(
            json.dumps(
                {
                    "ip2loc": "US",
                    "proxyRemoteSsl": True,
                    "proxyRemoteHost": "https://api.ibkr.com",
                    "listenPort": port,
                    "listenSsl": True,
                    "ccp": False,
                    "svcEnvironment": "v1",
                    "sslCert": "wayland.jks",
                    "sslPwd": password,
                    "authDelay": 3000,
                    "portalBaseURL": "",
                    "serverOptions": {
                        "eventLoopPoolSize": 2,
                        "workerPoolSize": 4,
                        "internalBlockingPoolSize": 2,
                    },
                    "cors": {"origin.allowed": self.url, "allowCredentials": False},
                    "webApps": [],
                    "ips": {"allow": ["127.0.0.1"], "deny": []},
                }
            )
        )
        config.chmod(0o600)
        context = ssl.create_default_context(cafile=str(cert))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect()
        )
        self.publish("Starting IBKR login service")
        if self.stop.is_set():
            return
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("gateway_process.py")),
                str(java),
                "-Xmx256m",
                "-Dvertx.disableDnsResolver=true",
                "-Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory",
                "-Djava.net.preferIPv4Stack=true",
                "-cp",
                "root:dist/ibgroup.web.core.iblink.router.clientportal.gw.jar:build/lib/runtime/*",
                "ibgroup.web.core.clientportal.gw.GatewayStart",
                "--conf",
                "../root/wayland.yaml",
            ],
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.stop.wait(0.2):
                return
            if self.process.poll() is not None:
                raise RuntimeError("IBKR login service stopped during startup")
            try:
                self.get("iserver/auth/status")
                return
            except urllib.error.HTTPError as error:
                if error.code == 401:  # Gateway is ready; the user has not authenticated yet.
                    return
            except Exception:  # noqa: BLE001, S110 -- startup polling
                pass
        raise RuntimeError("IBKR login service did not become ready")

    def get(self, endpoint):
        # Fixed read-only calls only. No arbitrary URLs, methods, orders or cancellation endpoint.
        if endpoint not in {
            "iserver/auth/status",
            "iserver/accounts",
            "portfolio/accounts",
            "iserver/account/orders",
        } and not re.fullmatch(r"portfolio/DU[0-9]+/positions/[0-9]+", endpoint):
            raise ValueError("Unsupported read-only IBKR endpoint")
        assert self.opener is not None
        with self.opener.open(self.url + "/v1/api/" + endpoint, timeout=5) as response:
            return json.loads(response.read(2_000_000))

    def check(self):
        try:
            auth = self.get("iserver/auth/status")
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            auth = {}
        if (
            auth.get("authenticated") is not True
            or auth.get("connected") is not True
            or auth.get("competing")
        ):
            self.publish("Waiting for IBKR browser login", url=self.url)
            return
        accounts = self.get("iserver/accounts").get("accounts", [])
        paper = [a for a in accounts if isinstance(a, str) and re.fullmatch(r"DU[0-9]+", a)]
        if not paper or len(paper) != len(accounts):
            self.publish("Rejected · log in with paper credentials, not a live account", url=self.url)
            return
        config = self.runtime.settings()
        account = (
            config.ibkr_account if config.ibkr_account in paper else paper[0] if len(paper) == 1 else None
        )
        if account is None:
            self.publish("Multiple paper accounts · choose the account in Connections", url=self.url)
            return
        if config.ibkr_account != account or account not in config.account_allowlist:
            self.runtime.dispatch(
                "trading_settings", patch={"ibkr_account": account, "account_allowlist": account}
            )
        self.publish("Connected · IBKR browser session · execution locked", connected=True, url=self.url)

    def run(self):
        try:
            self.prepare()
            while not self.stop.is_set():
                try:
                    self.check()
                except Exception:  # noqa: BLE001 -- never persist broker response payloads
                    self.publish("IBKR connection unavailable · retry browser login", url=self.url)
                if self.stop.wait(5):
                    break
                if self.process and self.process.poll() is not None:
                    raise RuntimeError("IBKR login service stopped")
        except Exception as error:  # noqa: BLE001 -- no secrets in errors
            self.publish("IBKR login setup failed · " + type(error).__name__ + " · retry Sign in")
        finally:
            self.terminate()

    def terminate(self):
        if self.process and self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=6)
        self.terminate()
        self.publish("Disconnected")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected local gateway redirect")
