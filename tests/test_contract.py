"""契約テスト。

docs/api/v1.schema.json が API 仕様の正本。実際に API を立てて、全エンドポイントの
応答がスキーマに合うかを見る。C++ 版に置き換えたとき、同じテストが両方で
通ることを条件にする(仕様書「開発とテスト方針」契約テスト)。

GUI 側のリポジトリも同じスキーマファイルを参照して、モックの応答を検証する。
"""

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jsonschema import Draft202012Validator  # noqa: E402

from svcscope import API_VERSION, server  # noqa: E402
from svcscope.auth import TokenAuth, hash_token  # noqa: E402
from svcscope.config import Config  # noqa: E402
from svcscope.sampler import Sampler, fixture_roots  # noqa: E402

ROOT = Path(__file__).parent.parent
SCHEMA = json.loads((ROOT / "docs/api/v1.schema.json").read_text(encoding="utf-8"))
TOKEN = "f" * 64

FIXTURE_DIR = ROOT / "tests/fixtures/arch"
# 模擬データは生成物でリポジトリに持たないので、無ければここで作る
if not FIXTURE_DIR.is_dir():
    import make_fixtures

    make_fixtures.main()


def validator(name: str) -> Draft202012Validator:
    """スキーマの $defs の1つを指す検証器を作る。"""
    return Draft202012Validator({
        "$schema": SCHEMA["$schema"],
        "$id": SCHEMA["$id"],
        "$defs": SCHEMA["$defs"],
        "$ref": f"#/$defs/{name}",
    })


def check(name: str, body: dict) -> None:
    errors = sorted(validator(name).iter_errors(body), key=lambda e: list(e.path))
    if errors:
        lines = [f"  {list(e.path)}: {e.message}" for e in errors[:5]]
        raise AssertionError(f"{name} がスキーマに合いません:\n" + "\n".join(lines))


class Agent:
    """fixture を読む API を一時的に立てる。"""

    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cfg = Config(bind="127.0.0.1", port=self.port)
        self.sampler = Sampler(cfg, fixtures=fixture_roots(ROOT / "tests/fixtures/arch"))
        # 鮮度は time.monotonic() 基準なので、過去の時刻で回して
        # 「たった今取れた値」の状態を作る
        t = time.monotonic()
        for offset in (-6.0, -4.0, -2.0, 0.0):
            self.sampler.tick(t + offset)
        self.httpd = server.build(cfg, self.sampler, TokenAuth(hash_token(TOKEN)))

    def __enter__(self):
        threading.Thread(target=server.serve_forever, args=(self.httpd,), daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        time.sleep(0.2)

    def get(self, path: str, token: str | None = TOKEN) -> tuple[int, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {})


def test_responses_match_schema() -> None:
    with Agent() as a:
        status, body = a.get("/version")
        assert status == 200
        check("version", body)
        assert body["api_version"] == API_VERSION

        for name in ("snapshot", "units", "alerts", "health", "history"):
            status, body = a.get(f"/api/v1/{name}")
            assert status == 200, (name, status)
            check(name, body)

        # 取れている項目は collectors が ok、キーも在る。
        # 取れていない項目はキーを出さない(GUI が「0」と誤読しないように)
        _, snap = a.get("/api/v1/snapshot")
        for key in ("cpu", "memory", "psi"):
            assert snap["collectors"][key] == "ok", snap["collectors"]
            assert key in snap, key
        assert snap["collectors"]["disk_usage"] == "unsupported"
        assert "filesystems" not in snap.get("disk", {}), snap["disk"]


def test_history_since_is_exclusive() -> None:
    with Agent() as a:
        _, all_ = a.get("/api/v1/history")
        check("history", all_)
        assert len(all_["points"]) >= 2, all_["points"]
        last = all_["points"][-1]["ts"]
        _, narrowed = a.get(f"/api/v1/history?since={last}")
        check("history", narrowed)
        assert narrowed["points"] == []
        # 1 点前から取れば最後の 1 点だけ返る
        prev = all_["points"][-2]["ts"]
        _, tail = a.get(f"/api/v1/history?since={prev}")
        assert [p["ts"] for p in tail["points"]] == [last], tail["points"]


def test_errors_match_schema() -> None:
    with Agent() as a:
        # 認証は全エンドポイント必須。/version も免除しない
        for path in ("/version", "/api/v1/snapshot", "/api/v1/stream"):
            status, body = a.get(path, token=None)
            assert status == 401, (path, status)
            check("error", body)
            assert body["error"]["code"] == "unauthorized"

        for path, code, err in (
            ("/api/v1/history?since=xyz", 400, "bad_request"),
            ("/api/v1/nope", 404, "not_found"),
            ("/nope", 404, "not_found"),
        ):
            status, body = a.get(path)
            assert status == code, (path, status)
            check("error", body)
            assert body["error"]["code"] == err, body


def test_lockout_after_repeated_failures() -> None:
    """連続失敗で締め出し、429 と Retry-After を返す。"""
    from svcscope.auth import MAX_FAILURES

    with Agent() as a:
        for _ in range(MAX_FAILURES):
            assert a.get("/version", token="0" * 64)[0] == 401
        # 正しいトークンでも、締め出し中は通さない
        assert a.get("/version")[0] == 429


def test_stream_sends_events() -> None:
    with Agent() as a:
        req = urllib.request.Request(f"http://127.0.0.1:{a.port}/api/v1/stream")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.headers["Content-Type"].startswith("text/event-stream"), r.headers
            for raw in r:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("data: "):
                    check("snapshot", json.loads(line[6:]))
                    break
            else:
                raise AssertionError("SSE が1件も来ませんでした")


def main() -> None:
    assert Draft202012Validator.check_schema(SCHEMA) is None
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  {t.__name__} OK")
    print(f"contract {len(tests)} 件 OK")


if __name__ == "__main__":
    main()
