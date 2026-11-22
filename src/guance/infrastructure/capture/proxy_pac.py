"""Route the capture allowlist through the helper and preserve normal browsing."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
from threading import Thread
from uuid import uuid4

from guance.ports.capture import ProxySnapshot, UpstreamProxy


class ProxyRoutingError(ValueError):
    """The original route cannot be retained with a scoped PAC."""


_HOST_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]*", re.ASCII)
_BYPASS_PATTERN = re.compile(r"[a-z0-9.*?_-]+", re.ASCII)
_ENDPOINT = re.compile(r"(\[[0-9a-f:]+\]|[a-z0-9._-]+)(?::([0-9]{1,5}))?", re.I)
_SECURE_SCHEMES = ("https", "wss")


def _endpoint(text: str) -> UpstreamProxy:
    match = _ENDPOINT.fullmatch(text.strip())
    if not match:
        raise ProxyRoutingError("当前手动代理地址的格式暂不支持，采集未启动。")
    host, port = match.groups()
    try:
        return UpstreamProxy(host, int(port or "80"))
    except ValueError as exc:
        raise ProxyRoutingError("当前手动代理地址的格式暂不支持，采集未启动。") from exc


def _manual_routes(snapshot: ProxySnapshot) -> dict[str, UpstreamProxy]:
    if not snapshot.enabled:
        return {}
    if "=" not in snapshot.server:
        return {"all": _endpoint(snapshot.server)}
    routes = {}
    for entry in snapshot.server.split(";"):
        if not entry.strip():
            continue
        scheme, _, endpoint = entry.partition("=")
        scheme = scheme.strip().lower()
        if scheme not in {"http", "https"} or scheme in routes:
            raise ProxyRoutingError("当前手动代理使用了未支持的协议或重复规则，采集未启动。")
        routes[scheme] = _endpoint(endpoint)
    if not routes:
        raise ProxyRoutingError("系统手动代理缺少可用地址，采集未启动。")
    return routes


def _directive(proxy: UpstreamProxy | None) -> str:
    return "DIRECT" if proxy is None else "PROXY " + proxy.url.removeprefix("http://")


@dataclass(frozen=True)
class ScopedProxyRouting:
    script: bytes
    upstream: UpstreamProxy | None


def build_scoped_routing(
    original: ProxySnapshot, allowed_hosts: tuple[str, ...], listen_host: str, listen_port: int
) -> ScopedProxyRouting:
    hosts = sorted({host.lower().rstrip(".") for host in allowed_hosts})
    if not hosts or any(not _HOST_PATTERN.fullmatch(host) for host in hosts):
        raise ProxyRoutingError("采集域名配置无效。")
    if listen_host != "127.0.0.1":
        raise ProxyRoutingError("分流采集仅支持 IPv4 本机监听。")
    routes = _manual_routes(original)
    bypass = []
    if routes:
        for item in re.split(r"[;,]", original.bypass.lower()):
            item = item.strip()
            if not item:
                continue
            if item != "<local>" and not _BYPASS_PATTERN.fullmatch(item):
                raise ProxyRoutingError("当前代理包含未支持的绕过规则，采集未启动。")
            bypass.append("*" + item if item.startswith(".") else item)

    https = routes.get("all", routes.get("https"))
    http = routes.get("all", routes.get("http"))
    # Chromium's WebSocket fallback is HTTPS, then HTTP for per-scheme rules.
    websocket = https or http
    upstreams = {
        None if any(fnmatchcase(host, pattern) for pattern in bypass) else route
        for host in hosts for route in (https, websocket)
    }
    if len(upstreams) != 1:
        raise ProxyRoutingError("牌桌域名需要不同的系统代理路径，采集未启动。")
    upstream = upstreams.pop()
    if upstream and upstream.url.removeprefix("http://").lower() in {
        f"127.0.0.1:{listen_port}", f"localhost:{listen_port}",
        f"[::1]:{listen_port}",
    }:
        raise ProxyRoutingError("系统代理与采集监听使用了同一个端口。")
    fallback = {scheme: _directive(proxy) for scheme, proxy in (
        ("http", http), ("https", https), ("ws", websocket), ("wss", websocket),
    )}
    capture = f"PROXY {listen_host}:{listen_port}; {_directive(upstream)}"
    script = f"""function FindProxyForURL(url, host) {{
    var captureHosts = {json.dumps(hosts)};
    var bypass = {json.dumps(bypass)};
    var routes = {json.dumps(fallback)};
    var scheme = url.slice(0, url.indexOf(":")).toLowerCase();
    host = host.toLowerCase().replace(/\\.$/, "");
    if ((scheme === "https" || scheme === "wss") && captureHosts.indexOf(host) >= 0) {{
        return {json.dumps(capture)};
    }}
    if (host === "localhost" || host === "loopback" || /^127\\.|^169\\.254\\./.test(host)
        || host === "0.0.0.0" || host === "::1" || host === "[::1]") {{
        return "DIRECT";
    }}
    for (var i = 0; i < bypass.length; i++) {{
        var rule = bypass[i];
        if (rule === "<local>" ? host.indexOf(".") < 0 && host.indexOf(":") < 0
                              : shExpMatch(host, rule)) {{
            return "DIRECT";
        }}
    }}
    return routes[scheme] || {json.dumps(_directive(routes.get("all")))};
}}
"""
    return ScopedProxyRouting(script.encode("ascii"), upstream)


class LoopbackPacServer:
    """Serve one immutable PAC to Windows over a session-specific loopback URL."""

    def __init__(self, script: bytes) -> None:
        path = f"/guance-{uuid4().hex}.pac"

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(1.0)
                super().setup()

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                if self.path != path or self.headers.get("Host") != address:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
                self.send_header("Content-Length", str(len(script)))
                self.end_headers()
                self.wfile.write(script)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_port
        address = f"127.0.0.1:{port}"
        self.url = f"http://{address}{path}"
        self._thread = Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
            name="guance-proxy-pac", daemon=True,
        )
        self._thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", port, timeout=1.0)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                if response.status != 200 or response.read(len(script) + 1) != script:
                    raise OSError("Local PAC readiness check failed")
            finally:
                connection.close()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
            self._thread.join()
        self._server.server_close()
