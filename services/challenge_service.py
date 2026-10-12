"""Firestore layer for Buzzpoint Board challenges and runs.

Thin orchestration over the pure state machine in ``core/challenge.py`` and grading in
``core/grading.py``, written to mirror ``services/auth_service.py`` (same firebase-admin
client, same code-allocation retry, same error mapping). The board is server-authoritative:
this module — not the browser — grades answers and is the only writer of run documents, so
a modified client cannot forge a result or spend a second attempt.

Storage layout:
  challenges/{code}          readable metadata (ownerUid, title, counts, rules)
  challenge_banks/{code}     the packet INCLUDING answers — server-only, never served
  challenges/{code}/runs/{uid}   one run per member; written only here

Members are served one question at a time with its answer stripped (``next_question``),
so the answer key is never present in a member's browser.
"""

import time

from firebase_admin import firestore

from core.challenge import (
    apply_bonus_to_record,
    advance_after_bonus,
    advance_after_tossup,
    build_tossup_record,
    create_run_doc,
    join_outcome,
    next_question_payload,
)
from core.grading import grade_bonus, grade_tossup
from core.words import tokenize_words
from core.validation import validate_question_bank
from services.auth_service import (
    _firebase_app,
    _is_room_code_taken,
    generate_room_code,
)

# Answers live only in the server-only bank collection, never in the readable challenge.
CHALLENGES = "challenges"
BANKS = "challenge_banks"
RUNS = "runs"

RULES_SNAPSHOT = {
    "tossupWindowSeconds": 5.0,
    "bonusWindowSeconds": 20.0,
    "attemptLimit": 1,
    "scoring": "buzz-word only, no points",
}


def _client():
    return firestore.client(_firebase_app())


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_title(title):
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise ValueError("Challenge title must be 1 to 120 characters")
    if any(ord(char) < 32 for char in title):
        raise ValueError("Challenge title cannot contain control characters")
    return title.strip()


