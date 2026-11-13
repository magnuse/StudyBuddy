from app.vocab import Verdict, check_answer, expand_slashes, opposite_gender, parse_word_list


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


def test_expand_slashes():
    assert expand_slashes("cansado/a") == ["cansado", "cansada"]
    assert expand_slashes("el/la amigo/a") == ["el amigo", "la amiga"]
    assert expand_slashes("el/la profesor/a") == ["el profesor", "la profesora"]
    assert expand_slashes("un/una chico/a") == ["un chico", "una chica"]
    assert expand_slashes("la casa") == ["la casa"]


def test_opposite_gender_spanish():
    assert opposite_gender("el chico", "pojke") == ("la chica", "femininum", "flicka")
    assert opposite_gender("la chica", "flicka") == ("el chico", "maskulinum", "pojke")
    assert opposite_gender("cansado", "trött") == ("cansada", "femininum", "trött")
    assert opposite_gender("cansada", "trött") == ("cansado", "maskulinum", "trött")
    assert opposite_gender("el profesor", "lärare") == ("la profesora", "femininum", "lärare")
    assert opposite_gender("la mesa", "bordet") is None


def test_opposite_gender_treated_as_wrong_not_typo():
    res = check_answer("chico", ["la chica"], "es")
    assert res.verdict == Verdict.WRONG and res.reason == "gender"

    res = check_answer("el chico", ["la chica"], "es")
    assert res.verdict == Verdict.WRONG and res.reason == "gender"

    res = check_answer("cansado", ["cansada"], "es")
    assert res.verdict == Verdict.WRONG and res.reason == "gender"

    # Real typo (not opposite gender) is still ALMOST typo
    res = check_answer("la chika", ["la chica"], "es")
    assert res.verdict == Verdict.ALMOST and res.reason == "typo"

