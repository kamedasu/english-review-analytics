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
- `rag/document_builder.py` による意味単位のRAG Document生成
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
OPENAI_API_KEY=
OPENAI_MODEL=gpt-6.1-terra
OPENAI_SUMMARY_RETRY_COUNT=3
OPENAI_RAG_MODEL=gpt-4.1-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
RAG_EMBEDDING_BATCH_SIZE=64
RAG_CHROMA_DIR=
RAG_COLLECTION_NAME=english_review_documents
DATA_ROOT_DIR=
```

NotionページIDは、月別ページをカンマ区切りで指定します。未設定の場合、画面に設定不足のエラーを表示します。

## データ保存先

保存先ベースディレクトリは `DATA_ROOT_DIR` で指定できます。未設定の場合は従来通りプロジェクト直下の `data/` を使います。

```env
DATA_ROOT_DIR=/Users/you/Library/Mobile Documents/com~apple~CloudDocs/english-review-analytics-data
```

指定したディレクトリ配下に `raw/`, `processed/`, `state/` を作って使います。iCloud Drive や Google Drive の同期フォルダを指定すると、ローカル保存済みデータを複数環境で共有しやすくなります。

## 保存モード

保存先は `STORAGE_MODE` で切り替えます。未指定時と `local` は従来どおり `DATA_ROOT_DIR`（未指定ならプロジェクト直下の `data/`）を使います。

```env
STORAGE_MODE=local
```

S3はオプションです。S3モードでは、ローカルの `raw/`, `processed/`, `state/` と同じ相対構造を `s3://<S3_BUCKET>/<S3_PREFIX>/` に保存します。

```env
STORAGE_MODE=s3
S3_BUCKET=english-review-analytics-510437263254
S3_PREFIX=data
AWS_REGION=ap-northeast-1
```

AWS認証情報は `.env` には書かず、AWS CLI / boto3 の通常の認証チェーンを利用します。通常表示では保存済みデータだけを読み、Notion APIは呼びません。`Sync from Notion` 実行時だけ指定ページを同期し、対象外の月はlocal/S3を問わず保存済みデータだけを利用します。localとS3でアプリ機能の差はありません。

ローカルの既存データを一度だけS3へコピーするには、以下を実行します。`sample_reviews.md` は対象外で、同じS3キーが既にある場合は上書きします。ローカルファイルは削除しません。

```bash
python scripts/migrate_local_to_s3.py --dry-run
python scripts/migrate_local_to_s3.py
```

## 同期対象の月の切り替え

同期対象は `NOTION_PAGE_IDS` のみで指定します。ページタイトルは有効な年月の `YYYY-MM` 形式（例: `2026-11`）にしてください。複数ページはカンマ区切りで指定できます。同じ月の異なるページ、不正なタイトル、APIエラーは保存前に検出し、同期全体を中止します。

```env
# 11月
NOTION_PAGE_IDS=<11月のページID>
# 12月に切り替える場合
NOTION_PAGE_IDS=<12月のページID>
# 複数月を同期する場合
NOTION_PAGE_IDS=<10月のページID>,<11月のページID>
```

月を切り替える前に、前月の最終同期を行ってください。切り替え後は指定したページだけが同期され、対象外のページにはNotion APIでアクセスしません。保存済みの過去月のraw、processed、state、レビュー単位の差分同期情報を保持し、引き続き表示・分析に利用します。未指定の場合はAPIキーの有無によらずNotion APIを呼ばず、保存済みデータのみ利用します。

通常表示はLocal / S3の保存済みデータを読み、`Sync from Notion` を押したときだけ同期します。レビューのハッシュが同じ場合は再処理しません。保存はstateを最後に更新し、保存エラー時は変更したファイルの復元を試みます。複数ファイルの更新はLocal / S3とも単一トランザクションではないため、プロセスの強制終了や復元中のストレージ障害では手動確認が必要です。同じ保存先への同時同期は避けてください。

## Notion通常同期のレート制限と再試行

