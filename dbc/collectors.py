"""収集するもの。

項目ごとに独立した collector にする。1つが失敗しても他は返す
(仕様書「収集仕様」)。collector を足すときは、このファイルにクラスを1つ
書いて ALL に並べるだけ。

読む場所はすべて self.root の下から組む。既定は "/" だが、テストでは
fixture のディレクトリを渡して実機の /proc の写しをパースさせる。
開発機に WSL が無くてもパーサを検証できるようにするため。
"""

import json
import os
import time
from pathlib import Path


def _now_ms() -> int:
    """API で使う時刻。Unix 時刻のミリ秒(UTC)。

    sampler にも同じものがあるが、collectors から sampler を import すると
    循環するのでこちらに持つ。
    """
    return int(time.time() * 1000)


class Unsupported(Exception):
    """データ源がこの環境に無い。API では "unsupported" として返す。"""


class NotReady(Exception):
    """差分が必要な項目の初回。まだ値を出せないだけで、異常ではない。"""


class Collector:
    name = ""
    interval = 2.0
    # 外に出る collector はここを True にする。sampler が別スレッドで回すので、
    # 応答待ちの10秒で 2 秒間隔の項目を止めない。
    blocking = False

    def __init__(self, config):
        # config をそのまま持つ。許可リストや SMART の置き場を見る collector が
        # あるので、root だけ渡す形にしていると後から足せなくなる。
        self.config = config
        self.root = Path(config.root)

    # --- 下請け ---

    def _text(self, rel: str) -> str:
        try:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise Unsupported(f"{rel} を読めません: {e}") from e

    def _require(self, rel: str) -> None:
        if not (self.root / rel).exists():
            raise Unsupported(f"{rel} がありません")

    def probe(self) -> None:
        """起動時に1回だけ呼ぶ。データ源が無ければ Unsupported を投げる。"""

    def collect(self, now: float) -> dict:
        raise NotImplementedError


def _kv_meminfo(text: str) -> dict[str, int]:
    """"MemTotal:  4012345 kB" の形をバイトの辞書にする。"""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        out[key] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return out


class Cpu(Collector):
    name = "cpu"
    interval = 2.0

    def __init__(self, config):
        super().__init__(config)
        self._prev: tuple[int, int] | None = None

    def probe(self) -> None:
        self._require("proc/stat")
        self._require("proc/loadavg")

    def collect(self, now: float) -> dict:
        lines = self._text("proc/stat").splitlines()
        head = lines[0].split()
        if head[0] != "cpu":
            raise Unsupported("proc/stat の1行目が cpu で始まっていません")
        vals = [int(v) for v in head[1:9]]
        # iowait も「空いていない」側に数えない。待たされ具合は PSI で見る。
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        cores = sum(1 for ln in lines if ln.startswith("cpu") and ln[3:4].isdigit())

        prev, self._prev = self._prev, (total, idle)
        if prev is None:
            raise NotReady
        d_total, d_idle = total - prev[0], idle - prev[1]
        # 再起動やカウンタ巻き戻しで負になったら、次の回に持ち越す
        if d_total <= 0 or d_idle < 0:
            raise NotReady

        load = self._text("proc/loadavg").split()
        return {
            "percent": round((1.0 - d_idle / d_total) * 100.0, 2),
            "load1": float(load[0]),
            "load5": float(load[1]),
            "load15": float(load[2]),
            "cores": cores,
        }


class Memory(Collector):
    name = "memory"
    interval = 2.0

    def probe(self) -> None:
        self._require("proc/meminfo")

    def collect(self, now: float) -> dict:
        m = _kv_meminfo(self._text("proc/meminfo"))
        total = m.get("MemTotal", 0)
        available = m.get("MemAvailable", m.get("MemFree", 0))
        swap_total = m.get("SwapTotal", 0)
        out = {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": max(total - available, 0),
            "cached_bytes": m.get("Cached", 0) + m.get("SReclaimable", 0),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": max(swap_total - m.get("SwapFree", 0), 0),
        }
        # zram が無ければ項目を省く(仕様書「収集仕様」)
        try:
            mm = self._text("sys/block/zram0/mm_stat").split()
        except Unsupported:
            return out
        if len(mm) >= 3:
            out["zram"] = {
                "orig_data_bytes": int(mm[0]),
                "compr_data_bytes": int(mm[1]),
                "mem_used_total_bytes": int(mm[2]),
            }
        return out


