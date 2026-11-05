"""A fault-injecting proxy in front of a real node, to act as a bad RPC provider."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RpcProxy:
    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.down = False
        self.rate_limited = False
        self.max_log_range: int | None = None
        self.calls: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["content-length"]))
                request = json.loads(body)
                outer.calls.append(request["method"])
                if outer.down:
                    self.send_error(503)
                    return
                if outer.rate_limited:
                    self.send_error(429)
                    return
                if outer._log_range_exceeded(request):
                    self._reply(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {
                                    "code": -32600,
                                    "message": "query exceeds max block range "
                                    f"{outer.max_log_range}",
                                },
                            }
                        ).encode()
                    )
                    return
                forwarded = urllib.request.Request(
                    outer.upstream, data=body, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(forwarded, timeout=10) as response:
                    self._reply(response.read())

            def _reply(self, payload: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _log_range_exceeded(self, request: dict) -> bool:
        if self.max_log_range is None or request["method"] != "eth_getLogs":
            return False
        query = request["params"][0]
        if "fromBlock" not in query:
            return False
        size = int(query["toBlock"], 16) - int(query["fromBlock"], 16) + 1
        return size > self.max_log_range

    @property
    def url(self) -> str:
        # The path stands in for the API key that real provider URLs contain.
        return f"http://127.0.0.1:{self._server.server_port}/v2/SECRET-API-KEY"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
