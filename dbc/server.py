"""HTTP API。

GUI とエージェントの唯一の接点。外には出さない。待ち受けは
127.0.0.1 だけで、PC からは ssh のポートフォワードで届く
(仕様書「接続方式」)。

HTTP は標準ライブラリの ThreadingHTTPServer を使う。個人利用で同時接続
4つが上限なので、これで足りる。
"""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import API_VERSION, __version__
from .auth import AuthError, TokenAuth
from .config import Config
from .sampler import Sampler, now_ms

log = logging.getLogger("dbc.server")

# 仕様書「性能要件」同時接続数: 4まで(個人利用)。
# SSE は1本につきスレッドを1つ占めるので、ここで止めないと
# 接続を取り直すたびにスレッドが積み上がる。
MAX_STREAMS = 4
STREAM_INTERVAL_S = 2.0

# /version が返す対応機能。GUI は未知の名前を無視する。
FEATURES = ["snapshot", "history", "units", "alerts", "health", "stream"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"dbc/{__version__}"
    sys_version = ""  # Python の版を外に出さない

    # ThreadingHTTPServer が属性としてぶら下げる
    sampler: Sampler
    auth: TokenAuth
    streams: threading.Semaphore
    stopping: threading.Event

    def log_message(self, fmt: str, *args) -> None:
        # 既定は stderr へ直書きなので journald 向けに logging へ流す。
        # 定常時のアクセスログは出さない(HDD とログ量を増やさないため)。
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- 返す ---

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err_code: str, message: str) -> None:
        self._send(code, {"error": {"code": err_code, "message": message}})

    # --- 受ける ---

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler の規約
        path = urlparse(self.path).path.rstrip("/") or "/"
        query = parse_qs(urlparse(self.path).query)

        # /version も認証を免除しない(仕様書「API仕様」共通の規約)。
        # 版だけでも、どのサーバで何が動いているかの手掛かりになる。
        try:
            self.auth.check_header(self.headers.get("Authorization"))
        except AuthError as e:
            if e.locked:
                self.send_response(429)
                self.send_header("Retry-After", "60")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._error(401, "unauthorized", str(e))
            return

        if path == "/version":
            self._send(200, {
                "agent_version": __version__,
                "api_version": API_VERSION,
                "features": FEATURES,
            })
            return

        prefix = f"/api/v{API_VERSION}/"
        if not path.startswith(prefix):
            self._error(404, "not_found", f"{path} は提供していません")
            return

        name = path[len(prefix):]
        if name == "snapshot":
            self._send(200, self.sampler.snapshot())
        elif name == "units":
            self._send(200, self.sampler.units())
        elif name == "alerts":
            self._send(200, self.sampler.alerts())
        elif name == "health":
            self._send(200, self.sampler.health())
        elif name == "history":
            self._history(query)
        elif name == "stream":
            self._stream()
        else:
            self._error(404, "not_found", f"{path} は提供していません")

    def _history(self, query: dict) -> None:
        raw = query.get("since", [None])[0]
        since = None
        if raw is not None:
            try:
                since = int(raw)
            except ValueError:
                self._error(400, "bad_request", "since は Unix 時刻のミリ秒で指定してください")
                return
        self._send(200, {
            "ts": now_ms(),
            "since": since,
            "points": self.sampler.history.since(since),
            "unit_points": self.sampler.units_history.since(since),
        })

    def _stream(self) -> None:
        """SSE。2秒ごとに現在値を流す。

        1本でスレッドを1つ握るので、上限に達したら 503 で断る。
        """
        if not self.streams.acquire(blocking=False):
            self._error(503, "too_many_streams",
                        f"同時接続は {MAX_STREAMS} 本までです")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while not self.stopping.is_set():
                body = json.dumps(self.sampler.snapshot(), ensure_ascii=False)
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
                # 停止要求に 2 秒待たされないよう、Event の wait で寝る
                if self.stopping.wait(STREAM_INTERVAL_S):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            # GUI 側が切っただけ。異常ではない。
            pass
        finally:
            self.streams.release()

    def do_POST(self) -> None:  # noqa: N802
        # 書き込み系の口は持たない。監視専用。
        self._error(405, "method_not_allowed", "GET だけを受け付けます")


