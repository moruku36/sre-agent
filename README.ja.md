# SRE Agent

[English](README.md) · [日本語](README.ja.md)

クラウド障害と料金の急増を OpenClaw でどう切り分けるか試すローカル試作です。設計上の対象は AWS、Azure、Google Cloud、Oracle Cloud Infrastructure（OCI）、さくらのクラウドです。

**現時点の範囲：** デモは模擬データを使います。別の Cloud Monitoring サンプルは、モデル呼び出し件数という1指標のローカル JSON を検証します。指標を取得したり、サービスの稼働を証明したりするものではありません。ライブ通知、OpenClaw やモデル API の呼び出し、自動復旧は行いません。OCI の実環境との連携は未検証です。欠測は正常や料金ゼロを意味しません。

## まず試す

SQLite が使える Python 3.10 以降で、リポジトリのルートから実行します。追加パッケージや認証情報は不要です。

```sh
cd examples/synthetic_signals
demo_dir="$(mktemp -d)"
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" demo
python3 -B sre_mvp.py --db "$demo_dir/signals.sqlite3" incidents
```

模擬インシデント5件が解決済みとして表示されます。実際のサービスの稼働状況を示すものではありません。

## 詳細

- [模擬デモの詳細](docs/synthetic-signals.ja.md) — テスト、料金判定、インシデント参照、模擬調査。
- [ローカルのハンドオフと ACK](docs/synthetic-handoff-delivery.md) — スナップショットの取得と受領記録を試す。
- [オフライン指標取込](examples/monitoring_intake/README.md) — 対応する JSON 形式と検証の限界。
- [全体設計と導入計画](docs/architecture.md) — 接続案と承認ルール。
