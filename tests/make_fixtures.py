"""テスト用の /proc・/sys の写しを作る。

ここで作るのは**手書きの模擬データ**で、実機から取ったものではない。
Core i3 / RAM 4GB / zram / HDD 1本 という対象サーバの構成に合わせて
もっともらしい数字を置いてある。パーサが形を正しく読めるかの検証と、
GUI を作るときのモックサーバに使う。

実機の写しは `packaging/capture-fixtures.sh` で取れる。実機で取れたら
`tests/fixtures/real/` に置き、こちらと両方でテストを通す。

t0 と t1 は 2 秒差の2時点。CPU 使用率・I/O・ネットワーク・unit の CPU は
差分でしか出せないため、1枚では値が出ない。
"""

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 読み込むと標準出力が UTF-8 になる。Windows の既定のコードページでは
# 日本語を print した時点で落ちるため
import dbc  # noqa: E402,F401

OUT = Path(__file__).parent / "fixtures" / "arch"

# --- 2秒間の変化量。期待値を逆算できるように定数で置く ---
ELAPSED_S = 2.0
# 全 CPU 合計で 800 tick(4コア × 2秒 × 100Hz)。うち busy 100 = 12.5%
CPU_TICKS = 800
CPU_BUSY_TICKS = 100
SDA_READ_SECTORS = 4096        # 2MB/2s = 1MB/s
SDA_WRITE_SECTORS = 8192       # 4MB/2s = 2MB/s
SDA_IO_TICKS = 300             # 300ms / 2000ms = 15%
ENO1_RX = 250_000              # 125000 B/s
ENO1_TX = 120_000              # 60000 B/s
UNIT_CPU_USEC = 2_000          # 2000us / 2000000us = 0.1%

SERVICES = {
    "sshd.service": (1_234_567, 8_388_608),
    "dbc.service": (456_789, 41_943_040),
    "systemd-journald.service": (9_876_543, 25_165_824),
    "NetworkManager.service": (2_345_678, 16_777_216),
}


# --- 異常検知の collector 用 ---
#
# journalctl の出力・SMART の写し・入っているパッケージの一覧・外部 API の応答。
# 実機のものは packaging/capture-fixtures.sh で取れるが、形は同じ。

# journalctl -p err -o json --no-pager の1行1件。PRIORITY 3 = err、2 = crit
JOURNAL_LINES = [
    {"__REALTIME_TIMESTAMP": "1790000000000000", "PRIORITY": "3",
     "_SYSTEMD_UNIT": "nginx.service",
     "MESSAGE": "connect() failed (111: Connection refused)"},
    {"__REALTIME_TIMESTAMP": "1790000010000000", "PRIORITY": "3",
     "_SYSTEMD_UNIT": "nginx.service", "MESSAGE": "upstream timed out"},
    {"__REALTIME_TIMESTAMP": "1790000020000000", "PRIORITY": "2",
     "_SYSTEMD_UNIT": "nginx.service",
     "MESSAGE": "worker process exited on signal 11"},
    {"__REALTIME_TIMESTAMP": "1790000005000000", "PRIORITY": "3",
     "_SYSTEMD_UNIT": "cronie.service", "MESSAGE": "(root) FAILED to open PAM"},
    # unit 名が無い行。SYSLOG_IDENTIFIER へ落ちることを確かめる
    {"__REALTIME_TIMESTAMP": "1790000006000000", "PRIORITY": "3",
     "SYSLOG_IDENTIFIER": "kernel", "MESSAGE": "ata1.00: failed command: READ DMA"},
    # UTF-8 でないログはバイトの配列で来る
    {"__REALTIME_TIMESTAMP": "1790000007000000", "PRIORITY": "3",
     "_SYSTEMD_UNIT": "odd.service", "MESSAGE": [104, 105, 255]},
]

# root の timer(packaging/dbc-smart.sh)が /run に書く形。
# 温度 58℃(しきい値超え)と再配置済みセクタ 8 件で、警告が2本出る想定
SMART = {
    "ts": 1790000000000,
    "devices": {
        "/dev/sda": {
            "smart_status": {"passed": True},
            "temperature": {"current": 58},
            "ata_smart_attributes": {"table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 8}},
                {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
                {"id": 9, "name": "Power_On_Hours", "raw": {"value": 41234}},
            ]},
        },
    },
}

# pacman -Qq の出力
PACMAN_Q = ["bash", "coreutils", "curl", "linux", "openssl", "python", "vim", "nginx"]

