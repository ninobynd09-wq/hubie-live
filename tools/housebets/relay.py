#!/usr/bin/env python3
"""Local authenticated-proxy relay.

Chrome cannot pass proxy credentials on the command line.
This sits on 127.0.0.1:8899, adds Proxy-Authorization for the upstream
authenticated proxy, and lets a *plain* google-chrome start use it:

  google-chrome --proxy-server=http://127.0.0.1:8899 \
                --remote-debugging-port=9222 --user-data-dir=...

Upstream is HUBIE_GAMES_PROXY=http://user:pass@host:port (environment or the repo .env).
"""
import base64, re, socket, socketserver, sys, threading, select

LISTEN = ("127.0.0.1", 8899)


def _proxy_url() -> str:
    """HUBIE_GAMES_PROXY (http://user:pass@host:port): from the environment, else the repo's .env."""
    import os
    if os.environ.get("HUBIE_GAMES_PROXY"):
        return os.environ["HUBIE_GAMES_PROXY"].strip()
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    try:
        for line in open(env):
            if line.startswith("HUBIE_GAMES_PROXY="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    raise SystemExit("set HUBIE_GAMES_PROXY=http://user:pass@host:port (environment or .env)")


def upstream():
    m = re.match(r"http://([^:]+):([^@]+)@([^:]+):(\d+)", _proxy_url())
    if not m:
        raise SystemExit("HUBIE_GAMES_PROXY must look like http://user:pass@host:port")
    user, pw, host, port = m.groups()
    cred = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    return host, int(port), cred


UP_HOST, UP_PORT, UP_AUTH = upstream()


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.request.recv(65536)
                if not chunk:
                    return
                head += chunk
            text = head.decode("latin-1")
            line = text.split("\r\n", 1)[0]
            method, target, _ = line.split(" ", 2)

            if method.upper() == "CONNECT":
                up = socket.create_connection((UP_HOST, UP_PORT), timeout=30)
                up.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n"
                            "Proxy-Authorization: Basic %s\r\n\r\n"
                            % (target, target, UP_AUTH)).encode())
                resp = up.recv(4096)
                if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    up.close()
                    return
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                pipe(self.request, up)
                up.close()
                return

            # plain HTTP: forward with credentials
            path = target if target.startswith("http") else "http://%s%s" % (
                text.split("Host: ", 1)[1].split("\r\n", 1)[0].strip(), target)
            m = re.match(r"(https?)://([^/]+)(.*)", path)
            if not m:
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            _, hostport, rest = m.groups()
            host, _, port = hostport.partition(":")
            up = socket.create_connection((UP_HOST, UP_PORT), timeout=30)
            headers = ["%s %s HTTP/1.1" % (method, path), "Host: %s" % hostport,
                       "Proxy-Authorization: Basic %s" % UP_AUTH]
            for h in text.split("\r\n")[1:]:
                if not h or h.lower().startswith(("proxy-connection", "connection", "host")):
                    continue
                headers.append(h)
            up.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
            pipe(self.request, up)
            up.close()
        except Exception as e:
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
            print("relay error:", type(e).__name__, e, flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("relay up on %s:%d -> %s:%d" % (LISTEN[0], LISTEN[1], UP_HOST, UP_PORT), flush=True)
    Server(LISTEN, Handler).serve_forever()
