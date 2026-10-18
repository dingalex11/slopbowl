"""Normalized answer grading for typed Buzzpoint Board responses.

The Board grades on the server, so the rules live here. The solo drill keeps a mirrored
copy of the same algorithm in ``static/js/solo.js`` (``soloJudgeAnswer``); the two are
deliberately kept in step.

A packet line may carry alternatives — ``6,000 (ACCEPT: 6 x 10^3)`` — and by packet
convention every listed alternative is an equally correct answer. A line may also list its
answers with a semicolon (``MACH; KNOTS``). Grading is therefore *candidate based*: the base
answer and every listed alternative are tried in turn, and any one of them matching makes
the reply correct. Parentheticals that are not ``ACCEPT`` groups are annotations —
``(in any order)``, ``(Solution: ...)`` — and are never part of the answer.

Matching, in order:

* the reply is the printed answer line itself
* exact match after normalization (case, punctuation and spacing slide)
* numerical equality, so ``6,000``, ``6000`` and ``6 x 10^3`` are the same answer, with or
  without the unit, since the NSB appendix does not require units to be stated
* the reply contains the answer as a whole phrase
* every significant word (4+ characters) of the answer appears in the reply
* bounded typo tolerance and regular plurals, so a fast typist's ``mitocondria`` is not
  scored as a miss while an unrelated word of the same length is not accepted for it

Multiple choice follows NSB Rules 3-3 and 3-6. The answer is written ``W) STATIONARY``, and
a reply may give the letter (``W``) or the choice text as the moderator read it, which must
be exact apart from case and punctuation; when both are given, both must be correct. Only
the official W/X/Y/Z labels are read as letters, and a choice written as a digit also takes
its spelled-out form (``W) 0`` accepts ``zero``).
"""

import math
import re

# solo's normalization: fold smart quotes, then keep only [a-z0-9'\s], collapse, trim.
_SMART_QUOTES = re.compile(r"[\u2018\u2019\u201c\u201d]")
_NON_WORD = re.compile(r"[^a-z0-9'\s]")
_SPACES = re.compile(r"\s+")

# A packet alternative: "(ACCEPT: RADII)", "(ACCEPT RADII)", "(Accept: .3333)".
_ACCEPT_GROUP = re.compile(r"\(\s*accept\s*:?\s*([^)]*)\)", re.IGNORECASE)
# Any other parenthetical is an annotation — "(in any order)", "(Solution: ...)" — and is
# never part of the answer text.
_PARENTHETICAL = re.compile(r"\([^)]*\)")
# An alternative written as the whole line: "ACCEPT: 90 NORTH".
_ACCEPT_PREFIX = re.compile(r"^\s*accept\s*:?\s*(.+)$", re.IGNORECASE)
# Alternatives inside one group are separated by a semicolon or the word "or".
_ALT_SPLIT = re.compile(r"\s*(?:;|\bor\b)\s*", re.IGNORECASE)
# A top-level multiple-choice line: "W) STATIONARY", "W. stationary", "W STATIONARY".
# The letter must be followed by a bracket/period/colon or whitespace, so a short answer
# that merely begins with a choice letter ("X-ray") is never read as option X.
_MC_LINE = re.compile(r"^\s*([WXYZ])\s*(?:[).:]\s*|\s+)(\S.*)$", re.IGNORECASE)
# A reply that leads with a choice letter. The separator or end of input is required, so a
# word that merely starts with a choice letter ("water") is never read as the letter W.
_MC_REPLY = re.compile(r"^\s*([wxyz])\s*(?:[).:\-]\s*(.*)|$)", re.IGNORECASE)

_SUPERSCRIPTS = {"\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3", "\u2074": "4",
                 "\u2075": "5", "\u2076": "6", "\u2077": "7", "\u2078": "8", "\u2079": "9",
                 "\u207b": "-"}
