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

## AI Learning Coach：Review Practice

`AI Learning Coach`タブは**Review Practiceだけ**の学習画面です。最大10問を縦に並べ、問題ごとのカードで練習します。基本表示は練習表現・日本語のシチュエーション・「例文を見る」で表示する英文例だけです。英文入力・AI採点を利用でき、他タブへ切り替えてもセッション内の状態を復元します。問題選択・自己評価のプルダウンはありません。例文は唯一の正解ではありません。

上部に練習レベルと生成／再生成ボタン、表示中のレベル・問題数を表示します。出典、review_id、最終登場日、根拠の件数、選定理由、モデル・token数・データハッシュ・生成方式は学習画面に出しません。これらの情報の内部検証・記録は維持します。

### 練習レベル（CEFR B1〜B2は共通）

| レベル | 練習内容 |
|---|---|
| 基本（Basic） | 中級表現を一つ正確に使い、短く自然な英文を組み立てる |
| 応用（Applied、既定） | 表現や文法を組み合わせ、時制・品詞・前置詞・ニュアンスを使い分ける |
| 実践（Real Conversation） | 感想・理由・質問・自然な誘い方を組み合わせ、相手との会話を続ける |

基本は初級単語へ切り替える設定ではありません。練習レベルの変更だけで生成せず、ボタンを押したときの選択で生成します。表示中の保存プランと選択中のレベルが違う場合は案内します。設定は`src/coach/profile.py`に集約しています。

### 問題の配分と候補選定

Python側で重複しない候補を選び、弱点克服3問・表現力アップ4問・実践ミッション3問を目安に配分します。弱点克服には具体的な修正・使い分けの根拠が必要です。表現力アップと実践には、中級者向けの句・コロケーションなどの応用価値と日常テーマを重視します。候補不足時はカテゴリ配分・総数を柔軟に減らし、同じ候補や無関係な基本語で10問を水増ししません。

processedのPhrase Cards、More natural expressionsとNote、追加／次回使いたい表現、Weak pointsから候補を抽出します。NFKC・大小文字・空白・外側の句読点を正規化し、出現回数・最後の登場・修正前後の差分・指摘内容を整理します。文中で変更されていない単語を誤りの対象にしません。弱点欄への単なる登場回数や日数だけで基本表現を優先しません。`I usually`や`I just woke up`のような単純な習慣・行動表現は、語数を増やしただけでも通常の表現力アップ対象にはしません。具体的な課題がある場合は弱点克服で採用できます。

これは履歴・語彙例・構造を組み合わせた保守的なルールで、完全なCEFR判定ではありません。過去記録がないことから忘却・習得・使用可否を断定しません。`refresh / refreshed / refreshing`などの関連形や、単純に意味が重なる候補は、実際の記録にあるものだけをまとめ、個々の出典を保持します。興味のあるテーマは`src/coach/candidates.py`の`THEMES`で変更できます。

### AI生成・検証・保存

- `Generate Learning Plan`／`Regenerate`を押したときだけ生成し、通常表示・レベル変更・タブ切り替え・英文入力ではCoachのAPIを呼びません。採点は各問題の「AIで採点する」を押した場合だけ実行します。
- 既存の`OPENAI_API_KEY`と`OPENAI_MODEL`を使用します。1操作1リクエスト、出力上限6,500 tokensで、自動再試行はありません。
- AIには最大10候補と割り当てカテゴリ、選択した練習レベル、限定した統計・Topic・修正抜粋を送ります。レビュー全文・raw・NotionページIDは送りません。
- 新しいStructured Outputsは`candidate_id`、`category`、`situation_ja`、`example_en`、`internal_reason_ja`、`support_suggestions`です。表現や出典はAIに自由生成させず、候補IDとカテゴリの完全一致、型・件数・review_id／日付／Topicを検証します。
- 補助表現や文法を新しく提案することは許可します。`support_suggestions`と`support_origin=ai_suggestion`に保存し、過去の学習実績として扱いません。自然な会話、場面に合う文法・語法、複数表現の組み合わせを重視し、語数の下限・15〜25語の強制は設けません。
- Localは`DATA_ROOT_DIR`（既定`data/`）配下、S3は既存bucket／prefix配下の`coach/latest.json`に保存します。raw／processed／state／RAGは更新しません。検証済みの最新プラン1つだけを保存し、生成失敗・不正応答では既存プランを保持します。
- 元データのハッシュ、学習者レベル、練習レベル、カテゴリ、出典・根拠、モデル、生成日時・回数、token使用量を内部保存します。成功分の累計使用量であり、失敗時の課金まで含む金額はOpenAIの利用状況で確認してください。
- 新規プランはschema_version `2`、選定・プロンプト版`3`です。旧schema `1`や旧形式の生成データも読み込み、旧ミッション表示は行いません。旧プランの練習レベルは未指定として表示し、再生成を案内するだけで、明示操作なしに削除・上書きしません。
- 入力・例文表示・AI採点結果はセッション内に保持します。再生成時は新しいプランIDで初期化します。学習履歴の永続化、プラン履歴保存、同一保存先の同時再生成制御は未対応です。

