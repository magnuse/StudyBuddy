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


def _fake_ollama(monkeypatch, handler):
    import httpx

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def test_ollama_names_a_model_that_is_not_downloaded(monkeypatch):
    import httpx
    import pytest

    from app.llm.base import LLMError
    from app.llm.ollama import OllamaProvider

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:4b"}]})
        return httpx.Response(404, json={"error": "model 'gemma3:12b' not found"})

    _fake_ollama(monkeypatch, handler)
    writer = OllamaProvider("writer", "gemma3:12b", "http://ollama:11434")
    with pytest.raises(LLMError, match="gemma3:12b is not downloaded.*ollama pull gemma3:12b"):
        await writer.complete_json("s", "p", {"type": "object"})
    assert await writer.model_available() is False
    assert await OllamaProvider("grader", "gemma3:4b", "http://ollama:11434").model_available() is True


async def test_ollama_unreachable_is_not_reported_as_missing(monkeypatch):
    import httpx

    from app.llm.ollama import OllamaProvider

    def handler(request):
        raise httpx.ConnectError("down")

    _fake_ollama(monkeypatch, handler)
    assert await OllamaProvider("writer", "gemma3:12b").model_available() is None


async def test_startup_lists_models_the_config_uses_but_ollama_lacks(monkeypatch):
    import httpx
    from types import SimpleNamespace

    from app.llm.ollama import OllamaProvider
    from app.main import missing_local_models

    _fake_ollama(monkeypatch, lambda request: httpx.Response(200, json={"models": [{"name": "gemma3:4b"}]}))
    providers = {"writer": OllamaProvider("writer", "gemma3:12b"), "grader": OllamaProvider("grader", "gemma3:4b"),
                 "unused": OllamaProvider("unused", "llama3")}
    router = LLMRouter(providers, {"generate_questions": ["writer"], "grade": ["grader"]})
    assert await missing_local_models(SimpleNamespace(router=router)) == ["gemma3:12b"]


async def test_ollama_gets_room_for_prompt_and_answer_and_cut_answers_are_retried(monkeypatch):
    import json

    import httpx

    from app.llm.ollama import OllamaProvider

    bodies = []
    replies = [{"message": {"content": '{"summary": "x", "questions": [{"prompt'}, "done_reason": "length"},
               {"message": {"content": '{"summary": "ok", "questions": []}'}, "done_reason": "stop"}]

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    _fake_ollama(monkeypatch, handler)
    writer = OllamaProvider("writer", "gemma3:12b")
    router = LLMRouter({"writer": writer}, {"generate_questions": ["writer"]})
    schema = {"type": "object", "required": ["summary", "questions"]}
    result = await router.complete_json("generate_questions", "system", "x" * 12000, schema, 6000)
    assert result == {"summary": "ok", "questions": []}
    assert len(bodies) == 2  # the cut-off answer was retried
    num_ctx = bodies[0]["options"]["num_ctx"]
    assert 12000 // 3 + 6000 <= num_ctx <= 16384 and num_ctx % 1024 == 0
    assert writer.context_size("s", "short", 500) == 1024
