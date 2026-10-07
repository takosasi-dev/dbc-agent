"""設定の読み込み。

TOML は tomllib(Python 3.11 以降の標準ライブラリ)で読む。依存を増やさないため。
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("/etc/dbc/config.toml")

# 待ち受けを許すアドレス。ループバックか、明示した VPN インターフェースの IP だけ。
# 0.0.0.0 を書かれたら起動を拒否する(仕様書「認証とセキュリティ」バインド制限)。
_FORBIDDEN_BINDS = {"0.0.0.0", "::", "*", ""}


class ConfigError(Exception):
    pass


@dataclass
class Config:
    bind: str = "127.0.0.1"
    port: int = 8765
    token_hash_file: Path = Path("/etc/dbc/token.sha256")
    # 収集元のルート。fixture でパーサを検証するときに差し替える。
    # 開発機に WSL が無くてもテストが回るようにするための逃げ道。
    root: Path = Path("/")
    # SMART は root 権限が要るので別の timer が書いた結果を読むだけにする。
    smart_file: Path = Path("/run/dbc/smart.json")
    # 外部 API の取得先。許可リストに無い URL は叩かない。
    external_allow: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.bind.strip() in _FORBIDDEN_BINDS:
            raise ConfigError(
                f"bind = {self.bind!r} は許可されません。"
                "待ち受けはループバックか、明示した VPN の IP だけにしてください"
            )
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"port = {self.port} が範囲外です")
        for name in ("token_hash_file", "root", "smart_file"):
            setattr(self, name, Path(getattr(self, name)))


def load(path: Path | None = None) -> Config:
    """設定ファイルを読む。無ければ既定値のまま返す。"""
    path = DEFAULT_PATH if path is None else Path(path)
    if not path.exists():
        return Config()
    with path.open("rb") as fp:
        raw = tomllib.load(fp)
    known = {f for f in Config.__dataclass_fields__}
    # 知らないキーは黙って捨てず、設定の書き間違いとして止める
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"設定に知らないキーがあります: {sorted(unknown)}")
    return Config(**raw)


def demo() -> None:
    import tempfile

    assert load(Path("/nonexistent/dbc.toml")).bind == "127.0.0.1"

    for bad in ("0.0.0.0", "::", ""):
        try:
            Config(bind=bad)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"bind={bad!r} が通ってしまった")

    try:
        Config(port=0)
    except ConfigError:
        pass
    else:
        raise AssertionError("port=0 が通ってしまった")

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.toml"
        p.write_text('bind = "10.0.0.2"\nport = 9000\n', encoding="utf-8")
        c = load(p)
        assert (c.bind, c.port) == ("10.0.0.2", 9000)
        # 文字列で書いたパスは Path になる
        assert isinstance(c.root, Path)

        p.write_text('prot = 9000\n', encoding="utf-8")
        try:
            load(p)
        except ConfigError:
            pass
        else:
            raise AssertionError("綴り間違いのキーが通ってしまった")

    print("config OK")


if __name__ == "__main__":
    demo()
