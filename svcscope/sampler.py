"""収集ループ。

スレッドは1本だけ。collector ごとにスレッドを立てると、4GB / Core i3 の
機械で監視そのものが負荷になる(仕様書「性能要件」: 監視対象の負荷に
ならないことを最優先)。
"""

import logging
import threading
import time

from . import collectors as col
from .config import Config
from .ring import Ring

log = logging.getLogger("svcscope.sampler")

TICK_S = 1.0
GLOBAL_INTERVAL_S = 2.0
UNITS_INTERVAL_S = 5.0
# 2秒 × 30分 / 5秒 × 30分(仕様書「性能要件」履歴の保持)
GLOBAL_POINTS = 900
UNITS_POINTS = 360
# 同じエラーを出し続けないための間隔(仕様書「性能要件」ログ)
ERROR_LOG_INTERVAL_S = 60.0

OK, STALE, UNSUPPORTED = "ok", "stale", "unsupported"

# /api/v1/alerts に混ぜる collector。ここに並べた順に見る
ALERT_SOURCES = ("journal", "smart", "security", "news")
# 重い順に並べるための番号
_SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def now_ms() -> int:
    """API で使う時刻。Unix 時刻のミリ秒(UTC)。"""
    return int(time.time() * 1000)


class _Slot:
    """collector 1つ分の状態。最後に取れた値と、その時刻を持つ。"""

    __slots__ = ("c", "value", "at", "due", "unsupported", "error", "logged_at",
                 "running")

    def __init__(self, c: col.Collector):
        self.c = c
        self.value: dict | None = None
        self.at: float = 0.0
        self.due: float = 0.0
        self.unsupported: str | None = None
        self.error: str | None = None
        self.logged_at: float = 0.0
        # 別スレッドで回す collector が二重に走らないようにする
        self.running = False


def fixture_roots(directory) -> list:
    """fixture ディレクトリの中の写しを時系列順に並べて返す。

    `t0`, `t1`, ... という名前のサブディレクトリを1つの時点の写しとみなす。
    差分で出す項目(CPU%、I/O、unit の CPU)は、1枚だけでは値が出ないため
    複数枚を順に読ませる必要がある。
    """
    from pathlib import Path

    directory = Path(directory)
    roots = sorted(
        (p for p in directory.iterdir() if p.is_dir() and p.name.startswith("t")),
        key=lambda p: int(p.name[1:] or 0),
    )
    return roots or [directory]


