"""Storage for the vocab list, kept in a SQLite database.

Each word (in any language) maps to one or more meanings in English.

Words and meanings keep the spelling they were entered with, but are compared
ignoring capitals (see match_key): "hola" and "Hola" are the same word, and a
word can't have both "Bank" and "bank". Accents still count, so "si" and "sí"
are different words.
"""

import csv
import io
import sqlite3
import unicodedata
from datetime import datetime

import choice_function


SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL UNIQUE,
    word_key TEXT,            -- match_key(word); unique, see migrate()
    last_seen TEXT,           -- ISO 8601 timestamp of the last correct match
    times_shown INTEGER NOT NULL DEFAULT 0,       -- guesses made with this word
    times_correct INTEGER NOT NULL DEFAULT 0,     -- of those, how many were right
    recent_results TEXT NOT NULL DEFAULT '',      -- last 10 guesses, oldest first: '1' right, '0' wrong
    last_ten_correct INTEGER NOT NULL DEFAULT 0,  -- number of '1's in recent_results
    last_practiced TEXT,                          -- ISO 8601 timestamp of the last guess, right or wrong
    half_life_hours REAL NOT NULL DEFAULT 24      -- memory strength; grows when right, shrinks when wrong
);
CREATE TABLE IF NOT EXISTS meanings (
    word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
    english TEXT NOT NULL,
    english_key TEXT,         -- match_key(english); unique per word, see migrate()
    PRIMARY KEY (word_id, english)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """Bring databases made by earlier versions up to date."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(words)")]
    if "spanish" in columns:
        conn.execute("ALTER TABLE words RENAME COLUMN spanish TO word")
        conn.commit()

    added_columns = {
        "times_shown": "INTEGER NOT NULL DEFAULT 0",
        "times_correct": "INTEGER NOT NULL DEFAULT 0",
        "recent_results": "TEXT NOT NULL DEFAULT ''",
        "last_ten_correct": "INTEGER NOT NULL DEFAULT 0",
        "last_practiced": "TEXT",
        "half_life_hours": "REAL NOT NULL DEFAULT 24",
    }
    for name, definition in added_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE words ADD COLUMN {name} {definition}")
    if "last_practiced" not in columns:
        # Best guess for words practised before this column existed.
        conn.execute("UPDATE words SET last_practiced = last_seen")
    if "word_key" not in columns:
        conn.execute("ALTER TABLE words ADD COLUMN word_key TEXT")
    meaning_columns = [row[1] for row in conn.execute("PRAGMA table_info(meanings)")]
    if "english_key" not in meaning_columns:
        conn.execute("ALTER TABLE meanings ADD COLUMN english_key TEXT")
    conn.commit()

    _fill_keys_and_merge(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS words_by_key ON words (word_key)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS meanings_by_key ON meanings (word_id, english_key)"
    )
    conn.commit()


def match_key(text):
    """How words and meanings are compared: capitals ignored, accents kept.

    NFC first, so an accent typed as one character or as two compares equal.
    """
    return unicodedata.normalize("NFC", text).casefold()


def _fill_keys_and_merge(conn):
    """Fill in missing keys, and merge words or meanings that differ only in capitals.

    Only does work for databases made before keys existed. The first spelling
    (lowest id) is kept. Practice numbers: counts are added up, and the rest
    comes from whichever word was practised more.
    """
    missing = conn.execute(
        "SELECT (SELECT COUNT(*) FROM words WHERE word_key IS NULL)"
        " + (SELECT COUNT(*) FROM meanings WHERE english_key IS NULL)"
    ).fetchone()[0]
    if not missing:
        return

    for word_id, word in conn.execute("SELECT id, word FROM words").fetchall():
        conn.execute("UPDATE words SET word_key = ? WHERE id = ?", (match_key(word), word_id))
    for rowid, english in conn.execute("SELECT rowid, english FROM meanings").fetchall():
        conn.execute(
            "UPDATE meanings SET english_key = ? WHERE rowid = ?", (match_key(english), rowid)
        )

    clashes = conn.execute(
        "SELECT word_key FROM words GROUP BY word_key HAVING COUNT(*) > 1"
    ).fetchall()
    for (key,) in clashes:
        rows = conn.execute(
            f"SELECT id, {', '.join(STATS_COLUMNS)} FROM words WHERE word_key = ? ORDER BY id",
            (key,),
        ).fetchall()
        keep_id = rows[0][0]
        stats = [dict(zip(STATS_COLUMNS, r[1:])) for r in rows]
        busiest = max(stats, key=lambda st: st["times_shown"])
        seen = [st["last_seen"] for st in stats if st["last_seen"]]
        conn.execute(
            """
            UPDATE words SET times_shown = ?, times_correct = ?, recent_results = ?,
                last_ten_correct = ?, last_seen = ?, last_practiced = ?, half_life_hours = ?
            WHERE id = ?
            """,
            (
                sum(st["times_shown"] for st in stats),
                sum(st["times_correct"] for st in stats),
                busiest["recent_results"],
                busiest["last_ten_correct"],
                max(seen) if seen else None,
                busiest["last_practiced"],
                busiest["half_life_hours"],
                keep_id,
            ),
        )
        for other_id, *_ in rows[1:]:
            conn.execute(
                "UPDATE OR IGNORE meanings SET word_id = ? WHERE word_id = ?",
                (keep_id, other_id),
            )
            conn.execute("DELETE FROM words WHERE id = ?", (other_id,))

    # Meanings of one word that differ only in capitals: keep the first.
    conn.execute(
        """
        DELETE FROM meanings WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM meanings GROUP BY word_id, english_key
        )
        """
    )
    conn.commit()


def sort_key(text):
    """Lower-case and strip accents, so 'Café' sorts as 'cafe' and matches 'cafe'."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.casefold()


