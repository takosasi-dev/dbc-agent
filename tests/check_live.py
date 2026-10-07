"""本物の /proc・/sys に対して collector を回す。Linux でだけ意味がある。

    python tests/check_live.py

fixture のテストは「写しを正しく読めるか」しか見ない。実際のカーネルが出す
形は版や設定で違うので、本物に当てないと気づけない取りこぼしがある。
開発機が Windows でも、CI(ubuntu-latest)がここを通る。

Linux でなければ何もせず成功で抜ける。CI の matrix を分けずに済ませるため。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from svcscope import collectors as col  # noqa: E402
from svcscope.config import Config  # noqa: E402

# この環境なら必ず取れるはずのもの。取れなければパーサ側の問題
MUST_WORK = {"cpu", "memory", "disk_io", "net"}

# 環境によって無いことがあるもの。非対応と返るのが正しい振る舞い
MAY_MISS = {"psi", "units", "disk_usage", "journal", "smart", "security", "news"}


def sane(name: str, value: dict) -> None:
    """値が「あり得る範囲」かだけ見る。正解の数字は本物相手には作れない。"""
    if name == "cpu":
        assert 0.0 <= value["percent"] <= 100.0, value
        assert value["cores"] >= 1, value
        assert value["load1"] >= 0.0, value
    elif name == "memory":
        assert value["total_bytes"] > 0, value
        assert value["used_bytes"] <= value["total_bytes"], value
        assert value["available_bytes"] <= value["total_bytes"], value
    elif name == "disk_io":
        assert value["devices"], "ディスクが1本も見つかりません"
        for d in value["devices"]:
            assert 0.0 <= d["util_percent"] <= 100.0, d
            assert d["read_bytes_per_sec"] >= 0.0, d
            # パーティションを拾っていないこと
            assert (Path("/sys/block") / d["device"]).exists(), d
    elif name == "net":
        assert value["interfaces"], "インターフェースが1つも見つかりません"
        assert all(i["name"] != "lo" for i in value["interfaces"]), value
    elif name == "psi":
        for kind, d in value.items():
            assert d, f"{kind} が空です"
            assert all(v >= 0.0 for v in d.values()), d
    elif name == "units":
        for u in value["units"]:
            assert u["name"].endswith(".service"), u
            assert u.get("cpu_percent", 0.0) >= 0.0, u
    elif name == "disk_usage":
        assert value["filesystems"], "ファイルシステムが1つも見つかりません"
        for f in value["filesystems"]:
            assert f["used_bytes"] <= f["total_bytes"], f


def main() -> int:
    if not Path("/proc/stat").exists():
        print("Linux ではないので何もしません(/proc/stat が無い)")
        return 0

    config = Config(root=Path("/"))
    results: dict[str, str] = {}
    failed: list[str] = []

    for cls in col.ALL:
        c = cls(config)
        name = c.name
        try:
            c.probe()
        except col.Unsupported as e:
            results[name] = f"unsupported ({e})"
            continue

        # 外へ出る collector はここでは叩かない(tests/check_external.py の役目)
        if c.blocking:
            results[name] = "skipped (外部 API は check_external.py で見る)"
            continue

        t = time.monotonic()
        value = None
        try:
            try:
                c.collect(t)
            except col.NotReady:
                pass
            # 差分が要る項目のために実際に待つ
            time.sleep(2.0)
            value = c.collect(time.monotonic())
        except col.NotReady:
            results[name] = "not ready"
        except Exception as e:  # noqa: BLE001
            results[name] = f"失敗 {type(e).__name__}: {e}"
            failed.append(name)
            continue

        if value is not None:
            try:
                sane(name, value)
            except AssertionError as e:
                results[name] = f"値がおかしい: {e}"
                failed.append(name)
                continue
            results[name] = "ok"

    for name, state in results.items():
        mark = "OK  " if state == "ok" else "--  "
        print(f"  {mark}{name}: {state}")

    # 必ず取れるはずのものが取れていなければ失敗
    for name in sorted(MUST_WORK):
        if results.get(name) != "ok":
            print(f"\n{name} はこの環境で取れないとおかしい: {results.get(name)}",
                  file=sys.stderr)
            failed.append(name)

    unknown = set(results) - MUST_WORK - MAY_MISS
    assert not unknown, f"MUST_WORK / MAY_MISS に載っていない collector: {unknown}"

    if failed:
        print(f"\n{len(set(failed))} 件が失敗しました: {sorted(set(failed))}", file=sys.stderr)
        return 1
    print("\n本物の /proc で通りました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