`data/coach/`はGit管理対象外です。外部の`DATA_ROOT_DIR`でも学習プランを公開しないでください。今回の動作確認は隔離Local・モックS3・モックOpenAIのみです。実生成を試す場合は、応用を選んでRegenerateを1回実行し、カテゴリのバランス、問題が語数の水増しになっていないか、自然に会話が続くかを確認してください。Notionの記録方法の変更や再同期は不要です。

### AI英文採点

各問題で英文（最大4,000文字）を入力して「AIで採点する」を押すと、既存の`OPENAI_MODEL`を使って採点します。空欄ではボタンを無効にします。Responses APIのStructured Outputsで取得し、追加のモデル環境変数は不要です。1回の採点は1リクエスト、出力上限3,500 tokens、タイムアウトは接続10秒／読み取り90秒で、自動リトライは行いません。

送信するのは練習表現、日本語の状況、模範例文、入力英文、学習者レベル（intermediate／CEFR B1–B2）、保存プランの練習レベルです。旧プランに練習レベルがなければ応用として扱います。レビュー全文・出典・RAG検索結果は送信しません。`store=false`を指定し、入力や認証情報をログへ出しません。

出力は`grammar_spelling`、`naturalness`、`expression`、`communication`（各0〜100の整数）と、`strengths_ja`、`improvements_ja`、`corrected_en`、`enhanced_en`です。型・範囲・日本語説明・英文をPythonで検証し、総合点は4項目の平均値をPythonで計算します。模範との一致を要求せず、英米差・短縮形・自然な別解を認め、意味を保つ最小限の添削を指示します。採点は学習支援の目安であり、能力試験ではありません。

同じセッションの同一プラン・問題・回答・採点仕様の結果を再利用します。回答を変更した場合は旧結果を最新として表示せず、再採点を案内します。以前の回答に戻して採点するとキャッシュを使います。結果には版、プラン／候補ID、回答、日時、モデル、使用量を内部保持しますが、ファイルには保存しません。セッション終了で消えるため、履歴の永続保存には利用できません。API失敗時も回答と過去の結果を保持し、不正な応答はキャッシュしません。Regenerateでは新しいプランIDで回答・結果を分離します。

実APIを使う確認では、自然な別解、変更不要の英文、スペルミス、対象表現を使わない回答、基本／応用／実践での評価の妥当性を確認してください。通常表示・入力のみで通信しないこと、同一回答の再採点が通信を増やさないことも確認してください。

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

## Learning Data Foundation（Phase 1）

`src/learning/`はprocessedを正本とする、全件再構築可能な**メモリ上の派生データ基盤**です。Analytics／Ask／Coachの既存ロジック・UIにはまだ接続していません。既存ロジックと比較できるようそのまま残し、新しいLLM／Embedding処理も追加していません。

```text
processed/reviews.json（既存・変更しない）
  → evidence.py：原文と出典を保持した記録の抽出
  → normalization.py：表記だけの正規化・固定expression_id
  → catalog.py：表現一覧・履歴・期間別統計・検索
  → 次フェーズ：Analytics / Ask / Coach（今回は未接続）
```

| モジュール | 責務 |
|---|---|
| `models.py` | 不変のExpression・LearningEvidence・ExpressionStatistics・関連付けモデル |
| `normalization.py` | NFKC・大小文字・空白・引用符・末尾句読点の基本正規化 |
| `evidence.py` | Reviewの各学習フィールドから記録を抽出 |
| `catalog.py` | 構築、明示的な読み込み、一覧・履歴・絞り込み・統計 |
| `profile.py` | 本人申告のCEFR B1–B2、目標・興味。練習レベルとは別概念 |

### 証拠と表現