class Sampler:
    def __init__(
        self,
        config: Config,
        classes: list[type[col.Collector]] | None = None,
        fixtures: list | None = None,
    ):
        self.config = config
        # fixtures を渡すと、tick ごとに次の写しへ進む。開発機に Linux が
        # 無い状態で GUI を作るためのモックサーバ用(仕様書「開発とテスト方針」
        # GUI はモックサーバで検証する)。
        self._fixtures = [p for p in fixtures] if fixtures else None
        self._fixture_i = 0
        self.history = Ring(GLOBAL_POINTS)
        self.units_history = Ring(UNITS_POINTS)
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_global = 0.0
        self._next_units = 0.0

        for cls in (classes if classes is not None else col.ALL):
            c = cls(config)
            if self._fixtures:
                c.root = self._fixtures[0]
            slot = _Slot(c)
            try:
                c.probe()
            except col.Unsupported as e:
                slot.unsupported = str(e)
                log.info("collector %s は非対応: %s", c.name, e)
            self._slots[c.name] = slot

    # --- ループ ---

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(time.monotonic())
            except Exception:  # noqa: BLE001 - ループは絶対に落とさない
                log.exception("収集ループで想定外の例外")
            self._stop.wait(TICK_S)

    def tick(self, now: float) -> None:
        """期限の来た collector を回し、2秒/5秒ごとに履歴へ1点積む。

        now は time.monotonic() 相当。テストから好きな時刻を渡せるように
        引数で受ける。
        """
        with self._lock:
            if self._fixtures:
                # 1枚目から順に進み、末尾まで行ったら先頭へ折り返す。
                # ponytail: 折り返した回だけカウンタが巻き戻るので、差分で
                # 出す項目はその1回だけ stale になる。GUI を本格的に作る段で
                # 単調増加の写しを生成する形に替える。
                cur = self._fixtures[self._fixture_i % len(self._fixtures)]
                self._fixture_i += 1
                for slot in self._slots.values():
                    slot.c.root = cur

            for slot in self._slots.values():
                if slot.unsupported is not None or now < slot.due:
                    continue
                slot.due = now + slot.c.interval
                if slot.c.blocking:
                    # 外に出る collector は応答待ちが長い。ここで待つと
                    # 2秒間隔の項目が止まるので、別スレッドへ出す
                    if slot.running:
                        continue  # 前回がまだ終わっていない
                    slot.running = True
                    threading.Thread(
                        target=self._collect_offline, args=(slot, now),
                        name=f"collect-{slot.c.name}", daemon=True,
                    ).start()
                    continue
                self._store(slot, *self._attempt(slot, now), now)

            if now >= self._next_global:
                self._next_global = now + GLOBAL_INTERVAL_S
                self.history.append(self._assemble(now, with_units=False))
            if now >= self._next_units:
                self._next_units = now + UNITS_INTERVAL_S
                units = self._slots["units"].value if "units" in self._slots else None
                if units:
                    self.units_history.append({"ts": now_ms(), **units})

    def _attempt(self, slot: _Slot, now: float) -> tuple[dict | None, str | None, str | None]:
        """collect を1回試す。(値, 非対応の理由, エラー) を返す。

        ロックを取らない。別スレッドから呼ぶときは、時間のかかる collect を
        ロックの外で回すため。
        """
        try:
            return slot.c.collect(now), None, None
        except col.NotReady:
            return None, None, None
        except col.Unsupported as e:
            # 動いていた項目が途中で消えることもある(zram の解除など)
            return None, str(e), None
        except Exception as e:  # noqa: BLE001 - 1つの失敗で他を止めない
            return None, None, f"{type(e).__name__}: {e}"

    def _store(self, slot: _Slot, value, unsupported, error, now: float) -> None:
        """_attempt の結果を slot に入れる。呼び出し側がロックを持つこと。"""
        if unsupported is not None:
            slot.unsupported = unsupported
            self._log_error(slot, now, "非対応になりました: %s" % unsupported)
        elif error is not None:
            slot.error = error
            self._log_error(slot, now, "収集に失敗: %s" % error)
        elif value is not None:
            slot.value = value
            slot.at = now
            slot.error = None

    def _collect_offline(self, slot: _Slot, now: float) -> None:
        """遅い collector を別スレッドで回す。結果が出たらロックを取って入れる。"""
        try:
            result = self._attempt(slot, now)
            with self._lock:
                self._store(slot, *result, now)
        finally:
            slot.running = False

    def _log_error(self, slot: _Slot, now: float, msg: str) -> None:
        if now - slot.logged_at < ERROR_LOG_INTERVAL_S:
            return
        slot.logged_at = now
        log.warning("collector %s: %s", slot.c.name, msg)

    # --- 読み出し ---

    def _status(self, slot: _Slot, now: float) -> str:
        if slot.unsupported is not None:
            return UNSUPPORTED
        # 間隔の3倍を過ぎたら古い。1回の取りこぼしで stale にはしない。
        if slot.value is None or now - slot.at > slot.c.interval * 3:
            return STALE
        return OK

    def _assemble(self, now: float, with_units: bool) -> dict:
        """最新値を API の形に組む。取れていない項目はキーごと省く。"""
        v = {name: s.value for name, s in self._slots.items()}
        out: dict = {"ts": now_ms()}

        for name in ("cpu", "memory", "net", "psi"):
            if v.get(name):
                out[name] = v[name]

        # ディスクは間隔が違うので collector を2つに分けてある(I/O 2秒、
        # 使用量 60秒)。API では "disk" にまとめて見せる。
        disk = {**(v.get("disk_io") or {}), **(v.get("disk_usage") or {})}
        if disk:
            out["disk"] = disk

        if with_units and v.get("units"):
            out["units"] = v["units"]["units"]

        out["collectors"] = {n: self._status(s, now) for n, s in self._slots.items()}
        return out

    def snapshot(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            return self._assemble(now, with_units=True)

    def units(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            slot = self._slots.get("units")
            value = slot.value if slot else None
            return {
                "ts": now_ms(),
                "units": (value or {}).get("units", []),
                "collectors": {"units": self._status(slot, now) if slot else UNSUPPORTED},
            }

    def alerts(self, now: float | None = None) -> dict:
        """異常の一覧。journald / SMART / 脆弱性 の collector は v0.1 の次段で足す。

        形だけ先に固めておく。GUI 側を後から直さずに済ませるため。
        """
        now = time.monotonic() if now is None else now
        alerts: list[dict] = []
        states = {}
        with self._lock:
            for name in ALERT_SOURCES:
                slot = self._slots.get(name)
                states[name] = self._status(slot, now) if slot else UNSUPPORTED
                if slot is not None and slot.value:
                    alerts.extend(slot.value.get("alerts") or [])
        # 重いものから、同じ重さなら新しいものから
        alerts.sort(key=lambda a: (_SEVERITY_ORDER.get(a.get("severity"), 9),
                                   -(a.get("ts") or 0)))
        return {"ts": now_ms(), "alerts": alerts, "collectors": states}

    def health(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            items = {}
            for name, slot in self._slots.items():
                items[name] = {
                    "status": self._status(slot, now),
                    "interval_s": slot.c.interval,
                    "age_s": round(now - slot.at, 2) if slot.value is not None else None,
                    "detail": slot.unsupported or slot.error,
                }
        return {
            "ts": now_ms(),
            "collectors": items,
            "memory_rss_bytes": _rss_bytes(),
            "history_points": len(self.history),
            "units_history_points": len(self.units_history),
        }


def _rss_bytes() -> int | None:
    """自分の常駐メモリ。性能要件(80MB以下)を GUI から見られるようにする。"""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fp:
            pages = int(fp.read().split()[1])
        return pages * 4096
    except OSError:
        return None


def demo() -> None:
    from pathlib import Path

    class Fake(col.Collector):
        name = "fake"
        interval = 2.0

        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def collect(self, now: float) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise col.NotReady
            if self.calls == 3:
                raise RuntimeError("わざと失敗")
            return {"percent": 1.0}

    class Gone(col.Collector):
        name = "gone"

        def probe(self):
            raise col.Unsupported("ありません")

    s = Sampler(Config(bind="127.0.0.1"), classes=[Fake, Gone])

    # probe で落ちた collector は最初から unsupported
    assert s.health(0.0)["collectors"]["gone"]["status"] == UNSUPPORTED

    s.tick(0.0)   # 1回目: NotReady
    assert s.snapshot(0.0)["collectors"]["fake"] == STALE
    s.tick(2.0)   # 2回目: 値が出る
    assert s.snapshot(2.0)["collectors"]["fake"] == OK
    s.tick(4.0)   # 3回目: 例外 -> 直前の値は残るが status は ok のまま(間隔の3倍以内)
    assert s.health(4.0)["collectors"]["fake"]["detail"] == "RuntimeError: わざと失敗"
    # 間隔の3倍を過ぎたら stale
    assert s.snapshot(2.0 + 2.0 * 3 + 0.1)["collectors"]["fake"] == STALE

    # 2秒ごとに履歴が1点ずつ積まれる(0.0 と 2.0 と 4.0 で3点)
    assert len(s.history) == 3, len(s.history)
    # 1つが失敗しても他の項目と collectors は必ず返る
    assert "collectors" in s.snapshot(4.0)
    assert s.alerts(4.0)["alerts"] == []
    assert s.units(4.0)["units"] == []

    # 遅い collector は別スレッドで回す。tick が待たされないこと、
    # 結果が後から入ること、前の回が終わる前に二重に走らないこと
    class Slow(col.Collector):
        name = "journal"  # alerts に混ざる名前にして、集約も通す
        interval = 1.0
        blocking = True

        def __init__(self, config):
            super().__init__(config)
            self.started = 0

        def collect(self, now: float) -> dict:
            self.started += 1
            time.sleep(0.4)
            return {"alerts": [{"ts": 1, "source": "journal",
                                "severity": "warning", "message": "おそい"}]}

    s2 = Sampler(Config(), classes=[Slow])
    slow = s2._slots["journal"].c
    began = time.monotonic()
    s2.tick(time.monotonic())
    # 0.4 秒かかる collect に tick が付き合っていないこと
    assert time.monotonic() - began < 0.2, "tick が遅い collector に待たされている"
    # 終わる前にもう一度期限が来ても、二重には走らせない
    s2.tick(time.monotonic() + 2.0)
    assert slow.started == 1, slow.started
    time.sleep(0.8)
    got = s2.alerts()["alerts"]
    assert len(got) == 1 and got[0]["message"] == "おそい", got
    assert s2.alerts()["collectors"]["journal"] == OK
    # 1回終われば次は走る
    s2.tick(time.monotonic() + 4.0)
    time.sleep(0.8)
    assert slow.started == 2, slow.started

    print("sampler OK")


if __name__ == "__main__":
    demo()
