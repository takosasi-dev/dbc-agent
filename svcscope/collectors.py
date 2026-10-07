"""収集するもの。

項目ごとに独立した collector にする。1つが失敗しても他は返す
(仕様書「収集仕様」)。collector を足すときは、このファイルにクラスを1つ
書いて ALL に並べるだけ。

読む場所はすべて self.root の下から組む。既定は "/" だが、テストでは
fixture のディレクトリを渡して実機の /proc の写しをパースさせる。
開発機に WSL が無くてもパーサを検証できるようにするため。
"""

import os
import time
from pathlib import Path


class Unsupported(Exception):
    """データ源がこの環境に無い。API では "unsupported" として返す。"""


class NotReady(Exception):
    """差分が必要な項目の初回。まだ値を出せないだけで、異常ではない。"""


class Collector:
    name = ""
    interval = 2.0

    def __init__(self, root: Path = Path("/")):
        self.root = Path(root)

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

    def __init__(self, root: Path = Path("/")):
        super().__init__(root)
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

    def __init__(self, root: Path = Path("/")):
        super().__init__(root)
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
            # パーティションや loop を除くため、/sys/block に居るものだけ見る。
            # 名前の末尾が数字かどうかで判定すると nvme0n1 を落としてしまう。
            if not (self.root / "sys/block" / name).exists():
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
        "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
        "securityfs", "pstore", "bpf", "autofs", "hugetlbfs", "mqueue",
        "debugfs", "tracefs", "configfs", "fusectl", "ramfs", "squashfs",
        "efivarfs", "binfmt_misc", "nsfs", "overlay",
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

    def __init__(self, root: Path = Path("/")):
        super().__init__(root)
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

    def __init__(self, root: Path = Path("/")):
        super().__init__(root)
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


ALL: list[type[Collector]] = [Cpu, Memory, DiskIo, DiskUsage, Net, Psi, Units]


def demo() -> None:
    """実機の /proc があればそこから、無ければ fixture から回す。

    差分が要る項目は 2 回読む。実機なら 2 秒待ち、fixture なら t0 -> t1 と
    写しを進める。
    """
    fixtures = sorted((Path(__file__).parent.parent / "tests/fixtures/arch").glob("t*"))
    live = Path("/proc/stat").exists()
    roots = [Path("/"), Path("/")] if live else fixtures[:2]
    print(f"root = {roots[0]}" + ("" if live else f" -> {roots[1]}"))

    for cls in ALL:
        c = cls(roots[0])
        try:
            c.probe()
        except Unsupported as e:
            print(f"  {c.name}: unsupported ({e})")
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
            print(f"  {c.name}: ok {sorted(value)}")


if __name__ == "__main__":
    demo()