# security.archlinux.org/issues/all.json を小さくしたもの。
# 出るのは「Vulnerable かつ入っている」2件だけ
AVG = [
    {"name": "AVG-1001", "packages": ["coreutils"], "status": "Vulnerable",
     "severity": "Critical", "type": "arbitrary code execution",
     "affected": "9.4-1", "fixed": "9.5-1",
     "issues": ["CVE-2026-0001", "CVE-2026-0002"], "advisories": []},
    {"name": "AVG-1002", "packages": ["vim"], "status": "Vulnerable",
     "severity": "Medium", "type": "denial of service",
     "affected": "9.1-1", "fixed": None,
     "issues": ["CVE-2026-0003"], "advisories": []},
    # 直っているので出さない
    {"name": "AVG-1003", "packages": ["openssl"], "status": "Fixed",
     "severity": "High", "type": "information disclosure",
     "affected": "3.0-1", "fixed": "3.1-1",
     "issues": ["CVE-2026-0004"], "advisories": []},
    # 入っていないパッケージなので出さない
    {"name": "AVG-1004", "packages": ["audacity"], "status": "Vulnerable",
     "severity": "High", "type": "privilege escalation",
     "affected": "3.4-1", "fixed": "3.5-1",
     "issues": ["CVE-2026-0005"], "advisories": []},
]

# archlinux.org/feeds/news/ を小さくしたもの。日付は生成時からの相対で入れる
NEWS_ITEMS = [
    ("Manual intervention required for the /usr merge", 2),
    ("Now using Zstandard instead of xz for package compression", 10),
    # 窓(30日)の外なので出さない
    ("Very old news", 400),
]


def _journal() -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in JOURNAL_LINES)


def _news() -> str:
    import email.utils

    items = []
    for title, days_ago in NEWS_ITEMS:
        when = email.utils.formatdate(time.time() - days_ago * 86400)
        items.append(
            f"<item><title>{title}</title>"
            f"<link>https://archlinux.org/news/example-{days_ago}/</link>"
            f"<description>&lt;p&gt;本文の例&lt;/p&gt;</description>"
            f"<pubDate>{when}</pubDate></item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>Arch Linux: Recent news updates</title>"
        "<link>https://archlinux.org/news/</link>"
        "<description>例</description>"
        + "".join(items)
        + "</channel></rss>\n"
    )


def _stat(step: int) -> str:
    busy = CPU_BUSY_TICKS * step
    idle = (CPU_TICKS - CPU_BUSY_TICKS) * step
    lines = [
        # user nice system idle iowait irq softirq steal guest guest_nice
        f"cpu  {123456 + busy * 8 // 10} 789 {45678 + busy * 2 // 10} "
        f"{9876543 + idle} 12345 0 2345 0 0 0",
    ]
    for i in range(4):
        lines.append(
            f"cpu{i} {30864 + busy * 2 // 10} 197 {11419 + busy // 20} "
            f"{2469135 + idle // 4} 3086 0 586 0 0 0"
        )
    lines += [
        "intr 123456789 0 9 0 0 0",
        "ctxt 987654321",
        "btime 1789900000",
        "processes 123456",
        "procs_running 2",
        "procs_blocked 0",
        "softirq 87654321 0 1234567 0 234567 0 0 345678 0 0 456789",
    ]
    return "\n".join(lines) + "\n"


def _meminfo() -> str:
    # RAM 4GB、zram は ram/2 = 約2GB をスワップとして持つ構成
    rows = [
        ("MemTotal", 3_999_996), ("MemFree", 412_345), ("MemAvailable", 2_345_678),
        ("Buffers", 45_678), ("Cached", 1_234_567), ("SwapCached", 12_345),
        ("Active", 1_111_111), ("Inactive", 888_888),
        ("SwapTotal", 1_999_996), ("SwapFree", 1_899_996),
        ("Dirty", 1_234), ("Writeback", 0),
        ("Slab", 234_567), ("SReclaimable", 123_456), ("SUnreclaim", 111_111),
    ]
    return "".join(f"{k}:{v:>16} kB\n" for k, v in rows)


def _diskstats(step: int) -> str:
    rd = 12_345_678 + SDA_READ_SECTORS * step
    wr = 23_456_789 + SDA_WRITE_SECTORS * step
    ticks = 456_789 + SDA_IO_TICKS * step
    tail = "0 0 0 0 0 0 0 0"
    return (
        # major minor name rd_ios rd_merges rd_sectors rd_ticks
        # wr_ios wr_merges wr_sectors wr_ticks in_flight io_ticks time_in_queue
        f"   8       0 sda 234567 12345 {rd} 345678 "
        f"123456 6789 {wr} 234567 0 {ticks} 567890 {tail}\n"
        f"   8       1 sda1 234000 12000 {rd - 1000} 345000 "
        f"123000 6700 {wr - 1000} 234000 0 {ticks - 100} 567000 {tail}\n"
        f"   7       0 loop0 12 0 96 3 0 0 0 0 0 4 3 {tail}\n"
        f" 254       0 zram0 45678 0 365424 1234 23456 0 187648 567 0 890 1801 {tail}\n"
    )


