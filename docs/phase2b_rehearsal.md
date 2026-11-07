# Phase 2-B: 作業用コピーでの移行・復元検証

2026-11-07実施。本番適用は実施していない。ChatGPT生成MarkdownをNotion月ページへ貼り付ける運用は変更不要。

## 保存先と保護対象

- 本番: `data/`（Local）
- 既存バックアップ: `data_backup_20261103_154147/`
- 新規バックアップ: `data_backup_phase2b_20261107_112923/`
- 作業用コピー: `data_migration_work_20261107_112923/`

コピー時の元データは18ファイル・31,073,583バイト。新旧バックアップおよび本番のファイル単位SHA-256が一致し、SQLite quick_checkも正常だった。バックアップと作業用コピーはGit管理対象外。

作業前にプロセス名・PID、8501番ポート、保存ファイルのオープン状態を確認した。本番アプリ/同期処理による使用は検出されず、残っていたPythonは前回のメモリ解析用プロセスだった。11月3日スナップショットを作業用コピーへ一度だけ書き出し、その後は独立スクリプトで処理した。

## 最新Notionとの差分

既存stateで確定した2026-04〜11の8ページのみ、GETで取得。1,254リクエストすべて成功し、212見出しを取得した。カーソルページネーション・子ブロックを走査し、取得前後のページ最終編集日時一致と、見出しID・Markdown境界・ハッシュを検証した。

11月3日の208見出しはすべてブロックID・本文ハッシュが一致。編集0・削除0。追加4件はすべて11月で、11月3日10分、5日5分、6日10分、7日10分、計35分。有効な追加候補だが、確認済み74件とは分離し、`new_since_baseline_requires_review`として保留した。追加4件のRAG文書は100件相当であり、今回の索引には含めない。

## 適用結果（作業用コピーのみ）

- 既存有効133件: review_id・解析済み内容・内部参照を維持し、source_block_idを対応付け。
- 確認済み74候補: 最新スナップショットでも一意かつ有効。`notion:<source_block_id>`で復元・追加。
- 10月25日の空項目1件: processedから除外し、元レコードと理由を`audit/excluded_reviews.json`に保持。
- 空項目のブロックIDはstate.excluded_sourcesへ登録。通常同期でも再登録しない。
- 有効な15分レビューは別の固定IDで復元し、空項目のIDを流用しない。
- 最新4件: state.pending_sourcesに保留。本文を変更しても確認なしに登録しない。
- 最新rawは212見出しすべてを保持。空見出しも削除しない。移行前rawは`audit/original_raw/`にも保持。
- 旧state.reviewsは履歴として保持。新しいcurrent_reviews・source_bindingsは207件。

| 月 | processed移行前 | 移行後 | 移行後時間（分） |
|---|---:|---:|---:|
| 2026-04 | 12 | 12 | 130 |
| 2026-05 | 33 | 45 | 940 |
| 2026-06 | 19 | 46 | 536 |
| 2026-07 | 17 | 32 | 437 |
| 2026-08 | 22 | 32 | 361 |
| 2026-09 | 15 | 19 | 220 |
| 2026-10 | 14 | 19 | 295 |
| 2026-11 | 2 | 2 | 20 |
| 合計 | 134 | 207 | 2,939 |

本番は134件・1,975分のまま。新規4件を後日すべて採用する場合の参考値は211件・2,974分・RAG 3,571文書であり、今回の実測値ではない。

## RAG検証

作業用Chromaのみ増分更新。既存2,177文書を維持し、1,294文書を実際の設定モデルでEmbeddingして追加。変更0・削除0、実測3,471文書で期待値と一致した。

全ID・本文・メタデータの差分0。ベクトル形状は3,471×1,536、すべて有限値。保存済みベクトルによる検索3件と、実際のクエリEmbeddingを使ったRetriever検索3件が成功。SQLite quick_checkはok。再更新時の追加・変更・削除・Embeddingは0。

## 再実行と分析・同期検証

移行を繰り返しても追加0・追加除外0。raw・processed・state・Chromaの全ファイルハッシュが同一となることを確認した。通常表示は207件を読み、NotionClientを生成しない。分析結果は207件・2,939分。

最新スナップショットを用いたネットワークなしの通常差分同期試算は、追加0・更新0・スキップ207件・保留5件（最新4件＋除外済み空項目）。保存したprocessed・stateは整合している。

## 独立スクリプト

すべてプロジェクトルートから実行する。`.env`は変更しない。作業用ディレクトリは実在する絶対パスを明示する。

```sh
# GETだけで最新スナップショットを収集。既存notion_latest.jsonは上書きしない。
.venv/bin/python -B scripts/fetch_review_snapshot.py --work-root /absolute/path/data_migration_work_TIMESTAMP
# 既定は試算。作業用コピーへ適用するときだけ --apply を付ける。
.venv/bin/python -B scripts/rehearse_review_migration.py --work-root /absolute/path/data_migration_work_TIMESTAMP --apply
# 作業用RAGの増分更新と検索検証。新規文書・検索クエリはOpenAI APIを利用する。
.venv/bin/python -B scripts/verify_migration_rag.py --work-root /absolute/path/data_migration_work_TIMESTAMP
# ネットワークなしで再実行・分析・通常表示・同期試算を検証。
.venv/bin/python -B scripts/verify_migration_data.py --work-root /absolute/path/data_migration_work_TIMESTAMP
```

各書き込み前に`data_migration_work_*`名、`.migration_workspace.json`、実パス、保護対象との非重複、シンボリックリンク不在を検証する。保護対象のハッシュが変わった場合は中断する。S3は使用しない。Notion取得失敗時は古いスナップショットへフォールバックしない。

今回の作業用ディレクトリには、比較元`notion_20261103.json`、最新`notion_latest.json`、最初の適用結果`reports/migration_first.json`、再実行検証`data_verification.json`、RAG検証`rag_result.json`を保持している。個人の本文を含むスナップショットと除外台帳はGitへ追加しない。

## 本番適用前に必要な確認

1. 本番適用の別途指示を受け、最新4件を今回含めるか保留するか決める。
2. アプリ・同期処理を止め、最新バックアップとNotion GETで前提を再検証する。
3. 確認済み対応表・候補・除外台帳・期待値を固定する。変更や曖昧な対応は保留する。
4. 本番用の適用手順と切替・復元手順を別途レビューする。今回の作業用ガードを解除して本番へ流用しない。
5. processed/state/rawを検証してからRAGを増分更新し、通常表示・再同期・再実行を確認する。

複数ファイルの保存は単一トランザクションではなく、停止や継続的なストレージ障害に限界がある。作業用コピーでは保存失敗時の復元をテスト済み。本番適用時は停止したアプリと一式のバックアップ・切替手順が必要。今回、本番用の適用モードは実行していない。