- Phrase Cards、修正前後とNote、追加／次回表現、実使用欄、Weak／Good points、LINEのDraft／Natural version／修正理由、Situation／Commentを別の記録として保持します。
- 各記録は元の`review_id`・日付・Topic・種別・ページ／見出しID・元フィールド・項目位置・原文を持ちます。カードの意味・例文・priority・source・review_status・参照ID等も原値のメタデータとして保持し、習得評価には変換しません。
- 英語互換の構造化フィールドは、全文・句をそのまま一つの表現単位として扱います。日本語混在フィールドでは明示的な引用の英語だけを関連付けます。Weak／Good points・修正理由は、英語の文章でも引用等で表現が明示されない限りレビュー単位に留めます。
- 修正記録は1件のまま、修正前・修正後・Noteの引用表現へ役割付きで関連付けます。`correction_record_count`は関連する記録数で、当該表現の誤用回数ではありません。前後が同じ正規化表現でも二重計上しません。複数表現へ関連付いた記録を表現間で合算すると重複するため、全体件数は`catalog.evidence`から数えます。
- 表現を特定できない文章・空項目も`unlinked_evidence`に残します。元データにない単語・語形・修正理由を生成せず、文章から単語を切り出して学習済みとは扱いません。
- `original_variants`には正規化前の文字列を保持します。`expression_id`は`exp:v1:<正規化表現のSHA-256>`です。同じ正規化表現なら同じIDになり、レビュー順序に依存しません。
- Unicode NFKC、casefold、連続空白、曲がった引用符／アポストロフィ、外側の対応する引用符、末尾句読点を整理します。内部句読点・ハイフン・アポストロフィは保持し、語形・類義語・丁寧表現を意味的に統合しません。`refresh`／`refreshed`／`refreshing`、`one more time`／`One more time, please.`は別IDです。スラッシュで記録された複合表現も、推測で個別単語へ分解しません。
- `evidence_id`は出典位置と記録内容のハッシュです。同一レビュー内の同一項目も位置が違えば別証拠です。同じ入力なら再現しますが、項目編集・移動では変わる場合があります。重複review_idは安全のため構築エラーにします。

### Python API

```python
from datetime import date
from src.learning import build_catalog, load_catalog, EvidenceType

catalog = build_catalog(reviews)  # 既存Review、または旧形式を含むReview辞書の反復可能オブジェクト
# または明示的に既存Storageを渡す。Local/S3とも読み込みのみ。
# catalog = load_catalog(storage)

expressions = catalog.all()
expression = catalog.find('  REFRESHED!  ')  # 正規化後の完全一致。意味検索ではない。
if expression:
    same = catalog.get(expression.expression_id)
    history = catalog.history(expression.expression_id)
    feb_stats = catalog.statistics(
        expression.expression_id,
        start_date=date(2026, 2, 1), end_date=date(2026, 2, 28),
    )

# 範囲内の証拠だけから表現・意味・例文・統計を再構築するビュー
view = catalog.select(
    start_date=date(2026, 2, 1), end_date=date(2026, 2, 28),
    topic='coffee', evidence_types=[EvidenceType.EXPLICIT_USAGE_RECORD],
)
# 日付条件は両端を含む。Topicは正規化後の部分一致（意味的分類ではない）。
recent_corrections = catalog.search(last_correction_after=date(2026, 2, 1))
recorded_usage = catalog.search(has_usage=True, last_usage_before=date(2026, 3, 1))
not_recently_recorded = catalog.search(last_seen_before=date(2026, 1, 31))
```

`search`にも`select`と同じ期間・Topic・種別条件を指定でき、最終日条件は絞り込み後の証拠に適用します。対象期間にない表現の統計は0件・日付`None`となります。日付がない記録は最終日条件に一致しません。

`unique_review_count`／`occurrence_count`はユニークreview_id数です。同日の複数レビューを区別します。`record_count`は当該表現に関連付いた全証拠数、`learning_record_count`はPhrase Card・追加・次回練習の明示的な学習対象記録数です。修正・使用・弱点数は別に計算します。使用欄への記録は正用・習得の証明ではなく、Good pointsへの記載も習得判定ではありません。

### 保存・互換性・次フェーズ

インポート時の設定読み込み・API実行・自動保存はありません。`load_catalog(storage)`は`processed/reviews.json`だけを明示的に読み、raw／state／RAG／プランを変更しません。永続キャッシュは導入せず、processedの変更・追加・削除は毎回の再構築で反映します。`source_data_hash`はレビュー順序に依存しない内容ハッシュ、スキーマ・抽出・正規化の版はそれぞれ`1`です。将来キャッシュを導入する際は、これらの版と内容ハッシュに加え検索条件もキーに含め、破損時は再構築します。

共通プロフィールは旧Coachと同じ`level`／`cefr`を持ちますが、既存Coachのプロフィール・指紋・興味設定は今回は変更しません。将来の移行時は明示的に生成仕様を更新し、保存済みプランの互換性を維持します。

接続順序は、(1)Analyticsの記録・使用・修正の区別、(2)Askの期間抽出・全件集計、(3)Coachの候補供給を推奨します。既存関数との比較テスト後に個別に切り替えます。意味グループ・難易度評価・本人申告・採点履歴・復習予定は、このIDを参照する別の派生／履歴モデルとして追加できます。現Phaseではランキングや習得状態を作りません。
