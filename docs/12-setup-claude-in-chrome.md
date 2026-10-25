# 12. 初期設定（Claude in Chrome に渡す指示書）

所要 約30分（うち本人の入力 約5分）。下の「ここから」〜「ここまで」をそのまま Claude in Chrome に貼る。

---

<!-- ここから -->

# 依頼：AIworker の初期設定

私のブラウザで、副業用アプリ AIworker（https://github.com/kurawork-oss/AIworker）の初期設定を代行してください。

## 必ず守ること
- **パスワード・本人確認・税務・支払い情報は私が入力します。** その画面に来たら止まって知らせてください。
- **APIキー・秘密キーの値を、チャット・メモ・報告に書かないでください**（先頭の数文字も不可）。
  各サイトの「コピー」ボタンでコピーし、GitHub の入力欄に貼るだけにしてください。
- **課金・有料プラン・カード登録は一切しないでください。** 求められたら止まって知らせてください。
- 既存の Supabase プロジェクト「AIbou Project」には触れないでください。
- 想定と違う画面・不明点では推測で進めず、止まって質問してください。

## 1. Supabase（無料のデータベース）
1. https://supabase.com/dashboard →「New project」
   - Name: `aiworker` / Region: Northeast Asia (Tokyo) / Plan: Free
   - Database Password:「Generate a password」を押す（保存は私がします。あなたは記録しない）
2. 作成完了後、左メニュー「SQL Editor」→ 新しいクエリで次の2つを**この順に**実行し、それぞれ Success を確認:
   1. https://raw.githubusercontent.com/kurawork-oss/AIworker/main/supabase/migrations/20261025000001_core.sql の全文
   2. https://raw.githubusercontent.com/kurawork-oss/AIworker/main/supabase/migrations/20261025000002_storage.sql の全文
3. 「Authentication」→ サインインの設定で **「Allow new users to sign up」を OFF** にして保存
   （メールのサインイン自体は ON のまま）
4. 「Authentication」→「Users」→「Add user」→「Create new user」
   - Email: 私のメールアドレス（私に確認してください）
   - Password: **私が入力します**（ここで止まる）
   - 「Auto Confirm User」を ON
5. SQL Editor で次を実行（`私のメール` は手順4のアドレスに置き換え）:
   ```sql
   insert into public.owners(user_id)
   select id from auth.users where email = '私のメール'
   on conflict do nothing;
   select user_id from public.owners;
   ```
   表示された ID（UUID・秘密ではない）を手順4で使うので控える。
6. 「Authentication」→「URL Configuration」→ Site URL を `https://kurawork-oss.github.io/AIworker/` にして保存
7. 「Project Settings」→「API Keys」で次の3つの場所を確認（値は手順4で直接コピーする）:
   Project URL / Publishable key（`sb_publishable_`で始まる）/ Secret key（`sb_secret_`で始まる）

## 2. Google AI Studio（文章生成・無料）
1. https://aistudio.google.com/apikey →「API キーを作成」（新しいプロジェクトで作成）
2. 課金（Billing）の設定には進まない。キーは手順4で使う

## 3. Cloudflare（画像生成・無料・カード不要）
1. アカウントがなければ https://dash.cloudflare.com/sign-up（メール認証は私）
2. Account ID の場所を確認（ダッシュボードの右側、または「Workers AI」の画面）
3. 右上のアイコン →「My Profile」→「API Tokens」→「Create Token」→ テンプレート **「Workers AI」** → 作成。トークンは手順4で使う

## 4. GitHub（https://github.com/kurawork-oss/AIworker）
1. Settings → General → **Default branch を `main` に変更**
2. Settings → Pages → Build and deployment → Source: **GitHub Actions**
3. Settings → Secrets and variables → Actions
   - **Secrets** タブ →「New repository secret」で4つ:

     | Name | 値（コピー元） |
     |---|---|
     | `SUPABASE_SECRET_KEY` | Supabase の Secret key |
     | `GEMINI_API_KEY` | Google AI Studio のキー |
     | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare の Account ID |
     | `CLOUDFLARE_API_TOKEN` | Cloudflare のトークン |

   - **Variables** タブ →「New repository variable」で4つ:

     | Name | 値 |
     |---|---|
     | `SUPABASE_URL` | Supabase の Project URL（`https://〜.supabase.co`） |
     | `SUPABASE_PUBLISHABLE_KEY` | Publishable key（**`sb_secret_` は絶対に入れない**） |
     | `AIWORKER_OWNER_ID` | 手順1-5 の ID |
     | `AIWORKER_PIPELINE` | `on` |

4. Actions →「app」→「Run workflow」（main）→ 緑になったら、表示されたアプリの URL を控える
5. Actions →「pipeline」→「Run workflow」→ 緑になるのを確認し、「Run the pipeline」ログの**最後の1行**（件数だけの行）を控える

## 5. Adobe Stock 投稿者登録（本人入力あり）
1. https://contributor.stock.adobe.com/ を開く。Adobe ID のログイン/作成は私が入力
2. 支払い方法・税務情報の画面まで案内したら止まる（入力は私）

## 6. 報告（キーの値は書かない）
- 1〜5 の各手順：完了／未完了（止まった理由）
- アプリの URL
- pipeline 初回実行の最後の1行
- 想定と違った画面があれば、その説明

<!-- ここまで -->

---

## 本人がやること（Chrome の後）
1. スマホでアプリの URL を開く → 手順1-4のメールとパスワードでログイン
2. 共有メニュー →「ホーム画面に追加」
3. Claude Code に Chrome の報告を貼る（キーは含まれていないこと）→ 実環境での受入確認に進む

## うまくいかないとき
| 症状 | 対処 |
|---|---|
| アプリが「未接続」 | GitHub Variables の `SUPABASE_URL` と `SUPABASE_PUBLISHABLE_KEY` を確認 →「app」を再実行 |
| ログインできない | Supabase の Users にあるか、Auto Confirm が ON だったか |
| ログインできるが「権限がありません」 | 手順1-5 の SQL（owners への登録）が未実行 |
| pipeline が赤 | 最後の行の「設定エラー」の内容どおり Secrets を確認 |
| pipeline が灰色（skipped） | Variables の `AIWORKER_PIPELINE` が `on` か |
