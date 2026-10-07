"""API トークンの照合。

サーバに平文のトークンは置かない。置くのは SHA-256 のハッシュだけ
(仕様書「認証とセキュリティ」)。平文を持つのは PC 側だけ。
"""

import hashlib
import hmac
import time
from pathlib import Path

# 連続失敗の上限と、超えたときに拒否し続ける秒数。
# 総当たりを現実的でなくするためで、256 ビットのトークンに対しては
# 本来不要だが、ログを失敗で埋められるのを防ぐ意味もある。
MAX_FAILURES = 5
LOCKOUT_S = 60.0


class AuthError(Exception):
    """トークンが合わない、または締め出し中。

    locked が True なら締め出し中。GUI 側が「鍵が違う」と「少し待て」を
    区別して、総当たりのように再試行し続けないようにするため。
    """

    def __init__(self, message: str, locked: bool = False):
        super().__init__(message)
        self.locked = locked


def hash_token(token: str) -> str:
    """平文トークン -> 保存用の SHA-256 16進文字列。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def first_line(text: str) -> str:
    """1行目。空の文字列でも落ちない。

    空のハッシュファイルを渡されたときに IndexError ではなく
    「ハッシュの形が違う」として扱わせるため。
    """
    lines = text.splitlines()
    return lines[0] if lines else ""


class TokenAuth:
    def __init__(self, token_hash: str):
        self._hash = token_hash.strip().lower()
        if len(self._hash) != 64:
            raise ValueError("トークンのハッシュは SHA-256 の16進64文字である必要があります")
        self._failures = 0
        self._locked_until = 0.0

    @classmethod
    def from_file(cls, path: Path) -> "TokenAuth":
        """ハッシュを書いたファイルから作る。ファイルの1行目だけ読む。"""
        return cls(first_line(path.read_text(encoding="utf-8")))

    def check_header(self, header: str | None, now: float | None = None) -> None:
        """`Authorization: Bearer <token>` を検証する。合わなければ AuthError。

        /version も例外にしない(仕様書「API仕様」共通の規約)。
        """
        now = time.monotonic() if now is None else now
        if now < self._locked_until:
            raise AuthError("連続失敗により一時的に拒否しています", locked=True)

        token = ""
        if header and header.startswith("Bearer "):
            token = header[len("Bearer "):].strip()

        # 定数時間で比較する。長さの違いで分岐しないよう、平文ではなく
        # 同じ長さになるハッシュ同士を比べる(仕様書「認証とセキュリティ」照合)。
        if token and hmac.compare_digest(hash_token(token), self._hash):
            self._failures = 0
            return

        self._failures += 1
        if self._failures >= MAX_FAILURES:
            self._locked_until = now + LOCKOUT_S
            self._failures = 0
        raise AuthError("トークンが一致しません")


def demo() -> None:
    token = "a" * 64
    auth = TokenAuth(hash_token(token))

    auth.check_header(f"Bearer {token}", now=0.0)

    for form in (None, "", "Bearer", "Basic xxx", "Bearer wrong", f"Bearer {token}x"):
        try:
            auth.check_header(form, now=0.0)
        except AuthError:
            pass
        else:
            raise AssertionError(f"通ってはいけない形が通った: {form!r}")

    # 正しいトークンで失敗回数が戻るので、締め出しに入らない
    a = TokenAuth(hash_token(token))
    for _ in range(MAX_FAILURES - 1):
        try:
            a.check_header("Bearer wrong", now=0.0)
        except AuthError:
            pass
    a.check_header(f"Bearer {token}", now=0.0)

    # 連続 MAX_FAILURES 回で締め出し、正しいトークンでも LOCKOUT_S まで通らない
    b = TokenAuth(hash_token(token))
    for _ in range(MAX_FAILURES):
        try:
            b.check_header("Bearer wrong", now=0.0)
        except AuthError:
            pass
    try:
        b.check_header(f"Bearer {token}", now=1.0)
    except AuthError:
        pass
    else:
        raise AssertionError("締め出し中に通ってしまった")
    b.check_header(f"Bearer {token}", now=LOCKOUT_S + 1.0)

    for bad in ("short", "", "x" * 64):
        try:
            TokenAuth(bad)
        except ValueError:
            pass
        else:
            if bad == "x" * 64:
                continue  # 64文字なら形は正しい。中身が合わないだけ
            raise AssertionError(f"{bad!r} が通ってしまった")

    assert first_line("") == ""
    assert first_line("a\nb\n") == "a"
    # 空のハッシュファイルは IndexError ではなく ValueError になる
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "token.sha256"
        p.write_text("", encoding="utf-8")
        try:
            TokenAuth.from_file(p)
        except ValueError:
            pass
        else:
            raise AssertionError("空のハッシュファイルが通ってしまった")
        p.write_text(hash_token(token) + "\n", encoding="utf-8")
        TokenAuth.from_file(p).check_header(f"Bearer {token}", now=0.0)

    print("auth OK")


if __name__ == "__main__":
    demo()
