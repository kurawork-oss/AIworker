# 4. MVP セットアップ手順

所要時間: 約15分（初回の生成・承認・公開まで一周）

---

## 4.1 前提

- Python 3.11 以上
- インターネット接続（`provider: mock` のままなら不要）
- **APIキーは不要です。** 既定の `mock` プロバイダはオフラインで動きます。

## 4.2 インストール

```bash
git clone <このリポジトリ>
cd AIworker

# 方法A: 仮想環境に入れる（推奨）
python3 -m venv .venv && source .venv/bin/activate
pip install -e .              # `aiworker` コマンドが使えるようになる

# 方法B: インストールせずに使う
pip install PyYAML
./scripts/aiworker --help     # 以降 `aiworker` を `./scripts/aiworker` に読み替え
```

## 4.3 初期化

```bash
aiworker init
```

これで以下が作られます。

- `config/config.yaml`（`config.example.yaml` のコピー）
- `.env`（`.env.example` のコピー、パーミッション600）
- `var/aiworker.db`、`var/logs/`、`var/outbox/`

## 4.4 設定

`config/config.yaml` を開いて、最低限ここだけ変えます。

```yaml
generation:
  themes:                       # ← 自分が発信するテーマに置き換える
    - AIを使った業務効率化
    - 副業を続ける仕組みづくり

platforms:
  x:
    enabled: true
    publisher: manual           # manual = var/outbox/ に下書きを出す（投稿はしない）
    daily_limit: 5              # ← 控えめに始める
    min_interval_minutes: 90
    active_hours: [8, 23]
```

`dry_run: true` と `require_human_approval: true` は**そのままにしてください**。

### 禁止リスト（推奨）

```bash
cp config/policy/banned_terms.example.yaml config/policy/banned_terms.yaml
```

自分のジャンルで危険な語、取引先名、商標を追加します。このファイルは gitignore 済みです。

### 通知（本番運用では必須）

`.env` に設定します。

```bash
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx
```

`config/config.yaml` の `notify.channels` を `[console, discord]` にします。

> Discord Webhook の作り方: サーバー設定 → 連携サービス → ウェブフック → 新しいウェブフック
> → URLをコピー。無料で、アカウント連携も不要です。

## 4.5 設定の検証

```bash
aiworker doctor
```

`✗` が出たら直します。`⚠` は把握したうえで進めて構いません。

## 4.6 最初の一周

```bash
# 1. 生成（3件）
aiworker generate --channel social_post --count 3

# 2. 承認キューを見る（判定理由つき）
aiworker review list --verbose

# 3. 1件の全文を読む
aiworker review show 1

# 4. 判断する
aiworker review approve 1 2         # 承認
aiworker review revise 3 --note "冒頭をもっと具体的に"   # 修正依頼
# aiworker review reject 3 --note "テーマが重複"         # 却下

# 5. 投稿枠を割り当てる（時刻はランダムに分散される）
aiworker plan

# 6. 枠が来たジョブを実行する
aiworker publish

# 7. 状態を見る
aiworker status
```

`publisher: manual` なので、公開結果は `var/outbox/YYYY-MM-DD/x/<uid>.md` に
貼り付け用のMarkdownとして出力され、項目は **手動投稿待ち** の状態になります。

```bash
aiworker outbox        # これから投稿するものの一覧（= 作業リスト）
```

本文をコピーして投稿したら、記録します。

```bash
aiworker mark-published <uid> --url https://x.com/you/status/123
```

> **投稿枠は下書きを出力した時点で消費されます。** 記録を忘れても上限管理は壊れません
> （枠を早めに確保する方向なので、多く数えることはあっても少なく数えることはありません）。
> 記録が必要なのは、公開URLと実績（リーチ等）を後で突き合わせるためです。
> 記録していない項目は `aiworker outbox` に残り続けるので、やり残しが分かります。

## 4.7 承認をWeb UIで行う（任意）

CLIだけで完結しますが、スマホから承認したい場合：

```bash
pip install -r requirements-web.txt
aiworker serve                      # http://127.0.0.1:8787
```

スマホアプリの形をした画面で、下のタブが「タスク / レポート / 設定」、
タスクの中が「リスト / フロー / スワイプ / 対話」の4つに分かれています。

