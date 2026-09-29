from app.vocab import Verdict, check_answer, parse_word_list


def test_parse_common_formats():
    text = """1. la casa - huset
    el coche / el carro = bilen
    tener hambre – vara hungrig
    la canción\tsången

    rubbish line without separator
    """
    pairs = parse_word_list(text)
    assert [(p.term, p.translation) for p in pairs] == [
        ("la casa", "huset"), ("el coche", "bilen"), ("tener hambre", "vara hungrig"), ("la canción", "sången"),
    ]
    assert pairs[1].term_alts == ["el carro"]


def test_exact_answer_is_correct_ignoring_case_and_punctuation():
    assert check_answer("  La Casa! ", ["la casa"], "es").verdict == Verdict.CORRECT


def test_alternative_form_is_correct():
    assert check_answer("el carro", ["el coche", "el carro"], "es").verdict == Verdict.CORRECT


def test_missing_accent_is_almost():
    result = check_answer("la cancion", ["la canción"], "es")
    assert result.verdict == Verdict.ALMOST and result.reason == "accent" and result.score == 2


def test_missing_accent_is_wrong_when_strict():
    assert check_answer("la cancion", ["la canción"], "es", strict_accents=True).verdict == Verdict.WRONG


def test_wrong_article_is_almost():
    result = check_answer("el casa", ["la casa"], "es")
    assert result.verdict == Verdict.ALMOST and result.reason == "article"


def test_missing_article_is_almost():
    assert check_answer("casa", ["la casa"], "es").reason == "article"


def test_swedish_infinitive_marker_is_optional():
    assert check_answer("äta", ["att äta"], "sv").verdict == Verdict.CORRECT


def test_small_typo_is_almost():
    result = check_answer("la biblioteka", ["la biblioteca"], "es")
    assert result.verdict == Verdict.ALMOST and result.reason == "typo"


def test_different_word_is_wrong():
    result = check_answer("el perro", ["la casa"], "es")
    assert result.verdict == Verdict.WRONG and result.score == 0
