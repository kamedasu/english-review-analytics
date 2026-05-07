# English Review Analytics

Notionに蓄積した英会話レビューを取得し、月単位で学習状況を可視化するStreamlitアプリです。

## 実装計画

Phase 1では、ローカルで動く最小構成を優先します。

1. Notionページ本文を取得する
2. Markdown構造からレビュー単位に分割する
3. `Review` / `PhraseCard` に構造化する
4. Streamlitで月次ダッシュボードを表示する

Phase 2では、以下を追加しやすい土台を入れています。

- `state.json` によるページ単位の差分取得
- `local_store.py` / `s3_store.py` による保存先の分離
- `rag/chunker.py` によるRAG向けチャンク化
- `llm_summary.py` による月次サマリー生成の差し替え口
- `reuse_detector.py` による復習・再利用判定ロジックの分離

## ディレクトリ構成

```text
.
├── app.py
├── requirements.txt
├── .env.example
├── README.md
├── src
│   ├── analytics.py
│   ├── config.py
│   ├── data_loader.py
│   ├── llm_summary.py
│   ├── models.py
│   ├── notion_client.py
│   ├── notion_parser.py
│   ├── reuse_detector.py
│   ├── streak.py
│   ├── rag
│   │   ├── chunker.py
│   │   └── vector_store.py
│   ├── storage
│   │   ├── local_store.py
│   │   ├── s3_store.py
│   │   └── state_store.py
│   └── utils
│       ├── dates.py
│       └── hashing.py
└── data
    ├── raw
    │   └── sample_reviews.md
    ├── processed
    └── state
```

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Notion APIを使う場合は `.env` に設定します。

```env
NOTION_API_KEY=secret_xxx
NOTION_PAGE_IDS=page_id_1,page_id_2
```

NotionページIDは、月別ページをカンマ区切りで指定します。未設定の場合、画面に設定不足のエラーを表示します。

## 起動

```bash
streamlit run app.py
```

サイドバーの `Refresh from Notion` を押すと、キャッシュをクリアしてNotionから再取得します。

## Notion本文の前提

月別ページ本文に、以下のようなMarkdown相当の構造が複数件並んでいる前提です。

```markdown
# 2026-05-03 review
- Date: 2026-05-03
- Duration: 15
- Topic: ...
- Good points:
  - ...
- Expressions to add:
  - ...
- Expressions to use next time:
  - ...
- Comment: ...

## Phrase Cards
### Card 1
- Phrase: ...
- Meaning: ...
- Example: ...
- Next review date: ...
- Priority: ...
```

Notion APIから取得したブロックは `src/notion_client.py` でMarkdown風テキストに変換し、`src/notion_parser.py` で構造化します。

## Notion実データ確認手順

1. `.env` に `NOTION_API_KEY` と `NOTION_PAGE_IDS` を設定する
2. `streamlit run app.py` で起動する
3. `Refresh from Notion` を押す
4. 画面上部の `Debug / Status` を確認する

`Debug / Status` では以下を確認できます。

- `Refresh from Notion` の実行有無
- Streamlit cacheの状態: `cache hit` / `cache clear + fresh fetch` / `fresh fetch`
- 読み込んだページID
- ページタイトル
- ページごとのレビュー件数
- ページごとの状態: `変更なし` / `再取得` / `エラー`
- Notionページの最終更新時刻
- 保存されたraw markdownのパス

Notionから取得した各月ページのMarkdownは `data/raw/` に保存されます。画面上でも `Debug / Status` の `Raw Markdown By Page` から確認できます。

## Parser失敗時に見る場所

まず `Debug / Status` の `Page Load Status` を見て、対象ページが `再取得` または `変更なし` になっているか確認します。`エラー` の場合はNotion APIキー、ページID、Integrationの権限を確認してください。

次に `Raw Markdown By Page` で、Notion本文が期待するMarkdown構造に変換されているか確認します。特に以下が重要です。

- レビューの先頭が `# 2026-05-03 review` の形式になっているか
- `- Date: 2026-05-03` が存在するか
- `## Phrase Cards` が存在するか
- 各カードが `### Card 1` の形式になっているか
- フィールド名が `Duration`, `Topic`, `Good points`, `Expressions to add`, `Expressions to use next time`, `Comment` と一致しているか

Parser結果は `Debug / Status` の `Parser Result` に一覧表示されます。ここで `duration_minutes` が0、`topic` が空、`phrase_cards` が0になっている場合は、`data/raw/` の該当Markdownと [src/notion_parser.py](src/notion_parser.py) の正規表現・フィールド名を見比べてください。

## 現在できること

- 月選択
- 総勉強時間
- 学習日数
- 最長連続学習日数
- 新規フレーズ数
- レビュー一覧
- フレーズ一覧
- ルールベースの月次サマリー
- ルールベースの再利用フレーズ候補検出
- ページ単位の差分取得の土台
- Debug / Statusによる取得・キャッシュ・パース結果の確認
- Notion raw markdownの `data/raw/` 保存と画面表示

## 次にやるべきこと

- Notion上の実データでブロック構造の揺れを確認し、パーサーを調整する
- `llm_summary.py` にOpenAI API実装を追加する
- `rag/chunker.py` のチャンク設計を実データに合わせて改善する
- ChromaまたはFAISSの具体実装を `vector_store.py` に追加する
- `STORAGE_MODE` に応じてlocal/S3を切り替える保存層を追加する
- Streamlit Community Cloud用にSecrets設定手順をREADMEへ追記する