_THOUSANDS = re.compile(r"(?<=\d),(?=\d)")
_PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
# "6 x 10^3", "6 x 103" (a lost superscript), "6 times 10 to the 3rd", "6*10^3".
_SCIENTIFIC = re.compile(
    r"^" + _NUMBER + r"\s*(?:[x*\u00d7]|times)\s*10\s*(?:\^|to the|to)?\s*([+-]?\d+)(?:st|nd|rd|th)?$"
)
# The same exponent spoken as a word: "6 times 10 cubed", "6 x 10 to the fourth".
_SCIENTIFIC_SPOKEN = re.compile(
    r"^" + _NUMBER + r"\s*(?:[x*\u00d7]|times)\s*10\s*(?:to the\s+)?"
    r"(squared|cubed|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)$"
)
# Calculator shorthand: "6e3", "6.2E-4".
_EXPONENT = re.compile(r"^" + _NUMBER + r"[eE]([+-]?\d+)$")
_ORDINAL_POWERS = {"squared": 2, "cubed": 3, "third": 3, "fourth": 4, "fifth": 5,
                   "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
# A choice or a reply may spell the number out instead of writing the digit.
_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
                 "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
                 "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
                 "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
                 "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9,
                 "half": 0.5, "quarter": 0.25}

# Unit tokens that may trail a numeric answer. NSB's appendix says units do not need to be
# stated, so "6000 grams" answers a question asking for grams. Only these tokens — or words
# the packet's own answer line uses — may be dropped, so "90 south" is never read as a bare
# 90 when the packet's answer says north.
_UNIT_WORDS = frozenset("""
    m cm mm nm km meter meters metre metres centimeter centimeters centimetre centimetres
    millimeter millimeters millimetre millimetres micrometer micrometers micron microns
    nanometer nanometers angstrom angstroms inch inches in foot feet ft yard yards yd
    mile miles mi g gram grams gm kg kilogram kilograms kilogramme mg milligram milligrams
    pound pounds lb lbs ounce ounces oz ton tons tonne tonnes amu dalton daltons
    s sec secs second seconds minute minutes min hour hours hr hrs day days week weeks
    month months year years yr millisecond milliseconds microsecond microseconds
    nanosecond nanoseconds k kelvin kelvins celsius centigrade fahrenheit rankine
    degree degrees a amp ampere amperes amps v volt volts ohm ohms watt watts w joule
    joules j coulomb coulombs farad farads henry henries tesla teslas weber webers siemens
    hertz hz pascal pascals pa newton newtons n bar bars atmosphere atmospheres atm psi
    torr mmhg calorie calories cal kilocalorie kilocalories kcal electronvolt electronvolts
    ev kilojoule kilojoules kj megajoule megajoules mj mole moles mol molar candela lumen
    lumens lux l liter liters litre litres ml milliliter milliliters millilitre millilitres
    cc cubic gallon gallons quart quarts pint pints cup cups becquerel becquerels curie
    curies sievert sieverts gray grays rad rem percent percentage ppm
""".split())

# Words that carry no answer content: articles, prepositions, and the spoken preamble a
# typed answer often opens with. They may surround a numeric answer, but a word that means
# something ("south") never may.
_FILLER_WORDS = frozenset("""
    a an the is are was were be of and or it its that this i my me answer answered think
    believe maybe probably approximately approx roughly around about so then there their
    in any order
""".split())


def normalize_text(value):
    """Fold a typed response the same way solo does before matching."""
    text = "" if value is None else str(value)
    text = text.lower()
    text = _SMART_QUOTES.sub("'", text)
    text = _NON_WORD.sub(" ", text)
    text = _SPACES.sub(" ", text)
    return text.strip()


# ------------------------------------------------------------------------------ numbers

def _finite(value):
    return value if isinstance(value, float) and math.isfinite(value) else None


def _scaled(coefficient, exponent):
    """``coefficient x 10^exponent``, or ``None`` when it is not a usable number.

    ``math.pow`` is used so an absurd exponent overflows instead of building a giant
    integer, and a non-finite result is refused rather than compared.
    """
    try:
        return _finite(float(coefficient) * math.pow(10, int(exponent)))
    except (ValueError, OverflowError, TypeError):
        return None


