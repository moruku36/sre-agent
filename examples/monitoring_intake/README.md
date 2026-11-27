# Offline Cloud Monitoring snapshot intake / オフライン指標取込

**ローカル JSON だけを検証する CLI。** [intake.py](intake.py) はネットワークを使わず、入力を書き換えず、判定 JSON 1 件を標準出力に返す。対象は Cloud Monitoring のモデル呼び出し件数 `aiplatform.googleapis.com/publisher/online_serving/model_invocation_count`（`PublisherModel`、`DELTA / INT64 / 1`）のみ。REST のフィールド名の一部を使うが、v1 は独自のローカル形式であり、REST の生レスポンスや汎用 Console エクスポートを直接は読まない。[公開仕様と設計根拠](PUBLIC-DOCS.md)。

## 入力の条件

- 根に `schema: "sre-monitoring-intake/v1"`、`provenance: "synthetic"` または `"operator_supplied"`、`scope`、`read_state` が必要。 `scope` は正確な `resource_container`、`model_user_id`、`interval.startTime/endTime`。CLI に別途渡す期待値と照合。プロジェクト ID と番号の別名を推定しない。
- `read_state: "ok"` は、未整列・未集約の `FULL` な最初の単一完結ページを独立に確認できた場合のみ。 `capture` の `view: "FULL"`、`aggregation: "none"` と、`page_token_present`、`next_page_token_present`、`execution_errors_present`、`unreachable_present` の 4 つの偽値を明記する。 `response.timeSeries` に系列を落とさず収録する。入力に生トークン、エラー本文、未到達一覧を含めない。 `unit: "1"` は応答側または各系列に必要。複数ページや一部エラーを除いて `ok` とすることは不可。
- 系列はプロジェクト・モデルと、`publisher=google`・**メトリクスラベル** `source=global` が完全一致するものだけ。リソースとメトリクスの全ラベルを系列キーにし、`response_code` は数値や HTTP 成否に変換しない。ラベルを削除・改名・正規化しない。任意ラベルの欠落と空文字は別扱いなので、変換で片方をもう片方に置き換えない。
- 各系列ごとに指定窓 `(start,end]` 全体をナノ秒まで過不足なく覆う必要がある。時刻は UTC `Z`、小数は 0・3・6・9 桁。境界をまたぐ点の切り分けも、欠測のゼロ埋めもしない。再出現した系列、重複点、後続の不正値などはバンドル全体を拒否。点の `value.int64Value` と系列内の合計は非負の十進文字列。異なる系列は合算しない。
- 明示的拒否なら `read_state: "denied"` とし、`capture` / `response` を付けない。取得ができない・検証できない場合は `read_state: "unavailable"` と有限の `reason`（`console_display_only`、`scope_unverified`、`capture_unverified`、`incomplete_capture`、`source_unavailable`）だけを追加する。

## 合成での使い方と判定

このディレクトリで実行。以下の ID・時刻・値はすべて架空。

```sh
python3 -IB intake.py --snapshot fixtures/synthetic-positive.json \
  --resource-container projects/orchard-demo-invalid --model orchard-counter-v1 \
  --start 2031-04-02T00:00:00Z --end 2031-04-02T00:00:10Z
```

| フィクスチャ | `status / reason` | 系列内件数 | CLI exit |
| --- | --- | --- | --- |
| 合成の正値 | `observed / complete_series` | `"7"` | 0 |
| 合成の明示ゼロ | `observed / complete_series` | `"0"` | 0 |
| 確認済み空配列 | `unknown / no_series` | なし | 0 |
| 明示的拒否 | `unknown / denied` | なし | 0 |
| 表示のみ・点なし | `unknown / console_display_only` | なし | 0 |
| 系列内の 1 ns 欠落 | `rejected / incomplete_window` | なし | 2 |

呼び出し側は **終了 0 だけで採用せず JSON の `status` を読む**。 `observed` のときだけ `rows` に系列が入り、系列ごとのラベル、時系列点、`window_total_int64`、`observation`（`positive_count` / `explicit_zero`）を返す。どの状態も `attribution_to_relay: "not_established"`、`availability: "unknown"`。 `snapshot_sha256` は取り込んだバイトの識別子にすぎない。

Run from this directory with a local Python 3 interpreter: `python3 -EB test_intake.py`. Use only the synthetic fixtures for public examples. Real snapshots can contain private resource labels; keep the inputs and CLI output local. A structurally valid snapshot is not proof of provenance, capture completeness or application availability.
