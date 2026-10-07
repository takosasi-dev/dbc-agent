"""エージェントの入口。

    python -m svcscope                       # /etc/svcscope/config.toml で起動
    python -m svcscope --fixtures tests/fixtures/arch --token-hash <hash>

systemd からは前者の形で起動する(packaging/svcscope.service)。
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__, config as cfgmod, server
from .auth import TokenAuth, first_line
from .sampler import Sampler, fixture_roots

log = logging.getLogger("svcscope")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="svcscope", description="SvcScope エージェント")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, default=None,
                   help=f"設定ファイル(既定: {cfgmod.DEFAULT_PATH})")
    p.add_argument("--bind", default=None, help="待ち受けアドレス。設定より優先")
    p.add_argument("--port", type=int, default=None, help="待ち受けポート。設定より優先")
    p.add_argument("--token-hash", default=None, metavar="HEX",
                   help="トークンの SHA-256。ファイルの代わりに直接渡す。"
                        "平文ではないので引数に出しても鍵は漏れない")
    p.add_argument("--fixtures", type=Path, default=None, metavar="DIR",
                   help="/proc の写しから読む。Linux の無い機械で API を立てるため")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # journald が時刻と単位名を付けるので、こちらでは付けない
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")

    try:
        config = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        log.error("設定が不正です: %s", e)
        return 2

    overrides = {k: v for k, v in (("bind", args.bind), ("port", args.port)) if v is not None}
    if overrides:
        try:
            config = cfgmod.Config(**{**config.__dict__, **overrides})
        except cfgmod.ConfigError as e:
            log.error("指定が不正です: %s", e)
            return 2

    if args.token_hash:
        token_hash = args.token_hash
    elif config.token_hash_file.exists():
        token_hash = first_line(config.token_hash_file.read_text(encoding="utf-8"))
    else:
        log.error(
            "トークンのハッシュが見つかりません: %s\n"
            "packaging/gen-token.sh で作ってください",
            config.token_hash_file,
        )
        return 2
    try:
        auth = TokenAuth(token_hash)
    except ValueError as e:
        log.error("%s", e)
        return 2

    fixtures = fixture_roots(args.fixtures) if args.fixtures else None
    if fixtures:
        log.warning("fixture モードで起動します。実機の値ではありません: %s", args.fixtures)

    sampler = Sampler(config, fixtures=fixtures)
    sampler.start()

    httpd = server.build(config, sampler, auth)
    log.info("待ち受け開始 http://%s:%d (agent %s)", config.bind, config.port, __version__)

    # systemctl stop から来る SIGTERM で止める。SSE を掴んだスレッドも
    # stopping イベントで一緒に抜ける。
    def shutdown(signum, frame):  # noqa: ARG001
        log.info("停止します (signal %s)", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)

    try:
        server.serve_forever(httpd)
    finally:
        sampler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
