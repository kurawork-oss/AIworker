# AIworker

リスクヘッジを最優先にした、**人間の承認ゲート付き**コンテンツ運用システム。

AIで生成した投稿・記事・台本・素材メタデータを、規約チェックと重複検知と投稿上限管理を
通したうえで承認キューに積み、**人間が承認したものだけ**を、ランダムに分散した時刻で公開します。

```
生成 → 自動チェック → [人間の承認] → 枠の割当 → 公開直前の再チェック → 公開
         ↑                                        ↑
      規約・重複・品質                    停止/上限/承認状態をもう一度
```

## 設計の中心にある1つの原則

> **承認されていないコンテンツは、どの経路からも外部に出ない。**

- 生成エンジンは `approved` 状態を**書き込めない**
- 承認は1箇所だけが行い、必ず「誰が・いつ・なぜ」が記録される
- 自動ブロックの上書きには `--force` と**書面の理由**の両方が必要
- 公開の直前に、停止状態・投稿枠・承認状態を**もう一度**全部確認する
- `touch var/STOP` だけで、DBが壊れていても全停止できる

## 5分で試す

```bash
pip install PyYAML
./scripts/aiworker init
./scripts/aiworker generate --channel social_post --count 3
./scripts/aiworker review list --verbose
./scripts/aiworker review approve 1
./scripts/aiworker plan
./scripts/aiworker publish
./scripts/aiworker status
```

APIキーは不要です（既定の `mock` プロバイダはオフラインで動きます）。
`dry_run: true` なので外部には何も送信されません。

## できること

| 領域 | 内容 |
|------|------|
| **生成** | X/Threads投稿・スレッド・note記事・Shorts台本・ストック素材メタデータ・デジタル商品企画 |
| **多様性** | theme × angle × tone の3軸で組合せを選択。直近で使った組合せは除外 |
| **規約チェック** | 商標・実在人物・作風模倣・効果保証表現・PR表記漏れ・AI開示漏れ |
| **重複検知** | 文字4-gramのJaccard係数で、同一チャンネルの直近200件と比較 |
| **上限管理** | 日次/週次上限・最小投稿間隔・活動時間帯。予約済みの枠も消化として計上 |
| **時刻分散** | ランダムな投稿枠。固定パターンは自動化の指紋になるため |
| **承認ゲート** | CLI または Web UI。承認・却下・修正依頼・編集（編集は承認を引き継がない） |
| **承認UI** | スマホアプリ形式。リスト / フロー / スワイプ / 対話 の4つの作業画面＋レポート＋設定 |
| **異常検知** | 連続失敗・警告文言・リーチ急減・上限接近。前2つは自動停止 |
| **緊急停止** | DBフラグ + STOPファイルの二重化。自動解除はしない |
| **収益集計** | CSV取込（日本語列名・Shift-JIS対応）・ソース別集計・集中度警告 |
| **監査ログ** | 全ての判断と公開をDBとJSON Linesの両方に記録。30日保持 |

## 主なコマンド

```bash
aiworker init                  # 初期化
aiworker doctor                # 設定の検証（運用開始前に必ず）
aiworker generate --channel social_post --count 3
aiworker review list --verbose # 承認キュー
aiworker review show 12        # 全文と判定理由
aiworker review approve 12
aiworker review revise 12 --note "冒頭を具体的に"
aiworker plan                  # 投稿枠の割当
aiworker publish               # 枠が来たジョブの実行
aiworker mark-published <uid> --url ...   # 手動投稿の記録（上限計算に必要）
aiworker status                # 現在の状態
aiworker report --days 7       # 運用・収益レポート
aiworker revenue import a8.csv --source a8
aiworker stop --reason "..."   # 緊急停止
aiworker resume --yes          # 解除
aiworker checklist             # 運用開始前チェックリスト
aiworker serve                 # Web UI（任意、localhost限定）
```

## ドキュメント

| | |
|---|---|
| [01-architecture.md](docs/01-architecture.md) | アーキテクチャ図・状態遷移・設計判断 |
| [02-directory.md](docs/02-directory.md) | ディレクトリ構成と依存の向き |
| [03-module-design.md](docs/03-module-design.md) | 各モジュールの詳細設計 |
| [04-mvp-setup.md](docs/04-mvp-setup.md) | セットアップ手順・cron設定・トラブルシューティング |
| [05-risk-checklist.md](docs/05-risk-checklist.md) | **運用開始前チェックリスト（必読）** |
| [06-roadmap.md](docs/06-roadmap.md) | 実装状況と段階的な拡張計画 |

## 既定で「自動投稿しない」理由

初期設定の `publisher` は `manual` です。承認されたコンテンツを
`var/outbox/` に貼り付け用のMarkdownとして出力し、投稿自体は人間が行います。

1. **note には公開されている投稿APIがありません。** 非公式な自動化は規約違反で、
   アカウント停止リスクが最も高い部分です。このシステムが避けようとしているリスクそのものです。
2. **正規APIがある経路も、利用条件の確認が先です。** アダプタは
   `publishers/registry.py:ADAPTERS` に明示登録されたものだけが到達可能で、
   ファイルを置いただけでは有効になりません。
3. **manual でも自動化の価値はほぼ全部残ります。** 生成・多様性確保・規約チェック・
   重複検知・上限管理・時刻分散・記録・収益集計は全て自動です。
   人間に残るのは「貼って投稿する」だけで、ここが最もアカウントを守ります。

自動投稿に移行する手順は [docs/04-mvp-setup.md](docs/04-mvp-setup.md) の 4.11 にあります。

## 開発

```bash
pip install -r requirements-dev.txt
python -m pytest                # 全テスト
make help                       # よく使う操作
```

テストはガードレールと承認ゲートに厚く配分しています。
特に `tests/test_pipeline.py::test_unapproved_content_is_never_published` が
このシステムの中心的な不変条件です。

## 技術スタック

Python 3.11 + 標準ライブラリ + PyYAML / SQLite / FastAPI（任意）/ cron または n8n

コア機能は標準ライブラリ + PyYAML のみで動きます。無料枠のVPSやノートPCで運用できます。