def seed_if_empty(conn, seed):
    """Load the starter list, but only the first time the app ever runs.

    After that an empty list stays empty, so deleting all words sticks.
    """
    if get_setting(conn, "seeded") is not None:
        return
    if count_words(conn) == 0:
        for word, meanings in seed.items():
            add_meanings(conn, word, meanings)
    set_setting(conn, "seeded", "1")  # commits


def count_words(conn):
    return conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]


def delete_all_words(conn):
    """Remove every word, meaning and practice history. Settings are kept.

    Returns how many words were removed. Commits.
    """
    removed = count_words(conn)
    conn.execute("DELETE FROM words")  # meanings go too: ON DELETE CASCADE
    conn.commit()
    return removed


def load_words(conn):
    """Return {word: (english, ...)}, sorted alphabetically by the word."""
    rows = conn.execute(
        """
        SELECT w.word, m.english
        FROM words w
        LEFT JOIN meanings m ON m.word_id = w.id
        ORDER BY w.word, m.rowid
        """
    ).fetchall()

    grouped = {}
    for word, english in rows:
        meanings = grouped.setdefault(word, [])
        if english is not None:
            meanings.append(english)

    return {
        word: tuple(grouped[word])
        for word in sorted(grouped, key=sort_key)
    }


def _word_id(conn, word):
    row = conn.execute("SELECT id FROM words WHERE word_key = ?", (match_key(word),)).fetchone()
    return None if row is None else row[0]


def stored_spelling(conn, word):
    """The spelling a word is stored with, or None if it isn't in the list."""
    row = conn.execute(
        "SELECT word FROM words WHERE word_key = ?", (match_key(word),)
    ).fetchone()
    return None if row is None else row[0]


def add_meanings(conn, word, meanings):
    """Add meanings to a word, creating the word if needed.

    A word already in the list with different capitals gets the meanings,
    and keeps its spelling. Meanings it already has, ignoring capitals, are
    skipped. Returns how many meanings were new. Does not commit.
    """
    conn.execute(
        "INSERT OR IGNORE INTO words (word, word_key) VALUES (?, ?)", (word, match_key(word))
    )
    word_id = _word_id(conn, word)

    added = 0
    for english in meanings:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO meanings (word_id, english, english_key) VALUES (?, ?, ?)",
            (word_id, english, match_key(english)),
        )
        added += cursor.rowcount
    return added


def update_word(conn, old_word, new_word, meanings):
    """Rename a word and replace its meanings. Commits.

    Raises ValueError if the word doesn't exist or the new name is taken.
    """
    word_id = _word_id(conn, old_word)
    if word_id is None:
        raise ValueError(f"'{old_word}' is not in the list.")

    taken_by = _word_id(conn, new_word)
    if taken_by is not None and taken_by != word_id:
        raise ValueError(f"'{stored_spelling(conn, new_word)}' is already in the list.")

    # Changing only the capitals of a word is allowed.
    conn.execute(
        "UPDATE words SET word = ?, word_key = ? WHERE id = ?",
        (new_word, match_key(new_word), word_id),
    )
    conn.execute("DELETE FROM meanings WHERE word_id = ?", (word_id,))
    for english in meanings:
        conn.execute(
            "INSERT OR IGNORE INTO meanings (word_id, english, english_key) VALUES (?, ?, ?)",
            (word_id, english, match_key(english)),
        )
    conn.commit()


def delete_word(conn, word):
    """Remove a word, its meanings and its practice history. Commits.

    Returns True if the word was there.
    """
    cursor = conn.execute("DELETE FROM words WHERE word_key = ?", (match_key(word),))
    conn.commit()  # meanings go too: ON DELETE CASCADE
    return cursor.rowcount > 0


def set_last_seen(conn, word, when=None):
    """Record that a word was seen. Commits. Used later by the memory helper."""
    when = when or datetime.now()
    conn.execute(
        "UPDATE words SET last_seen = ? WHERE word_key = ?",
        (when.isoformat(timespec="seconds"), match_key(word)),
    )
    conn.commit()


RECENT_COUNT = 10