def _numeric_value(value):
    """The number a value stands for, or ``None`` when it is not a number.

    Handles the forms a science packet and a hurried typist actually produce: thousands
    separators, scientific notation with any multiplication sign, superscript digits, and
    a plain decimal written with or without a leading zero.
    """
    text = "" if value is None else str(value).strip().lower()
    for mark, digit in _SUPERSCRIPTS.items():
        text = text.replace(mark, digit)
    text = _SMART_QUOTES.sub("'", text)
    text = _THOUSANDS.sub("", text)
    text = _SPACES.sub(" ", text).strip()
    if not text:
        return None
    match = _SCIENTIFIC.match(text)
    if match:
        return _scaled(match.group(1), match.group(2))
    spoken = _SCIENTIFIC_SPOKEN.match(text)
    if spoken:
        return _scaled(spoken.group(1), _ORDINAL_POWERS[spoken.group(2)])
    exponent = _EXPONENT.match(text.replace(" ", ""))
    if exponent:
        return _scaled(exponent.group(1), exponent.group(2))
    compact = text.replace(" ", "")
    if _PLAIN_NUMBER.match(compact):
        try:
            return _finite(float(compact))
        except ValueError:
            return None
    return None


def _spelled_number_value(value):
    """The number a single spelled-out word stands for (``zero``, ``half``), or ``None``.

    Deliberately limited to one word: a compound such as "seven tenths" carries its own
    meaning, and guessing at it would make the exact multiple-choice wording loose again.
    """
    text = normalize_text(value)
    if not text or " " in text:
        return None
    spelled = _NUMBER_WORDS.get(text)
    return float(spelled) if spelled is not None else None


def _answer_value(value):
    """A comparable number for a value, whether written in digits or spelled out."""
    number = _numeric_value(value)
    if number is not None:
        return number
    return _spelled_number_value(value)


def _numbers_equal(first, second):
    return math.isclose(first, second, rel_tol=1e-9, abs_tol=1e-12)


def _ignorable_word(word, allowed_words):
    """True when a word adds nothing to a numeric answer."""
    return word in _FILLER_WORDS or word in _UNIT_WORDS or word in allowed_words


def _reply_number(value, allowed_words=frozenset()):
    """The number a reply stands for, ignoring a trailing unit.

    Only a trailing unit word is dropped and never a trailing number, so a reply of
    ``6 x 10^3`` stays the single number 6000 and is never reduced to a bare ``6``.
    """
    text = _SPACES.sub(" ", _PARENTHETICAL.sub(" ", str(value or "")).strip())
    if not text:
        return None
    whole = _answer_value(text)
    if whole is not None:
        return whole
    tokens = text.split(" ")
    while len(tokens) > 1:
        if _answer_value(tokens[-1]) is not None:
            break  # a trailing number is part of the expression, not a unit
        if not _ignorable_word(normalize_text(tokens[-1]), allowed_words):
            break
        tokens = tokens[:-1]
        head = _answer_value(" ".join(tokens))
        if head is not None:
            return head
    return None


def _numeric_reply_matches(answer_number, reply_raw, reply_words, allowed_words):
    """Decide a numeric answer by value, not by the digits happening to appear.

    A reply matches when it states the same number, with or without a unit (``6000 grams``
    for ``6,000``). A reply that states a *different* number, or states the number beside a
    word that means something — ``90 south`` for a packet answer of ``90 NORTH`` — is wrong.
    """
    reply_number = _reply_number(reply_raw, allowed_words)
    if reply_number is not None:
        return _numbers_equal(answer_number, reply_number)
    numbers = [token for token in reply_words if _answer_value(token) is not None]
    if len(numbers) != 1 or not _numbers_equal(answer_number, _answer_value(numbers[0])):
        return False
    return all(
        _ignorable_word(token, allowed_words)
        for token in reply_words
        if _answer_value(token) is None
    )


# ------------------------------------------------------------------------ text matching

def _typo_allowance(word):
    """How many characters a reply may differ by.

    Bounded on purpose: a misspelling of a long word is a slip, but a short answer has too
    few characters for a typo to stay unambiguous, so it must be spelled out.
    """
    if len(word) < 6:
        return 0
    if len(word) < 9:
        return 1
    if len(word) < 15:
        return 2
    return 3


def _is_plural_pair(first, second):
    """True when the two words differ only by a regular plural ending."""
    for short, long in ((first, second), (second, first)):
        if long == short + "s" or long == short + "es":
            return True
        if short.endswith("y") and long == short[:-1] + "ies":
            return True
        # nucleus/nuclei, radius/radii, fungus/fungi, axis/axes.
        if short.endswith("us") and long == short[:-2] + "i":
            return True
        if short.endswith("is") and long == short[:-2] + "es":
            return True
    return False


