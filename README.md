# SvcScope (agent)

Linux サーバの **systemd unit 単位**の負荷と、**待たされ率(PSI)** を集めて
HTTP API で返す常駐エージェント。サーバ側のリポジトリ。

PC 側(GUI)は別リポジトリ: [svcscope-gui](https://github.com/takosasi-dev/svcscope-gui)。
2つはコードを共有せず、[API 仕様](docs/api/README.md) だけで結ぶ。

> 開発中(v0.1 に向けて)。実機での常駐テストは未了。

## 何が見えるか

`htop` や `btop` はプロセス単位の使用率を見せるが、「なぜ遅いか」までは
示さない。このツールは次の2点に振っている。

- **サービス単位** — cgroup v2 から unit ごとの CPU・メモリ・I/O を取る。
  どのサービスが重いかが一覧で分かる。
- **待たされ率** — `/proc/pressure/` の PSI を、使用率ではなく
  「待たされた時間の割合」として出す。HDD の機械では、CPU 使用率が低いのに
  遅い状況がこれで見える。

## 動作環境

| | |
| --- | --- |
| OS | Arch Linux(v0.1 で動作保証する範囲)。設計はディストロ非依存 |
| カーネル | cgroup v2 (unified)。PSI 有効(`CONFIG_PSI=y`) |
| Python | 3.11 以降(`tomllib` を使うため) |
| 依存ライブラリ | **無し**(標準ライブラリだけ) |
| 任意 | `smartmontools`(SMART を見る場合) |

cgroup v1、PSI 無効、zram 無し、smartctl 無しの環境でも起動する。
その項目が `unsupported` として返るだけで、他の項目は通常どおり取れる。

常駐メモリの目標は 80MB 以下、アイドル時の CPU は 2% 以下
(Python プロトタイプの目標。実機で測って見直す)。

## 入れる

サーバで root で。

```sh
git clone https://github.com/takosasi-dev/svcscope-agent.git
cd svcscope-agent
sudo ./packaging/setup.sh          # 前提を確認し、専用ユーザと systemd unit を入れる
sudo ./packaging/gen-token.sh      # トークンを作る。平文は1回だけ表示される
sudo systemctl enable --now svcscope.service
sudo systemctl enable --now svcscope-smart.timer   # SMART を見る場合
```

`setup.sh` は cgroup のバージョン、PSI の有無、`sshd` の
`AllowTcpForwarding`、`smartctl` の有無をその場で確かめて表示する。

エージェントは **root では動かない**。専用の非特権ユーザ `svcscope` で動き、
systemd 側で `ProtectSystem=strict` などを掛けてある。SMART だけは root が
要るので、別の timer が10分ごとに結果を `/run/svcscope/smart.json` に置き、
エージェントはそれを読むだけにしている。

## 使う

### サーバの中から

```sh
python -m svcscope.cli --token-file ~/svcscope-token snapshot   # 1回だけ
python -m svcscope.cli --token-file ~/svcscope-token watch      # 2秒ごとに上書き表示
python -m svcscope.cli --token-file ~/svcscope-token --json health
```

```
SvcScope  http://127.0.0.1:8765  agent 0.1.0  2026-10-07 18:12:04

CPU    12.5%   load 0.42 / 0.31 / 0.25   (4 cores)
MEM   1.6G / 3.8G (41%)   swap 97.7M / 1.9G   zram 1.1G->435.6M
PSI   cpu some 0.52   mem some 1.23 full 0.41   io some 12.34 full 8.10
DISK  sda      r 1.0M/s  w 2.0M/s  util  15.0%
      /        12.3G / 220.0G (6%)
NET   eno1     rx 122.1K/s  tx 58.6K/s

unit                                 cpu%      mem   io r/w
systemd-journald.service             0.42    24.0M   1.0M / 2.0M
sshd.service                         0.10     8.0M   1.0M / 2.0M

collectors  すべて ok
```

### PC から

エージェントは `127.0.0.1` にしか待ち受けないので、SSH の
ポートフォワードを張ってから叩く。手順は [docs/ssh.md](docs/ssh.md)。

```sh
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 arch-tunnel
python -m svcscope.cli --url http://127.0.0.1:18765 watch
```

## 設定

`/etc/svcscope/config.toml`。雛形は
[packaging/config.example.toml](packaging/config.example.toml)。

待ち受け先はループバックか、明示した VPN インターフェースの IP だけ。
`0.0.0.0` を書くとエージェントは**起動を拒否する**。

トークンはサーバに SHA-256 のハッシュだけを置く。平文は PC 側にしか無い。

## 開発

開発は PC 側で行う。サーバ上ではビルドしない。

```sh
python tests/run_all.py            # 全部
python tests/test_collectors.py    # /proc のパーサ
python tests/test_contract.py      # API が仕様どおりか(jsonschema が要る)
```

Linux の無い機械でも API を立てられる。`/proc` の写しを読む fixture モード。

```sh
python tests/make_fixtures.py
python -m svcscope --fixtures tests/fixtures/arch --port 18765 \
  --token-hash $(python -c "import hashlib;print(hashlib.sha256(b'...').hexdigest())")
```

`tests/fixtures/arch` は**手書きの模擬データ**で、実機の写しではない。
実機のものは `./packaging/capture-fixtures.sh tests/fixtures/real` で取れる
(公開前に中身を確認すること。マウント先のパスやサービス名が入る)。

fixture モードは写しが2枚で一巡するため、差分で出す項目(CPU 使用率・I/O・
ネットワーク)が1回おきに `stale` になる。実機では起きない。

### 構成

| ファイル | 役割 |
| --- | --- |
| `svcscope/collectors.py` | `/proc`・`/sys`・cgroup のパーサ。collector を足すならここ |
| `svcscope/sampler.py` | 収集ループとリングバッファ。スレッドは1本だけ |
| `svcscope/server.py` | HTTP API |
| `svcscope/auth.py` | トークンの照合(定数時間比較、連続失敗で締め出し) |
| `svcscope/cli.py` | CUI クライアント。API だけを見る |
| `docs/api/` | **API 仕様の正本。** GUI 側はここだけを見る |

## ライセンス

MIT。[LICENSE](LICENSE) を見る。
