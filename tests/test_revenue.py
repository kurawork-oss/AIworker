from __future__ import annotations

from aiworker.revenue import importer, report


def write(tmp_path, name, text, encoding="utf-8"):
    p = tmp_path / name
    p.write_text(text, encoding=encoding)
    return p


def test_japanese_column_names_are_understood(conn, tmp_path):
    path = write(tmp_path, "a8.csv",
                 "日付,プログラム名,報酬額,件数\n2026/09/15,テストPG,1234,2\n")
    result = importer.import_csv(conn, path, source="a8")
    assert result.imported == 1


def test_english_column_names_are_understood(conn, tmp_path):
    path = write(tmp_path, "en.csv", "date,amount,units\n2026-09-15,10.50,1\n")
    assert importer.import_csv(conn, path, source="stock", currency="USD").imported == 1


def test_thousands_separators_and_currency_symbols(conn, tmp_path):
    path = write(tmp_path, "y.csv", '日付,報酬額\n2026-09-15,"12,345円"\n')
    importer.import_csv(conn, path, source="a8")
    rep = report.revenue_report(conn, report.Period("2026-09-01", "2026-09-30", "t"))
    assert rep.total == 12345


def test_shift_jis_files_are_handled(conn, tmp_path):
    path = write(tmp_path, "sjis.csv", "日付,報酬額\n2026-09-15,500\n", encoding="cp932")
    assert importer.import_csv(conn, path, source="a8").imported == 1


def test_bad_rows_are_skipped_not_fatal(conn, tmp_path):
    path = write(tmp_path, "mixed.csv",
                 "日付,報酬額\n2026-09-15,1000\nよくわからない行,abc\n2026-09-16,2000\n")
    result = importer.import_csv(conn, path, source="a8")
    assert result.imported == 2 and len(result.skipped) == 1


def test_missing_columns_report_what_was_expected(conn, tmp_path):
    path = write(tmp_path, "bad.csv", "foo,bar\n1,2\n")
    result = importer.import_csv(conn, path, source="a8")
    assert result.imported == 0 and "could not find" in result.skipped[0]


def test_reimporting_the_same_file_does_not_double_count(conn, tmp_path):
    path = write(tmp_path, "a8.csv", "日付,プログラム名,報酬額\n2026-09-15,PG,1000\n")
    importer.import_csv(conn, path, source="a8")
    importer.import_csv(conn, path, source="a8")
    rep = report.revenue_report(conn, report.Period("2026-09-01", "2026-09-30", "t"))
    assert rep.total == 1000


def test_missing_file_is_reported_not_raised(conn, tmp_path):
    result = importer.import_csv(conn, tmp_path / "nope.csv", source="a8")
    assert result.imported == 0 and "not found" in result.skipped[0]


def test_concentration_warning_fires(conn):
    importer.record_manual(conn, date="2026-09-15", source="a8", amount=9000)
    importer.record_manual(conn, date="2026-09-15", source="note", amount=500)
    rendered = report.revenue_report(conn, report.Period("2026-09-01", "2026-09-30", "t")).render()
    assert "集中" in rendered


def test_balanced_sources_do_not_warn(conn):
    for source in ("a8", "note", "youtube"):
        importer.record_manual(conn, date="2026-09-15", source=source, amount=1000)
    rendered = report.revenue_report(conn, report.Period("2026-09-01", "2026-09-30", "t")).render()
    assert "集中" not in rendered


def test_ops_report_renders_without_data(conn, settings):
    text = report.ops_report(conn, settings, days=7)
    assert "承認キュー" in text and "投稿枠" in text


def test_ops_report_surfaces_an_active_halt(conn, settings):
    from aiworker.guard import killswitch

    killswitch.engage(conn, settings.state_dir, reason="停止テスト")
    assert "停止テスト" in report.ops_report(conn, settings)