class DiskIo(Collector):
    name = "disk_io"
    interval = 2.0
    _SECTOR = 512
    # 中身のない仮想デバイス。/sys/block には居るが、見ても意味がない。
    # WSL の Arch では loop0-7 と ram0-15 が並ぶので、これを外さないと
    # 一覧が 24 行の 0 で埋まる。zram と dm-/md は実際の I/O なので残す。
    _SKIP_PREFIX = ("loop", "ram", "sr")

    def __init__(self, config):
        super().__init__(config)
        self._prev: tuple[float, dict[str, tuple[int, int, int]]] | None = None

    def probe(self) -> None:
        self._require("proc/diskstats")
        self._require("sys/block")

    def _read(self) -> dict[str, tuple[int, int, int]]:
        out = {}
        for line in self._text("proc/diskstats").splitlines():
            f = line.split()
            if len(f) < 14:
                continue
            name = f[2]
            # パーティションを除くため、/sys/block に居るものだけ見る。
            # 名前の末尾が数字かどうかで判定すると nvme0n1 を落としてしまう。
            if not (self.root / "sys/block" / name).exists():
                continue
            if name.startswith(self._SKIP_PREFIX):
                continue
            out[name] = (int(f[5]), int(f[9]), int(f[12]))
        return out

    def collect(self, now: float) -> dict:
        cur = self._read()
        prev, self._prev = self._prev, (now, cur)
        if prev is None:
            raise NotReady
        elapsed = now - prev[0]
        if elapsed <= 0:
            raise NotReady

        devices = []
        for name, (rd, wr, ticks) in sorted(cur.items()):
            old = prev[1].get(name)
            if old is None:
                continue
            devices.append({
                "device": name,
                "read_bytes_per_sec": round(max(rd - old[0], 0) * self._SECTOR / elapsed, 1),
                "write_bytes_per_sec": round(max(wr - old[1], 0) * self._SECTOR / elapsed, 1),
                # io_ticks はミリ秒。経過時間に対する割合が「混んでいた率」。
                "util_percent": round(min(max(ticks - old[2], 0) / (elapsed * 1000.0) * 100.0, 100.0), 2),
            })
        if not devices:
            raise NotReady
        return {"devices": devices}


