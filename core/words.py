"""Word tokenization and char-offset mapping for buzz-point capture.

The buzz-point is the word a member was on the instant they buzzed. The browser reports
that instant as a character offset into the utterance text (``charIndex``), so every
capture path — boundary event, counted stream, or elapsed-time estimate — is normalized
through this mapping first.

This module is the server-side reference for the same algorithm that runs in the browser
(``buzzTokenizeWords`` / ``buzzWordIndexAt`` in ``static/js/buzzpoint.js``). Keeping the
pattern literal identical between the two is what makes a recorded buzz word trustworthy;
``tests/test_buzzpoint_board.py`` asserts they stay in sync.

A word is a run of letters/digits joined by internal apostrophes or periods, so ``6.02``
and ``don't`` stay single tokens while ``end.`` still yields ``end``.
"""

import re

# Mirrored verbatim in static/js/buzzpoint.js as BUK_WORD_PATTERN. Keep them identical.
WORD_PATTERN = r"[A-Za-z0-9]+(?:[.'\u2019][A-Za-z0-9]+)*"
_WORD_RE = re.compile(WORD_PATTERN)


def tokenize_words(text):
    """Return a list of ``{"word", "start", "end"}`` for each word, keeping char offsets.

    ``start``/``end`` are indices into the *original* text, so a charIndex from the
    speech engine maps back to a word without re-parsing. Non-string input yields an
    empty list rather than raising.
    """
    if not isinstance(text, str):
        return []
    return [
        {"word": match.group(0), "start": match.start(), "end": match.end()}
        for match in _WORD_RE.finditer(text)
    ]


def word_index_at(words, char_index):
    """Map a character offset to the word index it falls on.

    Bounds-checked and forgiving by design: the speech engine's ``charIndex`` is not
    reliable across browsers (Safari has been observed undercounting spaces, and it can
    arrive past the end of the text), so this clamps instead of raising.

    Returns the index of the word containing ``char_index``; if the offset lands in the
    gap before a word, the nearest preceding word is returned; before any word returns 0.
    """
    if not words:
        return 0
    if isinstance(char_index, bool) or not isinstance(char_index, int):
        char_index = 0
    if char_index < 0:
        char_index = 0
    result = 0
    for index, word in enumerate(words):
        if word["start"] <= char_index:
            result = index
        if word["start"] <= char_index < word["end"]:
            return index
    return result


def word_at(words, char_index):
    """Convenience: the word string for a char offset, or ``""`` when there are none."""
    if not words:
        return ""
    return words[word_index_at(words, char_index)]["word"]
