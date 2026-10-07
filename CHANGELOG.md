# CHANGELOG

[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) 形式。
バージョンはセマンティックバージョニング。

## [Unreleased]

### Added

- エージェントの最小版。標準ライブラリだけで動く。
  - collector 6本: CPU・ロード / メモリ・zram / ディスク I/O / ディスク使用量 /
    ネットワーク / PSI / systemd unit(cgroup v2)。項目ごとに独立していて、
    1つが失敗しても他は返す。データ源が無い項目は `unsupported` を返す。
  - HTTP API: `/version`、`/api/v1/{snapshot,history,units,alerts,health,stream}`。
    全エンドポイントで Bearer トークン必須。`/version` も免除しない。
  - トークンの照合は定数時間比較。連続5回の失敗で60秒締め出し(429 + Retry-After)。
    サーバに置くのは SHA-256 のハッシュだけ。
  - 履歴はメモリ上のリングバッファだけ(全体 2秒 × 900点、unit 5秒 × 360点)。
    ディスクには書かない。
  - `bind` に `0.0.0.0` が指定されたら起動を拒否する。
- CUI クライアント `python -m svcscope.cli`(`snapshot` / `units` / `alerts` /
  `health` / `version` / `watch`)。API だけを見て動く。
- API 仕様の正本 `docs/api/`(JSON Schema draft 2020-12)と契約テスト。
- fixture モード(`--fixtures`)。`/proc` の写しから読むので、Linux の無い
  機械でも API を立てられる。
- systemd unit(硬化つき、`MemoryMax=150M`)、SMART 取得用の root timer、
  `setup.sh` / `gen-token.sh` / `capture-fixtures.sh`。
- PC から繋ぐ手順 `docs/ssh.md`(鍵2本、`permitopen` での縛り、トンネルの確認方法)。

### 未了

- 実機(Arch)での動作確認と1時間以上の常駐テスト。
- journald・SMART・外部 API の collector(`/api/v1/alerts` は形だけ返している)。
- 署名付きの更新(`minisign`)とロールバック。
- `tests/fixtures/` を実機から取った写しに差し替える。
