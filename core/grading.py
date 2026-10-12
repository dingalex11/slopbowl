"""Normalized answer grading for typed Buzzpoint Board responses.

A faithful port of ``soloJudgeAnswer`` / ``soloNormalizeText`` from ``static/js/solo.js``,
moved server-side because the Board grades on the server. The matching rules are the same
ones the solo drill already uses:

* exact match after normalization
* the reply contains the answer as a whole phrase
* every significant word (4+ chars) of the answer appears in the reply
* a single-word answer also accepts a 5-character opening, so a fast typist's
  ``mitocondria`` is not scored as a miss

Multiple-choice answers additionally accept either the letter (``W``) or the full phrase
(``STATIONARY``), because a packet writes them as ``W) STATIONARY``.

Normalization lowercases, folds smart quotes to a straight apostrophe, drops every
character outside ``[a-z0-9'\\s]``, and collapses whitespace — identical to solo.
"""

import re

# solo's normalization: fold smart quotes, then keep only [a-z0-9'\s], collapse, trim.
_SMART_QUOTES = re.compile(r"[\u2018\u2019\u201c\u201d]")
_NON_WORD = re.compile(r"[^a-z0-9'\s]")
_SPACES = re.compile(r"\s+")

# A multiple-choice answer line: a lone letter, a separator, then the phrase.
_MC_ANSWER = re.compile(r"^\s*[A-Za-z]\s*[).:\-]\s*\S")


def normalize_text(value):
    """Fold a typed response the same way solo does before matching."""
    text = "" if value is None else str(value)
    text = text.lower()
    text = _SMART_QUOTES.sub("'", text)
    text = _NON_WORD.sub(" ", text)
    text = _SPACES.sub(" ", text)
    return text.strip()


def judge_keyword(answer_text, response):
    """Solo's keyword matcher: exact, contained, all-significant-words, or 5-char prefix."""
    answer = normalize_text(answer_text)
    reply = normalize_text(response)
    if not reply or not answer:
        return False
    if reply == answer:
        return True
    if f" {answer} " in f" {reply} ":
        return True
    answer_words = [word for word in answer.split(" ") if word]
    reply_words = [word for word in reply.split(" ") if word]
    if len(answer_words) == 1:
        return len(answer) >= 6 and any(
            len(word) >= 5 and word.startswith(answer[:5]) for word in reply_words
        )
    significant = [word for word in answer_words if len(word) >= 4]
    if not significant:
        return all(word in reply_words for word in answer_words)
    return all(word in reply_words for word in significant)


def _is_multiple_choice(question, answer_text):
    if str((question or {}).get("type", "") or "").strip().upper() == "MC":
        return True
    return bool(_MC_ANSWER.match(answer_text or ""))


def _judge_multiple_choice(answer_text, response):
    """Accept the letter alone, the whole line, or the phrase on its own."""
    answer = normalize_text(answer_text)
    reply = normalize_text(response)
    if not reply or not answer:
        return False
    if reply == answer:
        return True
    parts = answer.split(" ", 1)
    letter = parts[0]
    phrase = parts[1] if len(parts) > 1 else ""
    if len(letter) == 1 and reply == letter:
        return True
    if phrase and (reply == phrase or judge_keyword(phrase, response)):
        return True
    return False


def grade_tossup(question, response):
    """Grade a typed toss-up answer against the question's ``tossup_answer``."""
    answer_text = str((question or {}).get("tossup_answer", "") or "")
    if _is_multiple_choice(question, answer_text):
        return _judge_multiple_choice(answer_text, response)
    return judge_keyword(answer_text, response)


def grade_bonus(question, response):
    """Grade a typed bonus answer against the question's ``bonus_answer``.

    Bonus type is not stored separately from the toss-up, so multiple-choice detection
    falls back to the answer line's shape (``W) STATIONARY``).
    """
    answer_text = str((question or {}).get("bonus_answer", "") or "")
    if _is_multiple_choice({"type": "MC"} if _MC_ANSWER.match(answer_text or "") else {}, answer_text):
        return _judge_multiple_choice(answer_text, response)
    return judge_keyword(answer_text, response)
