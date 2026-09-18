"""Analytics import, and the schema migration that makes it idempotent."""

from __future__ import annotations

import sqlite3

import pytest

from aiworker.core import db
from aiworker.metrics import importer


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


# --------------------------------------------------------------------------
# the export formats these consoles actually produce
# --------------------------------------------------------------------------
def test_x_analytics_export(conn, tmp_path):
    path = write(tmp_path, "x.csv",
                 "Date,Tweet id,impressions,engagements\n"
                 "2026-09-15,1800000001,4210,86\n2026-09-16,1800000002,3980,71\n")
    result = importer.import_csv(conn, path, platform="x")
    assert result.imported == 2
    assert result.used_impressions_as_reach, "X exports impressions, not reach"
    assert db.daily_reach(conn, "x") == [("2026-09-15", 4210.0), ("2026-09-16", 3980.0)]


def test_youtube_export_in_shift_jis(conn, tmp_path):
    path = write(tmp_path, "yt.csv",
                 "日付,動画,インプレッション数,視聴回数\n"
                 "2026/09/12,abc,12000,860\n2026/09/13,def,11500,790\n",
                 encoding="cp932")
    assert importer.import_csv(conn, path, platform="youtube").imported == 2
    assert len(db.daily_reach(conn, "youtube")) == 2


def test_export_with_a_real_reach_column(conn, tmp_path):
    path = write(tmp_path, "th.csv", "日付,リーチ,ビュー\n2026-09-15,2400,5100\n")
    result = importer.import_csv(conn, path, platform="threads")
    assert not result.used_impressions_as_reach
    # reach wins over the larger impressions figure: mixing the two measures
    # across days would make the median comparison meaningless.
    assert db.daily_reach(conn, "threads") == [("2026-09-15", 2400.0)]


def test_reach_source_can_be_forced(conn, tmp_path):
    path = write(tmp_path, "th.csv", "日付,リーチ,ビュー\n2026-09-15,2400,5100\n")
    importer.import_csv(conn, path, platform="threads", reach_from="impressions")
    assert db.daily_reach(conn, "threads") == [("2026-09-15", 5100.0)]


def test_unknown_reach_source_is_rejected(conn, tmp_path):
    path = write(tmp_path, "th.csv", "日付,リーチ\n2026-09-15,1\n")
    with pytest.raises(ValueError, match="reach_from"):
        importer.import_csv(conn, path, platform="threads", reach_from="nonsense")


# --------------------------------------------------------------------------
# idempotency -- the reason the schema needed a migration
# --------------------------------------------------------------------------
def test_reimporting_the_same_export_does_not_double_the_numbers(conn, tmp_path):
    """`daily_reach` sums, and the detector compares a day against the median
    of the days before it. A double import reads as growth, and the day you
    stop re-importing reads as a collapse."""
    path = write(tmp_path, "x.csv",
                 "Date,Tweet id,impressions\n2026-09-15,1,4000\n2026-09-16,2,4200\n")
    importer.import_csv(conn, path, platform="x")
    first = db.daily_reach(conn, "x")
    importer.import_csv(conn, path, platform="x")
    assert db.daily_reach(conn, "x") == first


def test_reimporting_updated_figures_replaces_them(conn, tmp_path):
    """Analytics figures settle over a day or two; a re-export must win."""
    write(tmp_path, "x.csv", "Date,Tweet id,impressions\n2026-09-15,1,4000\n")
    importer.import_csv(conn, tmp_path / "x.csv", platform="x")
    write(tmp_path, "x.csv", "Date,Tweet id,impressions\n2026-09-15,1,6500\n")
    importer.import_csv(conn, tmp_path / "x.csv", platform="x")
    assert db.daily_reach(conn, "x") == [("2026-09-15", 6500.0)]


def test_platforms_do_not_share_rows(conn, tmp_path):
    path = write(tmp_path, "m.csv", "Date,impressions\n2026-09-15,1000\n")
    importer.import_csv(conn, path, platform="x")
    importer.import_csv(conn, path, platform="threads")
    assert db.daily_reach(conn, "x") == db.daily_reach(conn, "threads") == \
        [("2026-09-15", 1000.0)]


# --------------------------------------------------------------------------
# malformed input
# --------------------------------------------------------------------------
def test_missing_date_column_is_reported(conn, tmp_path):
    path = write(tmp_path, "bad.csv", "foo,bar\n1,2\n")
    result = importer.import_csv(conn, path, platform="x")
    assert result.imported == 0 and "日付の列が見つかりません" in result.skipped[0]


def test_missing_measure_column_is_reported(conn, tmp_path):
    path = write(tmp_path, "bad.csv", "日付,メモ\n2026-09-15,あ\n")
    result = importer.import_csv(conn, path, platform="x")
    assert result.imported == 0 and "見つかりません" in result.skipped[0]


def test_bad_rows_are_skipped_not_fatal(conn, tmp_path):
    path = write(tmp_path, "x.csv",
                 "Date,impressions\n2026-09-15,4000\nよくわからない,abc\n2026-09-16,4200\n")
    result = importer.import_csv(conn, path, platform="x")
    assert result.imported == 2 and len(result.skipped) == 1


def test_thousands_separators_and_dashes(conn, tmp_path):
    path = write(tmp_path, "x.csv",
                 'Date,impressions\n2026-09-15,"12,345"\n2026-09-16,-\n')
    importer.import_csv(conn, path, platform="x")
    assert db.daily_reach(conn, "x")[0] == ("2026-09-15", 12345.0)


def test_missing_file_is_reported_not_raised(conn, tmp_path):
    result = importer.import_csv(conn, tmp_path / "nope.csv", platform="x")
    assert result.imported == 0 and "ファイルが見つかりません" in result.skipped[0]


# --------------------------------------------------------------------------
# the migration itself
# --------------------------------------------------------------------------
def test_a_fresh_database_is_at_the_current_version(conn):
    assert db.current_schema_version(conn) == db.SCHEMA_VERSION


def test_duplicate_rows_are_rejected_after_migration(conn):
    db.upsert_metric(conn, date="2026-09-15", platform="x", impressions=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO metrics(date, platform, account, external_id, impressions, "
            "reach, clicks, note, created_at) VALUES('2026-09-15','x','main','',9,0,0,'','t')"
        )


def test_migrating_a_v1_database_dedupes_and_keeps_everything_else(tmp_path):
    """The database holds the approval history, so it is migrated in place,
    never recreated."""
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.executescript(db.SCHEMA)
    raw.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version','1')")
    for _ in range(2):  # the same export imported twice under v1
        raw.execute(
            "INSERT INTO metrics(date, platform, account, external_id, impressions, "
            "reach, clicks, note, created_at) VALUES('2026-09-15','x','main','',0,1000,0,'','t')"
        )
    raw.execute(
        "INSERT INTO content_items(uid, channel, platform, status, created_at, updated_at) "
        "VALUES('keep-me','social_post','x','approved','t','t')"
    )
    raw.commit()
    raw.close()

    conn = db.init_db(path)
    try:
        assert db.current_schema_version(conn) == db.SCHEMA_VERSION
        assert db.daily_reach(conn, "x") == [("2026-09-15", 1000.0)], "double count survived"
        assert [i.uid for i in db.list_items(conn)] == ["keep-me"], "content was lost"
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    for _ in range(3):
        conn = db.init_db(path)
        assert db.current_schema_version(conn) == db.SCHEMA_VERSION
        conn.close()
