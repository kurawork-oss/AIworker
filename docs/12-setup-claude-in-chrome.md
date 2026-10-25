# 12. 初期設定（Claude in Chrome に渡す指示書）

所要 約60分（うち本人の入力 約15分）。下の「ここから」〜「ここまで」をそのまま Claude in Chrome に貼る。
途中で止まっても、終わった手順から再開できる。

---

<!-- ここから -->

# 依頼：AIworker の初期設定

私のブラウザで、副業用アプリ AIworker（https://github.com/kurawork-oss/AIworker）の初期設定を代行してください。
対象は 画像販売（Adobe Stock）・A8アフィリエイト（はてなブログ）・note・YouTubeショート です。

## 必ず守ること
- **パスワード・本人確認・住所・銀行口座・税務・支払い情報は私が入力します。** その画面に来たら止まって知らせてください。
- **APIキー・秘密キーの値を、チャット・メモ・報告に書かないでください**（先頭の数文字も不可）。
  各サイトの「コピー」ボタンでコピーし、GitHub の入力欄に貼るだけにしてください。
- **課金・有料プラン・カード登録は一切しないでください。** 求められたら止まって知らせてください。
- 既存の Supabase プロジェクト「AIbou Project」には触れないでください。
- 規約への同意画面では、同意の前に止まって私に確認してください。
- 想定と違う画面・不明点では推測で進めず、止まって質問してください。

## 1. Supabase（無料のデータベース）
プロジェクト `aiworker`（東京リージョン）は**作成済み**です。https://supabase.com/dashboard で開いてください。
1. 左メニュー「Table Editor」に `items` などの表が**無い**場合だけ: 「SQL Editor」→ 新しいクエリで次の3つを**この順に**実行し、それぞれ Success を確認
   1. https://raw.githubusercontent.com/kurawork-oss/AIworker/main/supabase/migrations/20261025000001_core.sql の全文
   2. https://raw.githubusercontent.com/kurawork-oss/AIworker/main/supabase/migrations/20261025000002_storage.sql の全文
   3. https://raw.githubusercontent.com/kurawork-oss/AIworker/main/supabase/migrations/20261026000001_affiliate.sql の全文
2. 「Authentication」→ サインインの設定で **「Allow new users to sign up」を OFF** にして保存（メールのサインイン自体は ON のまま）
3. 「Authentication」→「Users」→「Add user」→「Create new user」
   - Email: 私のメールアドレス（私に確認してください）
   - パスワード欄は **私が入力します**（ここで止まる）
   - 「Auto Confirm User」を ON
4. SQL Editor で次を実行（`私のメール` は手順3のアドレスに置き換え）:
   ```sql
   insert into public.owners(user_id)
   select id from auth.users where email = '私のメール'
   on conflict do nothing;
   select user_id from public.owners;
   ```
   表示された ID（UUID・秘密ではない）を手順6で使うので控える。
5. 「Authentication」→「URL Configuration」→ Site URL を `https://kurawork-oss.github.io/AIworker/` にして保存
6. 「Project Settings」→「API Keys」で次の3つの場所を確認（値は手順6で直接コピーする）:
   Project URL / Publishable key（`sb_publishable_`で始まる）/ Secret key（`sb_secret_`で始まる）

## 2. Google AI Studio（文章・台本の生成・無料）
1. https://aistudio.google.com/apikey →「API キーを作成」（新しいプロジェクトで作成）
2. 課金（Billing）の設定には進まない。キーは手順6で使う

## 3. Cloudflare（画像生成・無料・カード不要）
1. アカウントがなければ https://dash.cloudflare.com/sign-up（メール認証は私）
2. Account ID の場所を確認（ダッシュボードの右側、または「Workers AI」の画面）
3. 右上のアイコン →「My Profile」→「API Tokens」→「Create Token」→ テンプレート **「Workers AI」** → 作成。トークンは手順6で使う

## 4. はてなブログ（A8の記事を自動投稿する先・無料）
1. はてなIDがなければ https://www.hatena.ne.jp/register （入力は私）
2. https://blog.hatena.ne.jp/ →「ブログを作成」。ブログ名は私に確認。URLは `○○.hatenablog.com`、公開範囲は「公開」
3. ブログの「設定」→「基本設定」→ **編集モードを「Markdownモード」** にして保存
4. ブログの「設定」→「詳細設定」→「AtomPub」欄で **APIキー** の場所を確認（値は手順6で直接コピー）
5. 控える（秘密ではない）: はてなID、ブログのドメイン（`○○.hatenablog.com`）

