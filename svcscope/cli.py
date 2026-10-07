"""CUI クライアント。

    python -m svcscope.cli watch
    python -m svcscope.cli --url http://127.0.0.1:18765 snapshot

GUI を立ち上げずに値を見るための口。ssh で入ってそのまま叩けるので、
「PC から ssh 越しにデータが取れているか」の確認はこれでやる。
エージェントとは HTTP だけで話し、収集のコードは読まない。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"
# PC 側のトークン置き場。権限 600 を自分で守る前提(仕様書「認証とセキュリティ」)
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "svcscope" / "token"
TIMEOUT_S = 10.0

CLEAR = "\033[H\033[2J"


class CliError(Exception):
    pass


# --- 表示の下請け ---

def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}T"


def _pct(used: float, total: float) -> str:
    return f"{used / total * 100:.0f}%" if total else "-%"


def _psi_line(psi: dict) -> str:
    parts = []
    for kind, label in (("cpu", "cpu"), ("memory", "mem"), ("io", "io")):
        d = psi.get(kind) or {}
        if not d:
            continue
        bit = f"{label} some {d.get('some_avg10', 0):.2f}"
        if "full_avg10" in d:
            bit += f" full {d['full_avg10']:.2f}"
        parts.append(bit)
    return "   ".join(parts) if parts else "-"


def render(snap: dict, url: str, version: dict | None = None) -> str:
    """snapshot を1画面ぶんの文字列にする。"""
    out = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.get("ts", 0) / 1000))
    head = f"SvcScope  {url}"
    if version:
        head += f"  agent {version.get('agent_version', '?')}"
    out.append(f"{head}  {stamp}")
    out.append("")

    cpu = snap.get("cpu")
    if cpu:
        out.append(
            f"CPU   {cpu['percent']:5.1f}%   "
            f"load {cpu['load1']:.2f} / {cpu['load5']:.2f} / {cpu['load15']:.2f}"
            f"   ({cpu['cores']} cores)"
        )

    mem = snap.get("memory")
    if mem:
        line = (f"MEM   {human_bytes(mem['used_bytes'])} / {human_bytes(mem['total_bytes'])}"
                f" ({_pct(mem['used_bytes'], mem['total_bytes'])})")
        if mem.get("swap_total_bytes"):
            line += (f"   swap {human_bytes(mem['swap_used_bytes'])}"
                     f" / {human_bytes(mem['swap_total_bytes'])}")
        z = mem.get("zram")
        if z:
            line += (f"   zram {human_bytes(z['orig_data_bytes'])}"
                     f"->{human_bytes(z['compr_data_bytes'])}")
        out.append(line)

    psi = snap.get("psi")
    if psi:
        out.append(f"PSI   {_psi_line(psi)}")

    disk = snap.get("disk") or {}
    for d in disk.get("devices", []):
        out.append(
            f"DISK  {d['device']:<8} r {human_bytes(d['read_bytes_per_sec'])}/s"
            f"  w {human_bytes(d['write_bytes_per_sec'])}/s"
            f"  util {d['util_percent']:5.1f}%"
        )
    for f in disk.get("filesystems", []):
        out.append(
            f"      {f['mount']:<8} {human_bytes(f['used_bytes'])}"
            f" / {human_bytes(f['total_bytes'])} ({_pct(f['used_bytes'], f['total_bytes'])})"
        )

    for n in (snap.get("net") or {}).get("interfaces", []):
        out.append(
            f"NET   {n['name']:<8} rx {human_bytes(n['rx_bytes_per_sec'])}/s"
            f"  tx {human_bytes(n['tx_bytes_per_sec'])}/s"
        )

    units = snap.get("units") or []
    if units:
        out.append("")
        out.append(f"{'unit':<34}{'cpu%':>7}{'mem':>9}   io r/w")
        # 重い順に並べる。何が食っているかを探すのが目的なので
        for u in sorted(units, key=lambda u: -(u.get("cpu_percent") or 0))[:15]:
            out.append(
                f"{u['name'][:33]:<34}"
                f"{(u.get('cpu_percent') or 0):7.2f}"
                f"{human_bytes(u.get('memory_bytes')):>9}"
                f"   {human_bytes(u.get('io_read_bytes'))} / {human_bytes(u.get('io_write_bytes'))}"
            )

    cols = snap.get("collectors") or {}
    bad = [f"{k}:{v}" for k, v in sorted(cols.items()) if v != "ok"]
    out.append("")
    out.append("collectors  " + ("すべて ok" if not bad else "  ".join(bad)))
    return "\n".join(out)


# --- 通信 ---

class Client:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self._token = token

    def _request(self, path: str) -> urllib.request.Request:
        req = urllib.request.Request(self.url + path)
        req.add_header("Authorization", f"Bearer {self._token}")
        return req

    def get(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self._request(path), timeout=TIMEOUT_S) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise CliError("トークンが一致しません。--token-file を確認してください") from e
            if e.code == 429:
                raise CliError("認証の連続失敗で一時的に拒否されています。1分ほど待ってください") from e
            try:
                detail = json.loads(e.read())["error"]["message"]
            except Exception:  # noqa: BLE001
                detail = e.reason
            raise CliError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise CliError(
                f"{self.url} につながりません: {e.reason}\n"
                "ssh のトンネルが張れているか、エージェントが動いているかを確認してください"
            ) from e

    def stream(self):
        """SSE を1件ずつ返す。切断されたら終わる。"""
        with urllib.request.urlopen(self._request("/api/v1/stream"), timeout=TIMEOUT_S) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("data: "):
                    yield json.loads(line[6:])


def load_token(path: Path | None) -> str:
    """トークンを読む。引数 > 環境変数 > 既定のファイル の順。

    コマンドライン引数で平文のトークンを受け取る口は作らない。
    ps で他の利用者に見えるため(仕様書「認証とセキュリティ」)。
    """
    if path is not None:
        return _read_token(path)
    env = os.environ.get("SVCSCOPE_TOKEN")
    if env:
        return env.strip()
    if DEFAULT_TOKEN_FILE.exists():
        return _read_token(DEFAULT_TOKEN_FILE)
    raise CliError(
        f"トークンが見つかりません。{DEFAULT_TOKEN_FILE} に置くか、"
        "--token-file か環境変数 SVCSCOPE_TOKEN で渡してください"
    )


def _read_token(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise CliError(f"トークンを読めません: {e}") from e
    # 空ファイルで IndexError を出さない。原因の分かるエラーにする
    token = lines[0].strip() if lines else ""
    if not token:
        raise CliError(f"{path} が空です")
    # Windows には POSIX の権限が無いので、Linux でだけ警告する
    if os.name == "posix" and (path.stat().st_mode & 0o077):
        print(f"警告: {path} が自分以外から読めます。chmod 600 してください", file=sys.stderr)
    return token


def _watch(client: Client, version: dict) -> None:
    """SSE で流れてくる値を上書き表示する。切れたら間隔を広げて張り直す。"""
    backoff = 1.0
    while True:
        try:
            for snap in client.stream():
                backoff = 1.0
                print(CLEAR + render(snap, client.url, version), flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - 切断の理由は問わず張り直す
            print(f"\n切断されました({e})。{backoff:.0f} 秒後に再接続します", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="svcscope-cli", description="SvcScope の CUI クライアント")
    p.add_argument("--url", default=os.environ.get("SVCSCOPE_URL", DEFAULT_URL),
                   help=f"エージェントの URL(既定: {DEFAULT_URL})")
    p.add_argument("--token-file", type=Path, default=None,
                   help=f"トークンを書いたファイル(既定: {DEFAULT_TOKEN_FILE})")
    p.add_argument("--json", action="store_true", help="整形せずそのまま出す")
    p.add_argument("command", nargs="?", default="snapshot",
                   choices=["snapshot", "units", "alerts", "health", "version", "watch"])
    args = p.parse_args(argv)

    try:
        client = Client(args.url, load_token(args.token_file))
        version = client.get("/version")

        if args.command == "version":
            print(json.dumps(version, ensure_ascii=False, indent=2))
            return 0
        if args.command == "watch":
            _watch(client, version)
            return 0

        body = client.get(f"/api/v1/{args.command}")
        if args.json or args.command != "snapshot":
            print(json.dumps(body, ensure_ascii=False, indent=2))
        else:
            print(render(body, client.url, version))
        return 0
    except CliError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def demo() -> None:
    """表示の組み立てだけを検査する。通信は server.py の自己検査で見る。"""
    assert human_bytes(None) == "-"
    assert human_bytes(512) == "512B"
    assert human_bytes(1536) == "1.5K"
    assert human_bytes(4 * 1024 ** 3) == "4.0G"

    snap = {
        "ts": 1790000000000,
        "cpu": {"percent": 12.5, "load1": 0.42, "load5": 0.31, "load15": 0.25, "cores": 4},
        "memory": {"total_bytes": 4 * 1024 ** 3, "used_bytes": 1024 ** 3,
                   "swap_total_bytes": 2 * 1024 ** 3, "swap_used_bytes": 1024 ** 2,
                   "zram": {"orig_data_bytes": 1024 ** 3, "compr_data_bytes": 1024 ** 2,
                            "mem_used_total_bytes": 1024 ** 2}},
        "psi": {"io": {"some_avg10": 12.34, "full_avg10": 8.1}},
        "disk": {"devices": [{"device": "sda", "read_bytes_per_sec": 1024.0,
                              "write_bytes_per_sec": 2048.0, "util_percent": 15.0}]},
        "net": {"interfaces": [{"name": "eno1", "rx_bytes_per_sec": 1.0, "tx_bytes_per_sec": 2.0}]},
        "units": [{"name": "a.service", "cpu_percent": 0.1, "memory_bytes": 4096},
                  {"name": "b.service", "cpu_percent": 9.9, "memory_bytes": 8192}],
        "collectors": {"cpu": "ok", "disk_usage": "unsupported"},
    }
    text = render(snap, "http://127.0.0.1:8765", {"agent_version": "0.1.0"})
    assert "12.5%" in text
    # 重い順に並ぶので b.service が先
    assert text.index("b.service") < text.index("a.service"), text
    # ok でない collector だけを出す
    assert "disk_usage:unsupported" in text and "cpu:ok" not in text, text

    # 項目が欠けていても落ちない(collector が非対応の機械で出す形)
    thin = render({"ts": 0, "collectors": {}}, "http://x")
    assert "すべて ok" in thin, thin
    render({"ts": 0}, "http://x")

    print("cli OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-check":
        demo()
    else:
        sys.exit(main())