def _edit_distance_within(first, second, limit):
    """True when the two words are within ``limit`` single-character edits."""
    if abs(len(first) - len(second)) > limit:
        return False
    previous = list(range(len(second) + 1))
    for row, first_char in enumerate(first, start=1):
        current = [row]
        for column, second_char in enumerate(second, start=1):
            current.append(min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + (first_char != second_char),
            ))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _words_match(answer_word, reply_words):
    """True when the reply carries this answer word, allowing a plural or a typo."""
    if answer_word in reply_words:
        return True
    allowance = _typo_allowance(answer_word)
    for reply_word in reply_words:
        if _is_plural_pair(answer_word, reply_word):
            return True
        if allowance and len(reply_word) >= 5 and _edit_distance_within(answer_word, reply_word, allowance):
            return True
    return False


def _compact(value):
    """The value with every separator removed, so ``X-RAY`` and ``xray`` agree."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _compact_equal(answer, reply, reply_words):
    """Hyphen and space placement should not decide an answer.

    Equality only — never containment — so ``iron`` cannot be found inside ``environment``.
    """
    compact = _compact(answer)
    if len(compact) < 4:
        return False
    if compact == _compact(reply):
        return True
    return any(_compact(word) == compact for word in reply_words)


def _matches_candidate(candidate, reply_raw, reply, reply_words, allowed_words):
    """Judge one candidate answer against the reply."""
    answer = normalize_text(candidate)
    if not answer:
        return False
    if reply == answer:
        return True
    answer_number = _numeric_value(candidate)
    if answer_number is not None:
        # A numeric answer is decided by value, never by the digits appearing inside a
        # longer reply, so a packet answer of 6 is not satisfied by "6 x 10^3".
        return _numeric_reply_matches(answer_number, reply_raw, reply_words, allowed_words)
    if f" {answer} " in f" {reply} ":
        return True
    if _compact_equal(answer, reply, reply_words):
        return True
    answer_words = [word for word in answer.split(" ") if word]
    if len(answer_words) == 1:
        return _words_match(answer_words[0], reply_words)
    significant = [word for word in answer_words if len(word) >= 4]
    if not significant:
        return all(_words_match(word, reply_words) for word in answer_words)
    return all(_words_match(word, reply_words) for word in significant)


# ----------------------------------------------------------------------- packet parsing

def _looks_numeric(text):
    return _PLAIN_NUMBER.match(text.replace(" ", "")) is not None


def _alternatives(group):
    """Split one ACCEPT group into its alternatives.

    Commas are only treated as separators when the pieces are not all plain numbers, so
    ``(ACCEPT: 1,000)`` stays one answer while ``(ACCEPT: RADII, RADIUS)`` becomes two.
    """
    pieces = []
    for piece in _ALT_SPLIT.split(group):
        piece = piece.strip()
        if not piece:
            continue
        comma_parts = [part.strip() for part in piece.split(",") if part.strip()]
        if len(comma_parts) > 1 and not all(_looks_numeric(part) for part in comma_parts):
            pieces.extend(comma_parts)
        else:
            pieces.append(piece)
    return pieces


def _candidate_answers(answer_text):
    """Every acceptable answer for one packet line, the base answer first.

    ``6,000 (ACCEPT: 6 x 10^3)`` yields ``["6,000", "6 x 10^3"]``. A line that lists
    alternatives with a semicolon — ``MACH; KNOTS``, ``HECTARE; ACRE`` — yields one
    candidate per alternative, which is how packets write "either of these is correct".
    """
    raw = "" if answer_text is None else str(answer_text)
    candidates = []
    base = _PARENTHETICAL.sub(" ", raw).strip()
    base = _ACCEPT_PREFIX.sub(lambda match: match.group(1), base).strip()
    for piece in base.split(";"):
        piece = piece.strip()
        if piece and piece not in candidates:
            candidates.append(piece)
    for group in _ACCEPT_GROUP.findall(raw):
        for alternative in _alternatives(group):
            if alternative and alternative not in candidates:
                candidates.append(alternative)
    return candidates


def _printed_answer(answer_text):
    """The answer line with its annotations removed: ``13 (Solution: ...)`` becomes ``13``."""
    return normalize_text(_PARENTHETICAL.sub(" ", "" if answer_text is None else str(answer_text)))


def judge_keyword(answer_text, response):
    """Judge a free-text answer against a packet line, alternatives included."""
    reply_raw = "" if response is None else str(response)
    reply = normalize_text(reply_raw)
    if not reply:
        return False
    # Typing the answer line exactly as the packet prints it is always correct, even when
    # the line lists several values or carries a solution.
    printed = _printed_answer(answer_text)
    if printed and reply == printed:
        return True
    reply_words = [word for word in reply.split(" ") if word]
    candidates = _candidate_answers(answer_text)
    # Words the packet itself uses are acceptable trailing qualifiers, so a question whose
    # answer line says "90 NORTH" still accepts "90 degrees north" and rejects "90 south".
    allowed_words = frozenset(
        word for candidate in candidates for word in normalize_text(candidate).split(" ") if word
    )
    return any(
        _matches_candidate(candidate, reply_raw, reply, reply_words, allowed_words)
        for candidate in candidates
    )


# ---------------------------------------------------------------------- multiple choice

def _choice_line(answer_text):
    """``("w", "STATIONARY")`` for ``W) STATIONARY``; ``(None, "")`` when not a choice."""
    text = (answer_text or "").strip()
    match = _MC_LINE.match(text)
    if match:
        return match.group(1).lower(), match.group(2).strip()
    if len(text) == 1 and text.lower() in "wxyz":
        return text.lower(), ""
    return None, ""


def _reply_choice(response):
    """The letter a reply leads with, and whatever it says after it."""
    match = _MC_REPLY.match(response or "")
    if not match:
        return None, ""
    return match.group(1).lower(), (match.group(2) or "").strip()


def _is_multiple_choice(question, answer_text):
    if str((question or {}).get("type", "") or "").strip().upper() == "MC":
        return True
    # No stored type: the answer line's own shape ("W) STATIONARY") is the evidence.
    candidates = _candidate_answers(answer_text)
    return bool(candidates) and _choice_line(candidates[0])[0] is not None


def _phrases_equal(phrase, reply):
    """A choice's text matches exactly, or as the same number written another way."""
    if normalize_text(phrase) == normalize_text(reply):
        return True
    phrase_value = _answer_value(phrase)
    reply_value = _answer_value(reply)
    return (
        phrase_value is not None
        and reply_value is not None
        and _numbers_equal(phrase_value, reply_value)
    )