def build(config: Config, sampler: Sampler, auth: TokenAuth) -> ThreadingHTTPServer:
    """待ち受けを作る。bind の検証は Config 側で済んでいる。"""
    httpd = ThreadingHTTPServer((config.bind, config.port), _Handler)
    httpd.daemon_threads = True
    _Handler.sampler = sampler
    _Handler.auth = auth
    _Handler.streams = threading.Semaphore(MAX_STREAMS)
    _Handler.stopping = threading.Event()
    return httpd


def serve_forever(httpd: ThreadingHTTPServer) -> None:
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        _Handler.stopping.set()
        httpd.server_close()


def demo() -> None:
    """実際に待ち受けて、認証と各エンドポイントを1往復ずつ確かめる。"""
    import urllib.error
    import urllib.request
    from pathlib import Path

    from .auth import hash_token

    from .sampler import fixture_roots

    import socket

    # 空きポートを借りる。Config は port=0 を設定の書き間違いとして弾くので、
    # 実番号を取ってから渡す。
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    token = "0" * 64
    cfg = Config(bind="127.0.0.1", port=free_port)
    fixtures = Path(__file__).parent.parent / "tests/fixtures/arch"
    # 模擬データはリポジトリに持たないので、無ければ分かる言い方で止める
    assert fixtures.is_dir(), f"先に python tests/make_fixtures.py を実行してください ({fixtures})"
    sampler = Sampler(cfg, fixtures=fixture_roots(fixtures))
    # 鮮度の判定は time.monotonic() を基準にするので、過去 2 秒の
    # 時刻で回して「たった今取れた値」の状態を作る
    t = time.monotonic()
    sampler.tick(t - 2.0)
    sampler.tick(t)

    httpd = build(cfg, sampler, TokenAuth(hash_token(token)))
    port = httpd.server_address[1]
    threading.Thread(target=serve_forever, args=(httpd,), daemon=True).start()

    def get(path: str, tok: str | None = token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if tok is not None:
            req.add_header("Authorization", f"Bearer {tok}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    try:
        # 認証なしは 401。/version も例外にしない。
        for path in ("/version", "/api/v1/snapshot"):
            try:
                get(path, tok=None)
            except urllib.error.HTTPError as e:
                assert e.code == 401, (path, e.code)
                assert json.loads(e.read())["error"]["code"] == "unauthorized"
            else:
                raise AssertionError(f"{path} が認証なしで通った")

        status, body = get("/version")
        assert status == 200 and body["api_version"] == API_VERSION, body

        for path in ("snapshot", "units", "alerts", "health", "history"):
            status, body = get(f"/api/v1/{path}")
            assert status == 200 and "ts" in body, (path, body)

        # 履歴は since で絞れる。境界は含まない。
        _, all_points = get("/api/v1/history")
        assert all_points["points"], "履歴が空"
        last = all_points["points"][-1]["ts"]
        _, narrowed = get(f"/api/v1/history?since={last}")
        assert narrowed["points"] == [], narrowed["points"]

        for path, code in (("/api/v1/history?since=abc", 400), ("/api/v1/nope", 404), ("/nope", 404)):
            try:
                get(path)
            except urllib.error.HTTPError as e:
                assert e.code == code, (path, e.code)
            else:
                raise AssertionError(f"{path} が通ってしまった")

        # fixture に /proc があるので cpu は ok、statvfs 不能な disk_usage は unsupported
        _, snap = get("/api/v1/snapshot")
        assert snap["collectors"]["cpu"] == "ok", snap["collectors"]
        assert snap["collectors"]["disk_usage"] == "unsupported", snap["collectors"]
    finally:
        httpd.shutdown()
        time.sleep(0.2)

    print("server OK")


if __name__ == "__main__":
    demo()
