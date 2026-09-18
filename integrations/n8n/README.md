# n8n（セルフホスト）連携

## 方針: n8n は「呼ぶ人」であって「判断する人」ではない

AIworker のガードレール・承認ゲート・上限管理は**すべてCLI側**にあります。
n8n がやるのは、決まった時刻にコマンドを叩き、失敗したら通知することだけです。

この分担には理由があります。

- **ワークフローを止めても安全性は落ちない。** n8n が停止しても、cron に切り替えても、
  手で叩いても、同じチェックが同じように走ります。
- **n8n のノードを編集してもガードレールを外せない。** チェックはCLIの内側にあるので、
  ワークフロー側の設定ミスで「上限チェックを飛ばす」経路は作れません。
- **n8n は必須ではありません。** cron で完全に同じことができます
  （`scripts/crontab.example` 参照）。n8n を使うのは、実行履歴のUIと
  失敗通知の設定が楽だからです。

**n8n 側に承認フローを実装しないでください。** 承認は `aiworker review` または
Web UI が行い、その結果は監査ログに残ります。n8n 側で承認相当の分岐を作ると、
記録の残らない承認経路ができてしまいます。

## セットアップ

### 1. n8n をセルフホストで起動

```bash
docker run -d --name n8n \
  -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  -v /path/to/AIworker:/aiworker \
  -e AIWORKER_HOME=/aiworker \
  -e DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx \
  -e GENERIC_TIMEZONE=Asia/Tokyo \
  docker.n8n.io/n8nio/n8n
```

`AIworker` のディレクトリをコンテナにマウントし、`AIWORKER_HOME` でパスを渡します。

> コンテナ内で `python3` と `PyYAML` が必要です。用意が面倒な場合は、
> n8n をホスト側にインストールするか、SSH ノード経由でホストのコマンドを叩く構成にしてください。

### 2. ワークフローを読み込む

n8n の画面で **Workflows → Import from File** → `workflow-daily-cycle.json`

### 3. 環境変数を確認

| 変数 | 用途 |
|------|------|
| `AIWORKER_HOME` | AIworker のパス（必須） |
| `DISCORD_WEBHOOK_URL` | 失敗通知とレポート送信先 |
| `GENERIC_TIMEZONE` | `Asia/Tokyo`。スケジュールの解釈に使われます |

### 4. 手動実行で確認してから有効化

各ノードを個別に手動実行し、期待どおりの出力が出ることを確認してから
ワークフローを Active にしてください。

## ワークフローの中身

```
06:00 ──→ aiworker generate ─┐
08:05 ──→ aiworker plan ─────┼─→ 失敗した? ─→ Discord に通知
毎時   ──→ aiworker publish ──┘
20:00 ──→ aiworker report ──────→ Discord にレポート送信
```

**承認は含まれていません。** そこは人間の担当です
（`aiworker review list` または `aiworker serve` のWeb UI）。

## 緊急停止

n8n を止めるより、AIworker 側で止めるほうが確実です。

```bash
touch /path/to/AIworker/var/STOP
```

n8n のワークフローが動き続けても、`publish` は何も公開せずに終了します。
n8n を無効化する方法は、既に実行中のジョブには効きません。

## よくある落とし穴

| 症状 | 原因 |
|------|------|
| `publish` が毎時走るのに何も公開されない | 正常です。投稿枠が来たジョブだけが実行されます |
| `generate` が0件で終わる | 承認待ちキューが満杯（背圧制御）。まず承認してください |
| 失敗通知が来ない | `DISCORD_WEBHOOK_URL` が n8n の環境変数に入っていない |
| タイムゾーンがずれる | `GENERIC_TIMEZONE=Asia/Tokyo` を設定。AIworker 側の `timezone` とも揃える |
