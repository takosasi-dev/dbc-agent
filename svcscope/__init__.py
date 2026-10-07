"""SvcScope エージェント。

Linux サーバの systemd unit 単位の負荷を集めて HTTP API で返す常駐プロセス。
GUI とはこの API だけで結ぶ(仕様書「リポジトリ構成」: 共有コードを持たない)。

標準ライブラリだけで書いてある。常駐メモリ 80MB 以下という目標に対して、
FastAPI + uvicorn を入れると import だけで 30MB 以上乗るため。
"""

__version__ = "0.1.0"

# API のメジャー版。パスの /api/v1 と対応する。
# v1 の中では項目の追加だけ許す(仕様書「API仕様」)。
API_VERSION = 1
