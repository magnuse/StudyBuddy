from datetime import date

from app.captions import parse_caption, parse_date

TODAY = date(2026, 9, 28)  # a Monday


def test_subject_and_date_from_caption():
    caption = parse_caption("SO, prov 10 okt", ["SO", "NO"], TODAY)
    assert caption.subject == "SO"
    assert caption.test_date == date(2026, 10, 10)


def test_word_list_caption():
    caption = parse_caption("Spanska glosor", [], TODAY)
    assert caption.subject == "Spanska" and caption.is_word_list


def test_title_after_comma():
    caption = parse_caption("SO: Industriella revolutionen, prov fredag", ["SO"], TODAY)
    assert caption.subject == "SO"
    assert caption.test_date == date(2026, 10, 2)


def test_known_subject_matched_case_insensitively():
    assert parse_caption("veckoplanering no", ["NO"], TODAY).subject == "NO"


def test_date_formats():
    assert parse_date("prov 2026-10-09", TODAY) == date(2026, 10, 9)
    assert parse_date("prov 9/10", TODAY) == date(2026, 10, 9)
    assert parse_date("glosförhör tisdag", TODAY) == date(2026, 9, 29)
    assert parse_date("prov 15 januari", TODAY) == date(2027, 1, 15)
    assert parse_date("inget datum", TODAY) is None


def test_weekday_only_caption_is_not_a_subject():
    assert parse_caption("prov fredag", [], TODAY).subject is None
