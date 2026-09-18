# 2. ディレクトリ構成

```
AIworker/
├── README.md                     全体の入口。5分で動かすまでの手順
├── Makefile                      make init / doctor / test / report
├── pyproject.toml                パッケージ定義（`pip install -e .` で aiworker コマンド）
├── requirements.txt              コア依存（PyYAML のみ）
├── requirements-web.txt          Web UI を使う場合の追加依存
├── requirements-dev.txt          テスト用
├── .env.example                  秘密情報のテンプレート（.env は .gitignore 済み）
│
├── config/
│   ├── config.example.yaml       設定のひな形。挙動のみ、秘密情報は入れない
│   ├── config.yaml               ← 実際に使う設定（gitignore）
│   └── policy/
│       ├── banned_terms.example.yaml   禁止ワード・商標・実在人物名のひな形
│       └── *.yaml                      ← 実際の禁止リスト（gitignore）
│
├── docs/
│   ├── 01-architecture.md        アーキテクチャ図・状態遷移・設計判断
│   ├── 02-directory.md           このファイル
│   ├── 03-module-design.md       各モジュールの詳細設計
│   ├── 04-mvp-setup.md           MVP のセットアップ手順
│   ├── 05-risk-checklist.md      運用開始前チェックリスト（aiworker checklist で表示）
│   └── 06-roadmap.md             実装優先順位と段階的な拡張計画
│
├── integrations/
│   └── n8n/                      n8n セルフホストから叩く場合のワークフロー例
│       ├── README.md
│       └── workflow-daily-cycle.json
│
├── scripts/
│   ├── aiworker                  インストールせずに実行するラッパー
│   └── crontab.example           cron 設定例
│
├── src/aiworker/
│   ├── cli.py                    コマンドライン（人間の主な操作面）
│   ├── __main__.py               python -m aiworker
│   │
│   ├── core/                     土台。他の層はここにしか依存しない
│   │   ├── config.py             設定読込・検証・${ENV:} 解決
│   │   ├── db.py                 SQLite スキーマとクエリ
│   │   ├── models.py             ContentItem / PublishJob / Status / Channel
│   │   ├── clock.py              時刻（保存はUTC、表示はローカル）
│   │   ├── logging_setup.py      JSON Lines ログ + 30日保持
│   │   └── errors.py             例外の型階層（retryable かどうかが型で分かる）
│   │
│   ├── guard/                    ★リスクヘッジの中核。全経路がここを通る
│   │   ├── killswitch.py         緊急停止（DBフラグ + STOPファイル）
│   │   ├── quota.py              上限・最小間隔・活動時間帯・枠の探索
│   │   ├── policy.py             法務/規約チェック・PR表記・AI開示
│   │   ├── quality.py            重複検知・文字数・生成失敗痕跡・必須項目
│   │   └── anomaly.py            異常検知と自動停止
│   │
│   ├── generators/
│   │   ├── llm.py                LLM抽象化（mock / claude_cli / anthropic）
│   │   ├── prompts.py            チャンネル別のプロンプトと出力スキーマ
│   │   ├── diversity.py          theme × angle × tone の多様性制御
│   │   └── service.py            生成→判定→pending_review 保存（背圧制御つき）
│   │
│   ├── approval/
│   │   └── service.py            ★approved を書ける唯一の場所
│   │
│   ├── scheduler/
│   │   └── planner.py            枠の割当（plan）と実行（run_due）
│   │
│   ├── publishers/
│   │   ├── base.py               Publisher プロトコル + リトライ方針
│   │   ├── dryrun.py             dryrun（何もしない） / manual（下書き出力）
│   │   └── registry.py           アダプタ解決。dry_run はここで一括適用
│   │
│   ├── revenue/
│   │   ├── importer.py           CSV取込（日本語列名・Shift-JIS・重複取込対応）
│   │   └── report.py             運用レポートと収益サマリー（テキスト図表）
│   │
│   ├── notify/
│   │   └── notifier.py           Discord / Slack / console
│   │
│   └── webui/
│       └── app.py                任意のFastAPI承認UI（localhost限定）
│
├── tests/                        pytest。ガードレールと承認ゲートに厚く配分
│   ├── conftest.py
│   ├── test_config.py            設定の検証（承認ゲートは無効化できない等）
│   ├── test_guard_quota.py       上限・間隔・枠探索
│   ├── test_guard_policy.py      規約チェック
│   ├── test_guard_quality.py     重複検知
│   ├── test_killswitch.py        緊急停止
│   ├── test_approval.py          ★承認ゲートの不変条件
│   ├── test_pipeline.py          ★E2E：未承認は絶対に公開されない
│   ├── test_anomaly.py           異常検知
│   ├── test_revenue.py           収益取込と集計
│   └── test_cli.py               各コマンドのスモークテスト
│
└── var/                          実行時の状態（全て gitignore）
    ├── aiworker.db               SQLite 本体。バックアップ対象はこれ1つ
    ├── logs/                     aiworker-YYYY-MM-DD.log（30日で自動削除）
    ├── outbox/                   publisher: manual の下書き出力
    │   └── YYYY-MM-DD/<platform>/<uid>.md
    ├── imports/                  収益CSVの置き場
    ├── STOP                      ★これを touch すれば全停止
    └── STOP.<platform>           プラットフォーム単位の停止
```

## 依存の向き

```
cli / webui
    ↓
approval  scheduler  generators  revenue
    ↓         ↓          ↓          ↓
        guard（全経路が通る）
                ↓
              core
```

- `core` は他のどの層にも依存しません。
- `guard` は `core` と `notify` にのみ依存します。ガードレールが上位層に依存すると、
  上位層を差し替えたときにチェックが外れる余地が生まれるためです。
- 公開処理は `publishers` を**直接**呼ばず、必ず `scheduler/planner.py` 経由です。
  アダプタ側でチェックを呼び忘れても迂回できない構造にしています。

## バックアップ対象

| 対象 | 頻度 | 理由 |
|------|------|------|
| `var/aiworker.db` | 毎日 | 承認履歴・公開実績・収益。失うと上限管理が壊れる |
| `config/config.yaml` | 変更時 | 運用パラメータ |
| `config/policy/*.yaml` | 変更時 | 禁止リスト |
| `.env` | 変更時（別管理） | 秘密情報。リポジトリや通常バックアップに含めない |

`var/logs/` と `var/outbox/` はバックアップ不要です（前者は30日で自動削除、後者は再生成可能）。