通常同期のすべてのGET（ページ・子ブロック・ページネーション）は、`NotionClient._request()`を通して直列に実行します。開始間隔は最低0.5秒（最大約120リクエスト/分）です。[Notion公式の制限](https://developers.notion.com/reference/request-limits)には接続単位とワークスペース全体の制限があるため、待機を入れても429は発生し得ます。APIバージョン・レビュー識別・差分処理・保存形式は変更しません。

- HTTP 429と529のみ、失敗したGETを最大5回再試行します（初回を含め最大6回）。`public_api_request_blocked`は再試行しません。
- `Retry-After`の有効な非負整数秒を尊重し、60秒以上でも短縮しません。欠落・空・不正な値の場合は指数バックオフ（1、2、4、8、16秒、基準の上限30秒）＋0〜0.25秒のジッターを使用します。
- 1回の同期クライアント全体で、レート制限による累計待機は最大120秒です。通常の0.5秒間隔はこの予算に含めません。指定待機が残予算を超える場合は、短く待って再送せず直ちに中断します。中断時も共有クールダウンを残します。
- 同じStreamlitプロセスの全クライアントはロック・最短間隔・クールダウンを共有し、同時HTTP送信を防ぎます。別プロセス・別PC・外部ツールとの共有制御はありません。複数ワーカー運用には別途共有キュー等が必要です。同じ保存先への同時同期は引き続き避けてください。
- 接続タイムアウトは10秒、読み取りタイムアウトは30秒です。タイムアウト・接続エラー・429/529以外のHTTPエラーは自動再試行せず、安全に中断します。Sessionは接続再利用だけに使い、HTTPアダプターの自動再試行は無効です。
- すべてのページ取得に成功するまで保存を開始しません。取得失敗時はraw / processed / stateを変更せず、RAG更新もせず、保存済みレビューを表示します。不正・重複したページネーションカーソルはエラーにします。

`src.notion_client`ロガーはHTTPステータス、クライアント内累計リクエスト数、再試行番号、待機秒数、失敗理由を記録します。応答本文・URL・APIキー・認証ヘッダーは記録しません。INFOログを有効にすると通常の間隔待機も確認できます。再試行はWARNING、最終失敗はERRORです。画面の取得メッセージには、同期終了後に再試行の待機履歴と、失敗した場合の最終理由を表示します（リアルタイムのカウントダウンではありません）。

今回の検証はモック通信のみです。実環境での確認時は、1回の同期で待機・復旧の表示、追加／更新／スキップ件数、再同期による重複がないこと、RAG増分更新の結果を確認してください。

## レビューの固定IDと移行

ChatGPTが生成したMarkdownをNotion月ページに貼り付ける記録方法は変わりません。ID入力、テンプレート変更、Notionプロパティ追加は不要です。見出しブロックIDはアプリ側で自動取得します。

既存review_idは維持し、新規レビューには `notion:<見出しブロックID>` を使用します。本文ハッシュは現在版の変更検知だけに使うため、A→B→Aの修正も反映します。空見出し・識別の曖昧な項目・過去rawのみの候補は保留し、既存レビューを置き換えません。初回対応付け前に本文を編集した場合も安全のため保留することがあります。

Phase 1では本番データ移行と再同期を実行していません。初回本番同期前に [読み取り専用移行計画](docs/phase1_migration_plan.md) を確認してください。[識別仕様・移行ツール・バックアップ復元手順](docs/review_identity_migration.md) に詳細を記載しています。

## Phase 2-B 作業用コピーでの復元検証

既存133件の固定IDを維持し、74件を復元、空項目1件を除外した作業用コピーで、207レビュー・2,939分・RAG 3,471文書を確認しました。本番データには未適用です。最新Notionで追加された11月の4件は別枠で保留しています。

[検証結果・独立スクリプト・本番適用前の確認事項](docs/phase2b_rehearsal.md) を参照してください。`excluded_sources`によって除外した空項目の再登録を防ぎ、移行時の保留候補も通常同期で無条件追加しません。Notionへの記録方法は変更不要です。

## AI Learning Coach（Phase 1）

`AI Learning Coach`タブに、次の2つのセクションを追加しています。

- **Next Conversation Mission**：次回使ってみる表現を3件、意味・記録上の選定根拠・AIの学習提案・想定場面・例文・出典とともに表示します。目標は3つのうち2つ以上を自然に使うことです。
- **Review Practice**：最大10問の日本語の復習問題です。英文を入力してから「答えを見る」で模範解答の一例を表示し、自己採点できます。正解は一つとは限りません。

候補不足時は件数を減らし、候補0件ではAPIを呼ばず案内します。記録に再登場しないことだけで、実際に使えていないとは断定しません。表示する事実は学習記録に基づくもので、AIの選定理由・例文・場面は解釈や仮想の練習素材として区別します。

### 候補・優先順位・出典

`src/coach/candidates.py`が保存済みprocessedレビューを読み、Phrase Cards、More natural expressions、次回使う／追加したい表現、Weak pointsの英語・引用表現から候補を抽出します。NFKC・大小文字・連続空白・外側の引用符／句読点を正規化し、同じレビュー内の重複をまとめます。単語境界を確認して記録中の再登場を調べ、本人の使用記録は`Words and phrases actually used`と`My draft`からのみ取得します。模範例文を実使用の証拠にはしません。

複数レビューで弱点になった表現、最新レビューから30日以上登場していない表現を優先し、弱点関連件数・経過日数・関心テーマで順序を決めます。基準日は最新レビューの日付です。活用形・言い換え・意味上の同一性は推定しないため、見落としはあり得ます。英語として安全に取り出せない日本語の弱点説明は候補にしません。関心テーマは同ファイルの`THEMES`で変更でき、ヨガ・自然・ランニング・カフェ・友人・日常的な誘い方を初期設定しています。

AIには最大10候補のID・英語表現・短く制限した意味／Topic・出現統計だけを渡し、レビュー全文・raw・NotionページIDは送りません。Responses APIのStructured Outputsで、候補ID、意味、教育的な選定理由、想定場面、例文、復習問題、模範解答、使い方をJSONで受け取り、型・文字数・件数・候補IDの一意性と完全一致を検証します。英語表現・review_id・日付・TopicはAI出力を採用せずPython側の候補から結び付けます。先頭3候補をミッション、全候補を復習問題にします。生成した日本語・英語の教育的な品質は実際の利用で確認が必要です。

### 生成・保存・コスト

- `Generate Learning Plan`を押したときだけ生成します。保存後は`Regenerate`に変わり、明示的に押したときだけ新しく生成します。通常表示・再描画・タブ切り替えでCoachの生成APIは呼びません。
- 既存の`OPENAI_API_KEY`と`OPENAI_MODEL`を使用します。1操作につき1リクエスト、出力上限6,500 tokensで、自動再試行はしません。API障害・拒否・不完全応答・JSON／出典検証エラー時は保存しません。
- プランはストレージ配下の`coach/latest.json`に保存します。Localでは`DATA_ROOT_DIR`（未設定なら`data/`）配下、S3では既存bucket／prefix配下です。既存のraw／processed／state／RAGは更新しません。Localは既存の原子的置換、S3は単一オブジェクトPUTを使います。API失敗時は既存プランを維持し、保存失敗時も新規生成を自動で繰り返しません。
- 生成日時・モデル・レビュー件数・元データのSHA-256・プロンプト版・保存成功回数・今回／累計token使用量を保存します。失敗したリクエストの課金は累計に含められないため、正確な金額はOpenAIの利用状況で確認してください。画面にはセッション内の生成操作回数も表示します。
- データ追加・更新・削除を検知すると「新しい学習データがあります」と案内し、保存済みプランをそのまま表示します。再生成は自動実行しません。古い出典は生成時のスナップショットです。
- 復習の入力・解答表示・自己採点はStreamlitセッション内で保持し、タブ切り替え後も復元します。プラン再生成時は新しいIDで初期状態にし、ブラウザのセッション終了後までは保持しません。個人別プロフィール・採点履歴の永続化・AI自動採点はPhase 1に含めません。

`data/coach/`はGit管理対象外です。外部の`DATA_ROOT_DIR`を使用する場合も学習プランを公開しないでください。プランは最新版1つを保持し、過去版の履歴保存や複数ユーザーの同時再生成制御は未対応です。同じ保存先での再生成は1人ずつ行ってください。

今回の開発検証は隔離Local／モックS3／モックOpenAIのみです。実運用で試す際は、既存プランのない状態で生成ボタンを1回押し、3ミッション・最大10問・実レビューの出典・解答表示を確認してください。タブ切り替えで追加生成されないことと、再生成失敗時も既存プランが表示されることを確認します。Notionの記録方法や再同期は変更不要です。

## OpenAI API設定

月次サマリーでOpenAI APIを使う場合は `.env` に `OPENAI_API_KEY` を設定します。

```env
OPENAI_API_KEY=sk-...
```

モデル設定は既存の3変数を使います。用途別の新しい変数は追加していません。

| 機能 | 設定 | 未設定・空文字・空白のみの場合 | API |
|---|---|---|---|
| AI Learning Coach | `OPENAI_MODEL` | `gpt-4.1-mini` | Responses + Structured Outputs |
| AnalyticsのMonthly / Quarterly / Yearly要約 | `OPENAI_MODEL` | `gpt-4.1-mini` | Responses |
| Ask My English Historyの回答 | `OPENAI_RAG_MODEL` | `gpt-4.1-mini` | Responses |
| RAG文書・検索クエリのEmbedding | `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embeddings |

設定例（実際の`.env`は自分で編集してください）：

```env
OPENAI_MODEL=gpt-6.1-terra
OPENAI_RAG_MODEL=gpt-4.1-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

[OpenAI公式のGPT-6.1 Terra仕様](https://developers.openai.com/api/docs/models/gpt-6.1-terra)でResponses APIとStructured Outputsの対応を確認しています。Coachの`text.format`によるJSON Schema・`strict: true`・出典検証は維持します。実アカウントでのモデル利用権限・応答品質・コストは未検証です。

`.env.example`は明示設定の例であり、自動読み込みするデフォルト設定ファイルではありません。未指定時は`src/config.py`の互換デフォルトを使い、従来の動作を維持します。値の前後の空白は除去します。モデル名を固定の許可リストにはせず、存在しないモデル・権限不足などはAPIエラーとして安全に扱います。API応答本文や通信例外の詳細は画面・例外に出しません。Coachは保存済みプランを維持、Analyticsは既存のルールベース要約へフォールバック、RAGは生成失敗を表示します。別モデルへの自動変更は行いません。

`OPENAI_MODEL`だけを変えてもRAG回答・Embeddingは変わりません。CoachとAnalyticsを異なるモデルにする設定は現段階では追加せず、必要になった時点で検討します。生成モデルの変更でRAG再構築は不要です。Embeddingは既存の1,536次元ベクトルとの整合を保つため変更しないでください。同じ次元でも別のEmbeddingモデルは互換とは限りません。既存インデックスにモデル情報がある場合、増分更新は不一致を検出して再構築要求で停止します。モデル情報がない古いインデックスでは判定できないため、設定を維持してください。

モデル設定変更後はStreamlitを再起動してください。既存のCoachプランは古い生成モデルのまま表示し、必要な場合だけ`Regenerate`します。Analyticsの生成済み要約が残る場合はキャッシュをクリアしてください。今後の実API確認では、Coachの生成を1回、Analyticsの要約を1回、Askの質問を1回実行し、使用モデル・JSON検証・出典・エラー表示を確認します。Notion再同期やEmbeddingモデル変更・インデックス再構築は不要です。今回のテストでは実APIを呼んでいません。

一時的なOpenAI APIのserver errorに備えて、summary生成のリトライ回数を `OPENAI_SUMMARY_RETRY_COUNT` で設定できます。未設定または不正値の場合は3回です。

```env
OPENAI_SUMMARY_RETRY_COUNT=3
```

リトライ対象は `500`, `502`, `503`, `504` のみです。待機時間は `1秒 -> 2秒 -> 4秒` の指数バックオフです。`0` を指定するとリトライせず、失敗時は即fallbackします。

`OPENAI_API_KEY` が未設定、またはAPI呼び出しに失敗した場合、アプリはrule-based summaryに自動でフォールバックします。画面の `Monthly` / `Quarterly` / `Yearly` Summary に `Generated by OpenAI API` または `Generated by rule-based fallback` と表示されます。5xxでfallbackした場合は、最後のステータスコードとリトライ回数もwarningに表示します。

Summaryでは、単なる総評ではなく次回の会話で使える学習材料を出すことを優先しています。特に月次サマリーでは以下を表示します。

- 今月の成長ポイント: `Good points` の頻出傾向と、`Phrase Cards` のうち定着してきた phrase を要約
- 今月の弱点: `Weak points` の頻出傾向、`More natural expressions` の修正傾向、confirmed の繰り返し迷いポイントを要約
- 今月の重点ミス修正 Top 5: `Weak points` / `More natural expressions` / confirmed phrase の繰り返しから、今月よく出たミスの種類を要約
- 次に増やすべき表現: `Phrase Cards` から未定着で伸びしろの大きい表現を優先して抽出
- 次回会話で実行するミッション
- まだ使えていない表現 Top 5
- 日本語→英語、穴埋め、言い換えの1分復習ドリル。各問題に `Answer` を付与し、可能なら `Note` も付与

サマリー生成には `phrase_cards` / `expressions_to_add` / `expressions_to_use_next_time` / `words_and_phrases_actually_used` / `good_points` / `weak_points` / `more_natural_expressions` を統合した learning context を使います。少なくとも以下を LLM に渡します。

- `good_points` の頻度
- `weak_points` の頻度
- `more_natural_expressions` の修正傾向
- `phrase_cards` ごとの `phrase` / `source` / `priority` / `review_status`
- phrase ごとの出現回数、source 集計、review_status 集計

この集約により、`confirmed で何度も出てくる`、`recommended だがまだ未定着`、`Retained / Strong に寄ってきた` といった判定に加えて、`前置詞の選び分け`、`語順の不安定さ`、`fixed phrase の崩れ` などの重点ミス修正も summary に反映します。

LLMにはレビュー全文をそのまま渡さず、summary生成用に要約済みのコンテキストを渡します。Monthlyはやや詳しめ、Quarterly / Yearlyは簡潔めに生成しますが、どの集計単位でも上記の成長・弱点・未定着表現の考え方を共通で使います。

## RAG index（Phase 2）

保存済みReviewから、Good Point / Weak Point / More Natural Expression / Phrase CardをChromaへ全件再構築できます。Notion同期やStreamlit UIからの自動実行は行いません。

```bash
python -m src.rag.indexer
```

保存済みReviewに対する差分更新は、次のように明示実行します。

```bash
python -m src.rag.indexer --incremental
```

差分更新はadded / changedのDocumentだけをEmbeddingし、deletedはChromaから削除します。unchanged Documentは再Embeddingしません。indexがない場合は初回full buildを行います。embedding modelまたはDocument schemaがindex metadataと異なる場合は、安全のためfull rebuildを実行してください。

Chromaはローカルの `RAG_CHROMA_DIR`（未指定時は `data/chroma`）へ保存される派生データで、Git管理対象外です。デフォルトcollection名は `english_review_documents` です。

既存indexを意味検索するには、以下を実行します。検索時はquery 1件だけをEmbeddingし、indexの再構築やNotion同期は行いません。

```bash
python -m src.rag.retriever "前置詞でどんなミスをしてる？"
```

Retriever結果を根拠にした回答を生成するには、以下を実行します。回答生成は既存indexを読むだけで、Notion同期やindex再構築は行いません。

```bash
python -m src.rag.answerer "過去にも前置詞で同じようなミスしてた？"
```

Streamlitでは **Ask My English History** tabから質問できます。事前にindexを作成し、アプリを起動してください。質問の入力や画面の再描画ではAPIを呼ばず、**Ask** を押した時だけ検索Embeddingと回答生成を実行します。

```bash
python -m src.rag.indexer
streamlit run app.py
```

## 起動

```bash
streamlit run app.py
```

通常表示ではローカル保存済みの `processed/reviews.json` と `state/state.json` だけを使い、Notion APIは呼びません。

サイドバーの `Sync from Notion` を押したときだけ、指定ページをNotion APIから取得します。同期時は月ページの見出しブロックIDでreviewを識別し、現在のprocessedにある本文ハッシュと比較します。同日・同種でも別見出しは個別に管理し、既存ブロックの編集では固定review_idを維持します。同期結果には追加・更新・スキップ・保留件数と同期月が表示されます。

SyncとReview保存が成功した後は、保存済みReviewをsource of truthとしてIncremental RAG Index Updateも自動実行します。差分がなければEmbeddingは呼ばれません。RAG更新が失敗しても保存済みReviewはrollbackせず安全に維持され、embedding model／schemaの不一致時にも自動full rebuildは行いません。Ask My English Historyの表示・質問や通常の画面rerunではindexを更新しません。

## 集計単位の切り替え

画面上部の `Aggregation` で集計単位を切り替えられます。

- `Monthly`: 月単位で集計し、月次サマリーを表示します
- `Quarterly`: 四半期単位で集計し、四半期サマリーを表示します。例: `2026-Q2`
- `Yearly`: 年単位で集計し、年次サマリーを表示します。例: `2026`

どの集計単位でも、以下の指標を表示します。

- 総勉強時間
- 学習日数
- 最長連続学習日数
- review数
- phrase数
- reused phrase数

LLMサマリーは `Monthly` / `Quarterly` / `Yearly` に対応しています。`OPENAI_API_KEY` が未設定の場合は、どの集計単位でもrule-based summaryにフォールバックします。

## 画面構成

通常UIは学習者向けに、以下の順で表示します。

1. Aggregation切り替え
2. KPIカード
3. Summary
4. Improvement Focus

`Review & Retention Analysis` / `Reviews` / `Line Reviews` は通常UIから外しています。内部ロジックは残っていても、通常画面では summary と改善ポイントに集中できる構成です。raw review本文やraw match結果も通常UIには表示しません。詳細を確認したい場合はNotion側のレビュー本文を確認します。

## Line English Review

通常の `# YYYY-MM-DD review` に加えて、2026-06以降は `# YYYY-MM-DD line english review` 形式のレビューも取り込めます。

line reviewは通常レビューと同じ `Review` として保存し、`review_type: "line"` で識別します。通常レビューや旧レビューは `review_type: "normal"` として扱います。line reviewでは以下の項目を追加で保持できます。

- `situation`
- `my_draft`
- `more_natural_version`
- `why_it_was_corrected`

これらの項目が存在しない通常レビューや旧レビューでは、空配列または `None` として安全に扱います。line reviewの `Phrase Cards` では `Next review date` がない場合も許容し、未記録として処理します。

line reviewは通常UIの個別テーブルには出しませんが、Monthly / Quarterly / Yearly のKPI、Summary、Improvement Focus には通常レビューと合算して反映します。line review内の `expressions_to_add`、`expressions_to_use_next_time`、`phrase_cards`、`words_and_phrases_actually_used` も既存の復習・再利用判定に含まれます。

## Improvement Focus

新しいレビュー構造では `Weak points` と `More natural expressions` を扱えます。

`Weak Points` では、期間内レビューの弱点を重複に近い内容ごとにまとめて表示します。

`More Natural Expressions` では、以下を表形式で表示します。

- `Your phrase`
- `More natural`
- `Note`

古いレビューには `Weak points` や `More natural expressions` が存在しない場合があります。これは正常な状態として扱い、該当項目がない期間では「まだ記録はありません」というメッセージを表示します。

## Review & Retention Analysis

`Review & Retention Analysis` は、復習対象の表現がその後どれだけ再登場したかを見る内部集約ロジックです。通常UIには表示しませんが、summary生成と定着判定の基盤として使います。

復習対象のベース集合として以下を扱います。

- `phrase_cards[].phrase`
- `expressions_to_add`
- `expressions_to_use_next_time`

これらの表現が後続レビューの以下に再登場した場合、復習・再利用できた候補としてカウントします。

- `words_and_phrases_actually_used`
- `phrase_cards[].phrase`
- `expressions_to_add`
- `expressions_to_use_next_time`

`words_and_phrases_actually_used` は、今回の会話でユーザー本人が実際に使った単語・フレーズだけを入れる想定です。旧レビューには存在しない場合がありますが、欠損時は空配列として安全に扱います。

統合テーブルでは以下を確認できます。

- `phrase`
- `first_seen_date`
- `last_seen_date`
- `highest_priority`
- `meanings`
- `examples`
- `reused_count`
- `review_status`: `New` / `Reused` / `Retained` / `Strong` の簡易定着度

`review_status` の意味は以下です。

- `New`: まだ後続レビューで再利用されていない
- `Reused`: 後続レビューで1回再利用された
- `Retained`: 後続レビューで複数回再利用され、定着し始めている
- `Strong`: 繰り返し再利用され、かなり定着している

単独の `Words and Phrases Actually Used` / `Reused Phrase Candidates` / `Phrase Cards` セクションは通常UIから外し、この統合テーブルに役割を集約しています。詳細な元データを確認したい場合は、Notion側のレビュー本文を確認します。

`Duration` はレビュー本文の `Duration:` に入っている数字をそのまま使います。iPhoneショートカット等から渡した分数を保存・表示・集計する前提で、アプリ側では独自に再計算しません。不正値や空欄だけ安全に0分として扱います。

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
- Source: confirmed / recommended
- Review status: New / Reused / Retained / Strong
```

2026-06以降のline reviewは以下のようなヘッダーも認識します。

```markdown
# 2026-06-01 line english review
- Date: 2026-06-01
- Duration: 10
- Topic: ...
- Situation: ...
- Expressions to add:
  - ...

## My draft
- ...

## More natural version
- ...

## Why it was corrected
- ...
```

Notion APIから取得したブロックは `src/notion_client.py` でMarkdown風テキストに変換し、`src/notion_parser.py` で構造化します。

## Notion実データ確認手順

1. `.env` に `NOTION_API_KEY` と `NOTION_PAGE_IDS` を設定する
2. `streamlit run app.py` で起動する
3. `Sync from Notion` を押す
4. 画面上部に警告が出ていないことを確認する

取得に失敗した場合は、サイドバーのメッセージと `data/raw/` を確認します。

- Notion APIキーが正しいか
- `NOTION_PAGE_IDS` が正しいか
- Notion Integrationに対象ページの権限があるか
- `data/raw/` に保存されたMarkdownが期待する構造になっているか

Notionから取得した各月ページのMarkdownは `data/raw/` に保存されます。

## Parser失敗時に見る場所

まずサイドバーの取得メッセージを見て、対象ページが取得できているか確認します。`エラー` が出る場合はNotion APIキー、ページID、Integrationの権限を確認してください。

次に `data/raw/` で、Notion本文が期待するMarkdown構造に変換されているか確認します。特に以下が重要です。

- レビューの先頭が `# 2026-05-03 review` の形式になっているか
- line reviewの場合は `# 2026-06-01 line english review` の形式になっているか
- `- Date: 2026-05-03` が存在するか
- `## Phrase Cards` が存在するか
- 各カードが `### Card 1` の形式になっているか
- フィールド名が `Duration`, `Topic`, `Good points`, `Expressions to add`, `Expressions to use next time`, `Comment` と一致しているか

`duration_minutes` が0、`topic` が空、`phrase_cards` が0になる場合は、`data/raw/` の該当Markdownと [src/notion_parser.py](src/notion_parser.py) の正規表現・フィールド名を見比べてください。

## 現在できること

- Monthly / Quarterly / Yearly の集計切り替え
- 総勉強時間
- 学習日数
- 最長連続学習日数
- 新規フレーズ数
- reused phrase数
- 学習者向けSummary表示
- Improvement Focusによる weak points / more natural expressions の確認
- actually used を含む再利用判定
- ルールベースの月次 / 四半期 / 年次サマリー
- OpenAI APIによるMonthly / Quarterly / Yearlyサマリー
- ルールベースの再利用フレーズ候補検出
- ページ単位の差分取得の土台
- Notion raw markdownの `data/raw/` 保存

## 次にやるべきこと

- Notion上の実データでブロック構造の揺れを確認し、パーサーを調整する
- `llm_summary.py` にOpenAI API実装を追加する
- `rag/chunker.py` のチャンク設計を実データに合わせて改善する
- ChromaまたはFAISSの具体実装を `vector_store.py` に追加する
- `STORAGE_MODE` に応じてlocal/S3を切り替える保存層を追加する
- Streamlit Community Cloud用にSecrets設定手順をREADMEへ追記する
