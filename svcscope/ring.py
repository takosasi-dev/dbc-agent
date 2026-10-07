"""リングバッファ。

メトリクスはここにだけ置く。ディスクには書かない(仕様書「性能要件」:
HDD への書き込みを抑えるため。再起動で履歴が消えるのは v0.1 では許容する)。
"""

from collections import deque
from threading import Lock


class Ring:
    """時刻つきの点を一定数だけ保持する。古いものから落ちる。

    maxlen は「間隔 × 保持時間」で決める。全体は 2 秒 × 30 分 = 900 点、
    unit 単位は 5 秒 × 30 分 = 360 点(仕様書「性能要件」)。
    """

    def __init__(self, maxlen: int):
        self._buf: deque = deque(maxlen=maxlen)
        self._lock = Lock()

    def append(self, point: dict) -> None:
        with self._lock:
            self._buf.append(point)

    def since(self, ts_ms: int | None = None) -> list[dict]:
        """ts_ms より後の点を古い順に返す。None なら全部。

        GUI は切断から復帰したとき最後に受けた時刻を since に付けて呼び、
        欠損を埋める(仕様書「API仕様」ストリーミング)。
        """
        with self._lock:
            points = list(self._buf)
        if ts_ms is None:
            return points
        return [p for p in points if p["ts"] > ts_ms]

    def latest(self) -> dict | None:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


def demo() -> None:
    r = Ring(maxlen=3)
    for ts in (10, 20, 30, 40):
        r.append({"ts": ts})
    # maxlen を超えた分は古いほうから落ちる
    assert [p["ts"] for p in r.since()] == [20, 30, 40], r.since()
    # since は境界を含まない(GUI は「最後に受けた時刻」を渡すため)
    assert [p["ts"] for p in r.since(20)] == [30, 40]
    assert r.since(40) == []
    assert r.latest() == {"ts": 40}
    assert len(r) == 3
    assert Ring(maxlen=2).latest() is None
    print("ring OK")


if __name__ == "__main__":
    demo()
