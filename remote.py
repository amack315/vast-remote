#!/usr/bin/env python3
"""Run commands on a Vast.ai instance through Jupyter's terminal API over a Cloudflare tunnel.

Works from HTTPS-only environments (no SSH). A named Jupyter terminal is a
persistent bash session on the instance: cwd, env vars, activated venvs and
background jobs survive between `run` calls.

Usage:
  remote.py run  "<shell command>"      [--id ID] [--term NAME] [--timeout S]
  remote.py put  <local> <remote_path>  [--id ID]
  remote.py get  <remote_path> <local>  [--id ID]
  remote.py ls   [remote_dir]           [--id ID]
  remote.py terms                       [--id ID]   # list terminals
  remote.py reset [--term NAME]         [--id ID]   # kill the terminal (fresh shell next run)

Connection info comes from ~/.vast-remote/<id>.json written by `vastctl.py wait|info`
(--id defaults to ~/.vast-remote/current), or from env VAST_JUPYTER_URL + VAST_TOKEN.
Requires: pip install websocket-client
"""
import argparse, base64, json, os, sys, time, urllib.parse, urllib.request, uuid

try:
    import websocket
except ImportError:
    sys.exit("pip install websocket-client")

import re
STATE_DIR = os.path.expanduser("~/.vast-remote")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>]")


def load_conn(iid):
    if os.environ.get("VAST_JUPYTER_URL") and os.environ.get("VAST_TOKEN"):
        return os.environ["VAST_JUPYTER_URL"].rstrip("/"), os.environ["VAST_TOKEN"]
    if iid is None:
        try:
            iid = open(os.path.join(STATE_DIR, "current")).read().strip()
        except FileNotFoundError:
            sys.exit("no --id and no ~/.vast-remote/current; run `vastctl.py wait <id>` first")
    info = json.load(open(os.path.join(STATE_DIR, f"{iid}.json")))
    return info["jupyter"].rstrip("/"), info["token"]


