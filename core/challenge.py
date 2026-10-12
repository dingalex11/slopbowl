"""Pure state machine for a Buzzpoint Board run.

No Firestore or network here: these functions take and return plain dicts so the
attempt lock, one-question-at-a-time stripping and run progression can be unit-tested
without credentials. ``services/challenge_service.py`` is the thin Firestore layer on top.

A run is one document per member per challenge. Its ``questions`` array holds one flat
record per toss-up (bonuses fold into the same record), matching the board's spec:

    buzzWord, buzzWordIndex, buzzCharIndex, wordsHeard, totalWords, captureSource,
    lastConfirmedWord, confidence, reactionSeconds, firstKeystrokeMs, submitMs,
    typedAnswer, verdict, bonusTypedAnswer, bonusVerdict, bonusExpired

The answers are stripped from anything served to a member, so a member's browser only
ever holds the question in front of them — never the packet's answer key.
"""

from core.subjects import subject_for

# A question's answer fields must never reach a member's browser before they answer.
_ANSWER_FIELDS = {"tossup_answer", "bonus_answer"}


def create_run_doc(code, uid, display_name, question_count, now_iso):
    """The single run document a member claims. Creating it is what locks the attempt."""
    return {
        "challengeCode": code,
        "uid": uid,
        "displayName": display_name,
        "startedAt": now_iso,
        "finishedAt": None,
        "questionCount": int(question_count),
        "cursor": 0,
        "phase": "tossup",
        "status": "in_progress",
        "questions": [],
    }


def join_outcome(existing_run):
    """Decide what a join means given any run this uid already holds.

    Returns ``(action, run)`` where action is ``"create"`` (no run yet), ``"resume"``
    (an in-progress attempt — the same attempt, not a second one), or ``"refuse"`` (the
    attempt is spent and the member may not take it again).
    """
    if not existing_run:
        return "create", None
    if existing_run.get("status") == "finished":
        return "refuse", existing_run
    return "resume", existing_run


def strip_question(question, phase):
    """Serve one question's text with its answer removed.

    The toss-up and its bonus are separate phases, so a bonus is only ever served after
    the toss-up that earned it. Options and the visual flag are safe to send; the answer
    is not.
    """
    question = question or {}
    if phase == "bonus":
        return {
            "text": question.get("bonus_text", ""),
            "options": list(question.get("bonus_options") or []),
            "visual": bool(question.get("bonus_visual", False)),
        }
    return {
        "text": question.get("tossup_text", ""),
        "options": list(question.get("tossup_options") or []),
        "visual": bool(question.get("tossup_visual", False)),
    }


def next_question_payload(bank, run):
    """The next question a member may see, or ``{"done": True}`` at the end of the packet."""
    bank = bank or []
    cursor = int(run.get("cursor", 0))
    if cursor >= len(bank):
        return {"done": True}
    phase = run.get("phase", "tossup")
    payload = strip_question(bank[cursor], phase)
    payload.update({
        "done": False,
        "qIndex": cursor,
        "phase": phase,
        "totalQuestions": len(bank),
        "category": bank[cursor].get("category", ""),
        "subject": subject_for(bank[cursor].get("category")),
        "type": bank[cursor].get("type", ""),
    })
    return payload


def build_tossup_record(question, q_index, buzz, typed_answer, verdict, outcome, timing):
    """One flat record for a toss-up. ``buzz`` may be None when the question expired."""
    question = question or {}
    buzz = buzz or {}
    timing = timing or {}
    return {
        "qIndex": int(q_index),
        "category": question.get("category", ""),
        "subject": subject_for(question.get("category")),
        "type": question.get("type", ""),
        "buzzWord": buzz.get("word"),
        "buzzWordIndex": buzz.get("wordIndex"),
        "buzzCharIndex": buzz.get("charIndex"),
        "wordsHeard": buzz.get("wordsHeard"),
        "totalWords": buzz.get("totalWords"),
        "captureSource": buzz.get("captureSource"),
        "lastConfirmedWord": buzz.get("lastConfirmedWord"),
        "confidence": buzz.get("confidence"),
        "reactionSeconds": timing.get("reactionSeconds"),
        "firstKeystrokeMs": timing.get("firstKeystrokeMs"),
        "submitMs": timing.get("submitMs"),
        "typedAnswer": typed_answer,
        "outcome": outcome,
        "verdict": verdict,
        "bonusTypedAnswer": None,
        "bonusVerdict": None,
        "bonusExpired": None,
    }


def apply_bonus_to_record(record, typed_answer, verdict, expired):
    """Fold a bonus result into the toss-up record that earned it."""
    record["bonusTypedAnswer"] = typed_answer
    record["bonusVerdict"] = verdict
    record["bonusExpired"] = bool(expired)
    return record


def _close_if_done(run, total, now_iso):
    if int(run.get("cursor", 0)) >= int(total):
        run["status"] = "finished"
        run["finishedAt"] = now_iso
    return run


def advance_after_tossup(run, correct, total, now_iso):
    """A correct toss-up earns the bonus on the same question; otherwise move on."""
    if correct:
        run["phase"] = "bonus"
    else:
        run["cursor"] = int(run.get("cursor", 0)) + 1
        run["phase"] = "tossup"
    return _close_if_done(run, total, now_iso)


def advance_after_bonus(run, total, now_iso):
    """A bonus closes its question either way; the packet moves to the next toss-up."""
    run["cursor"] = int(run.get("cursor", 0)) + 1
    run["phase"] = "tossup"
    return _close_if_done(run, total, now_iso)
