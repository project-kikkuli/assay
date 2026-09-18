"""A fixed-target test ingress; candidates have no route to the external network."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Edge(BaseHTTPRequestHandler):
    def forward(self):
        provider = self.path.startswith("/provider/")
        path = self.path[len("/provider"):] if provider else self.path
        target = ("http://provider:8001" if provider else "http://api:8000") + path
        size = int(self.headers.get("Content-Length", "0"))
        if size > 65536 or size < 0:
            self.send_error(413)
            return
        headers = {key: self.headers[key] for key in ("Content-Type", "Authorization") if key in self.headers}
        data = self.rfile.read(size) if self.command == "POST" else None
        request = urllib.request.Request(target, data=data, headers=headers, method=self.command)
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        except OSError:
            self.send_error(502)
            return
        with response:
            body = response.read(1024 * 1024)
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'")
            self.end_headers()
            self.wfile.write(body)

    do_GET = forward
    do_POST = forward

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Edge).serve_forever()
