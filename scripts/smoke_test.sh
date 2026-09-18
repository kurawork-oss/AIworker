#!/usr/bin/env bash
# End-to-end smoke test against an INSTALLED package.
#
#   ./scripts/smoke_test.sh            # uses `aiworker` from PATH
#   AIWORKER_BIN="python3 -m aiworker" ./scripts/smoke_test.sh
#
# The unit tests import the package directly, so they cannot catch a packaging
# mistake -- a module left out of the wheel, a broken console-script entry
# point, a data file that only exists in the source tree. This walks the real
# operator path through the real CLI and fails loudly if any step regresses.
#
# It writes only to its own temp directory and never touches config/ or var/.
set -euo pipefail

BIN="${AIWORKER_BIN:-aiworker}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

cat > "$WORK/config.yaml" <<YAML
timezone: Asia/Tokyo
state_dir: $WORK/state
db_path: $WORK/state/aiworker.db
log_dir: $WORK/state/logs
dry_run: true
platforms:
  x:
    enabled: true
    publisher: manual
    daily_limit: 5
    weekly_limit: 20
    min_interval_minutes: 60
    active_hours: [0, 24]
generation:
  provider: mock
  themes: [スモークテスト用テーマ, 二つ目のテーマ]
notify:
  channels: []
  min_level: critical
YAML
export AIWORKER_CONFIG="$WORK/config.yaml"
export AIWORKER_ACTOR="smoke-test"

step "version / help"
$BIN --version
$BIN --help > /dev/null

step "doctor"
$BIN doctor || fail "doctor が異常終了しました"

step "generate"
$BIN generate --channel social_post --count 3 > "$WORK/gen.txt"
grep -q "pending" "$WORK/gen.txt" || { cat "$WORK/gen.txt"; fail "生成されませんでした"; }

step "guardrails block a policy violation"
$BIN review edit 1 --body "この方法なら絶対に稼げます。詳細はこちら https://px.a8.net/x/abc" > /dev/null
$BIN review list --status blocked | grep -q "blocked" \
  || fail "規約違反がブロックされていません（ガードレールが効いていない）"

step "an approval must be refused while it is blocked"
if $BIN review approve 1 2>/dev/null | grep -q "^\[OK\]"; then
  fail "ブロック中の項目が --force なしで承認されました"
fi

step "approve / plan / publish"
$BIN review approve 2 3 | grep -q "OK" || fail "承認できませんでした"
$BIN plan | grep -q "scheduled" || fail "スケジュールされませんでした"
$BIN publish --at "2030-01-01T12:00:00+09:00" > "$WORK/pub.txt"
grep -q "ok" "$WORK/pub.txt" || { cat "$WORK/pub.txt"; fail "公開処理が失敗しました"; }

step "manual publishing lands on the worklist, not in 'published'"
$BIN outbox | grep -q "手動投稿待ち" || fail "outbox に出ていません"
$BIN status | grep -q "手動投稿待ち" || fail "status に出ていません"

step "kill switch stops publishing"
$BIN stop --reason "smoke test" > /dev/null
$BIN publish --at "2030-01-02T12:00:00+09:00" | grep -q "実行対象のジョブはありません" \
  || fail "停止中なのに公開処理が走りました"
$BIN resume --yes > /dev/null

step "revenue import + report"
# Today's date, because `report --days N` looks back from the real clock.
TODAY="$(date +%Y-%m-%d)"
printf '日付,プログラム名,報酬額,件数\n%s,テストPG,"1,234",2\n' "$TODAY" > "$WORK/rev.csv"
$BIN revenue import "$WORK/rev.csv" --source a8 | grep -q "1/1行を取り込みました" \
  || fail "収益CSVが取り込めませんでした"
$BIN report --days 30 | grep -q "1,234" || fail "レポートに収益が反映されていません"

step "metrics import feeds the reach-drop detector"
# Six normal days then a collapse: the detector must actually fire, otherwise
# the only guardrail that can notice a shadowban is decorative.
{
  echo "Date,impressions"
  for d in 01 02 03 04 05 06; do echo "2030-02-$d,4000"; done
  echo "2030-02-07,120"
} > "$WORK/reach.csv"
$BIN metrics import "$WORK/reach.csv" --platform x > "$WORK/metrics.txt" 2>&1 \
  || { cat "$WORK/metrics.txt"; fail "実績CSVが取り込めませんでした"; }
grep -q "リーチ" "$WORK/metrics.txt" \
  || { cat "$WORK/metrics.txt"; fail "リーチ急減が検知されていません"; }

step "an unreadable CSV must fail, not pass quietly"
printf 'foo,bar\n1,2\n' > "$WORK/bad.csv"
if $BIN metrics import "$WORK/bad.csv" --platform x > /dev/null 2>&1; then
  fail "読めないCSVが成功扱いになりました（cronで黙って失敗し続けます）"
fi

step "checklist"
$BIN checklist | grep -q "チェックリスト" || fail "チェックリストが表示されません"

printf '\n\033[32m✓ スモークテスト通過\033[0m\n'
