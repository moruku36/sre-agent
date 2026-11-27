# 公開仕様と、この取込形式の境界

公開仕様に基づく独自のローカル取込形式。Cloud Monitoring API には接続しない。

| 公開仕様 | 取込で守ること |
| --- | --- |
| [Agent Platform の metric 一覧](https://docs.cloud.google.com/monitoring/api/metrics_gcp_a_b#aiplatform) — `aiplatform.googleapis.com/publisher/online_serving/model_invocation_count` は BETA、project、`DELTA / INT64 / 1`、model invocations (prediction requests)。メトリクスラベル `source` は global endpoint なら `global`、他の surface や `batch_` 接頭辞もありうる。 | 件数のメトリクスだけ。`source` はメトリクスラベルとして大小文字を含め完全一致。 `response_code` は prediction request のコードであり、ここでは符号体系を仮定しない。 |
| [PublisherModel の resource descriptor](https://docs.cloud.google.com/monitoring/api/resources#tag_aiplatform.googleapis.com/PublisherModel) — `resource_container` は Endpoint 所有プロジェクトの識別子。リソースラベルに `location`、`publisher`、`model_user_id`、`model_version_id`。 | 読み取りスコープのプロジェクト名と系列の所有プロジェクトは別に確認する。ID/番号を推定変換しない。リソースの `location` を `source` とみなさない。 |
| [TimeSeries](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/TimeSeries) — “A time series is identified by a combination of a fully-specified monitored resource and a fully-specified metric.” DELTA の点は非ゼロ区間で、続く点は contiguous and non-overlapping。読取結果の kind/type は alignment/reduction で変わりうる。 | resource と metric の全ラベルをキーにする。同ラベルの点だけを加算し、別系列を加算しない。kind/type だけでは未加工とみなさない。 |
| [TimeInterval](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.snoozes#TimeInterval) — “Reads: A half-open time interval. It includes the end time but excludes the start time: `(startTime, endTime]`.” RFC 3339、生成時は Z 正規化し小数桁は 0/3/6/9。 | UTC Z に限定したプロファイル。ナノ秒単位で比較。同じ境界で隣接する点は連続、窓をまたぐ DELTA を切り分けたり補間したりしない。 |
| [TypedValue](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/TypedValue) — 相互排他的な値フィールド。 `int64Value` の REST 表記は string。 | 文字列を整数で検証。出力も十進文字列。浮動小数点に変換しない。非負かつ signed INT64 上限以内に限定。 |
| [projects.timeSeries.list](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list) — aggregation を指定しなければ raw、`FULL` は点を含む。`pageToken` は追加結果、点の列挙順は最近順。 [ListTimeSeriesResponse](https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/ListTimeSeriesResponse) — `nextPageToken` は未取得結果、`executionErrors` / `unreachable` は不完全な戻り値となりうる。 | 生の最初の単一完結ページだけ許容。トークンやエラー本文を貼らず、存在の真偽だけ転記。REST 形式のフィールド名の一部を借りた LOCAL v1 であり、REST のレスポンスそのものや Console の汎用エクスポート形式ではない。 |

全期間が各系列の点で隙間なく覆われること、合計も INT64 上限内であること、単一ページに限定することは、**このツールの保守的な受入条件**。ベンダーが全ラベルや全期間の点を出す保証ではない。空配列はスコープ内で「当該取得結果に系列がない」であって、適用サービスにトラフィックがないという意味ではない。
