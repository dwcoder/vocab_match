"""Decides how likely each word is to be shown next.

Each word gets a score between 0 and 1, and the chance of picking a word
is its score divided by the total of the scores (the scores are normalised).

The overdue part of the score works in one of two ways (the "method",
chosen on the Settings screen):

* "rank": words that have been matched are put in order from
  the one matched longest ago to the one matched most recently. The oldest
  scores 1, and the score halves for every RANK_HALF_FRACTION of those words
  further down the order. Only the order matters, not the times.
* "time": the word matched longest ago scores 1. Every word matched later
  scores less, halving for every HALF_LIFE_HOURS between its last correct
  match and the oldest word's.
* "memory" (the default): each word has its own half-life, a guess at how long it stays
  remembered. It starts at START_HALF_LIFE_HOURS, doubles on every correct
  guess and halves on every wrong one (like the Leitner box system). The
  chance the word is still remembered is 0.5 ** (hours since it was last
  practised / its half-life), and the score is the chance it has been
  forgotten: 1 minus that.

With every method:

* Mistakes: a word scores at least (wrong answers in its last ten guesses) / 10,
  so a word missed 10 times out of 10 is as likely as the oldest word.
* A word that has never been matched correctly scores 1.
* A word's score is the higher of its overdue score and its mistake score.
"""

import random
from datetime import datetime


RANK = "rank"
TIME = "time"
MEMORY = "memory"
METHODS = (RANK, TIME, MEMORY)
DEFAULT_METHOD = MEMORY

HALF_LIFE_HOURS = 24
RANK_HALF_FRACTION = 0.2  # score halves for every 20% of the matched words
RECENT_COUNT = 10

# Memory method. The start value must match half_life_hours' default in vocab_db.
START_HALF_LIFE_HOURS = 24
HALF_LIFE_GROWTH = 2.0     # multiplied in on a correct guess
HALF_LIFE_SHRINK = 0.5     # multiplied in on a wrong guess
MIN_HALF_LIFE_HOURS = 1
MAX_HALF_LIFE_HOURS = 24 * 365


def next_half_life(half_life_hours, correct):
    """A word's half-life after one more guess: doubled if right, halved if wrong."""
    factor = HALF_LIFE_GROWTH if correct else HALF_LIFE_SHRINK
    new = (half_life_hours or START_HALF_LIFE_HOURS) * factor
    return min(MAX_HALF_LIFE_HOURS, max(MIN_HALF_LIFE_HOURS, new))


def recall_chance(last_practiced, half_life_hours, now):
    """Chance a word is still remembered: halves every half-life since it was practised."""
    hours = max(0.0, (now - last_practiced).total_seconds() / 3600)
    return 0.5 ** (hours / half_life_hours)


def memory_score(last_practiced, half_life_hours, now):
    """Chance the word has been forgotten. 1 if it has never been practised."""
    if last_practiced is None:
        return 1.0
    return 1.0 - recall_chance(last_practiced, half_life_hours or START_HALF_LIFE_HOURS, now)


def time_score(last_seen, oldest, half_life_hours=HALF_LIFE_HOURS):
    """1 for the oldest word, halving every half_life_hours after it."""
    if last_seen is None or oldest is None:
        return 1.0
    hours_after_oldest = max(0.0, (last_seen - oldest).total_seconds() / 3600)
    return 0.5 ** (hours_after_oldest / half_life_hours)


def rank_score(position, count, half_fraction=RANK_HALF_FRACTION):
    """1 for position 0 (the oldest), halving every half_fraction of count positions."""
    if count <= 1:
        return 1.0
    return 0.5 ** (position / (count * half_fraction))


def mistake_score(recent_results):
    """Fraction of the last ten guesses that were wrong ('0' in recent_results)."""
    recent = (recent_results or "")[-RECENT_COUNT:]
    return recent.count("0") / RECENT_COUNT


def overdue_scores(seen, method=RANK, half_life_hours=HALF_LIFE_HOURS,
                   rank_half_fraction=RANK_HALF_FRACTION):
    """Return {word: overdue score} from {word: last_seen datetime or None}.

    Only for the rank and time methods; scores() handles the memory method.
    """
    if method not in (RANK, TIME):
        raise ValueError(f"Unknown method {method!r}; use one of {METHODS}.")

    times = sorted(t for t in seen.values() if t is not None)
    if method == TIME:
        oldest = times[0] if times else None
        return {w: time_score(t, oldest, half_life_hours) for w, t in seen.items()}

    # Rank: words matched at the same moment share the earlier position.
    position = {}
    for i, t in enumerate(times):
        position.setdefault(t, i)
    return {
        w: 1.0 if t is None else rank_score(position[t], len(times), rank_half_fraction)
        for w, t in seen.items()
    }


def scores(stats, method=DEFAULT_METHOD, half_life_hours=HALF_LIFE_HOURS,
           rank_half_fraction=RANK_HALF_FRACTION, now=None):
    """Return {word: score} from {word: stats dict}.

    Each stats dict has "last_seen" and "recent_results", and for the memory
    method also "last_practiced" and "half_life_hours". Times may be None,
    ISO 8601 strings or datetimes. now defaults to the current time and is
    only used by the memory method.
    """
    if method == MEMORY:
        now = now or datetime.now()
        overdue = {
            word: memory_score(
                _as_datetime(s.get("last_practiced")), s.get("half_life_hours"), now
            )
            for word, s in stats.items()
        }
    else:
        seen = {word: _as_datetime(s.get("last_seen")) for word, s in stats.items()}
        overdue = overdue_scores(seen, method, half_life_hours, rank_half_fraction)
    return {
        word: max(overdue[word], mistake_score(s.get("recent_results")))
        for word, s in stats.items()
    }


def probabilities(word_scores):
    """Normalise scores so they add up to 1. Equal chances if every score is 0."""
    total = sum(word_scores.values())
    if total <= 0:
        return {word: 1 / len(word_scores) for word in word_scores} if word_scores else {}
    return {word: score / total for word, score in word_scores.items()}


def choose(candidates, word_scores, rng=random):
    """Pick one of candidates, with a chance in proportion to its score.

    Words missing from word_scores count as never seen (score 1).
    """
    candidates = list(candidates)
    if not candidates:
        raise ValueError("No words to choose from.")
    chances = probabilities({w: word_scores.get(w, 1.0) for w in candidates})
    return rng.choices(candidates, weights=[chances[w] for w in candidates])[0]


def _as_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)
