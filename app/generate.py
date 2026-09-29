"""Uses the language model to classify material, summarize it and write questions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .llm import LLMRouter

MAX_CHARS_PER_CALL = 12000  # keeps prompts inside a local 12B model's context

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "topic": {"type": "string"},
        "test_date": {"type": "string", "description": "YYYY-MM-DD or empty"},
        "is_word_list": {"type": "boolean"},
    },
    "required": ["subject", "topic", "test_date", "is_word_list"],
    "additionalProperties": False,
}

QUESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["mc", "short", "explain"]},
                    "prompt": {"type": "string"},
                    "answer": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "rubric": {"type": "string"},
                    "source_ref": {"type": "string"},
                },
                "required": ["kind", "prompt", "answer", "options", "rubric", "source_ref"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "questions"],
    "additionalProperties": False,
}

SYSTEM_WRITER = (
    "You help a Swedish student in grade {grade} (age {age}) study for school tests. "
    "Write everything the student sees in clear, natural Swedish at högstadiet level. "
    "Use only facts that are in the material you are given. Never invent facts."
)


@dataclass
class Classification:
    subject: str
    topic: str
    test_date: date | None
    is_word_list: bool


@dataclass
class GeneratedQuestion:
    kind: str
    prompt: str
    answer: str
    options: list[str]
    rubric: str
    source_ref: str


def chunk_text(text: str, size: int = MAX_CHARS_PER_CALL) -> list[str]:
    paragraphs = re.split(r"\n\s*\n", text)
    chunks, current = [], ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 > size and current:
            chunks.append(current)
            current = ""
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        chunks.append(current)
    return chunks


def parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


async def classify(router: LLMRouter, text: str, subjects: list[str], today: date) -> Classification:
    prompt = (
        f"Today is {today.isoformat()}. Known school subjects: {', '.join(subjects) or 'none yet'}.\n"
        "Read the school material below and answer:\n"
        "- subject: which subject it belongs to (reuse a known subject name when it fits)\n"
        "- topic: a short Swedish title for the topic, for example 'Industriella revolutionen'\n"
        "- test_date: the test date if the material mentions one, as YYYY-MM-DD, else empty\n"
        "- is_word_list: true if it is mainly a vocabulary list of word pairs\n\n"
        f"MATERIAL:\n{text[:6000]}"
    )
    result = await router.complete_json("classify", "You sort school material for a Swedish student.", prompt, CLASSIFY_SCHEMA, 500)
    return Classification(
        subject=result["subject"].strip() or "Övrigt",
        topic=result["topic"].strip() or "Nytt område",
        test_date=parse_date(result.get("test_date")),
        is_word_list=bool(result.get("is_word_list")),
    )


async def generate_questions(router: LLMRouter, subject: str, topic: str, text: str,
                             student: dict, per_chunk: int = 12) -> tuple[str, list[GeneratedQuestion]]:
    system = SYSTEM_WRITER.format(grade=student.get("grade", 8), age=student.get("age", 14))
    summaries, questions = [], []
    chunks = chunk_text(text)
    for index, chunk in enumerate(chunks, 1):
        prompt = (
            f"Subject: {subject}. Topic: {topic}. Part {index} of {len(chunks)}.\n\n"
            f"Write a short Swedish summary (3-6 sentences) of this part, then {per_chunk} quiz questions in Swedish:\n"
            "- About half 'mc' (multiple choice): exactly 3 short options, one of them identical to 'answer'.\n"
            "- Some 'short': a fact answered in a few words. 'options' must be an empty list.\n"
            "- A few 'explain': the student explains in their own words. 'options' must be an empty list.\n"
            "- 'answer' is the model answer. 'rubric' says in Swedish what a full answer must contain "
            "(for 'explain': 2-3 key points).\n"
            "- 'source_ref' points to where in the material the answer is, e.g. 'Sida 42' or a heading.\n"
            "- Cover the most important content for a test, from easy to harder.\n\n"
            f"MATERIAL:\n{chunk}"
        )
        result = await router.complete_json("generate_questions", system, prompt, QUESTIONS_SCHEMA, 6000)
        summaries.append(result["summary"].strip())
        for item in result["questions"]:
            question = _clean_question(item)
            if question:
                questions.append(question)
    return "\n\n".join(summaries), questions


def _clean_question(item: dict) -> GeneratedQuestion | None:
    kind = item.get("kind")
    prompt = (item.get("prompt") or "").strip()
    answer = (item.get("answer") or "").strip()
    if kind not in {"mc", "short", "explain"} or not prompt or not answer:
        return None
    options = [o.strip() for o in item.get("options") or [] if o and o.strip()]
    if kind == "mc":
        if answer not in options:
            options = options[:2] + [answer]
        if len(options) < 2:
            kind, options = "short", []
    else:
        options = []
    return GeneratedQuestion(kind, prompt, answer, options, (item.get("rubric") or "").strip(),
                             (item.get("source_ref") or "").strip())
