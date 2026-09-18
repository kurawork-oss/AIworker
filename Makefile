PY ?= python3
export PYTHONPATH := src

.PHONY: help init doctor test check smoke hygiene ui generate review status report clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

init:  ## DBと設定を初期化
	$(PY) -m aiworker init

doctor:  ## 設定を検証
	$(PY) -m aiworker doctor

test:  ## テストを実行
	$(PY) -m pytest

check: test hygiene ui  ## CIと同じ検査をローカルで実行（smoke は別途）

hygiene:  ## 秘密情報・実データ・危険な既定値が混入していないか
	$(PY) scripts/check_repo_hygiene.py

ui:  ## 承認UIの全画面が描画できるか
	$(PY) scripts/check_web_ui.py

smoke:  ## インストール済みパッケージに対するE2E（要 pip install .）
	./scripts/smoke_test.sh

generate:  ## サンプル生成 (social_post 3件)
	$(PY) -m aiworker generate --channel social_post --count 3

review:  ## 承認待ち一覧
	$(PY) -m aiworker review list --verbose

status:  ## 現在の状態
	$(PY) -m aiworker status

report:  ## 運用レポート
	$(PY) -m aiworker report --days 7

clean:  ## 生成物を削除 (DBは消さない)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