def _validate_display_name(name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise ValueError("Display name must be 1 to 64 characters")
    if any(ord(char) < 32 for char in name):
        raise ValueError("Display name cannot contain control characters")
    return name.strip()


def _packet_summary(bank):
    counts = {}
    for question in bank:
        subject = question.get("category") or "GENERAL"
        counts[subject] = counts.get(subject, 0) + 1
    return {"subjects": counts, "count": len(bank)}


# --------------------------------------------------------------------------- create

def create_challenge(owner_uid, title, bank):
    """Store a packet server-only and open a challenge under a fresh code."""
    if not isinstance(owner_uid, str) or not owner_uid:
        raise ValueError("Challenge owner must be a signed-in user")
    title = _validate_title(title)
    bank = validate_question_bank(bank)

    client = _client()
    last_error = None
    for _ in range(5):
        code = generate_room_code()
        challenge_ref = client.collection(CHALLENGES).document(code)
        try:
            challenge_ref.create({
                "ownerUid": owner_uid,
                "title": title,
                "createdAt": time.time(),
                "questionCount": len(bank),
                "packetSummary": _packet_summary(bank),
                "rulesSnapshot": RULES_SNAPSHOT,
            })
        except Exception as exc:  # noqa: BLE001 - collision vs service failure
            if _is_room_code_taken(exc):
                last_error = exc
                continue
            raise RuntimeError("Challenge service is unavailable") from exc
        # The bank holds the answers, so it lives where clients cannot read it.
        client.collection(BANKS).document(code).set({"bank": bank, "createdAt": time.time()})
        return {"code": code, "title": title, "questionCount": len(bank)}
    raise RuntimeError("Could not allocate a free challenge code — try again") from last_error


def get_challenge_meta(code):
    """Readable challenge metadata (no bank, no answers)."""
    try:
        snapshot = _client().collection(CHALLENGES).document(code).get(timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Challenge service is unavailable") from exc
    if not snapshot.exists:
        raise PermissionError("That challenge code does not exist")
    data = snapshot.to_dict() or {}
    return {
        "code": code,
        "title": data.get("title", ""),
        "ownerUid": data.get("ownerUid", ""),
        "questionCount": data.get("questionCount", 0),
        "packetSummary": data.get("packetSummary", {}),
        "rulesSnapshot": data.get("rulesSnapshot", RULES_SNAPSHOT),
    }


def _get_bank(code):
    try:
        snapshot = _client().collection(BANKS).document(code).get(timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Challenge service is unavailable") from exc
    if not snapshot.exists:
        raise PermissionError("That challenge packet is missing")
    bank = (snapshot.to_dict() or {}).get("bank")
    if not isinstance(bank, list) or not bank:
        raise PermissionError("That challenge packet is empty")
    return bank


def _run_ref(code, uid):
    return _client().collection(CHALLENGES).document(code).collection(RUNS).document(uid)


def _get_run(code, uid):
    try:
        snapshot = _run_ref(code, uid).get(timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Challenge service is unavailable") from exc
    return snapshot.to_dict() if snapshot.exists else None


# ----------------------------------------------------------------------------- join

def join_challenge(code, uid, display_name):
    """Claim the member's single attempt, or resume the one already in progress.

    The run document id is the member's uid and is created with ``create()``, so a second
    run for the same member cannot exist. A finished attempt refuses a re-join: that is
    the server-side lock the board depends on.
    """
    display_name = _validate_display_name(display_name)
    get_challenge_meta(code)  # 404-ish if the code is unknown
    existing = _get_run(code, uid)
    action, run = join_outcome(existing)
    if action == "refuse":
        raise PermissionError("You have already used your attempt on this challenge")
    if action == "resume":
        return {"status": "resumed", "run": run}
    run = create_run_doc(code, uid, display_name, get_challenge_meta(code)["questionCount"], _now_iso())
    try:
        _run_ref(code, uid).create(run)
    except Exception as exc:  # noqa: BLE001 - lost a race; treat as already claimed
        if _is_room_code_taken(exc):
            raise PermissionError("You have already used your attempt on this challenge") from exc
        raise RuntimeError("Challenge service is unavailable") from exc
    return {"status": "started", "run": run}


# ------------------------------------------------------------------------ gameplay

def next_question(code, uid):
    """Serve the member's current question with its answer stripped."""
    bank = _get_bank(code)
    run = _get_run(code, uid)
    if not run:
        raise PermissionError("Join this challenge before playing")
    return next_question_payload(bank, run)


def submit_answer(code, uid, q_index, phase, typed_answer="", buzz=None,
                  timing=None, expired=False, outcome="answered"):
    """Grade one typed answer on the server and advance the run.

    ``buzz`` is the client's captured buzz-point (word + position + capture source);
    ``timing`` carries reactionSeconds / firstKeystrokeMs / submitMs. Neither is trusted
    for grading — grading is purely the typed answer against the packet's answer.
    """
    bank = _get_bank(code)
    run = _get_run(code, uid)
    if not run:
        raise PermissionError("Join this challenge before playing")
    if run.get("status") == "finished":
        raise PermissionError("This attempt is already finished")
    try:
        q_index = int(q_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid question index") from exc
    if q_index != int(run.get("cursor", 0)) or phase != run.get("phase"):
        raise ValueError("That is not the question in play")
    if q_index < 0 or q_index >= len(bank):
        raise ValueError("Invalid question index")

    question = bank[q_index]
    expired = bool(expired)
    if phase == "bonus":
        correct = False if expired else grade_bonus(question, typed_answer)
        record = run["questions"][q_index] if run["questions"] else None
        if record is None:
            raise ValueError("No toss-up to attach this bonus to")
        apply_bonus_to_record(record, typed_answer, "auto-correct" if correct else "auto-wrong", expired)
        advance_after_bonus(run, len(bank), _now_iso())
        answer_text = question.get("bonus_answer", "")
    else:
        correct = False if expired else grade_tossup(question, typed_answer)
        record = build_tossup_record(
            question, q_index, buzz, typed_answer,
            "auto-correct" if correct else "auto-wrong",
            "expired" if expired else outcome,
            timing,
        )
        run["questions"].append(record)
        advance_after_tossup(run, correct, len(bank), _now_iso())
        answer_text = question.get("tossup_answer", "")

    try:
        _run_ref(code, uid).set(run)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Challenge service is unavailable") from exc

    return {
        "correct": correct,
        "verdict": record.get("verdict") if phase != "bonus" else record.get("bonusVerdict"),
        "phase": phase,
        "qIndex": q_index,
        "answer": answer_text,
        "next": next_question_payload(bank, run),
    }


# ----------------------------------------------------------------------------- board

def get_board(code, requester_uid):
    """The captain's compile: every run, plus the packet text for the buzz-line view."""
    meta = get_challenge_meta(code)
    if meta["ownerUid"] != requester_uid:
        raise PermissionError("Only the challenge captain can see the board")
    bank = _get_bank(code)
    try:
        snapshots = _run_ref(code, "__none__").parent.stream()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Challenge service is unavailable") from exc
    runs = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        runs.append(data)
    runs.sort(key=lambda item: str(item.get("displayName", "")).lower())
    return {
        "meta": meta,
        # The captain uploaded the packet, so they may see its text (and answers).
        "questions": bank,
        "runs": runs,
    }


# -------------------------------------------------------------------------- dev seed

def seed_dev_runs(code, requester_uid):
    """Fabricate two finished runs so the board is verifiable without real accounts.

    Development only: refused outside a non-production environment. Kept out of the
    production path entirely by the route as well.
    """
    meta = get_challenge_meta(code)
    if meta["ownerUid"] != requester_uid:
        raise PermissionError("Only the challenge captain can seed this board")
    bank = _get_bank(code)
    now = _now_iso()

    def _buzz_for(question, depth, source):
        words = tokenize_words(question.get("tossup_text", ""))
        if not words:
            return {"word": None, "wordIndex": None, "charIndex": None,
                    "wordsHeard": 0, "totalWords": 0, "captureSource": source,
                    "lastConfirmedWord": None, "confidence": "no words"}
        index = min(len(words) - 1, max(0, int(depth * len(words))))
        chosen = words[index]
        return {
            "word": chosen["word"],
            "wordIndex": index,
            "charIndex": chosen["start"],
            "wordsHeard": index + 1,
            "totalWords": len(words),
            "captureSource": source,
            "lastConfirmedWord": words[index - 1]["word"] if index > 0 else None,
            "confidence": source,
        }

    profiles = [
        {"uid": "dev-ava", "displayName": "Ava Chen", "depths": [0.12, 0.2, 0.15, 0.35, 0.18],
         "sources": ["boundary", "boundary", "counted", "boundary", "estimated"],
         "wrong": {2}},
        {"uid": "dev-ben", "displayName": "Ben Ortiz", "depths": [0.75, 0.6, 0.8, 0.4, 0.7],
         "sources": ["counted", "boundary", "boundary", "estimated", "counted"],
         "wrong": {0, 4}},
    ]

    for profile in profiles:
        run = create_run_doc(code, profile["uid"], profile["displayName"], len(bank), now)
        for position, question in enumerate(bank):
            depth = profile["depths"][position % len(profile["depths"])]
            source = profile["sources"][position % len(profile["sources"])]
            buzz = _buzz_for(question, depth, source)
            wrong = position in profile["wrong"]
            record = build_tossup_record(
                question, position, buzz,
                "" if wrong else (question.get("tossup_answer", "")).split("\n")[0][:40],
                "auto-wrong" if wrong else "auto-correct",
                "answered",
                {"reactionSeconds": round(depth * 8.0, 2),
                 "firstKeystrokeMs": int(400 + depth * 900),
                 "submitMs": int(1200 + depth * 1800)},
            )
            if not wrong:
                apply_bonus_to_record(record, question.get("bonus_answer", "")[:40],
                                      "auto-correct", False)
            run["questions"].append(record)
            advance_after_tossup(run, not wrong, len(bank), now)
            if not wrong:
                advance_after_bonus(run, len(bank), now)
        run["status"] = "finished"
        run["finishedAt"] = now
        _run_ref(code, profile["uid"]).set(run)
    return {"seeded": len(profiles)}