| 画面 | 使いどころ |
|------|-----------|
| リスト | 既定。状態別のカード一覧で承認・却下 |
| フロー | いま何がどこで詰まっているかを図で確認 |
| スワイプ | 1件ずつ全文を読んで一気に捌く（ブロック項目は対象外） |
| 対話 | 会話形式。キューが短いとき |
| レポート | 承認待ち件数・収益推移・収益源別・カテゴリー別 |
| 設定 | 投稿枠の消化・緊急停止 |

認証はありません。`127.0.0.1` 以外へのバインドは拒否されます。
リモートから使う場合はSSHトンネルを使ってください。

```bash
ssh -L 8787:127.0.0.1:8787 <サーバー>
```

## 4.8 実際のLLMに切り替える

`mock` は placeholder テキストなので、そのままでは公開できません。

### 方法A: Claude Code（ローカルに `claude` がある場合）

```yaml
generation:
  provider: claude_cli
  model: claude-opus-5
```

### 方法B: Anthropic API

```yaml
generation:
  provider: anthropic
  model: claude-opus-5
```

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

切り替えたら、少数（3件程度）生成して全文を読み、品質を確認してください。

## 4.9 自動実行（cron）

```bash
crontab -e
```

```cron
# 朝6時: 生成（承認待ちキューが満杯なら自動でスキップされる）
0 6 * * * cd /path/to/AIworker && ./scripts/aiworker generate --channel social_post --count 3 -q

# 朝8時5分: 承認済みに投稿枠を割り当てる
5 8 * * * cd /path/to/AIworker && ./scripts/aiworker plan -q

# 毎時: 枠が来たジョブを実行
0 * * * * cd /path/to/AIworker && ./scripts/aiworker publish -q

# 夜20時: 日次レポートを通知
0 20 * * * cd /path/to/AIworker && ./scripts/aiworker report --notify -q

# 日曜3時: ログ整理
0 3 * * 0 cd /path/to/AIworker && ./scripts/aiworker prune -q
```

**`aiworker review` は cron に入れません。** そこが人間の担当です。

n8n セルフホストを使う場合は `integrations/n8n/` を参照してください
（同じCLIを Execute Command ノードから叩くだけです）。

## 4.10 バックアップ

```cron
# 毎日2時: DBをバックアップ（7日分保持）
0 2 * * * cd /path/to/AIworker && cp var/aiworker.db "var/backup-$(date +\%u).db"
```

`.env` はこれに含めず、パスワードマネージャ等で別管理してください。

## 4.11 本番移行（1プラットフォームずつ）

`docs/05-risk-checklist.md` のセクションCを実施してから進めてください。要点のみ：

1. **2週間は `publisher: manual` のまま運用する。** 生成品質が安定するまで自動投稿しない。
2. **1つずつ有効化する。** 全部同時に本番化しない。
3. API自動投稿に移行する場合：
   - そのプラットフォームの規約で自動投稿が許可されていることを確認
   - `src/aiworker/publishers/` にアダプタを実装
   - `publishers/registry.py:ADAPTERS` に登録（ここに書かない限り到達不可能）
   - `config.yaml` で `publisher: <アダプタ名>`、`dry_run: false`
   - 最初の1週間は `daily_limit` を通常の半分にする

```bash
# 何かおかしいと感じたら、考える前にこれ
aiworker stop --reason "様子がおかしい"
```

## 4.12 トラブルシューティング

| 症状 | 原因と対処 |
|------|-----------|
| `generate` が0件を返す | 承認待ちキューが満杯（背圧制御）。`aiworker review list` で処理する |
| `plan` が「no slot」と言う | 上限に達しているか活動時間外。`aiworker status` で消化状況を確認 |
| `publish` が何もしない | 停止中か、枠の時刻がまだ来ていない。`aiworker status` を確認 |
| 全部 `blocked` になる | `aiworker review list --status blocked --verbose` で理由を読む。禁止リストが広すぎる可能性 |
| 類似度で弾かれ続ける | テーマ数が少なすぎる。`generation.themes` を増やす |
| 通知が届かない | `aiworker doctor` を実行。`notify.channels` と Webhook URL の両方が必要 |
| `database is locked` | 別プロセスが長時間トランザクションを持っている。cronの重複実行を確認 |