class DiskUsage(Collector):
    name = "disk_usage"
    interval = 60.0
    # 実体を持たない、または容量を見ても意味がないファイルシステム
    _SKIP = {
        # カーネルが見せているだけのもの
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "securityfs", "pstore", "bpf", "autofs", "hugetlbfs", "mqueue",
        "debugfs", "tracefs", "configfs", "fusectl", "ramfs", "squashfs",
        "efivarfs", "binfmt_misc", "nsfs", "overlay",
        "rootfs", "initramfs", "initrd",
        # このサーバのディスクではないもの。容量を足すと二重に数えるうえ、
        # 「サーバが満杯」と誤読させる。WSL では /mnt/c などが 9p で見えた
        "9p", "drvfs", "virtiofs", "cifs", "smbfs", "smb3",
        "nfs", "nfs4", "sshfs", "fuse.sshfs", "fuse.rclone", "afs",
    }

    def probe(self) -> None:
        if not hasattr(os, "statvfs"):
            raise Unsupported("statvfs がない環境です")
        if self.root != Path("/"):
            # fixture の中身に対して statvfs を掛けても実機の値にならない。
            # 嘘の数字を返すより「非対応」と言うほうが正しい。
            raise Unsupported("fixture モードでは実ファイルシステムを測れません")
        self._require("proc/mounts")

    def collect(self, now: float) -> dict:
        seen: set[str] = set()
        rows = []
        for line in self._text("proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3 or f[2] in self._SKIP:
                continue
            # 同じデバイスを複数箇所に mount していても1回だけ数える
            if f[0] in seen:
                continue
            mount = f[1].replace("\\040", " ")
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            seen.add(f[0])
            total = st.f_blocks * st.f_frsize
            if total == 0:
                continue
            rows.append({
                "mount": mount,
                "fstype": f[2],
                "total_bytes": total,
                # f_bavail は非特権ユーザが使える分。used は予約分を含めた実使用。
                "used_bytes": (st.f_blocks - st.f_bfree) * st.f_frsize,
                "available_bytes": st.f_bavail * st.f_frsize,
            })
        if not rows:
            raise NotReady
        return {"filesystems": rows}


class Net(Collector):
    name = "net"
    interval = 2.0

    def __init__(self, config):
        super().__init__(config)
        self._prev: tuple[float, dict[str, tuple[int, int]]] | None = None

    def probe(self) -> None:
        self._require("proc/net/dev")

    def collect(self, now: float) -> dict:
        cur = {}
        for line in self._text("proc/net/dev").splitlines():
            name, _, rest = line.partition(":")
            name = name.strip()
            f = rest.split()
            if not name or name == "lo" or len(f) < 9:
                continue
            cur[name] = (int(f[0]), int(f[8]))

        prev, self._prev = self._prev, (now, cur)
        if prev is None:
            raise NotReady
        elapsed = now - prev[0]
        if elapsed <= 0:
            raise NotReady

        rows = []
        for name, (rx, tx) in sorted(cur.items()):
            old = prev[1].get(name)
            if old is None:
                continue
            rows.append({
                "name": name,
                "rx_bytes_per_sec": round(max(rx - old[0], 0) / elapsed, 1),
                "tx_bytes_per_sec": round(max(tx - old[1], 0) / elapsed, 1),
            })
        if not rows:
            raise NotReady
        return {"interfaces": rows}


class Psi(Collector):
    """待たされ率。このツールの売りなので、無効な環境ははっきり「非対応」と返す。"""

    name = "psi"
    interval = 2.0
    _KINDS = ("cpu", "memory", "io")

    def probe(self) -> None:
        # CONFIG_PSI=n のカーネルではディレクトリ自体が無い
        if not (self.root / "proc/pressure").is_dir():
            raise Unsupported("proc/pressure がありません(カーネルで PSI が無効)")

    def collect(self, now: float) -> dict:
        out: dict[str, dict[str, float]] = {}
        for kind in self._KINDS:
            try:
                text = self._text(f"proc/pressure/{kind}")
            except Unsupported:
                continue
            for line in text.splitlines():
                f = line.split()
                if not f:
                    continue
                # "some avg10=0.00 avg60=0.00 avg300=0.00 total=0"
                scope = f[0]
                for item in f[1:]:
                    key, _, val = item.partition("=")
                    if key.startswith("avg"):
                        out.setdefault(kind, {})[f"{scope}_{key}"] = float(val)
        if not out:
            raise Unsupported("proc/pressure から値が取れません")
        return out


class Units(Collector):
    """systemd unit ごとの負荷。cgroup v2(unified)前提。v1 は非対応。"""

    name = "units"
    interval = 5.0
    _SLICE = "sys/fs/cgroup/system.slice"

    def __init__(self, config):
        super().__init__(config)
        self._prev: tuple[float, dict[str, int]] | None = None

    def probe(self) -> None:
        # cgroup v1 には cgroup.controllers が無い。これで v2 かを判別する。
        self._require("sys/fs/cgroup/cgroup.controllers")
        if not (self.root / self._SLICE).is_dir():
            raise Unsupported("system.slice がありません")

    def _value(self, rel: str) -> int | None:
        try:
            return int((self.root / rel).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def collect(self, now: float) -> dict:
        slice_dir = self.root / self._SLICE
        cpu_usec: dict[str, int] = {}
        rows: dict[str, dict] = {}

        for d in sorted(slice_dir.iterdir()):
            if not d.is_dir() or not d.name.endswith(".service"):
                continue
            rel = f"{self._SLICE}/{d.name}"
            row: dict = {"name": d.name}

            mem = self._value(f"{rel}/memory.current")
            if mem is not None:
                row["memory_bytes"] = mem

            # cpu.stat は "usage_usec 12345" の行を持つ累積値
            try:
                for line in (d / "cpu.stat").read_text(encoding="utf-8").splitlines():
                    k, _, v = line.partition(" ")
                    if k == "usage_usec":
                        cpu_usec[d.name] = int(v)
            except (OSError, ValueError):
                pass

            # io.stat は "8:0 rbytes=.. wbytes=.." がデバイスごとに並ぶ
            read = write = 0
            try:
                for line in (d / "io.stat").read_text(encoding="utf-8").splitlines():
                    for item in line.split()[1:]:
                        k, _, v = item.partition("=")
                        if k == "rbytes":
                            read += int(v)
                        elif k == "wbytes":
                            write += int(v)
                row["io_read_bytes"] = read
                row["io_write_bytes"] = write
            except (OSError, ValueError):
                pass

            rows[d.name] = row

        prev, self._prev = self._prev, (now, cpu_usec)
        if prev is None:
            raise NotReady
        elapsed = now - prev[0]
        if elapsed <= 0:
            raise NotReady

        for name, row in rows.items():
            old = prev[1].get(name)
            cur = cpu_usec.get(name)
            if old is None or cur is None:
                continue
            # usec の差を経過秒で割る。コア数で割らないので 1 コア飽和 = 100%。
            row["cpu_percent"] = round(max(cur - old, 0) / (elapsed * 1_000_000.0) * 100.0, 2)

        return {"units": [rows[k] for k in sorted(rows)]}


# --- 異常検知。ここから下の collector は alerts の形で返す ---
#
# 返すのは docs/api/v1.schema.json の $defs/alert に合わせた辞書の並び。
# /api/v1/alerts は各 collector の "alerts" をつなげるだけにしてある。

# PRIORITY(syslog) -> 重さ。journalctl には -p err で 0..3 だけ取らせる
_PRIORITY = {"0": "critical", "1": "critical", "2": "critical", "3": "error", "4": "warning"}

# Arch Security Tracker の severity -> こちらの重さ
_AVG_SEVERITY = {
    "Critical": "critical", "High": "error",
    "Medium": "warning", "Low": "info", "Unknown": "info",
}

# 外へ出るときに名乗る。相手に迷惑をかけたとき辿れるようにしておく
USER_AGENT = "dbc/0.1 (+https://github.com/takosasi-dev/dbc-agent)"
FETCH_TIMEOUT_S = 10.0


class Alerting(Collector):
    """異常を返す collector の共通部分。"""

    def _run(self, args: list[str], timeout: float = 10.0) -> str:
        """外部コマンドを1回だけ実行して標準出力を返す。"""
        import subprocess

        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except FileNotFoundError as e:
            raise Unsupported(f"{args[0]} がありません") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"{args[0]} が {timeout} 秒で終わりませんでした") from e
        return proc.stdout

    def _fetch(self, url: str) -> bytes:
        """外部 API を叩く。許可リストに無い URL は叩かない。

        取得先を設定ファイルの許可リストに限るのは仕様書「認証とセキュリティ」
        外部通信のとおり。既定は空なので、設定しない限り外へ出ない。
        """
        import urllib.error
        import urllib.request

        allow = list(self.config.external_allow or [])
        if not any(url.startswith(prefix) for prefix in allow):
            raise Unsupported(
                f"{url} は external_allow にありません。設定しない限り外へ出ません"
            )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as e:
            # 外が落ちていてもエージェント全体は止めない。次の回に取り直す
            raise RuntimeError(f"{url} を取得できません: {e}") from e


class Journal(Alerting):
    """journald のエラー以上のログ。直近30分ぶんを unit ごとに数える。"""

    name = "journal"
    interval = 30.0
    WINDOW_S = 1800
    MAX_LINES = 500
    MAX_UNITS = 20

    def probe(self) -> None:
        if self.root != Path("/"):
            # fixture では journalctl を呼ばず、写した出力を読む
            self._require("journal.json")
            return
        import shutil

        if shutil.which("journalctl") is None:
            raise Unsupported("journalctl がありません")

    def _lines(self, now: float) -> list[str]:
        if self.root != Path("/"):
            return self._text("journal.json").splitlines()
        # @<秒> で絶対時刻を渡す。相対指定の書き方は systemd の版で揺れる
        since = int(time.time()) - self.WINDOW_S
        out = self._run([
            "journalctl", "-p", "err", "-o", "json", "--no-pager",
            "-n", str(self.MAX_LINES), f"--since=@{since}",
        ], timeout=15.0)
        return out.splitlines()

    @staticmethod
    def _message(value) -> str:
        # MESSAGE は UTF-8 でないログだとバイトの配列で来る
        if isinstance(value, list):
            return bytes(v & 0xFF for v in value).decode("utf-8", "replace")
        return str(value or "")

    def collect(self, now: float) -> dict:
        per_unit: dict[str, dict] = {}
        total = 0
        for line in self._lines(now):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue  # 壊れた1行で全体を捨てない
            total += 1
            unit = e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "(不明)"
            severity = _PRIORITY.get(str(e.get("PRIORITY")), "error")
            try:
                ts = int(int(e.get("__REALTIME_TIMESTAMP", 0)) / 1000)
            except (TypeError, ValueError):
                ts = 0
            row = per_unit.setdefault(unit, {
                "ts": ts, "source": "journal", "severity": severity,
                "message": "", "unit": unit, "count": 0,
            })
            row["count"] += 1
            # 直近のものを残す。重さは窓の中で最も重いものに上げる
            if ts >= row["ts"]:
                row["ts"] = ts
                row["message"] = self._message(e.get("MESSAGE"))[:300]
            if severity == "critical" or row["severity"] == "warning":
                row["severity"] = severity

        alerts = sorted(per_unit.values(), key=lambda r: -r["count"])[:self.MAX_UNITS]
        return {"alerts": alerts, "error_count": total, "window_s": self.WINDOW_S}


class Smart(Collector):
    """SMART の異常値。root の timer が書いたファイルを読むだけ。

    smartctl を自分で呼ばない。エージェントに root を持たせないため
    (仕様書「認証とセキュリティ」SMART の取得)。
    """

    name = "smart"
    interval = 600.0
    TEMP_WARN_C = 55
    # 再配置・保留セクタ。0 以外は手当てが要る合図
    _BAD_ATTRS = {5: "再配置済みセクタ", 196: "再配置イベント",
                  197: "保留セクタ", 198: "回復不能セクタ"}

    @property
    def _path(self) -> Path:
        # 設定は絶対パスで書くが、読む場所は root の下から組む。
        # Windows では "/run/..." が is_absolute() で False になり、そのまま
        # join するとドライブ直下を指してしまうので、頭の区切りを自分で落とす。
        rel = str(self.config.smart_file).replace("\\", "/").lstrip("/")
        return self.root / rel

    def probe(self) -> None:
        if self._path.exists():
            return
        if self.root != Path("/"):
            raise Unsupported("fixture に SMART の写しがありません")
        import shutil

        if shutil.which("smartctl") is None:
            raise Unsupported("smartctl がありません (pacman -S smartmontools)")
        # timer が初回に書くまではファイルが無い。collect 側で NotReady になる

    def collect(self, now: float) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as e:
            raise NotReady from e
        except ValueError as e:
            raise RuntimeError(f"{self._path} の JSON が壊れています: {e}") from e

        ts = int(data.get("ts") or 0)
        alerts = []
        devices = {}
        for dev, info in (data.get("devices") or {}).items():
            if not isinstance(info, dict) or "dbc_error" in info:
                alerts.append({"ts": ts, "source": "smart", "severity": "warning",
                               "message": f"{dev}: SMART を取得できませんでした", "unit": dev})
                continue

            passed = (info.get("smart_status") or {}).get("passed")
            temp = (info.get("temperature") or {}).get("current")
            summary = {"passed": passed, "temperature_c": temp}

            if passed is False:
                alerts.append({"ts": ts, "source": "smart", "severity": "critical",
                               "message": f"{dev}: SMART の総合判定が不合格です", "unit": dev})
            if isinstance(temp, (int, float)) and temp >= self.TEMP_WARN_C:
                alerts.append({"ts": ts, "source": "smart", "severity": "warning",
                               "message": f"{dev}: 温度 {temp}℃ ("
                                          f"{self.TEMP_WARN_C}℃ 以上)", "unit": dev})

            table = ((info.get("ata_smart_attributes") or {}).get("table")) or []
            bad = {}
            for attr in table:
                label = self._BAD_ATTRS.get(attr.get("id"))
                if not label:
                    continue
                raw = (attr.get("raw") or {}).get("value")
                if isinstance(raw, int) and raw > 0:
                    bad[label] = raw
                    alerts.append({"ts": ts, "source": "smart", "severity": "warning",
                                   "message": f"{dev}: {label}が {raw} 件", "unit": dev})
            if bad:
                summary["bad_sectors"] = bad
            devices[dev] = summary

        if not devices and not alerts:
            raise NotReady
        return {"alerts": alerts, "devices": devices}


class Security(Alerting):
    """Arch Security Tracker の未修正の脆弱性を、入っているパッケージだけに絞る。"""

    name = "security"
    interval = 6 * 3600.0
    blocking = True
    URL = "https://security.archlinux.org/issues/all.json"
    MAX_ALERTS = 30

    def probe(self) -> None:
        if self.root != Path("/"):
            self._require("pacman-q.txt")
            return
        import shutil

        if shutil.which("pacman") is None:
            raise Unsupported("pacman がありません(Arch 以外では入っている物を照合できません)")

    def _installed(self) -> set[str]:
        if self.root != Path("/"):
            return {ln.strip() for ln in self._text("pacman-q.txt").splitlines() if ln.strip()}
        return {ln.strip() for ln in self._run(["pacman", "-Qq"]).splitlines() if ln.strip()}

    def collect(self, now: float) -> dict:
        return self.build(json.loads(self._fetch(self.URL)), self._installed(), _now_ms())

    def build(self, issues: list, installed: set, ts: int) -> dict:
        """取れた JSON を alerts の形にする。通信しないのでテストしやすい。"""
        alerts = []
        for avg in issues:
            if "Vulnerable" not in (avg.get("status") or ""):
                continue
            hit = sorted(set(avg.get("packages") or []) & installed)
            if not hit:
                continue
            cves = avg.get("issues") or []
            alerts.append({
                "ts": ts, "source": "security",
                "severity": _AVG_SEVERITY.get(avg.get("severity"), "info"),
                "message": f"{', '.join(hit)}: {avg.get('type') or '不明'} "
                           f"({avg.get('name')}, {len(cves)} 件の CVE, 修正版 "
                           f"{avg.get('fixed') or '未リリース'})",
                "package": ", ".join(hit),
                "url": f"https://security.archlinux.org/{avg.get('name')}",
                "count": len(cves),
            })

        order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
        alerts.sort(key=lambda a: order.get(a["severity"], 9))
        return {
            "alerts": alerts[:self.MAX_ALERTS],
            "vulnerable_count": len(alerts),
            "installed_count": len(installed),
        }


class News(Alerting):
    """Arch のニュース。手動の対応が要る告知を見落とさないため。"""

    name = "news"
    interval = 6 * 3600.0
    blocking = True
    URL = "https://archlinux.org/feeds/news/"
    WINDOW_DAYS = 30
    MAX_ALERTS = 10
    # ponytail: 「手で対応が要る」の判定は題名と本文の語句で見ているだけ。
    # 取りこぼしても info として一覧には出るので、見落としても致命的にならない。
    _MANUAL = ("manual intervention", "requires manual", "action required")

    def probe(self) -> None:
        # ネットワークだけで足りる。許可リストの確認は _fetch 側でやる
        pass

    def collect(self, now: float) -> dict:
        return self.build(self._fetch(self.URL), time.time())

    def build(self, xml_bytes: bytes, now_s: float) -> dict:
        """RSS を alerts の形にする。通信しないのでテストしやすい。"""
        import email.utils
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_bytes)
        cutoff = now_s - self.WINDOW_DAYS * 86400
        alerts = []
        for item in root.iterfind("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            body = (item.findtext("description") or "")
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
            when = published.timestamp()
            if when < cutoff:
                continue
            text = f"{title} {body}".lower()
            manual = any(k in text for k in self._MANUAL)
            alerts.append({
                "ts": int(when * 1000), "source": "news",
                "severity": "warning" if manual else "info",
                "message": ("[手動の対応が要る可能性] " if manual else "") + title,
                "url": link,
            })
        alerts.sort(key=lambda a: -a["ts"])
        return {"alerts": alerts[:self.MAX_ALERTS], "window_days": self.WINDOW_DAYS}


ALL: list[type[Collector]] = [
    Cpu, Memory, DiskIo, DiskUsage, Net, Psi, Units,
    Journal, Smart, Security, News,
]


def demo() -> None:
    """実機の /proc があればそこから、無ければ fixture から回す。

    差分が要る項目は 2 回読む。実機なら 2 秒待ち、fixture なら t0 -> t1 と
    写しを進める。
    """
    from .config import Config

    tests = Path(__file__).parent.parent / "tests"
    fixtures = sorted((tests / "fixtures/arch").glob("t*"))
    live = Path("/proc/stat").exists()
    roots = [Path("/"), Path("/")] if live else fixtures[:2]
    assert roots, "先に python tests/make_fixtures.py を実行してください"
    print(f"root = {roots[0]}" + ("" if live else f" -> {roots[1]}"))

    for cls in ALL:
        c = cls(Config(root=roots[0]))
        try:
            c.probe()
        except Unsupported as e:
            print(f"  {c.name}: unsupported ({e})")
            continue

        # 外へ出る collector は、通信せずに解析だけ試す。
        # 実際の取得は tests/check_external.py で確かめる。
        if c.blocking and not live:
            ext = tests / "fixtures/external"
            if c.name == "security":
                value = c.build(json.loads((ext / "avg.json").read_text(encoding="utf-8")),
                                set(c._installed()), _now_ms())
            else:
                value = c.build((ext / "news.xml").read_bytes(), time.time())
            print(f"  {c.name}: ok(解析のみ) alerts={len(value['alerts'])}")
            continue

        t = time.monotonic()
        try:
            try:
                c.collect(t)
            except NotReady:
                pass
            if live:
                time.sleep(2.0)
            c.root = roots[1]
            value = c.collect(t + 2.0)
        except NotReady:
            print(f"  {c.name}: not ready(差分がまだ出ません)")
        except Unsupported as e:
            print(f"  {c.name}: unsupported ({e})")
        else:
            extra = f" alerts={len(value['alerts'])}" if "alerts" in value else ""
            print(f"  {c.name}: ok {sorted(value)}{extra}")


if __name__ == "__main__":
    demo()