class Jup:
    def __init__(self, base, token):
        self.base, self.token = base, token
        self.hdr = {"Authorization": f"Bearer {token}"}  # portal edge wants Bearer; Jupyter wants ?token=

    def url(self, path, **q):
        q["token"] = self.token
        return f"{self.base}{path}?{urllib.parse.urlencode(q)}"

    def req(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url(path), data=data, method=method,
                                   headers={**self.hdr, "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=120) as resp:
            b = resp.read()
            return b if raw else (json.loads(b) if b else None)

    def ws(self, path):
        wsurl = self.url(path).replace("https://", "wss://", 1)
        kw = {"header": [f"Authorization: Bearer {self.token}"], "timeout": 30}
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            p = urllib.parse.urlparse(proxy)
            kw.update(http_proxy_host=p.hostname, http_proxy_port=p.port, proxy_type="http")
        return websocket.create_connection(wsurl, **kw)

    # ---- terminals -------------------------------------------------------
    def ensure_term(self, name):
        names = {t["name"] for t in self.req("GET", "/api/terminals")}
        if name not in names:
            self.req("POST", "/api/terminals", {"name": name})
            return True
        return False

    SETUP = ("stty -echo 2>/dev/null; set +m; bind 'set enable-bracketed-paste off' 2>/dev/null; "
             "export PS1='' PS2='' PROMPT_COMMAND=; unset HISTFILE; printf '\\n__RE%s__\\n' ADY\n")

    def _connect(self, term):
        ws = self.ws(f"/terminals/websocket/{term}")
        ws.send(json.dumps(["set_size", 50, 400, 800, 1000]))
        ws.settimeout(30)
        while True:
            m = ws.recv()
            if m and json.loads(m)[0] == "setup":
                break
        return ws

    def _recv_stdout(self, ws):
        """Next stdout chunk (ANSI-stripped, CRLF-normalised), or None if the socket closed."""
        while True:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                return ""
            except (websocket.WebSocketConnectionClosedException, ConnectionError, OSError):
                return None
            if not raw:
                return None
            m = json.loads(raw)
            if m[0] == "stdout":
                return ANSI_RE.sub("", m[1].replace("\r\n", "\n").replace("\r", ""))

    def _prepare(self, term):
        """Connect, put the shell into quiet mode, wait until it confirms."""
        self.ensure_term(term)
        ws = self._connect(term)
        ws.send(json.dumps(["stdin", self.SETUP]))
        buf, t_end = "", time.time() + 60
        ws.settimeout(2)
        while "__READY__" not in buf:
            if time.time() > t_end:
                raise RuntimeError(f"terminal '{term}' never became ready")
            c = self._recv_stdout(ws)
            if c is None:
                raise RuntimeError("websocket closed during setup")
            buf += c
        return ws

    def run(self, cmd, term="claude", timeout=None, quiet=False):
        """Run `cmd` in persistent terminal `term`; stream stdout; return (exit_code, output).
        `cmd` is eval'd in the terminal's shell, so cd/export/source persist across calls.
        Don't `exit` - that kills the shell (a fresh one is created next call)."""
        ws = self._prepare(term)
        nonce = uuid.uuid4().hex[:12]
        start, done = f"__S_{nonce}__", f"__E_{nonce}_"
        b64 = base64.b64encode(cmd.encode()).decode()
        # single line -> exactly one prompt, printed after the end marker
        line = (f"__c=$(printf %s {b64} | base64 -d); printf '\\n%s\\n' '{start}'; eval \"$__c\"; "
                f"__rc=$?; PS1='' PS2='' PROMPT_COMMAND=; printf '\\n%s%d__\\n' '{done}' $__rc\n")
        ws.send(json.dumps(["stdin", line]))
        buf, out, started, t0 = "", [], False, time.time()
        ws.settimeout(5)

        def emit(s):
            if s:
                out.append(s)
                if not quiet:
                    sys.stdout.write(s); sys.stdout.flush()

        while True:
            if timeout and time.time() - t0 > timeout:
                ws.close()
                raise TimeoutError(f"timeout after {timeout}s; command may still be running in terminal '{term}'")
            c = self._recv_stdout(ws)
            if c is None:
                # tunnel dropped the socket. Reconnect and queue a fresh end-marker: bash reads it only
                # after the running command finishes, and $? is still that command's status.
                if not quiet:
                    sys.stderr.write("[remote.py: websocket dropped; reconnecting, output in the gap is lost]\n")
                names = {t["name"] for t in self.req("GET", "/api/terminals")}
                if term not in names:
                    raise RuntimeError(f"terminal '{term}' died (did the command `exit`?); exit status unknown")
                ws = self._connect(term); ws.settimeout(5)
                ws.send(json.dumps(["stdin", f"printf '\\n%s%d__\\n' '{done}' $?\n"]))
                started = True
                continue
            buf += c
            if not started:
                i = buf.find(start)
                if i < 0:
                    continue
                buf, started = buf[i + len(start):].lstrip("\n"), True
            j = buf.find(done)
            if j >= 0:
                chunk = buf[:j]
                emit(chunk[:-1] if chunk.endswith("\n") else chunk)  # drop the newline our own printf added
                rest = buf[j + len(done):]
                while "__" not in rest:
                    c = self._recv_stdout(ws)
                    rest += c or ""
                ws.close()
                return int(rest[:rest.find("__")]), "".join(out)
            safe = max(0, len(buf) - len(done) - 8)   # keep a tail in case the marker is split across chunks
            emit(buf[:safe]); buf = buf[safe:]

    # ---- files via contents API -------------------------------------------
    def put(self, local, remote):
        data = open(local, "rb").read()
        remote = remote.lstrip("/")
        try:
            text = data.decode("utf-8")
            body = {"type": "file", "format": "text", "content": text}
        except UnicodeDecodeError:
            body = {"type": "file", "format": "base64", "content": base64.b64encode(data).decode()}
        return self.req("PUT", f"/api/contents/{remote}", body)

    def get(self, remote, local):
        remote = remote.lstrip("/")
        d = self.req("GET", f"/api/contents/{remote}")
        if d["type"] == "directory":
            sys.exit("remote path is a directory")
        content = d["content"]
        data = base64.b64decode(content) if d["format"] == "base64" else content.encode()
        with open(local, "wb") as f:
            f.write(data)
        return len(data)

    def ls(self, remote=""):
        d = self.req("GET", f"/api/contents/{remote.lstrip('/')}")
        if d["type"] != "directory":
            return [d]
        return sorted(d["content"], key=lambda c: (c["type"] != "directory", c["name"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id"); ap.add_argument("--term", default="claude")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run"); p.add_argument("command"); p.add_argument("--timeout", type=float)
    p = sub.add_parser("put"); p.add_argument("local"); p.add_argument("remote")
    p = sub.add_parser("get"); p.add_argument("remote"); p.add_argument("local")
    p = sub.add_parser("ls"); p.add_argument("remote", nargs="?", default="")
    sub.add_parser("terms"); sub.add_parser("reset")
    a = ap.parse_args()
    j = Jup(*load_conn(a.id))
    if a.cmd == "run":
        try:
            rc, _ = j.run(a.command, term=a.term, timeout=a.timeout)
        except (TimeoutError, RuntimeError) as e:
            sys.exit(f"remote.py: {e}")
        sys.exit(rc)
    if a.cmd == "put":
        r = j.put(a.local, a.remote); print(f"uploaded {a.local} -> /{r['path']} ({r.get('size')} bytes)")
    if a.cmd == "get":
        n = j.get(a.remote, a.local); print(f"downloaded {a.remote} -> {a.local} ({n} bytes)")
    if a.cmd == "ls":
        for c in j.ls(a.remote):
            print(f"{'d' if c['type']=='directory' else '-'} {str(c.get('size') or ''):>10}  {c['name']}")
    if a.cmd == "terms":
        for t in j.req("GET", "/api/terminals"):
            print(t["name"], t["last_activity"])
    if a.cmd == "reset":
        j.req("DELETE", f"/api/terminals/{a.term}"); print(f"terminal '{a.term}' killed")


if __name__ == "__main__":
    main()
