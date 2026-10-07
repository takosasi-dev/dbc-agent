"""DBC エージェント。

Linux サーバの systemd unit 単位の負荷を集めて HTTP API で返す常駐プロセス。
GUI とはこの API だけで結ぶ(仕様書「リポジトリ構成」: 共有コードを持たない)。

標準ライブラリだけで書いてある。常駐メモリ 80MB 以下という目標に対して、
FastAPI + uvicorn を入れると import だけで 30MB 以上乗るため。
"""

__version__ = "0.1.0"

# API のメジャー版。パスの /api/v1 と対応する。
# v1 の中では項目の追加だけ許す(仕様書「API仕様」)。
API_VERSION = 1


def _use_utf8_output() -> None:
    """出力を UTF-8 にする。Windows ではコンソール側も合わせる。

    Windows のコンソールの既定のコードページ(日本語環境なら cp932、英語環境なら
    cp1252)では、日本語を print した時点で UnicodeEncodeError で落ちる。
    CUI クライアントは PC から叩くので、ここで落ちると何も見えない。

    Python 側を UTF-8 にするだけでは落ちなくなる代わりに文字化けするので、
    コンソールの出力コードページも 65001 に替える。コンソールの設定は
    プロセスを抜けても残るため、終了時に元へ戻す。

    pythonw.exe のようにコンソールを持たない起動や、journald へ流す systemd
    からの起動では該当しないので、失敗しても黙って続ける。
    """
    import sys

    if sys.platform == "win32":
        try:
            import atexit
            import ctypes

            kernel32 = ctypes.windll.kernel32
            before = kernel32.GetConsoleOutputCP()
            # 0 はコンソールが無いとき。替えるものが無いので何もしない
            if before and before != 65001 and kernel32.SetConsoleOutputCP(65001):
                atexit.register(kernel32.SetConsoleOutputCP, before)
        except (OSError, AttributeError):
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# パッケージを読み込んだ時点で効かせる。入口が増えても付け忘れないため。
_use_utf8_output()
