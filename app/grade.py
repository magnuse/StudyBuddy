"""Grades the student's answers. Multiple choice and vocabulary are checked in code; the rest by a model."""

from __future__ import annotations

import html
from dataclasses import dataclass

from .llm import LLMRouter

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": [0, 1, 2]},
        "feedback": {"type": "string"},
        "missing": {"type": "string"},
    },
    "required": ["score", "feedback", "missing"],
    "additionalProperties": False,
}

SYNONYM_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
    "required": ["accepted", "feedback"],
    "additionalProperties": False,
}

SYSTEM_GRADER = (
    "You grade a Swedish {age}-year-old's answers to school quiz questions and give short, encouraging "
    "feedback in Swedish. Judge the content, not the language: accept everyday wording, spelling mistakes "
    "and missing details that the rubric does not require.{spelling_note} Never scold. "
    "Feedback is at most 2 sentences. When something is missing, say what, and mention the source reference "
    "if one is given."
)


@dataclass
class Grade:
    score: int  # 0 wrong, 1 partly right, 2 right
    feedback: str  # Telegram HTML, already escaped


def grade_multiple_choice(answer: str, correct: str) -> Grade:
    if answer.strip().lower() == correct.strip().lower():
        return Grade(2, "Rätt!")
    return Grade(0, f"Inte riktigt. Rätt svar är: <b>{html.escape(correct)}</b>")


async def grade_free_text(router: LLMRouter, question: dict, answer: str, student: dict, subject: str) -> Grade:
    spelling_note = ""
    if subject.lower().startswith("svenska") or not student.get("lenient_spelling", True):
        spelling_note = " In this subject spelling matters, so do point out spelling mistakes in the feedback."
    system = SYSTEM_GRADER.format(age=student.get("age", 14), spelling_note=spelling_note)
    prompt = (
        f"Question: {question['prompt']}\n"
        f"Model answer: {question['answer']}\n"
        f"Rubric: {question.get('rubric') or 'The main point of the model answer.'}\n"
        f"Source reference: {question.get('source_ref') or '-'}\n"
        f"Student's answer: {answer}\n\n"
        "Score 2 if the answer covers the rubric, 1 if partly, 0 if wrong or empty. "
        "'missing' names what was missing, or is empty."
    )
    result = await router.complete_json("grade", system, prompt, GRADE_SCHEMA, 600)
    score = result["score"] if result["score"] in (0, 1, 2) else 0
    feedback = result["feedback"].strip() or ("Rätt!" if score == 2 else question["answer"])
    return Grade(score, html.escape(feedback))


async def check_synonym(router: LLMRouter, prompt_word: str, expected: str, answer: str, target_language: str) -> Grade:
    language_name = {"sv": "Swedish", "es": "Spanish", "en": "English", "de": "German", "fr": "French"}.get(
        target_language, target_language)
    prompt = (
        f"A student translated '{prompt_word}' into {language_name}.\n"
        f"The word list says: '{expected}'. The student wrote: '{answer}'.\n"
        "Is the student's answer also a correct translation in a school vocabulary test? "
        "Accept real synonyms and small spelling mistakes, not related but different words. "
        "Give one short sentence of feedback in Swedish."
    )
    result = await router.complete_json("vocab_synonym", "You check school vocabulary answers.", prompt, SYNONYM_SCHEMA, 300)
    if result["accepted"]:
        feedback = result["feedback"].strip() or f"Godkänt! Listan säger: {expected}"
        return Grade(2, html.escape(feedback))
    return Grade(0, f"Inte riktigt. Rätt svar: <b>{html.escape(expected)}</b>")
