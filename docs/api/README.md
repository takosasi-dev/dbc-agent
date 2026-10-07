# SvcScope API v1

**このディレクトリが API 仕様の正本。** GUI 側のリポジトリ(`svcscope-gui`)は
コードを共有せず、ここだけを見て実装する。スキーマの機械可読版は
[`v1.schema.json`](v1.schema.json)(JSON Schema draft 2020-12)。

形式に OpenAPI ではなく JSON Schema を選んだのは、契約テストでそのまま
検証器に渡せて、ツールチェーンを増やさずに済むため。

## エンドポイント

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/version` | エージェントの版、API のメジャー版、対応機能の一覧。版に依存しない固定パス |
| GET | `/api/v1/snapshot` | 全 collector の現在値 |
| GET | `/api/v1/history?since=<ms>` | リングバッファの履歴 |
| GET | `/api/v1/units` | systemd unit の一覧と現在値 |
| GET | `/api/v1/alerts` | 異常の一覧(journald、SMART、脆弱性、サポート終了) |
| GET | `/api/v1/health` | エージェント自身の状態 |
| GET | `/api/v1/stream` | SSE で2秒ごとに現在値を流す |

GET 以外は受け付けない(405)。書き込み系の口は持たない。監視専用。

## 共通の規約

- **認証**: すべてのエンドポイントで `Authorization: Bearer <token>` が必須。
  `/version` も免除しない。トークンが違えば `401`、連続失敗で締め出し中は
  `429` と `Retry-After`。
- **形式**: UTF-8 の JSON。時刻は Unix 時刻のミリ秒(UTC)。単位はフィールド名に
  含める(`memory_used_bytes`、`cpu_percent`)。
- **エラー**: HTTP ステータスと `{"error": {"code": "...", "message": "..."}}`。
- **互換性**: `v1` の中では項目の**追加だけ**を許す。削除・型の変更・意味の
  変更は `v2` として分ける。**GUI は未知の項目を無視する。** そのため
  スキーマはどの応答も `additionalProperties` を閉じていない。
- **取れなかった項目**: キーごと省く。`0` を返さない。理由は `collectors` を見る。

### collectors の値

| 値 | 意味 | GUI の扱い |
| --- | --- | --- |
| `ok` | 新しい値がある | そのまま表示する |
| `stale` | 値が古い、または未取得 | 灰色にするなど「今の値ではない」と示す |
| `unsupported` | この環境にデータ源が無い | 「非対応」と出す。再試行しない |

`unsupported` になる主な理由は、カーネルで PSI が無効、cgroup v1、
zram が無い、smartmontools が入っていない、など。

## 応答の例

```json
{
  "ts": 1790000000000,
  "cpu": {"percent": 12.5, "load1": 0.42, "load5": 0.31, "load15": 0.25, "cores": 4},
  "memory": {
    "total_bytes": 4095995904, "used_bytes": 1693487104, "available_bytes": 2402508800,
    "swap_total_bytes": 2047995904, "swap_used_bytes": 102400000,
    "zram": {"orig_data_bytes": 1234567890, "compr_data_bytes": 456789012,
             "mem_used_total_bytes": 489660416}
  },
  "disk": {
    "devices": [{"device": "sda", "read_bytes_per_sec": 1048576.0,
                 "write_bytes_per_sec": 2097152.0, "util_percent": 15.0}]
  },
  "net": {"interfaces": [{"name": "eno1", "rx_bytes_per_sec": 125000.0,
                          "tx_bytes_per_sec": 60000.0}]},
  "psi": {"io": {"some_avg10": 12.34, "full_avg10": 8.1}},
  "units": [{"name": "sshd.service", "cpu_percent": 0.1, "memory_bytes": 8388608}],
  "collectors": {"cpu": "ok", "psi": "ok", "disk_usage": "unsupported"}
}
```

## 履歴とストリーム

GUI は接続時に `/history` で過去ぶんを取り、続けて `/stream` に繋ぐ。
切断したら、最後に受けた点の `ts` を `since` に付けて `/history` を取り直し、
欠損を埋める。`since` は**境界を含まない**(`since=T` は `ts > T` を返す)。

保持は全体が2秒間隔で30分(900点)、unit 単位が5秒間隔で30分(360点)。
メモリ上だけに持つので、エージェントを再起動すると履歴は消える。

`/stream` は1本につきスレッドを1つ使うので、同時に4本までしか受けない。
超えた接続には `503` と `too_many_streams` を返す。

## 契約テスト

`tests/test_contract.py` が実際に API を立てて、全エンドポイントの応答を
このスキーマで検証する。C++ 版へ置き換えるときは、同じテストが Python 版と
C++ 版の両方で通ることを条件にする。

```
python tests/test_contract.py
```

GUI 側も同じ `v1.schema.json` を参照して、モックの応答を検証する。
スキーマを変えたら両方のテストを通す。
