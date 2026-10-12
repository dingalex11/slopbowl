"""Packet category folding onto the drill's design subjects.

A faithful port of ``SOLO_SUBJECT_ALIASES`` / ``soloSubjectFor`` from
``static/js/solo.js``. The Board's subject accuracy grid groups by these subjects, so a
packet's own labels (``Life Science``) land in a drill subject (``Biology``) exactly as
they do in solo practice. ``tests/test_buzzpoint_board.py`` pins the two maps together.
"""

SUBJECT_ALIASES = {
    "life science": "Biology",
    "biology": "Biology",
    "physical science": "Physics",
    "physics": "Physics",
    "chemistry": "Chemistry",
    "earth science": "Earth and Space",
    "earth and space": "Earth and Space",
    "math": "Math",
    "mathematics": "Math",
    "energy": "Energy",
    "general science": "General Science",
}

SUBJECT_COLORS = {
    "Biology": "#00ff66",
    "Chemistry": "#ffaa00",
    "Physics": "#00f3ff",
    "Earth and Space": "#2dd4bf",
    "Math": "#b026ff",
    "Energy": "#ff0055",
    "General Science": "#94a3b8",
}


def subject_for(category):
    """Fold a packet category onto a drill subject, falling back to the raw label."""
    raw = "" if category is None else str(category).strip()
    return SUBJECT_ALIASES.get(raw.lower(), raw)


def subject_color(subject):
    return SUBJECT_COLORS.get(subject, "#94a3b8")
