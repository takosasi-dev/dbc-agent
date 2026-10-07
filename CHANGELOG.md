# CHANGELOG

[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) 形式。
バージョンはセマンティックバージョニング。

## [Unreleased]

### Added

- エージェントの最小版。標準ライブラリだけで動く。
  - collector 11本。項目ごとに独立していて、1つが失敗しても他は返す。
    データ源が無い項目は `unsupported` を返す(`0` を返さない)。
    - 基本: CPU・ロード / メモリ・zram / ディスク I/O / ディスク使用量 /
      ネットワーク / PSI / systemd unit(cgroup v2)
    - 異常検知: journald(エラー以上を unit ごとに集計) / SMART(root の timer が
      書いた結果を読むだけ) / Arch Security Tracker(入っているパッケージだけに
      絞る) / Arch News(手動の対応が要る告知を拾う)
  - 外へ出る collector は別スレッドで回す。応答待ちの10秒で2秒間隔の項目を
    止めないため。前回が終わる前に二重には走らせない。
  - 外部 API の取得先は設定の許可リストに限る。**既定は空で、設定しない限り
    外へ出ない。**
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

- GitHub Actions の CI。ubuntu-latest で **本物の `/proc`・`/sys` に対して
  collector を回す**(`tests/check_live.py`)。開発機が Windows なので、
  実際のカーネルが出す形を確かめられるのは今ここだけ。
  shellcheck と `systemd-analyze verify` も通す。
- `tests/check_external.py`。外部 API を実際に叩いて、今も取れることと
  形が変わっていないかを見る。ネットワークに依存するので `run_all.py` には
  入れていない。

### Fixed

- Windows のコンソールで日本語を出した時点で `UnicodeEncodeError` で落ちていた。
  既定のコードページ(日本語環境は cp932、英語環境は cp1252)では日本語が
  encode できない。パッケージの読み込み時に出力を UTF-8 にし、Windows では
  コンソールの出力コードページも 65001 に替える(終了時に元へ戻す)。
  CI を windows-latest で回していて見つかった。

### 未了

- 実機(Arch)での動作確認と1時間以上の常駐テスト。
- endoflife.date の collector。ローリングリリースの Arch では
  「サポート終了」がほぼ当たらないので後回しにした。alert の `source` には
  `eol` を残してあるので、足すときにスキーマは変えずに済む。
- 署名付きの更新(`minisign`)とロールバック。
- `tests/fixtures/` を実機から取った写しに差し替える。
- fixture モードを単調増加の写しにする(今は写しが2枚で一巡するため、
  差分で出す項目が1回おきに `stale` になる)。GUI を本格的に作る段で。

