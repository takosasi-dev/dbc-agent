"""collector のパーサを fixture で検証する。

fixture は 2 秒差の 2 枚なので、差分で出す項目の期待値を手計算できる。
数字の根拠は tests/make_fixtures.py の定数に書いてある。

枠組みは使わない。`python tests/test_collectors.py` で走る。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from svcscope import collectors as col  # noqa: E402
from svcscope.sampler import Sampler, fixture_roots  # noqa: E402
from svcscope.config import Config  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arch"
# 模擬データは生成物でリポジトリに持たないので、無ければここで作る
if not FIXTURE_DIR.is_dir():
    import make_fixtures

    make_fixtures.main()

ROOTS = fixture_roots(FIXTURE_DIR)
assert len(ROOTS) >= 2, f"{FIXTURE_DIR} に写しが2枚ありません: {ROOTS}"
T0, T1 = ROOTS[0], ROOTS[1]


def two_step(cls: type[col.Collector]) -> dict:
    """t0 を読んでから t1 を読み、2 秒差の値を返す。"""
    c = cls(T0)
    c.probe()
    try:
        c.collect(0.0)
    except col.NotReady:
        pass
    c.root = T1
    return c.collect(2.0)


def test_cpu() -> None:
    v = two_step(col.Cpu)
    # 800 tick のうち busy 100 = 12.5%
    assert v["percent"] == 12.5, v
    assert (v["load1"], v["load5"], v["load15"]) == (0.42, 0.31, 0.25), v
    assert v["cores"] == 4, v

    # 1 枚目だけでは値を出さない(異常ではなく NotReady)
    c = col.Cpu(T0)
    try:
        c.collect(0.0)
    except col.NotReady:
        pass
    else:
        raise AssertionError("初回で値を返してしまった")

    # 同じ写しを 2 回読んだら差が 0 なので、やはり値を出さない
    c2 = col.Cpu(T0)
    try:
        c2.collect(0.0)
    except col.NotReady:
        pass
    try:
        c2.collect(2.0)
    except col.NotReady:
        pass
    else:
        raise AssertionError("差分 0 で値を返してしまった")


def test_memory() -> None:
    v = col.Memory(T0).collect(0.0)
    assert v["total_bytes"] == 3_999_996 * 1024, v
    assert v["available_bytes"] == 2_345_678 * 1024, v
    assert v["used_bytes"] == (3_999_996 - 2_345_678) * 1024, v
    assert v["swap_used_bytes"] == (1_999_996 - 1_899_996) * 1024, v
    # Cached + SReclaimable
    assert v["cached_bytes"] == (1_234_567 + 123_456) * 1024, v
    assert v["zram"]["mem_used_total_bytes"] == 489_660_416, v

    # zram が無い機械では項目そのものを省く
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        alt = Path(d) / "root"
        (alt / "proc").mkdir(parents=True)
        shutil.copy(T0 / "proc" / "meminfo", alt / "proc" / "meminfo")
        assert "zram" not in col.Memory(alt).collect(0.0)


def test_disk_io() -> None:
    v = two_step(col.DiskIo)
    by_name = {d["device"]: d for d in v["devices"]}
    # /sys/block に居る sda と zram0 だけ。sda1 と loop0 は落とす
    assert set(by_name) == {"sda", "zram0"}, by_name
    sda = by_name["sda"]
    assert sda["read_bytes_per_sec"] == 4096 * 512 / 2, sda
    assert sda["write_bytes_per_sec"] == 8192 * 512 / 2, sda
    assert sda["util_percent"] == 15.0, sda
    # 動いていない zram0 は 0 で返る(項目を消さない)
    assert by_name["zram0"]["read_bytes_per_sec"] == 0.0


def test_net() -> None:
    v = two_step(col.Net)
    names = [i["name"] for i in v["interfaces"]]
    # lo は除く。ヘッダ 2 行を取り込まない
    assert names == ["eno1"], names
    assert v["interfaces"][0]["rx_bytes_per_sec"] == 250_000 / 2
    assert v["interfaces"][0]["tx_bytes_per_sec"] == 120_000 / 2


def test_psi() -> None:
    v = col.Psi(T0).collect(0.0)
    assert v["io"]["some_avg10"] == 12.34, v
    assert v["io"]["full_avg300"] == 2.10, v
    assert v["cpu"]["full_avg10"] == 0.00, v
    assert set(v) == {"cpu", "memory", "io"}, v

    # PSI 無効のカーネルは「非対応」。黙って 0 を返さない
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        try:
            col.Psi(Path(d)).probe()
        except col.Unsupported:
            pass
        else:
            raise AssertionError("PSI が無いのに probe が通った")


def test_units() -> None:
    v = two_step(col.Units)
    names = [u["name"] for u in v["units"]]
    # .service だけ。dbus.socket は拾わない。名前順に並ぶ
    assert names == [
        "NetworkManager.service", "sshd.service",
        "svcscope.service", "systemd-journald.service",
    ], names
    sshd = v["units"][1]
    assert sshd["memory_bytes"] == 8_388_608, sshd
    # 2000us / 2秒 = 0.1%
    assert sshd["cpu_percent"] == 0.1, sshd
    assert sshd["io_write_bytes"] == 2_097_152 + 8192

    # cgroup v1(cgroup.controllers が無い)は非対応
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "sys/fs/cgroup/system.slice").mkdir(parents=True)
        try:
            col.Units(Path(d)).probe()
        except col.Unsupported:
            pass
        else:
            raise AssertionError("cgroup v1 で probe が通った")


def test_disk_usage_unsupported_on_fixture() -> None:
    # fixture に statvfs を掛けても実機の値にならないので、嘘を返さず非対応と言う
    try:
        col.DiskUsage(T0).probe()
    except col.Unsupported:
        pass
    else:
        raise AssertionError("fixture で disk_usage が probe を通った")


def test_sampler_rotates_fixtures() -> None:
    """モックサーバ用。tick ごとに写しが進んで値が動く。"""
    s = Sampler(Config(), fixtures=ROOTS)
    s.tick(0.0)
    assert s.snapshot(0.0)["collectors"]["cpu"] == "stale"
    s.tick(2.0)
    snap = s.snapshot(2.0)
    assert snap["collectors"]["cpu"] == "ok", snap["collectors"]
    assert snap["cpu"]["percent"] == 12.5, snap["cpu"]
    # ディスクは collector 2 つを "disk" にまとめて見せる
    assert "devices" in snap["disk"], snap["disk"]
    assert snap["collectors"]["disk_usage"] == "unsupported"
    # 2 秒ごとに履歴へ 1 点。unit は 5 秒ごとなので別勘定
    assert len(s.history) == 2, len(s.history)

    # unit は 5 秒間隔なので、2 回目の収集はここで来る
    s.tick(6.0)
    snap = s.snapshot(6.0)
    assert snap["collectors"]["units"] == "ok", snap["collectors"]
    assert len(snap["units"]) == 4, snap["units"]
    assert len(s.units_history) == 1, len(s.units_history)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  {t.__name__} OK")
    print(f"collectors {len(tests)} 件 OK")


if __name__ == "__main__":
    main()