def _netdev(step: int) -> str:
    rx = 987_654_321 + ENO1_RX * step
    tx = 87_654_321 + ENO1_TX * step
    return (
        "Inter-|   Receive                                                "
        "|  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast"
        "|bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo: 123456     789    0    0    0     0          0         0"
        "   123456     789    0    0    0     0       0          0\n"
        f"  eno1: {rx} 1234567    0    0    0     0          0      1234"
        f"  {tx}  876543    0    0    0     0       0          0\n"
    )


PRESSURE = {
    # cpu には full が無いカーネルもあるが、新しめのものは持っている
    "cpu": "some avg10=0.52 avg60=0.31 avg300=0.12 total=12345678\n"
           "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
    "memory": "some avg10=1.23 avg60=0.88 avg300=0.42 total=98765432\n"
              "full avg10=0.41 avg60=0.22 avg300=0.09 total=12345678\n",
    # HDD なので io の待たされ率が高く出る、という想定
    "io": "some avg10=12.34 avg60=8.76 avg300=4.21 total=987654321\n"
          "full avg10=8.10 avg60=5.43 avg300=2.10 total=543210987\n",
}

MOUNTS = (
    "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
    "sys /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0\n"
    "dev /dev devtmpfs rw,nosuid,relatime,size=1983316k 0 0\n"
    "/dev/sda2 / ext4 rw,relatime 0 0\n"
    "/dev/sda1 /boot vfat rw,relatime 0 0\n"
    "tmpfs /run tmpfs rw,nosuid,nodev,size=399996k 0 0\n"
)


def write(root: Path, step: int) -> None:
    def put(rel: str, text: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # 改行は LF 固定。Windows で生成しても実機と同じ形にするため
        p.write_text(text, encoding="utf-8", newline="\n")

    put("proc/stat", _stat(step))
    put("proc/loadavg", "0.42 0.31 0.25 2/412 98765\n")
    put("proc/meminfo", _meminfo())
    put("proc/diskstats", _diskstats(step))
    put("proc/net/dev", _netdev(step))
    put("proc/mounts", MOUNTS)
    for kind, text in PRESSURE.items():
        put(f"proc/pressure/{kind}", text)

    # /sys/block に居るものだけを「ディスク1本」として数えるので、
    # sda と zram0 のディレクトリが要る(sda1 と loop0 は置かない)
    put("sys/block/sda/size", "488397168\n")
    put("sys/block/zram0/mm_stat",
        "1234567890 456789012 489660416 0 489660416 123 0 456\n")
    put("sys/block/zram0/disksize", "2047995904\n")

    # cgroup v2 の目印。v1 にはこのファイルが無い
    put("sys/fs/cgroup/cgroup.controllers",
        "cpuset cpu io memory hugetlb pids rdma misc\n")
    for name, (usec, mem) in SERVICES.items():
        u = usec + UNIT_CPU_USEC * step
        put(f"sys/fs/cgroup/system.slice/{name}/cpu.stat",
            f"usage_usec {u}\nuser_usec {u * 7 // 10}\nsystem_usec {u * 3 // 10}\n"
            "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n")
        put(f"sys/fs/cgroup/system.slice/{name}/memory.current", f"{mem}\n")
        put(f"sys/fs/cgroup/system.slice/{name}/io.stat",
            f"8:0 rbytes={1048576 + step * 4096} wbytes={2097152 + step * 8192} "
            "rios=100 wios=200 dbytes=0 dios=0\n")
    # サービス以外(user.slice など)を拾わないことを確かめるための紛れ込み
    put("sys/fs/cgroup/system.slice/dbus.socket/memory.current", "4096\n")

    # journald / SMART / pacman の写し(異常検知の collector 用)
    put("journal.json", _journal())
    put("run/dbc/smart.json", json.dumps(SMART, ensure_ascii=False, indent=1) + "\n")
    put("pacman-q.txt", "\n".join(PACMAN_Q) + "\n")


def main() -> None:
    # 作り直す前に消す。put() は上書きしかしないので、消さないと前回の
    # 残りが混ざる(プロジェクト名を変えたとき、古い unit 名の写しが
    # 残っていてテストが落ちた)
    for stale in (OUT, OUT.parent / "external"):
        if stale.is_dir():
            shutil.rmtree(stale)

    for step in (0, 1):
        write(OUT / f"t{step}", step)
    # 外部 API の写しは時点に依らないので、写しの外に1組だけ置く
    ext = OUT.parent / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "avg.json").write_text(
        json.dumps(AVG, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n")
    (ext / "news.xml").write_text(_news(), encoding="utf-8", newline="\n")
    print(f"作成: {OUT}/t0, {OUT}/t1, {ext}")


if __name__ == "__main__":
    main()
