"""The launcher's web server: plain HTTP on asyncio, one request per
connection, on 127.0.0.1 only. The page is open to anyone who can reach it
(it holds nothing), every /api/ request must bring the run's token, and
nothing is answered for another host name or another site's page."""

import asyncio
import hmac
import json
import traceback
import urllib.parse

MAX_BODY = 64 << 10
MAX_HEADERS = 64
READ_TIMEOUT = 10.0
TOKEN_HEADER = "x-flsync-token"
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           413: "Payload Too Large", 500: "Internal Server Error"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class Request:
    def __init__(self, method, target, headers, body):
        self.method = method
        parts = urllib.parse.urlsplit(target)
        self.path = parts.path
        self.query = urllib.parse.parse_qs(parts.query)
        self.headers = headers  # lower-case names
        self.body = body

    def json(self):
        """The body as a JSON object ({} if empty); ValueError if it isn't one."""
        if not self.body:
            return {}
        data = json.loads(self.body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ett JSON-objekt väntades")
        return data


class Response:
    def __init__(self, status=200, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        self.status = status
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = dict(SECURITY_HEADERS)
        self.headers["Content-Type"] = content_type
        self.headers.update(headers or {})


def json_response(data, status=200):
    return Response(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")


def text_response(status, text):
    return Response(status, text)


class Guard:
    """Who may talk to the launcher: requests for 127.0.0.1:<port> (or
    localhost) from no page or our own page, and for the API with the token."""

    def __init__(self, token):
        self.token = token
        self.port = None

    def hosts(self):
        return {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    def problem(self, request, api=True):
        """Why `request` is refused (Swedish), or None."""
        hosts = self.hosts()
        if request.headers.get("host", "").lower() not in hosts:
            return "fel adress"  # e.g. DNS rebinding: someone else's name for 127.0.0.1
        origin = request.headers.get("origin")
        if origin is not None and origin.lower() not in {"http://" + host for host in hosts}:
            return "fel ursprung"
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return "fel ursprung"
        if api:
            given = request.headers.get(TOKEN_HEADER, "").encode("utf-8", "replace")
            if not hmac.compare_digest(given, self.token.encode("utf-8")):
                return "fel nyckel"
        return None


class Server:
    """Calls `await handler(request)` for every request and sends back the Response it returns."""

    def __init__(self, handler, host="127.0.0.1", port=0, logger=None):
        self.handler = handler
        self.host = host
        self.port = port
        self.log = logger or (lambda msg: None)
        self._server = None
        self._tasks = set()

    async def start(self):
        self._server = await asyncio.start_server(self._serve, self.host, self.port, limit=16 << 10)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self._server is None:
            return
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=2)
        try:
            await asyncio.wait_for(self._server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass
        self._server = None

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            try:
                request = await asyncio.wait_for(read_request(reader), READ_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                request = None
            if request is None:
                response = text_response(400, "trasig förfrågan")
            elif isinstance(request, Response):
                response = request
            else:
                try:
                    response = await self.handler(request)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.log("fel i webbsidans server:\n" + traceback.format_exc())
                    response = text_response(500, "internt fel")
            writer.write(encode(response))
            await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionError, OSError):
                pass
            self._tasks.discard(task)


async def read_request(reader):
    """The next request, a Response refusing it, or None when nothing came."""
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return text_response(400, "trasig förfrågan")
    method, target, _ = parts
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        if len(headers) >= MAX_HEADERS:
            return text_response(400, "för många rubriker")
        name, colon, value = line.decode("latin-1").partition(":")
        if not colon:
            return text_response(400, "trasig rubrik")
        headers[name.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        return text_response(400, "trasig längd")
    if length < 0 or length > MAX_BODY:
        return text_response(413, "för stor förfrågan")
    body = await reader.readexactly(length) if length else b""
    return Request(method, target, headers, body)


def encode(response):
    head = ["HTTP/1.1 %d %s" % (response.status, REASONS.get(response.status, "OK"))]
    headers = dict(response.headers, **{"Content-Length": str(len(response.body)), "Connection": "close"})
    head.extend("%s: %s" % item for item in headers.items())
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + response.body
