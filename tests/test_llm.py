import pytest

from app.generate import _clean_question, chunk_text
from app.grade import check_synonym, grade_free_text, grade_multiple_choice
from app.llm import LLMError, LLMProvider, LLMRouter
from app.llm.router import build_provider


class FakeProvider(LLMProvider):
    def __init__(self, name, answers, is_cloud=False):
        super().__init__(name, "fake")
        self.answers = list(answers)
        self.is_cloud = is_cloud
        self.calls = 0

    async def complete_json(self, system, prompt, schema, max_tokens=4000):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


SCHEMA = {"type": "object", "required": ["score", "feedback", "missing"]}
GOOD = {"score": 2, "feedback": "Bra!", "missing": ""}


async def test_router_falls_back_to_next_provider():
    cloud = FakeProvider("cloud", [LLMError("down")], is_cloud=True)
    local = FakeProvider("local", [GOOD])
    router = LLMRouter({"cloud": cloud, "local": local}, {"grade": ["cloud", "local"]})
    assert await router.complete_json("grade", "s", "p", SCHEMA) == GOOD
    assert router.last_used["grade"] == "local"


async def test_router_retries_incomplete_json_once():
    local = FakeProvider("local", [{"score": 2}, GOOD])
    router = LLMRouter({"local": local}, {"grade": ["local"]})
    assert await router.complete_json("grade", "s", "p", SCHEMA) == GOOD
    assert local.calls == 2


async def test_router_skips_cloud_when_not_allowed():
    cloud = FakeProvider("cloud", [GOOD], is_cloud=True)
    router = LLMRouter({"cloud": cloud}, {"grade": ["cloud"]}, allow_cloud=False)
    with pytest.raises(LLMError):
        await router.complete_json("grade", "s", "p", SCHEMA)
    assert cloud.calls == 0


def test_build_provider_types():
    assert build_provider("a", {"type": "ollama", "model": "m", "base_url": "http://x"}).model == "m"
    assert build_provider("b", {"type": "openai_compatible", "model": "m"}).base_url.endswith("/v1")
    assert build_provider("c", {"type": "anthropic", "model": "claude-opus-5"}).is_cloud
    with pytest.raises(ValueError):
        build_provider("d", {"type": "nope", "model": "m"})


async def test_free_text_grading_escapes_feedback():
    router = LLMRouter({"g": FakeProvider("g", [{"score": 1, "feedback": "Nästan <b>", "missing": "x"}])}, {"grade": ["g"]})
    grade = await grade_free_text(router, {"prompt": "Q", "answer": "A"}, "svar", {"age": 14}, "SO")
    assert grade.score == 1 and grade.feedback == "Nästan &lt;b&gt;"


async def test_synonym_rejected_shows_the_list_answer():
    router = LLMRouter({"g": FakeProvider("g", [{"accepted": False, "feedback": "nej"}])}, {"vocab_synonym": ["g"]})
    grade = await check_synonym(router, "huset", "la casa", "el hogar", "es")
    assert grade.score == 0 and "la casa" in grade.feedback


def test_multiple_choice():
    assert grade_multiple_choice("Storbritannien", "Storbritannien").score == 2
    assert grade_multiple_choice("Frankrike", "Storbritannien").score == 0


def test_chunking_keeps_all_text():
    text = "\n\n".join(f"Stycke {i} " + "x" * 500 for i in range(60))
    chunks = chunk_text(text, size=5000)
    assert len(chunks) > 1 and all(len(c) <= 5000 for c in chunks)
    assert sum(c.count("Stycke") for c in chunks) == 60


def test_clean_question_fixes_multiple_choice():
    q = _clean_question({"kind": "mc", "prompt": "P?", "answer": "A", "options": ["B", "C", "D"], "rubric": "", "source_ref": ""})
    assert q.kind == "mc" and "A" in q.options and len(q.options) == 3
    short = _clean_question({"kind": "mc", "prompt": "P?", "answer": "A", "options": [], "rubric": "", "source_ref": ""})
    assert short.kind == "short" and short.options == []
    assert _clean_question({"kind": "mc", "prompt": "", "answer": "A", "options": [], "rubric": "", "source_ref": ""}) is None