## 5. A8.net（アフィリエイト・無料）
1. https://www.a8.net/ →「無料会員登録」。氏名・住所・口座などの入力は私（ここで止まる）
2. サイト登録で、手順4のブログURLを登録（カテゴリは私に確認）
3. 「プログラム検索」で **即時提携** のものから、ブログの内容（AI活用・副業・生活の工夫）に合う案件を3〜5件探し、
   **一覧を私に見せて**、私が選んだものだけ「提携申請」する
4. 提携できた案件ごとに:
   - 「広告リンク作成」→ テキスト広告 → 表示されたコードをコピー
   - AIworker アプリ（手順6-4のURL）→「事業」→「A8」→「案件を登録」に、案件名・コードを貼る
   - 「提供情報」には、案件詳細ページの説明・特典・注意事項を**そのまま**コピーして貼る（AIは記事にこの情報しか使いません）
   - 案件の掲載条件で「ブログ不可」なら登録しない

## 6. GitHub（https://github.com/kurawork-oss/AIworker）
1. Settings → General → **Default branch を `main` に変更**
2. Settings → Pages → Build and deployment → Source: **GitHub Actions**
3. Settings → Secrets and variables → Actions
   - **Secrets** タブ →「New repository secret」で5つ:

     | Name | 値（コピー元） |
     |---|---|
     | `SUPABASE_SECRET_KEY` | Supabase の Secret key |
     | `GEMINI_API_KEY` | Google AI Studio のキー |
     | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare の Account ID |
     | `CLOUDFLARE_API_TOKEN` | Cloudflare のトークン |
     | `HATENA_API_KEY` | はてなブログの AtomPub APIキー |

   - **Variables** タブ →「New repository variable」で7つ:

     | Name | 値 |
     |---|---|
     | `SUPABASE_URL` | Supabase の Project URL（`https://〜.supabase.co`） |
     | `SUPABASE_PUBLISHABLE_KEY` | Publishable key（**`sb_secret_` は絶対に入れない**） |
     | `AIWORKER_OWNER_ID` | 手順1-4 の ID |
     | `HATENA_ID` | はてなID |
     | `HATENA_BLOG_ID` | ブログのドメイン（`○○.hatenablog.com`） |
     | `AIWORKER_PIPELINE` | `on` |
     | `AIWORKER_SHORTS` | `on` |

4. Actions →「app」→「Run workflow」（main）→ 緑になったら、表示されたアプリの URL を控える
5. Actions →「pipeline」→「Run workflow」→ 緑を確認し、「Run the pipeline」ログの**最後の3行**（件数だけの行）を控える
6. Actions →「shorts」→「Run workflow」→ 緑を確認し、ログの最後の1行を控える（10分ほどかかる）

## 7. 投稿先のアカウント（本人入力あり）
1. Adobe Stock: https://contributor.stock.adobe.com/ → Adobe ID でログイン/作成（入力は私）。支払い・税務情報の画面まで案内したら止まる
2. note: https://note.com/signup（入力は私）。プロフィール文は私に確認
3. YouTube: https://www.youtube.com/create_channel でチャンネル作成（Googleアカウントの選択は私）。チャンネル名は私に確認

## 8. 報告（キーの値は書かない）
- 1〜7 の各手順：完了／未完了（止まった理由）
- アプリの URL、ブログの URL、提携できたA8案件の名前
- pipeline と shorts の初回実行の最後の行
- 想定と違った画面があれば、その説明

<!-- ここまで -->

---

## 本人がやること（Chrome の後）
1. スマホでアプリの URL を開く → 手順1-3のメールとパスワードでログイン → 共有メニュー →「ホーム画面に追加」
2. 毎日〜週1回: アプリの「確認」で承認・修正依頼、「投稿」でコピーして貼る／保存して上げる
3. Claude Code に Chrome の報告を貼る（キーは含まれていないこと）→ 実環境での受入確認に進む

## うまくいかないとき
| 症状 | 対処 |
|---|---|
| アプリが「未接続」 | GitHub Variables の `SUPABASE_URL` と `SUPABASE_PUBLISHABLE_KEY` を確認 →「app」を再実行 |
| ログインできない | Supabase の Users にあるか、Auto Confirm が ON だったか |
| ログインできるが「権限がありません」 | 手順1-4 の SQL（owners への登録）が未実行 |
| pipeline / shorts が赤 | 最後の行の「設定エラー」の内容どおり Secrets を確認 |
| pipeline / shorts が灰色（skipped） | Variables の `AIWORKER_PIPELINE` / `AIWORKER_SHORTS` が `on` か |
| 履歴に「はてなブログ未接続」 | `HATENA_ID` / `HATENA_BLOG_ID`（Variables）と `HATENA_API_KEY`（Secrets） |
| 履歴に「提携案件が未登録」 | 手順5-4（アプリへの案件登録）が未完了 |