def record_guess(conn, word, correct, when=None):
    """Record one guess made with a word. Commits.

    Counts the guess, keeps the result of the last 10 guesses, sets
    last_practiced, grows or shrinks the word's half-life, and on a correct
    guess also sets last_seen.
    """
    row = conn.execute(
        "SELECT recent_results, half_life_hours FROM words WHERE word_key = ?",
        (match_key(word),),
    ).fetchone()
    if row is None:
        return
    when = when or datetime.now()
    recent = (row[0] + ("1" if correct else "0"))[-RECENT_COUNT:]
    conn.execute(
        """
        UPDATE words
        SET times_shown = times_shown + 1,
            times_correct = times_correct + ?,
            recent_results = ?,
            last_ten_correct = ?,
            last_practiced = ?,
            half_life_hours = ?
        WHERE word_key = ?
        """,
        (
            1 if correct else 0,
            recent,
            recent.count("1"),
            when.isoformat(timespec="seconds"),
            choice_function.next_half_life(row[1], correct),
            match_key(word),
        ),
    )
    if correct:
        set_last_seen(conn, word, when)  # commits
    else:
        conn.commit()


STATS_COLUMNS = (
    "times_shown",
    "times_correct",
    "recent_results",
    "last_ten_correct",
    "last_seen",
    "last_practiced",
    "half_life_hours",
)


def load_stats(conn, word):
    """Return a word's guess statistics as a dict, or None if it isn't in the list."""
    row = conn.execute(
        f"SELECT {', '.join(STATS_COLUMNS)} FROM words WHERE word_key = ?",
        (match_key(word),),
    ).fetchone()
    return None if row is None else dict(zip(STATS_COLUMNS, row))


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def set_setting(conn, key, value):
    """Save a setting. Commits."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def load_all_stats(conn):
    """Return {word: stats dict} for every word, in the same form as load_stats."""
    rows = conn.execute(f"SELECT word, {', '.join(STATS_COLUMNS)} FROM words").fetchall()
    return {row[0]: dict(zip(STATS_COLUMNS, row[1:])) for row in rows}


def _read_pairs(text):
    """Yield (line_number, word, english) from a CSV file, or from older 'word|english' lines.

    Yields (line_number, None, problem) for a line that can't be read.
    """
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        line_number = reader.line_num
        if not any(cell.strip() for cell in row):
            continue

        # Older files used word|english on each line.
        if "|" in row[0]:
            word, _, english = row[0].partition("|")
        elif len(row) >= 2:
            word, english = row[0], row[1]
        else:
            yield (line_number, None, ",".join(row))
            continue

        word, english = word.strip(), english.strip()
        if not word or not english:
            yield (line_number, None, ",".join(row))
        else:
            yield (line_number, word, english)


def import_text(conn, text, start_clean=False):
    """Import a CSV list of word,english rows. Duplicate pairs are skipped,
    ignoring capitals.

    start_clean removes every word first, so the list is just the file. It
    only does so if the file has at least one readable pair, so a wrong or
    empty file never wipes the list. Both happen in one transaction.

    Returns a dict with the number added, the number skipped, any lines that
    couldn't be read, and how many words were removed by start_clean.
    """
    added = 0
    skipped = 0
    errors = []
    removed = 0

    if start_clean and any(word is not None for _, word, _ in _read_pairs(text)):
        removed = count_words(conn)
        conn.execute("DELETE FROM words")

    for line_number, word, english in _read_pairs(text):
        if word is None:
            errors.append(f"Line {line_number}: {english}")
            continue

        exists = conn.execute(
            """
            SELECT 1 FROM meanings m
            JOIN words w ON w.id = m.word_id
            WHERE w.word_key = ? AND m.english_key = ?
            """,
            (match_key(word), match_key(english)),
        ).fetchone()
        if exists:
            skipped += 1
            continue

        add_meanings(conn, word, [english])
        added += 1

    conn.commit()
    return {"added": added, "skipped": skipped, "errors": errors, "removed": removed}


def backup_bytes(conn):
    """The whole database as bytes, for saving as a backup file.

    Includes everything: words, meanings, practice history and settings.
    """
    conn.commit()
    return conn.serialize()


def restore_from_bytes(conn, data):
    """Replace the whole database with a backup made by backup_bytes. Commits.

    Older backups are brought up to date. Raises ValueError, and changes
    nothing, if data isn't a Vocab Match backup. Returns how many words the
    restored list has.
    """
    if not data:  # sqlite3 can't load an empty file (it raises MemoryError)
        raise ValueError("That file isn't a Vocab Match backup.")
    backup = sqlite3.connect(":memory:")
    try:
        try:
            backup.deserialize(data)
            tables = {
                row[0]
                for row in backup.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        except sqlite3.DatabaseError:
            raise ValueError("That file isn't a Vocab Match backup.") from None
        if not {"words", "meanings"} <= tables:
            raise ValueError("That file isn't a Vocab Match backup.")
        conn.commit()
        backup.backup(conn)
    finally:
        backup.close()
    conn.executescript(SCHEMA)
    migrate(conn)
    return count_words(conn)


def export_csv(conn):
    """Return the whole list as CSV rows of word,english, one per meaning."""
    words = load_words(conn)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for word, meanings in words.items():
        for english in meanings:
            writer.writerow([word, english])
    return buffer.getvalue()
