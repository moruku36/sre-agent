# Local synthetic handoff delivery and ACK / ローカル合成ハンドオフと ACK

This example models the pull/ACK part of [architecture §4](architecture.md#4-openclawを運用ハーネスにする) for one local consumer. It wraps the [existing v1 synthetic handoff](synthetic-signals.md#local-synthetic-handoff-for-openclaw) without changing how the health and cost snapshot is built. Nothing is sent to OpenClaw or another service.

[設計 §4](architecture.md#4-openclawを運用ハーネスにする) の取得と ACK だけを、合成データと単一のローカル受信者で試せます。[既存の v1 ハンドオフ](synthetic-signals.ja.md#openclaw向けのローカル模擬ハンドオフ) と同じ障害・料金スナップショットを使い、OpenClaw などには送信しません。

Run from `examples/synthetic_signals` / `examples/synthetic_signals` で実行:

```sh
demo_dir="$(mktemp -d)"
signals="$demo_dir/signals.sqlite3"
ledger="$demo_dir/delivery.sqlite3"
python3 -B sre_mvp.py --db "$signals" demo
python3 -B sre_handoff_delivery.py --db "$signals" --ledger "$ledger" offer \
  --policy cost_policy.json --as-of 2026-01-15T12:21:00Z
python3 -B sre_handoff_delivery.py --db "$signals" --ledger "$ledger" pull > "$demo_dir/pulled.json"
delivery_id="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["delivery_id"])' < "$demo_dir/pulled.json")"
python3 -B sre_handoff_delivery.py --db "$signals" --ledger "$ledger" ack "$delivery_id"
python3 -B sre_handoff_delivery.py --db "$signals" --ledger "$ledger" pull
```

- `offer` records one valid snapshot before returning it. `handoff_json` is the exact legacy JSON as text (without its trailing newline); parse that field as JSON to read the snapshot, using a decimal-capable parser for precise cost numbers. `delivery_id` is the SHA-256 hex digest of that text's UTF-8 bytes. Offering the same text returns the existing ID and ACK state.
- `pull` returns the oldest unacknowledged snapshot, or JSON `null` if none remain. It leaves the snapshot pending, so a restarted process can pull the same stored bytes even if the source has changed. Keep the ledger to preserve receipts across restarts.
- `ack` confirms receipt only for the exact ID provided. A retry returns the same receipt; a different snapshot, even from the same cutoff, remains pending. An invalid or unknown ID is an error with no success JSON. ACK does not mean approval, investigation, remediation, or resolution.

同じ内容を `offer` しても ID は重複せず、ACK 済みならその状態を保持します。`handoff_json` は末尾の改行を除いた従来の JSON 文字列で、料金の数値表記をそのまま保持します。ID はその UTF-8 バイト列の SHA-256 です。`pull` は未 ACK の最古の保存済みスナップショットを返し、なければ `null` を返します。再起動しても ACK までは同じ内容を受け取れます。`ack` は指定した ID の受領記録だけを保存し、不明・不正な ID はエラーになります。承認・調査・対応・解決は意味しません。

Use separate files in a trusted local directory, and keep using the same signal path and ledger across retries. The ledger is not an authenticated interface or proof of external receipt. / 専用のローカル台帳ファイルを使い、再試行時も同じシグナル DB のパスと台帳を指定してください。認証付きの接続口や外部への到達証明ではありません。

Keep the `offer` policy separate from both SQLite databases and their `-journal`, `-wal`, and `-shm` files, including aliases to those files. SQLite can still manage its own sidecars. / `offer` のポリシーは、両方の SQLite DB とそれぞれの補助ファイル（`-journal`、`-wal`、`-shm`）から分けてください。同じファイルを指す別名も使えません。SQLite 自身による補助ファイルの利用は妨げません。