def _judge_multiple_choice(answer_text, response):
    """Rule 3-3: the letter or the choice text; Rule 3-6: both, if both are given.

    The choice text must match exactly once case and punctuation are folded, so a reply
    that only gets part of the wording right is not accepted for it.
    """
    candidates = _candidate_answers(answer_text)
    base = candidates[0] if candidates else ""
    letter, phrase = _choice_line(base)
    if letter is None:
        # A multiple-choice question whose answer line carries no letter is judged as text.
        return judge_keyword(answer_text, response)
    reply_letter, reply_phrase = _reply_choice(response)
    if reply_letter is None:
        for candidate in candidates:
            _, candidate_phrase = _choice_line(candidate)
            text = candidate_phrase or candidate
            if text and _phrases_equal(text, response):
                return True
        return False
    if reply_letter != letter:
        return False
    if reply_phrase and not _phrases_equal(phrase, reply_phrase):
        return False
    return True


def grade_tossup(question, response):
    """Grade a typed toss-up answer against the question's ``tossup_answer``."""
    answer_text = str((question or {}).get("tossup_answer", "") or "")
    if _is_multiple_choice(question, answer_text):
        return _judge_multiple_choice(answer_text, response)
    return judge_keyword(answer_text, response)


def grade_bonus(question, response):
    """Grade a typed bonus answer against the question's ``bonus_answer``.

    Bonus type is not stored separately from the toss-up, so multiple-choice detection falls
    back to the answer line's shape (``W) STATIONARY``).
    """
    answer_text = str((question or {}).get("bonus_answer", "") or "")
    if _is_multiple_choice(question, answer_text):
        return _judge_multiple_choice(answer_text, response)
    return judge_keyword(answer_text, response)
