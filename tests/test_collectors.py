"""collector のパーサを fixture で検証する。

fixture は 2 秒差の 2 枚なので、差分で出す項目の期待値を手計算できる。
数字の根拠は tests/make_fixtures.py の定数に書いてある。

外へ出る collector(security・news)は取得と解析を分けてあるので、ここでは
解析だけを試す。実際に取れるかは tests/check_external.py で確かめる。

枠組みは使わない。`python tests/test_collectors.py` で走る。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from svcscope import collectors as col  # noqa: E402
from svcscope.config import Config  # noqa: E402
from svcscope.sampler import Sampler, fixture_roots  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arch"
EXTERNAL_DIR = Path(__file__).parent / "fixtures" / "external"
# 模擬データは生成物でリポジトリに持たないので、無ければここで作る
if not FIXTURE_DIR.is_dir() or not EXTERNAL_DIR.is_dir():
    import make_fixtures

    make_fixtures.main()

ROOTS = fixture_roots(FIXTURE_DIR)
assert len(ROOTS) >= 2, f"{FIXTURE_DIR} に写しが2枚ありません: {ROOTS}"
T0, T1 = ROOTS[0], ROOTS[1]


def mk(cls: type[col.Collector], root: Path = T0, **kw) -> col.Collector:
    """collector を作る。collector は Config を受け取る。"""
    return cls(Config(root=root, **kw))


def two_step(cls: type[col.Collector]) -> dict:
    """t0 を読んでから t1 を読み、2 秒差の値を返す。"""
    c = mk(cls)
    c.probe()
    try:
        c.collect(0.0)
    except col.NotReady:
        pass
    c.root = T1
    return c.collect(2.0)


# --- 基本メトリクス ---

def test_cpu() -> None:
    v = two_step(col.Cpu)
    # 800 tick のうち busy 100 = 12.5%
    assert v["percent"] == 12.5, v
    assert (v["load1"], v["load5"], v["load15"]) == (0.42, 0.31, 0.25), v
    assert v["cores"] == 4, v

    # 1 枚目だけでは値を出さない(異常ではなく NotReady)
    c = mk(col.Cpu)
    try:
        c.collect(0.0)
    except col.NotReady:
        pass
    else:
        raise AssertionError("初回で値を返してしまった")

    # 同じ写しを 2 回読んだら差が 0 なので、やはり値を出さない
    c2 = mk(col.Cpu)
    for _ in range(2):
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
    v = mk(col.Memory).collect(0.0)
    assert v["total_bytes"] == 3_999_996 * 1024, v
    assert v["available_bytes"] == 2_345_678 * 1024, v
    assert v["used_bytes"] == (3_999_996 - 2_345_678) * 1024, v
    assert v["swap_used_bytes"] == (1_999_996 - 1_899_996) * 1024, v
    # Cached + SReclaimable
    assert v["cached_bytes"] == (1_234_567 + 123_456) * 1024, v
    assert v["zram"]["mem_used_total_bytes"] == 489_660_416, v

    # zram が無い機械では項目そのものを省く
    import shutil
    with tempfile.TemporaryDirectory() as d:
        alt = Path(d) / "root"
        (alt / "proc").mkdir(parents=True)
        shutil.copy(T0 / "proc" / "meminfo", alt / "proc" / "meminfo")
        assert "zram" not in mk(col.Memory, alt).collect(0.0)


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
    v = mk(col.Psi).collect(0.0)
    assert v["io"]["some_avg10"] == 12.34, v
    assert v["io"]["full_avg300"] == 2.10, v
    assert v["cpu"]["full_avg10"] == 0.00, v
    assert set(v) == {"cpu", "memory", "io"}, v

    # PSI 無効のカーネルは「非対応」。黙って 0 を返さない
    with tempfile.TemporaryDirectory() as d:
        try:
            mk(col.Psi, Path(d)).probe()
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
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "sys/fs/cgroup/system.slice").mkdir(parents=True)
        try:
            mk(col.Units, Path(d)).probe()
        except col.Unsupported:
            pass
        else:
            raise AssertionError("cgroup v1 で probe が通った")


def test_disk_usage_unsupported_on_fixture() -> None:
    # fixture に statvfs を掛けても実機の値にならないので、嘘を返さず非対応と言う
    try:
        mk(col.DiskUsage).probe()
    except col.Unsupported:
        pass
    else:
        raise AssertionError("fixture で disk_usage が probe を通った")


def test_disk_usage_skips_remote_and_virtual() -> None:
    """このサーバのディスクでないものを容量に混ぜない。

    WSL では /mnt/c などが 9p で見え、Windows 側の 930GB が
    「サーバのディスク」として出てしまった。
    """
    skip = col.DiskUsage._SKIP
    for fstype in ("9p", "drvfs", "nfs", "nfs4", "cifs", "sshfs", "tmpfs", "overlay"):
        assert fstype in skip, fstype
    # 本物のディスクは落とさない
    for fstype in ("ext4", "btrfs", "xfs", "vfat", "f2fs", "zfs"):
        assert fstype not in skip, fstype


def test_disk_io_skips_virtual_devices() -> None:
    """loop と ram を実ディスクとして数えない。

    WSL の /sys/block には loop0-7 と ram0-15 が居るので、
    これを外さないと一覧が 0 の行で埋まる。
    """
    c = col.DiskIo
    for name in ("loop0", "ram15", "sr0"):
        assert name.startswith(c._SKIP_PREFIX), name
    for name in ("sda", "nvme0n1", "zram0", "dm-0", "md0", "vda"):
        assert not name.startswith(c._SKIP_PREFIX), name


# --- 異常検知 ---

def test_journal() -> None:
    c = mk(col.Journal)
    c.probe()
    v = c.collect(0.0)
    assert v["error_count"] == 6, v
    by_unit = {a["unit"]: a for a in v["alerts"]}
    # unit ごとにまとめる。unit 名の無い行は SYSLOG_IDENTIFIER へ落ちる
    assert set(by_unit) == {
        "nginx.service", "cronie.service", "kernel", "odd.service"}, by_unit
    # 件数の多い順
    assert v["alerts"][0]["unit"] == "nginx.service", v["alerts"]

    nginx = by_unit["nginx.service"]
    assert nginx["count"] == 3, nginx
    # PRIORITY 2 が混ざるので critical に上がる
    assert nginx["severity"] == "critical", nginx
    # 直近のメッセージを残す
    assert "signal 11" in nginx["message"], nginx
    assert nginx["ts"] == 1_790_000_020_000, nginx
    assert by_unit["cronie.service"]["severity"] == "error"
    # UTF-8 でないバイト列でも落ちず、置換文字で読める
    assert by_unit["odd.service"]["message"].startswith("hi"), by_unit["odd.service"]

    # journalctl が無い機械は非対応
    with tempfile.TemporaryDirectory() as d:
        try:
            mk(col.Journal, Path(d)).probe()
        except col.Unsupported:
            pass
        else:
            raise AssertionError("journal の写しが無いのに probe が通った")


def test_smart() -> None:
    c = mk(col.Smart)
    c.probe()
    v = c.collect(0.0)
    # 温度 58℃(しきい値 55)と再配置済みセクタ 8 件で 2 本
    assert len(v["alerts"]) == 2, v["alerts"]
    assert all(a["severity"] == "warning" for a in v["alerts"]), v["alerts"]
    assert all(a["unit"] == "/dev/sda" for a in v["alerts"]), v["alerts"]
    assert any("58" in a["message"] for a in v["alerts"]), v["alerts"]
    sda = v["devices"]["/dev/sda"]
    assert sda["passed"] is True and sda["temperature_c"] == 58, sda
    # 0 の属性は拾わない
    assert set(sda["bad_sectors"]) == {"再配置済みセクタ"}, sda

    # 総合判定が不合格なら critical
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        p = root / "run/svcscope/smart.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"ts": 1, "devices": {
            "/dev/sdb": {"smart_status": {"passed": False}}}}), encoding="utf-8")
        v2 = mk(col.Smart, root).collect(0.0)
        assert [a["severity"] for a in v2["alerts"]] == ["critical"], v2

    # 取得に失敗したデバイスは警告として残す(黙って消さない)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        p = root / "run/svcscope/smart.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"ts": 1, "devices": {
            "/dev/sdc": {"svcscope_error": "出力なし"}}}), encoding="utf-8")
        v3 = mk(col.Smart, root).collect(0.0)
        assert len(v3["alerts"]) == 1 and v3["alerts"][0]["severity"] == "warning", v3

    # ファイルがまだ無いのは「非対応」ではなく「まだ」
    with tempfile.TemporaryDirectory() as d:
        try:
            mk(col.Smart, Path(d)).probe()
        except col.Unsupported:
            pass  # fixture モードでは写しが無ければ非対応
        else:
            raise AssertionError("写しが無いのに probe が通った")


def test_security_build() -> None:
    c = mk(col.Security)
    c.probe()
    installed = c._installed()
    assert "coreutils" in installed and "audacity" not in installed, installed

    issues = json.loads((EXTERNAL_DIR / "avg.json").read_text(encoding="utf-8"))
    v = c.build(issues, installed, 1790000000000)
    # Vulnerable かつ入っているものだけ。Fixed と未インストールは出さない
    assert v["vulnerable_count"] == 2, v
    assert [a["package"] for a in v["alerts"]] == ["coreutils", "vim"], v["alerts"]
    # 重い順
    assert [a["severity"] for a in v["alerts"]] == ["critical", "warning"], v["alerts"]
    core = v["alerts"][0]
    assert core["count"] == 2, core           # CVE 2 件
    assert "AVG-1001" in core["url"], core
    assert "9.5-1" in core["message"], core   # 修正版
    # 修正版が無いものは「未リリース」と書く
    assert "未リリース" in v["alerts"][1]["message"], v["alerts"][1]


def test_news_build() -> None:
    c = mk(col.News)
    v = c.build((EXTERNAL_DIR / "news.xml").read_bytes(), __import__("time").time())
    # 30 日より古いものは出さない
    assert len(v["alerts"]) == 2, v["alerts"]
    assert all("Very old" not in a["message"] for a in v["alerts"]), v["alerts"]
    # 新しい順。手動の対応が要りそうなものは warning に上げる
    first = v["alerts"][0]
    assert first["severity"] == "warning", first
    assert "手動の対応" in first["message"], first
    assert first["url"].startswith("https://archlinux.org/news/"), first
    assert v["alerts"][1]["severity"] == "info", v["alerts"][1]


def test_external_allow_list_is_closed_by_default() -> None:
    """既定では外へ出ない。許可リストに無い URL は叩かない。"""
    c = mk(col.News)
    assert c.config.external_allow == [], c.config.external_allow
    try:
        c._fetch("https://archlinux.org/feeds/news/")
    except col.Unsupported as e:
        assert "external_allow" in str(e), e
    else:
        raise AssertionError("許可リストに無い URL を叩いてしまった")


# --- 組み上げ ---

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


def test_alerts_are_merged_and_sorted() -> None:
    """journal と smart の異常が1本に混ざり、重い順に並ぶ。"""
    s = Sampler(Config(), fixtures=ROOTS)
    s.tick(0.0)
    body = s.alerts(0.0)
    sources = {a["source"] for a in body["alerts"]}
    assert sources == {"journal", "smart"}, sources
    # journal は critical 1 + error 3、smart は warning 2
    order = ["critical", "error", "error", "error", "warning", "warning"]
    assert [a["severity"] for a in body["alerts"]] == order, body["alerts"]
    # 同じ重さなら新しいものが先
    errors = [a["ts"] for a in body["alerts"] if a["severity"] == "error"]
    assert errors == sorted(errors, reverse=True), errors
    # 外へ出る collector は許可リストが空なので取れていない
    assert body["collectors"]["security"] in ("stale", "unsupported")
    assert set(body["collectors"]) == {"journal", "smart", "security", "news"}


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  {t.__name__} OK")
    print(f"collectors {len(tests)} 件 OK")


if __name__ == "__main__":
    main()
