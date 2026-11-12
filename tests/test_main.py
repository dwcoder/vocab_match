"""Unit tests for the vocab database and the app logic in main.py.

The app is driven through a small fake page, so no Flet window is needed.
Run with `uv run pytest` from the project directory.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import flet as ft
import pytest

import main
import vocab_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakePage:
    """Just enough of ft.Page for main() to run without a Flet window."""

    def __init__(self):
        self.controls = []
        self.services = []
        self.tasks = []
        self.updates = 0
        self.appbar = None
        self.dialog = None

    def show_dialog(self, dialog):
        self.dialog = dialog

    def pop_dialog(self):
        self.dialog = None

    def add(self, *controls):
        self.controls.extend(controls)

    def update(self):
        self.updates += 1

    def run_task(self, func, *args):
        # Keep the coroutine function so a test can run it when it wants to.
        self.tasks.append((func, args))


def walk(control):
    """Yield a control and everything nested inside it."""
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from walk(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from walk(content)


def current_view(page):
    assert len(page.controls) == 1
    return page.controls[0]


def find_button(page, label):
    """Find a button by its text, or the back arrow in the app bar by its tooltip."""
    for c in walk(current_view(page)):
        if isinstance(c, ft.Button) and c.content == label:
            return c
    if page.appbar is not None and page.appbar.leading.tooltip == label:
        return page.appbar.leading
    raise AssertionError(f"No button labelled {label!r} on screen")


def click(page, label):
    result = find_button(page, label).on_click(None)
    if asyncio.iscoroutine(result):
        asyncio.run(result)


def heading(page):
    return current_view(page).controls[0].value


def bubbles(page):
    """Return (left, right) lists of bubble containers on the game screen."""
    for c in walk(current_view(page)):
        if isinstance(c, ft.Row) and len(c.controls) == 2 and all(
            isinstance(col, ft.Column) and len(col.controls) == main.ON_SCREEN
            for col in c.controls
        ):
            return c.controls[0].controls, c.controls[1].controls
    raise AssertionError("Game board not on screen")


def walk_visible(control):
    """Like walk, but skips hidden controls and everything inside them."""
    if not control.visible:
        return
    yield control
    for child in getattr(control, "controls", None) or []:
        yield from walk_visible(child)
    content = getattr(control, "content", None)
    if isinstance(content, ft.Control):
        yield from walk_visible(content)


def texts(page):
    """The text a player can see on the current screen."""
    return [c.value for c in walk_visible(current_view(page)) if isinstance(c, ft.Text)]


def timer(page):
    """The countdown on the game screen, such as "0:30"."""
    return next(
        c.value for c in walk_visible(current_view(page))
        if isinstance(c, ft.Text) and c.value and c.value[:-3].isdigit() and c.value[-3] == ":"
    )


def score(page):
    """The green match counter on the game screen."""
    return next(
        c.value for c in walk_visible(current_view(page))
        if isinstance(c, ft.Text) and c.color == ft.Colors.GREEN_600
    )


def find_first(page, kind):
    return next(c for c in walk(current_view(page)) if isinstance(c, kind))


def table_words(page):
    table = find_first(page, ft.DataTable)
    return [row.data for row in table.rows]  # stored spellings


def run_tasks(page):
    """Run every queued task (with sleeping skipped) and clear the queue."""
    tasks, page.tasks = page.tasks, []
    for func, args in tasks:
        asyncio.run(func(*args))


@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", instant)


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "vocab.db"
    monkeypatch.setattr(main, "database_path", lambda: str(path))
    return path


@pytest.fixture
def page(db_file):
    p = FakePage()
    main.main(p)
    return p


@pytest.fixture
def conn():
    c = vocab_db.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# vocab_db
# ---------------------------------------------------------------------------


class TestSortKey:
    def test_strips_accents_and_lowercases(self):
        assert vocab_db.sort_key("Café") == "cafe"
        assert vocab_db.sort_key("ADIÓS") == "adios"

    def test_plain_text_unchanged_apart_from_case(self):
        assert vocab_db.sort_key("Hello World") == "hello world"

    def test_empty(self):
        assert vocab_db.sort_key("") == ""


class TestConnect:
    def test_creates_tables(self, conn):
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"words", "meanings"} <= tables

    def test_foreign_keys_on(self, conn):
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_deleting_word_removes_meanings(self, conn):
        vocab_db.add_meanings(conn, "Banco", ["Bank", "Bench"])
        conn.execute("DELETE FROM words WHERE word = 'Banco'")
        assert conn.execute("SELECT COUNT(*) FROM meanings").fetchone()[0] == 0

    def test_migrates_old_spanish_column(self, tmp_path):
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE words (id INTEGER PRIMARY KEY, spanish TEXT NOT NULL UNIQUE,
                                last_seen TEXT);
            CREATE TABLE meanings (word_id INTEGER NOT NULL, english TEXT NOT NULL,
                                   PRIMARY KEY (word_id, english));
            INSERT INTO words (id, spanish) VALUES (1, 'Hola');
            INSERT INTO meanings VALUES (1, 'Hello');
            """
        )
        old.commit()
        old.close()

        conn = vocab_db.connect(str(path))
        columns = [row[1] for row in conn.execute("PRAGMA table_info(words)")]
        assert "word" in columns and "spanish" not in columns
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}
        conn.close()

    def test_reopening_keeps_data(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        conn.commit()
        conn.close()

        conn = vocab_db.connect(path)
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}
        conn.close()


class TestSeedIfEmpty:
    def test_seeds_empty_database(self, conn):
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",), "Banco": ("Bank", "Bench")})
        assert vocab_db.load_words(conn) == {
            "Banco": ("Bank", "Bench"),
            "Hola": ("Hello",),
        }

    def test_does_nothing_when_words_exist(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",)})
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",)}

    def test_app_seed_words_are_valid(self, conn):
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        assert vocab_db.load_words(conn) == {
            w: main.SEED_WORDS[w] for w in sorted(main.SEED_WORDS, key=vocab_db.sort_key)
        }


class TestLoadWords:
    def test_empty(self, conn):
        assert vocab_db.load_words(conn) == {}

    def test_sorted_ignoring_accents_and_case(self, conn):
        for word in ["zorro", "Árbol", "casa", "Banco"]:
            vocab_db.add_meanings(conn, word, ["x" + word])
        assert list(vocab_db.load_words(conn)) == ["Árbol", "Banco", "casa", "zorro"]

    def test_meanings_kept_in_insertion_order(self, conn):
        vocab_db.add_meanings(conn, "Tiempo", ["Weather", "Time"])
        assert vocab_db.load_words(conn)["Tiempo"] == ("Weather", "Time")

    def test_word_without_meanings_has_empty_tuple(self, conn):
        conn.execute("INSERT INTO words (word) VALUES ('Solo')")
        assert vocab_db.load_words(conn) == {"Solo": ()}


class TestAddMeanings:
    def test_creates_word_and_counts_new_meanings(self, conn):
        assert vocab_db.add_meanings(conn, "Banco", ["Bank", "Bench"]) == 2
        assert vocab_db.load_words(conn) == {"Banco": ("Bank", "Bench")}

    def test_duplicates_not_counted(self, conn):
        vocab_db.add_meanings(conn, "Banco", ["Bank"])
        assert vocab_db.add_meanings(conn, "Banco", ["Bank", "Bench"]) == 1
        assert vocab_db.add_meanings(conn, "Banco", ["Bank"]) == 0
        assert vocab_db.load_words(conn) == {"Banco": ("Bank", "Bench")}

    def test_does_not_commit(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        other = sqlite3.connect(path)
        assert other.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 0
        other.close()
        conn.close()


class TestUpdateWord:
    def test_replaces_meanings(self, conn):
        vocab_db.add_meanings(conn, "Pluma", ["Pen"])
        vocab_db.update_word(conn, "Pluma", "Pluma", ["Feather", "Pen"])
        assert vocab_db.load_words(conn) == {"Pluma": ("Feather", "Pen")}

    def test_renames(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.update_word(conn, "Hola", "Buenas", ["Hi"])
        assert vocab_db.load_words(conn) == {"Buenas": ("Hi",)}

    def test_missing_word_raises(self, conn):
        with pytest.raises(ValueError, match="not in the list"):
            vocab_db.update_word(conn, "Nada", "Nada", ["Nothing"])

    def test_rename_to_taken_name_raises_and_changes_nothing(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.add_meanings(conn, "Perro", ["Dog"])
        with pytest.raises(ValueError, match="already in the list"):
            vocab_db.update_word(conn, "Gato", "Perro", ["Cat"])
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",), "Perro": ("Dog",)}


class TestCapitals:
    def test_match_key_ignores_capitals_but_not_accents(self):
        assert vocab_db.match_key("HOLA") == vocab_db.match_key("hola")
        assert vocab_db.match_key("Sí") != vocab_db.match_key("si")
        # One accented character vs letter + combining accent.
        assert vocab_db.match_key("Caf\u00e9") == vocab_db.match_key("Cafe\u0301")

    def test_same_word_with_other_capitals_adds_to_it(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        assert vocab_db.add_meanings(conn, "hola", ["Hi"]) == 1
        assert vocab_db.load_words(conn) == {"Hola": ("Hello", "Hi")}

    def test_meaning_with_other_capitals_is_a_duplicate(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        assert vocab_db.add_meanings(conn, "HOLA", ["hello"]) == 0
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}

    def test_accents_still_make_different_words(self, conn):
        vocab_db.add_meanings(conn, "Sí", ["Yes"])
        vocab_db.add_meanings(conn, "Si", ["If"])
        assert vocab_db.load_words(conn) == {"Si": ("If",), "Sí": ("Yes",)}

    def test_import_ignores_capitals(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        result = vocab_db.import_text(conn, "hola,hello\nHOLA,Hi\n")
        assert result == {"added": 1, "skipped": 1, "errors": [], "removed": 0}
        assert vocab_db.load_words(conn) == {"Hola": ("Hello", "Hi")}

    def test_import_file_with_mixed_capitals(self, conn):
        vocab_db.import_text(conn, "Banco,Bank\nbanco,bank\nBANCO,Bench\n")
        assert vocab_db.load_words(conn) == {"Banco": ("Bank", "Bench")}

    def test_rename_can_change_only_capitals(self, conn):
        vocab_db.add_meanings(conn, "hola", ["Hello"])
        vocab_db.update_word(conn, "hola", "Hola", ["Hello"])
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}

    def test_rename_to_taken_name_with_other_capitals_raises(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.add_meanings(conn, "Perro", ["Dog"])
        with pytest.raises(ValueError, match="'Perro' is already in the list"):
            vocab_db.update_word(conn, "Gato", "perro", ["Cat"])

    def test_edit_drops_meanings_that_differ_only_in_capitals(self, conn):
        vocab_db.add_meanings(conn, "Banco", ["Bank"])
        vocab_db.update_word(conn, "Banco", "Banco", ["Bank", "bank", "Bench"])
        assert vocab_db.load_words(conn) == {"Banco": ("Bank", "Bench")}

    def test_stats_and_delete_ignore_capitals(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "hola", True)
        assert vocab_db.load_stats(conn, "HOLA")["times_correct"] == 1
        assert vocab_db.delete_word(conn, "hOLA") is True
        assert vocab_db.load_words(conn) == {}

    def old_database(self, tmp_path):
        """A database from before keys existed, with words differing in capitals."""
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE words (id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE,
                last_seen TEXT, times_shown INTEGER NOT NULL DEFAULT 0,
                times_correct INTEGER NOT NULL DEFAULT 0,
                recent_results TEXT NOT NULL DEFAULT '',
                last_ten_correct INTEGER NOT NULL DEFAULT 0, last_practiced TEXT,
                half_life_hours REAL NOT NULL DEFAULT 24);
            CREATE TABLE meanings (word_id INTEGER NOT NULL REFERENCES words(id)
                ON DELETE CASCADE, english TEXT NOT NULL, PRIMARY KEY (word_id, english));
            INSERT INTO words VALUES (1, 'Hola', '2026-11-01T10:00:00', 2, 1, '01', 1,
                                      '2026-11-01T10:00:00', 24);
            INSERT INTO words VALUES (2, 'hola', '2026-11-05T10:00:00', 5, 4, '11101', 4,
                                      '2026-11-05T10:00:00', 96);
            INSERT INTO words VALUES (3, 'Gato', NULL, 0, 0, '', 0, NULL, 24);
            INSERT INTO meanings VALUES (1, 'Hello');
            INSERT INTO meanings VALUES (2, 'hello');
            INSERT INTO meanings VALUES (2, 'Hi');
            INSERT INTO meanings VALUES (3, 'Cat');
            INSERT INTO meanings VALUES (3, 'cat');
            """
        )
        old.commit()
        old.close()
        return str(path)

    def test_old_database_is_merged(self, tmp_path):
        conn = vocab_db.connect(self.old_database(tmp_path))
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",), "Hola": ("Hello", "Hi")}
        assert vocab_db.load_stats(conn, "Hola") == {
            "times_shown": 7,                       # added up
            "times_correct": 5,
            "recent_results": "11101",              # from the more practised 'hola'
            "last_ten_correct": 4,
            "last_seen": "2026-11-05T10:00:00",     # the latest
            "last_practiced": "2026-11-05T10:00:00",
            "half_life_hours": 96.0,
        }
        conn.close()

    def test_merge_runs_once_and_keeps_working(self, tmp_path):
        path = self.old_database(tmp_path)
        vocab_db.connect(path).close()
        conn = vocab_db.connect(path)
        assert vocab_db.add_meanings(conn, "HOLA", ["hi"]) == 0
        assert len(vocab_db.load_words(conn)) == 2
        conn.close()


class TestDeleteAll:
    def test_removes_everything_but_settings(self, conn):
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",), "Banco": ("Bank", "Bench")})
        vocab_db.record_guess(conn, "Hola", True)
        vocab_db.set_setting(conn, "choice_method", "time")
        assert vocab_db.delete_all_words(conn) == 2
        assert vocab_db.load_words(conn) == {}
        assert conn.execute("SELECT COUNT(*) FROM meanings").fetchone()[0] == 0
        assert vocab_db.get_setting(conn, "choice_method") == "time"

    def test_starter_list_not_loaded_again(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",)})
        vocab_db.delete_all_words(conn)
        conn.close()
        conn = vocab_db.connect(path)
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",)})
        assert vocab_db.load_words(conn) == {}
        conn.close()

    def test_existing_list_is_marked_as_seeded(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",)})
        vocab_db.delete_all_words(conn)
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",)})
        assert vocab_db.load_words(conn) == {}

    def test_clean_import_replaces_the_list(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.add_meanings(conn, "Perro", ["Dog"])
        result = vocab_db.import_text(conn, "Hola,Hello\nbad line\n", start_clean=True)
        assert result == {"added": 1, "skipped": 0, "errors": ["Line 2: bad line"], "removed": 2}
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}

    def test_clean_import_of_unreadable_file_keeps_the_list(self, conn):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        result = vocab_db.import_text(conn, "nothing\n\n", start_clean=True)
        assert result["removed"] == 0
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",)}


class TestBackupRestore:
    def test_round_trip_keeps_everything(self, conn):
        vocab_db.seed_if_empty(conn, {"Hola": ("Hello",), "Banco": ("Bank", "Bench")})
        vocab_db.record_guess(conn, "Hola", True, datetime(2026, 11, 7, 9, 0, 0))
        vocab_db.set_setting(conn, "choice_method", "time")
        data = vocab_db.backup_bytes(conn)

        other = vocab_db.connect(":memory:")
        vocab_db.add_meanings(other, "Gato", ["Cat"])
        other.commit()
        assert vocab_db.restore_from_bytes(other, data) == 2
        assert vocab_db.load_words(other) == vocab_db.load_words(conn)
        assert vocab_db.load_stats(other, "Hola") == vocab_db.load_stats(conn, "Hola")
        assert vocab_db.get_setting(other, "choice_method") == "time"
        other.close()

    def test_restored_database_keeps_working(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        data = vocab_db.backup_bytes(conn)
        other = vocab_db.connect(":memory:")
        vocab_db.restore_from_bytes(other, data)
        assert vocab_db.add_meanings(other, "HOLA", ["hello"]) == 0  # keys still unique
        vocab_db.delete_word(other, "Hola")
        assert vocab_db.load_words(other) == {}
        other.close()

    @pytest.mark.parametrize("data", [b"", b"not a database at all", b"SQLite format 3\x00junk"])
    def test_rejects_files_that_are_not_backups(self, conn, data):
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        conn.commit()
        with pytest.raises(ValueError, match="isn't a Vocab Match backup"):
            vocab_db.restore_from_bytes(conn, data)
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",)}

    def test_rejects_other_sqlite_databases(self, conn):
        other = sqlite3.connect(":memory:")
        other.execute("CREATE TABLE something (x)")
        data = other.serialize()
        other.close()
        with pytest.raises(ValueError):
            vocab_db.restore_from_bytes(conn, data)

    def test_older_backup_is_brought_up_to_date(self, conn):
        old = sqlite3.connect(":memory:")
        old.executescript(
            """
            CREATE TABLE words (id INTEGER PRIMARY KEY, spanish TEXT NOT NULL UNIQUE,
                                last_seen TEXT);
            CREATE TABLE meanings (word_id INTEGER NOT NULL, english TEXT NOT NULL,
                                   PRIMARY KEY (word_id, english));
            INSERT INTO words (id, spanish) VALUES (1, 'Hola');
            INSERT INTO meanings VALUES (1, 'Hello');
            """
        )
        data = old.serialize()
        old.close()
        assert vocab_db.restore_from_bytes(conn, data) == 1
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}
        assert vocab_db.load_stats(conn, "Hola")["half_life_hours"] == 24.0


class TestDeleteWord:
    def test_removes_word_meanings_and_history(self, conn):
        vocab_db.add_meanings(conn, "Banco", ["Bank", "Bench"])
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Banco", True)
        assert vocab_db.delete_word(conn, "Banco") is True
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}
        assert vocab_db.load_stats(conn, "Banco") is None
        assert conn.execute("SELECT COUNT(*) FROM meanings").fetchone()[0] == 1

    def test_missing_word(self, conn):
        assert vocab_db.delete_word(conn, "Nada") is False

    def test_commits(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        conn.commit()
        vocab_db.delete_word(conn, "Hola")
        other = sqlite3.connect(path)
        assert other.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 0
        other.close()
        conn.close()


class TestSetLastSeen:
    def test_stores_given_time(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.set_last_seen(conn, "Hola", datetime(2026, 11, 7, 15, 35, 12, 999))
        value = conn.execute("SELECT last_seen FROM words").fetchone()[0]
        assert value == "2026-11-07T15:35:12"

    def test_defaults_to_now(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        before = datetime.now().replace(microsecond=0)
        vocab_db.set_last_seen(conn, "Hola")
        value = datetime.fromisoformat(
            conn.execute("SELECT last_seen FROM words").fetchone()[0]
        )
        assert before <= value <= datetime.now()


class TestRecordGuess:
    def test_new_word_starts_at_zero(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        assert vocab_db.load_stats(conn, "Hola") == {
            "times_shown": 0,
            "times_correct": 0,
            "recent_results": "",
            "last_ten_correct": 0,
            "last_seen": None,
            "last_practiced": None,
            "half_life_hours": 24.0,
        }

    def test_correct_guess(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Hola", True, datetime(2026, 11, 7, 9, 0, 0))
        assert vocab_db.load_stats(conn, "Hola") == {
            "times_shown": 1,
            "times_correct": 1,
            "recent_results": "1",
            "last_ten_correct": 1,
            "last_seen": "2026-11-07T09:00:00",
            "last_practiced": "2026-11-07T09:00:00",
            "half_life_hours": 48.0,
        }

    def test_wrong_guess_leaves_last_seen(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Hola", True, datetime(2026, 11, 7, 9, 0, 0))
        vocab_db.record_guess(conn, "Hola", False, datetime(2026, 11, 8, 9, 0, 0))
        stats = vocab_db.load_stats(conn, "Hola")
        assert stats["times_shown"] == 2
        assert stats["times_correct"] == 1
        assert stats["recent_results"] == "10"
        assert stats["last_ten_correct"] == 1
        assert stats["last_seen"] == "2026-11-07T09:00:00"

    def test_only_last_ten_guesses_count(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        results = [True] * 5 + [False] * 8 + [True, True]
        for correct in results:
            vocab_db.record_guess(conn, "Hola", correct)
        stats = vocab_db.load_stats(conn, "Hola")
        assert stats["times_shown"] == 15
        assert stats["times_correct"] == 7
        assert stats["recent_results"] == "0000000011"
        assert stats["last_ten_correct"] == 2

    def test_other_words_untouched(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.record_guess(conn, "Hola", False)
        assert vocab_db.load_stats(conn, "Gato")["times_shown"] == 0

    def test_wrong_guess_sets_last_practiced_but_not_last_seen(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Hola", False, datetime(2026, 11, 7, 9, 0, 0))
        stats = vocab_db.load_stats(conn, "Hola")
        assert stats["last_practiced"] == "2026-11-07T09:00:00"
        assert stats["last_seen"] is None

    def test_half_life_doubles_when_right_and_halves_when_wrong(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        half_lives = []
        for correct in (True, True, False, True):
            vocab_db.record_guess(conn, "Hola", correct)
            half_lives.append(vocab_db.load_stats(conn, "Hola")["half_life_hours"])
        assert half_lives == [48.0, 96.0, 48.0, 96.0]

    def test_load_all_stats_matches_load_stats(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.add_meanings(conn, "Gato", ["Cat"])
        vocab_db.record_guess(conn, "Hola", True)
        everything = vocab_db.load_all_stats(conn)
        assert everything == {
            "Hola": vocab_db.load_stats(conn, "Hola"),
            "Gato": vocab_db.load_stats(conn, "Gato"),
        }

    def test_unknown_word_ignored(self, conn):
        vocab_db.record_guess(conn, "Nada", True)
        assert vocab_db.load_stats(conn, "Nada") is None

    def test_stats_survive_rename(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Hola", True)
        vocab_db.update_word(conn, "Hola", "Buenas", ["Hi"])
        assert vocab_db.load_stats(conn, "Buenas")["times_correct"] == 1

    def test_commits(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.record_guess(conn, "Hola", False)
        other = sqlite3.connect(path)
        assert other.execute("SELECT times_shown FROM words").fetchone()[0] == 1
        other.close()
        conn.close()

    def test_adds_columns_to_older_database(self, tmp_path):
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE words (id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE,
                                last_seen TEXT);
            CREATE TABLE meanings (word_id INTEGER NOT NULL, english TEXT NOT NULL,
                                   PRIMARY KEY (word_id, english));
            INSERT INTO words (id, word, last_seen) VALUES (1, 'Hola', '2026-01-01T00:00:00');
            INSERT INTO meanings VALUES (1, 'Hello');
            """
        )
        old.commit()
        old.close()

        conn = vocab_db.connect(str(path))
        assert vocab_db.load_stats(conn, "Hola") == {
            "times_shown": 0,
            "times_correct": 0,
            "recent_results": "",
            "last_ten_correct": 0,
            "last_seen": "2026-01-01T00:00:00",
            # Filled in from last_seen when the column is added.
            "last_practiced": "2026-01-01T00:00:00",
            "half_life_hours": 24.0,
        }
        vocab_db.record_guess(conn, "Hola", True)
        assert vocab_db.load_stats(conn, "Hola")["last_ten_correct"] == 1
        conn.close()

        # Opening it again doesn't try to add the columns twice.
        vocab_db.connect(str(path)).close()


class TestSettings:
    def test_missing_setting_gives_default(self, conn):
        assert vocab_db.get_setting(conn, "choice_method") is None
        assert vocab_db.get_setting(conn, "choice_method", "rank") == "rank"

    def test_set_and_overwrite(self, conn):
        vocab_db.set_setting(conn, "choice_method", "time")
        vocab_db.set_setting(conn, "choice_method", "rank")
        assert vocab_db.get_setting(conn, "choice_method") == "rank"

    def test_persists(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.set_setting(conn, "choice_method", "time")
        conn.close()
        conn = vocab_db.connect(path)
        assert vocab_db.get_setting(conn, "choice_method") == "time"
        conn.close()

    def test_older_database_gets_settings_table(self, tmp_path):
        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE words (id INTEGER PRIMARY KEY, word TEXT NOT NULL UNIQUE,"
            " last_seen TEXT);"
        )
        old.close()
        conn = vocab_db.connect(str(path))
        vocab_db.set_setting(conn, "choice_method", "time")
        assert vocab_db.get_setting(conn, "choice_method") == "time"
        conn.close()


class TestImportText:
    def test_csv_rows(self, conn):
        result = vocab_db.import_text(conn, "Hola,Hello\nBanco,Bank\nBanco,Bench\n")
        assert result == {"added": 3, "skipped": 0, "errors": [], "removed": 0}
        assert vocab_db.load_words(conn) == {
            "Banco": ("Bank", "Bench"),
            "Hola": ("Hello",),
        }

    def test_pipe_lines(self, conn):
        result = vocab_db.import_text(conn, "Hola|Hello\nGato | Cat\n")
        assert result["added"] == 2
        assert vocab_db.load_words(conn) == {"Gato": ("Cat",), "Hola": ("Hello",)}

    def test_strips_whitespace_and_skips_blank_lines(self, conn):
        result = vocab_db.import_text(conn, "  Hola , Hello \n\n , \n")
        assert result == {"added": 1, "skipped": 0, "errors": [], "removed": 0}
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}

    def test_quoted_cells_with_commas(self, conn):
        vocab_db.import_text(conn, '"Por favor",Please\nTiempo,"Time, weather"\n')
        assert vocab_db.load_words(conn)["Tiempo"] == ("Time, weather",)

    def test_skips_duplicates_already_stored_and_within_file(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        result = vocab_db.import_text(conn, "Hola,Hello\nGato,Cat\nGato,Cat\n")
        assert result == {"added": 1, "skipped": 2, "errors": [], "removed": 0}

    def test_reports_bad_lines_with_line_numbers(self, conn):
        text = "Hola,Hello\nonlyone\nGato,\n|Cat\nPerro,Dog\n"
        result = vocab_db.import_text(conn, text)
        assert result["added"] == 2
        assert result["errors"] == ["Line 2: onlyone", "Line 3: Gato,", "Line 4: |Cat"]

    def test_extra_columns_ignored(self, conn):
        vocab_db.import_text(conn, "Hola,Hello,greeting\n")
        assert vocab_db.load_words(conn) == {"Hola": ("Hello",)}

    def test_commits(self, tmp_path):
        path = str(tmp_path / "v.db")
        conn = vocab_db.connect(path)
        vocab_db.import_text(conn, "Hola,Hello\n")
        other = sqlite3.connect(path)
        assert other.execute("SELECT COUNT(*) FROM words").fetchone()[0] == 1
        other.close()
        conn.close()


class TestExportCsv:
    def test_one_row_per_meaning_sorted(self, conn):
        vocab_db.add_meanings(conn, "Hola", ["Hello"])
        vocab_db.add_meanings(conn, "Banco", ["Bank", "Bench"])
        assert vocab_db.export_csv(conn) == "Banco,Bank\nBanco,Bench\nHola,Hello\n"

    def test_quotes_commas(self, conn):
        vocab_db.add_meanings(conn, "Tiempo", ["Time, weather"])
        assert vocab_db.export_csv(conn) == 'Tiempo,"Time, weather"\n'

    def test_empty(self, conn):
        assert vocab_db.export_csv(conn) == ""

    def test_round_trip(self, conn):
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        other = vocab_db.connect(":memory:")
        vocab_db.import_text(other, vocab_db.export_csv(conn))
        assert vocab_db.load_words(other) == vocab_db.load_words(conn)
        other.close()


# ---------------------------------------------------------------------------
# main.py
# ---------------------------------------------------------------------------


class TestSeedData:
    def test_seed_words_have_meanings(self):
        assert all(main.SEED_WORDS[w] for w in main.SEED_WORDS)

    def test_enough_words_to_fill_the_board(self):
        assert len(main.SEED_WORDS) > main.ON_SCREEN


@pytest.mark.parametrize(
    "seconds, shown", [(60, "1:00"), (30, "0:30"), (9, "0:09"), (0, "0:00"), (75, "1:15")]
)
def test_clock(seconds, shown):
    assert main.clock(seconds) == shown


def test_database_path_in_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = main.database_path()
    assert path == str(tmp_path / "vocab_match" / "vocab.db")
    assert (tmp_path / "vocab_match").is_dir()


class TestStartup:
    def test_shows_home_screen(self, page):
        assert heading(page) == "Vocabulary Match"
        for label in ("Start 30s game", "Start 1min game", "Vocab management", "Settings"):
            find_button(page, label)
        assert page.title == "Vocabulary Match"

    def test_seeds_database_and_loads_words(self, page, db_file):
        assert main.WORDS == main.SEED_WORDS
        conn = vocab_db.connect(str(db_file))
        assert len(vocab_db.load_words(conn)) == len(main.SEED_WORDS)
        conn.close()

    def test_uses_existing_database_without_seeding(self, db_file):
        conn = vocab_db.connect(str(db_file))
        for word, meaning in [("Uno", "One"), ("Dos", "Two"), ("Tres", "Three"),
                              ("Cuatro", "Four"), ("Cinco", "Five"), ("Seis", "Six")]:
            vocab_db.add_meanings(conn, word, [meaning])
        conn.commit()
        conn.close()

        main.main(FakePage())
        assert set(main.WORDS) == {"Uno", "Dos", "Tres", "Cuatro", "Cinco", "Seis"}

    def test_home_screen_is_centred(self, page):
        assert page.horizontal_alignment == ft.CrossAxisAlignment.CENTER
        assert page.vertical_alignment == ft.MainAxisAlignment.CENTER

    @pytest.mark.parametrize("label", ["Start 30s game", "Start 1min game"])
    def test_game_screen_is_centred(self, page, label):
        click(page, label)
        assert page.horizontal_alignment == ft.CrossAxisAlignment.CENTER
        assert page.vertical_alignment == ft.MainAxisAlignment.CENTER

    def test_vocab_screen_is_centred_across_and_starts_at_the_top(self, page):
        click(page, "Vocab management")
        assert page.horizontal_alignment == ft.CrossAxisAlignment.CENTER
        assert page.vertical_alignment == ft.MainAxisAlignment.START
        assert current_view(page).max_width == main.VOCAB_MAX_WIDTH

    def test_vocab_back_arrow_is_at_the_top(self, page):
        click(page, "Vocab management")
        # No big back button at the bottom of the list, only the arrow in the bar.
        assert not any(
            isinstance(c, ft.Button) and c.content == "Back to menu"
            for c in walk(current_view(page))
        )
        assert page.appbar.leading.icon == ft.Icons.ARROW_BACK
        click(page, "Back to menu")
        assert heading(page) == "Vocabulary Match"
        assert page.appbar is None

    def big_buttons(self, page):
        return [
            c.content for c in walk(current_view(page))
            if isinstance(c, ft.Button) and c.content in ("Back", "Back to menu")
        ]

    def test_upload_back_arrow_goes_to_vocab_screen(self, page):
        click(page, "Vocab management")
        click(page, "Upload list via CSV")
        assert self.big_buttons(page) == []
        assert page.appbar.leading.icon == ft.Icons.ARROW_BACK
        assert page.appbar.leading.tooltip == "Back"
        click(page, "Back")
        assert heading(page) == "Vocab management"
        assert page.appbar.leading.tooltip == "Back to menu"

    def test_settings_back_arrow_goes_to_menu(self, page):
        click(page, "Settings")
        assert self.big_buttons(page) == []
        assert page.appbar.leading.icon == ft.Icons.ARROW_BACK
        click(page, "Back to menu")
        assert heading(page) == "Vocabulary Match"
        assert page.appbar is None

    @pytest.mark.parametrize("label", ["Settings"])
    def test_other_screens_start_at_the_top(self, page, label):
        click(page, label)
        assert page.horizontal_alignment == ft.CrossAxisAlignment.START
        assert page.vertical_alignment == ft.MainAxisAlignment.START
        click(page, "Back to menu")
        assert page.vertical_alignment == ft.MainAxisAlignment.CENTER

    def test_registers_file_picker(self, page):
        assert any(isinstance(s, ft.FilePicker) for s in page.services)


class TestGame:
    def start(self, page, label="Start 30s game"):
        click(page, label)
        return bubbles(page)

    def label(self, bubble):
        """The stored spelling of the word or meaning on a bubble."""
        return bubble.data

    def find_pair(self, left, right, correct=True):
        """Return (left index, right index) of a matching (or non-matching) pair."""
        for li, lb in enumerate(left):
            for ri, rb in enumerate(right):
                if (self.label(rb) in main.WORDS[self.label(lb)]) == correct:
                    return li, ri
        raise AssertionError("No such pair on the board")

    def test_start_shows_board_and_timer(self, page):
        left, right = self.start(page)
        assert timer(page) == "0:30"
        assert score(page) == "0"
        assert "Time's up!" not in texts(page)
        assert not find_button(page, "Play again").visible
        assert len(left) == len(right) == main.ON_SCREEN

    def test_board_shrinks_to_fit_narrow_screens(self, page):
        self.start(page)
        board = next(
            c for c in walk(current_view(page))
            if isinstance(c, ft.Row) and c.max_width == main.BOARD_MAX_WIDTH
        )
        # A typical phone is 360-390 px wide, minus the page padding.
        assert main.BOARD_MAX_WIDTH <= 360
        for column in board.controls:
            assert column.expand == 1
            for bubble in column.controls:
                assert bubble.width is None  # takes the column's width

    def test_game_screen_shows_only_timer_score_and_board(self, page):
        left, right = self.start(page)
        words = [self.label(b).lower() for b in left + right]
        assert sorted(texts(page)) == sorted(["0:30", "0"] + words)
        assert not any(
            isinstance(c, ft.Button) and c.visible for c in walk(current_view(page))
        )
        assert any(
            isinstance(c, ft.Icon) and c.icon == ft.Icons.TIMER_OUTLINED
            for c in walk(current_view(page))
        )

    def test_back_arrow_only_on_game_screen(self, page):
        assert page.appbar is None
        self.start(page)
        assert page.appbar.leading.icon == ft.Icons.ARROW_BACK
        click(page, "Back to menu")
        assert heading(page) == "Vocabulary Match"
        assert page.appbar is None

    def test_guesses_show_no_messages(self, page, no_sleep):
        left, right = self.start(page)
        page.tasks.clear()
        for correct in (False, True):
            li, ri = self.find_pair(left, right, correct=correct)
            left[li].on_click(None)
            right[ri].on_click(None)
            run_tasks(page)
            assert sorted(texts(page)) == sorted(
                ["0:30", score(page)] + [self.label(b).lower() for b in left + right]
            )

    def test_board_shows_lower_case(self, page):
        left, right = self.start(page)
        for bubble in left + right:
            assert bubble.content.value == bubble.data.lower()
        assert any(b.data != b.content.value for b in left)  # seed words are capitalised

    def test_meanings_differing_only_in_capitals_never_share_the_board(self, db_file):
        conn = vocab_db.connect(str(db_file))
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        vocab_db.add_meanings(conn, "Saludo", ["hello"])  # Hola means "Hello"
        conn.commit()
        conn.close()
        page = FakePage()
        main.main(page)
        for _ in range(60):
            left, _ = self.start(page)
            words = {self.label(b) for b in left}
            assert not {"Hola", "Saludo"} <= words
            click(page, "Back to menu")

    def test_one_minute_game(self, page):
        self.start(page, "Start 1min game")
        assert timer(page) == "1:00"

    def test_starting_queues_countdown(self, page):
        self.start(page)
        assert [f.__name__ for f, _ in page.tasks] == ["countdown"]

    def test_board_has_one_meaning_per_word_with_no_clashes(self, page):
        for _ in range(20):
            left, right = self.start(page)
            words = [self.label(b) for b in left]
            assert len(set(words)) == main.ON_SCREEN
            meanings = [m for w in words for m in main.WORDS[w]]
            assert len(meanings) == len(set(meanings)), "two words share a meaning"
            # Every English word on the right belongs to exactly one word on the left.
            for b in right:
                owners = [w for w in words if self.label(b) in main.WORDS[w]]
                assert len(owners) == 1
            click(page, "Back to menu")

    def test_selecting_and_deselecting(self, page):
        left, _ = self.start(page)
        left[0].on_click(None)
        assert left[0].bgcolor == main.SELECTED
        left[1].on_click(None)
        assert left[0].bgcolor == main.NORMAL
        assert left[1].bgcolor == main.SELECTED
        left[1].on_click(None)
        assert left[1].bgcolor == main.NORMAL

    def test_correct_match(self, page, no_sleep):
        left, right = self.start(page)
        page.tasks.clear()  # skip the countdown
        li, ri = self.find_pair(left, right)
        old_word = self.label(left[li])

        left[li].on_click(None)
        right[ri].on_click(None)
        assert score(page) == "1"
        assert [f.__name__ for f, _ in page.tasks] == ["on_correct"]

        # Matched bubbles ignore clicks until the animation is done.
        left[li].on_click(None)
        assert left[li].bgcolor != main.SELECTED

        run_tasks(page)
        new_word = self.label(left[li])
        assert new_word != old_word
        assert self.label(right[ri]) in main.WORDS[new_word]
        assert left[li].opacity == right[ri].opacity == 1
        assert left[li].bgcolor == right[ri].bgcolor == main.NORMAL

        # The board still has no clashing meanings.
        words = [self.label(b) for b in left]
        meanings = [m for w in words for m in main.WORDS[w]]
        assert len(meanings) == len(set(meanings))

    def test_wrong_match(self, page, monkeypatch):
        left, right = self.start(page)
        page.tasks.clear()
        li, ri = self.find_pair(left, right, correct=False)
        words_before = [self.label(b) for b in left]

        right[ri].on_click(None)
        left[li].on_click(None)
        assert score(page) == "0"
        assert [f.__name__ for f, _ in page.tasks] == ["on_wrong"]

        # The pair flashes red during the animation.
        colours = []

        async def record(_seconds):
            colours.append((left[li].bgcolor, right[ri].bgcolor))

        monkeypatch.setattr(main.asyncio, "sleep", record)
        run_tasks(page)
        assert colours[0] == (main.WRONG, main.WRONG)
        assert left[li].bgcolor == right[ri].bgcolor == main.NORMAL
        assert left[li].offset == ft.Offset(0, 0)
        assert [self.label(b) for b in left] == words_before

        # The pair can be clicked again.
        left[li].on_click(None)
        assert left[li].bgcolor == main.SELECTED

    def last_seen(self, db_file):
        conn = vocab_db.connect(str(db_file))
        rows = dict(conn.execute("SELECT word, last_seen FROM words"))
        conn.close()
        return rows

    def test_correct_match_records_last_seen(self, page, db_file):
        left, right = self.start(page)
        li, ri = self.find_pair(left, right)
        word = self.label(left[li])

        before = datetime.now().replace(microsecond=0)
        left[li].on_click(None)
        right[ri].on_click(None)

        seen = self.last_seen(db_file)
        assert before <= datetime.fromisoformat(seen[word]) <= datetime.now()
        assert all(value is None for w, value in seen.items() if w != word)

    def test_wrong_match_does_not_record_last_seen(self, page, db_file):
        left, right = self.start(page)
        li, ri = self.find_pair(left, right, correct=False)
        left[li].on_click(None)
        right[ri].on_click(None)
        assert all(value is None for value in self.last_seen(db_file).values())

    def stats(self, db_file, word):
        conn = vocab_db.connect(str(db_file))
        stats = vocab_db.load_stats(conn, word)
        conn.close()
        return stats

    def test_guesses_update_left_word_stats_only(self, page, db_file, no_sleep):
        left, right = self.start(page)
        page.tasks.clear()

        wrong_li, wrong_ri = self.find_pair(left, right, correct=False)
        word = self.label(left[wrong_li])
        owner = next(
            self.label(b) for b in left
            if self.label(right[wrong_ri]) in main.WORDS[self.label(b)]
        )
        left[wrong_li].on_click(None)
        right[wrong_ri].on_click(None)
        run_tasks(page)

        right_ri = next(
            i for i, b in enumerate(right) if self.label(b) in main.WORDS[word]
        )
        left[wrong_li].on_click(None)
        right[right_ri].on_click(None)

        stats = self.stats(db_file, word)
        assert stats["times_shown"] == 2
        assert stats["times_correct"] == 1
        assert stats["recent_results"] == "01"
        assert stats["last_ten_correct"] == 1

        # The word whose meaning was wrongly picked isn't marked.
        assert self.stats(db_file, owner)["times_shown"] == 0

    def page_with_history(self, db_file, old=(), missed=(), method="time"):
        """Start the app with every seed word just matched, except the given ones.

        Uses the time method by default: with every recent word matched at the
        same moment, the overdue words then win every deal.
        """
        conn = vocab_db.connect(str(db_file))
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        vocab_db.set_setting(conn, "choice_method", method)
        now = datetime.now()
        for word in main.SEED_WORDS:
            # 60 half-lives back, so recent words score ~1e-18 next to these.
            when = now - timedelta(days=60) if word in old else now
            vocab_db.set_last_seen(conn, word, when)
        for word in missed:
            for _ in range(10):
                vocab_db.record_guess(conn, word, False)
        conn.close()
        p = FakePage()
        main.main(p)
        return p

    def test_deal_favours_overdue_words(self, db_file):
        page = self.page_with_history(db_file, old={"Gato", "Pan"})
        for _ in range(30):
            left, _ = self.start(page)
            assert {"Gato", "Pan"} <= {self.label(b) for b in left}
            click(page, "Back to menu")

    def test_deal_favours_often_missed_words(self, db_file):
        page = self.page_with_history(db_file, old={"Gato"}, missed={"Sol"})
        for _ in range(30):
            left, _ = self.start(page)
            assert {"Gato", "Sol"} <= {self.label(b) for b in left}
            click(page, "Back to menu")

    def test_replacement_favours_overdue_words(self, db_file, no_sleep):
        # Six overdue words: five fill the board, one is left waiting in the pool.
        old = {"Gato", "Pan", "Sol", "Luna", "Mesa", "Silla"}
        page = self.page_with_history(db_file, old=old)
        left, right = self.start(page)
        page.tasks.clear()
        on_board = {self.label(b) for b in left}
        assert on_board < old
        (waiting,) = old - on_board

        li, ri = self.find_pair(left, right)
        left[li].on_click(None)
        right[ri].on_click(None)
        run_tasks(page)
        assert self.label(left[li]) == waiting

    def test_deal_still_avoids_clashing_meanings(self, db_file):
        page = self.page_with_history(db_file, old={"Gato"}, missed={"Banco"})
        for _ in range(30):
            left, right = self.start(page)
            words = [self.label(b) for b in left]
            meanings = [m for w in words for m in main.WORDS[w]]
            assert len(meanings) == len(set(meanings))
            click(page, "Back to menu")

    def test_rank_method_favours_words_matched_longest_ago(self, db_file):
        # Give every word its own time, a minute apart, oldest first.
        conn = vocab_db.connect(str(db_file))
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        vocab_db.set_setting(conn, "choice_method", "rank")
        order = list(main.SEED_WORDS)
        start = datetime(2026, 11, 1, 9, 0, 0)
        for i, word in enumerate(order):
            vocab_db.set_last_seen(conn, word, start + timedelta(minutes=i))
        conn.close()
        page = FakePage()
        main.main(page)

        counts = dict.fromkeys(order, 0)
        for _ in range(300):
            left, _ = self.start(page)
            for b in left:
                counts[self.label(b)] += 1
            click(page, "Back to menu")

        oldest_five = sum(counts[w] for w in order[:5])
        newest_five = sum(counts[w] for w in order[-5:])
        assert oldest_five > 5 * newest_five

    def test_memory_method_favours_weak_and_overdue_words(self, db_file):
        conn = vocab_db.connect(str(db_file))
        vocab_db.seed_if_empty(conn, main.SEED_WORDS)
        vocab_db.set_setting(conn, "choice_method", "memory")
        now = datetime.now()
        weak = {"Gato", "Pan", "Sol", "Luna", "Mesa"}
        for word in main.SEED_WORDS:
            if word in weak:
                # Practised a week ago and only ever got right once: long forgotten.
                vocab_db.record_guess(conn, word, True, now - timedelta(days=7))
            else:
                # Known very well (half-life grown to a year) and practised just now.
                for _ in range(15):
                    vocab_db.record_guess(conn, word, True, now)
        conn.close()
        page = FakePage()
        main.main(page)

        for _ in range(30):
            left, _ = self.start(page)
            assert {self.label(b) for b in left} == weak
            click(page, "Back to menu")

    def test_words_on_board_but_not_guessed_are_not_counted(self, page, db_file):
        left, _ = self.start(page)
        for b in left:
            assert self.stats(db_file, self.label(b))["times_shown"] == 0

    def test_many_correct_matches_keep_board_valid(self, page, no_sleep):
        left, right = self.start(page)
        page.tasks.clear()
        for n in range(1, 41):
            li, ri = self.find_pair(left, right)
            left[li].on_click(None)
            right[ri].on_click(None)
            run_tasks(page)
            assert score(page) == str(n)
            words = [self.label(b) for b in left]
            assert len(set(words)) == main.ON_SCREEN

    def test_countdown_ends_game(self, page, no_sleep):
        left, _ = self.start(page)
        run_tasks(page)
        assert timer(page) == "0:00"
        assert "Time's up!" in texts(page)
        assert find_button(page, "Play again").visible

        # Clicks do nothing once the time is up.
        left[0].on_click(None)
        assert left[0].bgcolor == main.NORMAL

    def test_end_message_counts_matches(self, page, no_sleep):
        left, right = self.start(page, "Start 1min game")
        countdown = page.tasks.pop()
        li, ri = self.find_pair(left, right)
        left[li].on_click(None)
        right[ri].on_click(None)
        run_tasks(page)
        page.tasks.append(countdown)
        run_tasks(page)
        assert "Time's up!" in texts(page)
        assert score(page) == "1"

    def test_play_again_keeps_time_limit(self, page, no_sleep):
        self.start(page, "Start 1min game")
        run_tasks(page)
        click(page, "Play again")
        assert timer(page) == "1:00"
        assert score(page) == "0"
        assert "Time's up!" not in texts(page)
        assert not find_button(page, "Play again").visible

    def test_back_to_menu_stops_old_timer(self, page, no_sleep):
        self.start(page)
        click(page, "Back to menu")
        assert heading(page) == "Vocabulary Match"
        run_tasks(page)  # the stale countdown exits straight away
        assert "Time's up!" not in texts(page)

    def test_stale_animation_after_restart_does_nothing(self, page, no_sleep):
        left, right = self.start(page)
        page.tasks.clear()
        li, ri = self.find_pair(left, right)
        left[li].on_click(None)
        right[ri].on_click(None)
        stale = page.tasks.pop()

        click(page, "Back to menu")
        left, _ = self.start(page)
        words = [self.label(b) for b in left]
        page.tasks = [stale]
        run_tasks(page)
        assert [self.label(b) for b in left] == words


class TestSettingsScreen:
    def open(self, page):
        click(page, "Settings")
        assert heading(page) == "Settings"
        return find_first(page, ft.RadioGroup)

    def saved(self, db_file):
        conn = vocab_db.connect(str(db_file))
        value = vocab_db.get_setting(conn, "choice_method")
        conn.close()
        return value

    def test_offers_all_methods_with_memory_selected(self, page):
        group = self.open(page)
        radios = [c for c in walk(group) if isinstance(c, ft.Radio)]
        assert [r.value for r in radios] == ["rank", "time", "memory"]
        assert group.value == "memory"
        labels = {r.value: r.label for r in radios}
        assert labels["memory"].endswith("(default)")
        assert "(default)" not in labels["rank"]
        assert any("24 hours" in t for t in texts(page))

    def choose(self, group, value):
        group.value = value
        group.on_change(None)

    def test_choosing_time_saves_it(self, page, db_file):
        group = self.open(page)
        self.choose(group, "time")
        assert self.saved(db_file) == "time"

    def test_choice_kept_after_restart(self, page, db_file):
        self.choose(self.open(page), "time")
        again = FakePage()
        main.main(again)
        assert self.open(again).value == "time"

    def test_choice_is_used_for_scoring(self, page, monkeypatch):
        methods = []
        real_scores = main.choice_function.scores

        def spy(stats, method="memory", *args, **kwargs):
            methods.append(method)
            return real_scores(stats, method, *args, **kwargs)

        monkeypatch.setattr(main.choice_function, "scores", spy)
        self.choose(self.open(page), "time")
        click(page, "Back to menu")
        click(page, "Start 30s game")
        assert methods and set(methods) == {"time"}

        methods.clear()
        click(page, "Back to menu")
        self.choose(self.open(page), "rank")
        click(page, "Back to menu")
        click(page, "Start 30s game")
        assert methods and set(methods) == {"rank"}

    def test_unknown_saved_value_falls_back_to_memory(self, db_file):
        conn = vocab_db.connect(str(db_file))
        vocab_db.set_setting(conn, "choice_method", "fastest")
        conn.close()
        page = FakePage()
        main.main(page)
        assert self.open(page).value == "memory"
        click(page, "Back to menu")
        click(page, "Start 30s game")  # deals without error

    def test_reopening_shows_current_choice(self, page):
        group = self.open(page)
        self.choose(group, "time")
        click(page, "Back to menu")
        assert self.open(page).value == "time"


class TestVocabScreen:
    def open(self, page):
        click(page, "Vocab management")
        assert heading(page) == "Vocab management"

    def form(self, page):
        return next(
            c for c in walk(current_view(page))
            if isinstance(c, ft.Container) and isinstance(c.content, ft.Column)
            and isinstance(c.content.controls[0], ft.Text)
            and any(isinstance(x, ft.TextField) for x in c.content.controls)
        )

    def fields(self, page):
        word, english = [
            c for c in walk(self.form(page)) if isinstance(c, ft.TextField)
        ]
        return word, english

    def search(self, page, text):
        field = find_first(page, ft.TextField)
        field.value = text
        field.on_change(None)

    def test_lists_all_words_sorted(self, page):
        self.open(page)
        assert table_words(page) == sorted(main.SEED_WORDS, key=vocab_db.sort_key)
        table = find_first(page, ft.DataTable)
        banco = next(r for r in table.rows if r.data == "Banco")
        assert banco.cells[0].content.value == "banco"
        assert banco.cells[1].content.value == "bank, bench"

    def test_search_by_word_ignores_accents(self, page):
        self.open(page)
        self.search(page, "adios")
        assert table_words(page) == ["Adiós"]

    def test_search_by_english(self, page):
        self.open(page)
        self.search(page, "  BENCH ")
        assert table_words(page) == ["Banco"]

    def test_list_shows_lower_case(self, page):
        self.open(page)
        table = find_first(page, ft.DataTable)
        for row in table.rows:
            assert row.cells[0].content.value == row.data.lower()

    def test_adding_with_other_capitals_adds_to_existing_word(self, page):
        self.open(page)
        click(page, "Add word")
        word, english = self.fields(page)
        word.value, english.value = "hola", "Hi, hello"
        click(page, "Save")
        assert "Added 1 new meaning(s) to 'Hola'." in texts(page)
        assert main.WORDS["Hola"] == ("Hello", "Hi")
        assert "hola" not in main.WORDS
        assert table_words(page).count("Hola") == 1

    def test_search_no_results(self, page):
        self.open(page)
        self.search(page, "zzz")
        assert table_words(page) == []

    def test_add_word(self, page, db_file):
        self.open(page)
        click(page, "Add word")
        assert self.form(page).visible
        word, english = self.fields(page)
        word.value = " Nube "
        english.value = "Cloud, , Sky "
        click(page, "Save")

        assert not self.form(page).visible
        assert main.WORDS["Nube"] == ("Cloud", "Sky")
        assert "Added 2 new meaning(s) to 'Nube'." in texts(page)
        assert "Nube" in table_words(page)

        conn = vocab_db.connect(str(db_file))
        assert vocab_db.load_words(conn)["Nube"] == ("Cloud", "Sky")
        conn.close()

    def test_add_meaning_to_existing_word(self, page):
        self.open(page)
        click(page, "Add word")
        word, english = self.fields(page)
        word.value, english.value = "Banco", "Bank, Seat"
        click(page, "Save")
        assert main.WORDS["Banco"] == ("Bank", "Bench", "Seat")
        assert "Added 1 new meaning(s) to 'Banco'." in texts(page)

    def test_add_existing_meanings(self, page):
        self.open(page)
        click(page, "Add word")
        word, english = self.fields(page)
        word.value, english.value = "Hola", "Hello"
        click(page, "Save")
        assert "'Hola' already has those meanings." in texts(page)

    @pytest.mark.parametrize("word, english", [("", "Cloud"), ("Nube", ""), ("  ", " , ")])
    def test_add_needs_word_and_meaning(self, page, word, english):
        self.open(page)
        click(page, "Add word")
        word_field, english_field = self.fields(page)
        word_field.value, english_field.value = word, english
        click(page, "Save")
        assert self.form(page).visible
        assert "Enter a word and at least one English meaning." in texts(page)
        assert main.WORDS == main.SEED_WORDS

    def test_cancel_hides_form(self, page):
        self.open(page)
        click(page, "Add word")
        click(page, "Cancel")
        assert not self.form(page).visible

    def test_edit_word(self, page):
        self.open(page)
        table = find_first(page, ft.DataTable)
        row = next(r for r in table.rows if r.data == "Pluma")
        row.on_select_change(None)

        assert "Edit word" in texts(page)
        word, english = self.fields(page)
        assert (word.value, english.value) == ("Pluma", "Pen, Feather")

        word.value, english.value = "Plumas", "Pens"
        click(page, "Save")
        assert "Pluma" not in main.WORDS
        assert main.WORDS["Plumas"] == ("Pens",)
        assert "Saved 'Plumas'." in texts(page)

    def test_edit_to_taken_name_shows_error(self, page):
        self.open(page)
        table = find_first(page, ft.DataTable)
        row = next(r for r in table.rows if r.data == "Gato")
        row.on_select_change(None)

        word, _ = self.fields(page)
        word.value = "Perro"
        click(page, "Save")
        assert self.form(page).visible
        assert "'Perro' is already in the list." in texts(page)
        assert main.WORDS["Gato"] == ("Cat",)

    def edit(self, page, word):
        table = find_first(page, ft.DataTable)
        row = next(r for r in table.rows if r.data == word)
        row.on_select_change(None)

    def delete_button(self, page):
        return next(
            c for c in walk(self.form(page))
            if isinstance(c, ft.Button) and c.content == "Delete"
        )

    def dialog_click(self, page, label):
        button = next(b for b in page.dialog.actions if b.content == label)
        button.on_click(None)

    def test_delete_only_offered_when_editing(self, page):
        self.open(page)
        click(page, "Add word")
        assert not self.delete_button(page).visible
        self.edit(page, "Gato")
        assert self.delete_button(page).visible
        click(page, "Add word")
        assert not self.delete_button(page).visible

    def test_delete_asks_first(self, page):
        self.open(page)
        self.edit(page, "Gato")
        self.delete_button(page).on_click(None)
        assert page.dialog.title.value == "Delete 'Gato'?"
        assert "can't be undone" in page.dialog.content.value
        assert "Gato" in main.WORDS

    def test_cancel_keeps_the_word(self, page):
        self.open(page)
        self.edit(page, "Gato")
        self.delete_button(page).on_click(None)
        self.dialog_click(page, "Cancel")
        assert page.dialog is None
        assert "Gato" in main.WORDS
        assert self.form(page).visible

    def test_confirm_deletes_the_word(self, page, db_file):
        self.open(page)
        self.edit(page, "Gato")
        self.delete_button(page).on_click(None)
        self.dialog_click(page, "Delete")

        assert page.dialog is None
        assert "Gato" not in main.WORDS
        assert "Gato" not in table_words(page)
        assert not self.form(page).visible
        assert "Deleted 'Gato'." in texts(page)
        conn = vocab_db.connect(str(db_file))
        assert "Gato" not in vocab_db.load_words(conn)
        conn.close()

    def test_deleted_word_never_dealt(self, page):
        self.open(page)
        self.edit(page, "Gato")
        self.delete_button(page).on_click(None)
        self.dialog_click(page, "Delete")
        click(page, "Back to menu")
        for _ in range(40):
            click(page, "Start 30s game")
            assert "Gato" not in [b.data for b in bubbles(page)[0]]
            click(page, "Back to menu")

    def test_cannot_go_below_minimum(self, db_file):
        conn = vocab_db.connect(str(db_file))
        for word, meaning in [("Uno", "One"), ("Dos", "Two"), ("Tres", "Three"),
                              ("Cuatro", "Four"), ("Cinco", "Five"), ("Seis", "Six")]:
            vocab_db.add_meanings(conn, word, [meaning])
        conn.commit()
        conn.close()
        page = FakePage()
        main.main(page)
        assert len(main.WORDS) == main.MIN_WORDS

        self.open(page)
        self.edit(page, "Uno")
        self.delete_button(page).on_click(None)
        assert page.dialog is None
        assert "The game needs at least 6 words, so this one can't be deleted." in texts(page)
        assert "Uno" in main.WORDS

    def test_game_works_with_the_minimum_number_of_words(self, db_file, monkeypatch):
        async def instant(_seconds):
            return None

        monkeypatch.setattr(main.asyncio, "sleep", instant)
        conn = vocab_db.connect(str(db_file))
        for word, meaning in [("Uno", "One"), ("Dos", "Two"), ("Tres", "Three"),
                              ("Cuatro", "Four"), ("Cinco", "Five"), ("Seis", "Six")]:
            vocab_db.add_meanings(conn, word, [meaning])
        conn.commit()
        conn.close()
        page = FakePage()
        main.main(page)

        click(page, "Start 30s game")
        page.tasks.clear()
        left, right = bubbles(page)
        for _ in range(10):
            li = 0
            word = left[li].data
            ri = next(i for i, b in enumerate(right) if b.data in main.WORDS[word])
            left[li].on_click(None)
            right[ri].on_click(None)
            run_tasks(page)
            assert len({b.data for b in left}) == main.ON_SCREEN

    def delete_all_button(self, page):
        return find_button(page, "Delete all words")

    def test_delete_all_without_warning_for_new_users(self, page):
        self.open(page)
        self.delete_all_button(page).on_click(None)
        assert page.dialog is None
        assert main.WORDS == {}
        assert table_words(page) == []
        assert (
            f"Deleted all {len(main.SEED_WORDS)} word(s). Add words or upload a list to play."
        ) in texts(page)

    def big_list(self, db_file, count=main.NEW_USER_WORDS):
        conn = vocab_db.connect(str(db_file))
        vocab_db.set_setting(conn, "seeded", "1")
        for i in range(count):
            vocab_db.add_meanings(conn, f"Palabra{i}", [f"Word{i}"])
        conn.commit()
        conn.close()
        page = FakePage()
        main.main(page)
        return page

    def test_delete_all_warns_from_50_words(self, db_file):
        page = self.big_list(db_file)
        self.open(page)
        self.delete_all_button(page).on_click(None)
        assert page.dialog.title.value == "Delete all 50 words?"
        warning = [c.value for c in page.dialog.content.controls]
        assert any("can't be undone" in t for t in warning)
        assert any("Backup database to file" in t for t in warning)
        assert len(main.WORDS) == 50

    def test_cancel_delete_all_keeps_the_list(self, db_file):
        page = self.big_list(db_file)
        self.open(page)
        self.delete_all_button(page).on_click(None)
        self.dialog_click(page, "Cancel")
        assert page.dialog is None
        assert len(main.WORDS) == 50

    def test_confirm_delete_all(self, db_file):
        page = self.big_list(db_file)
        self.open(page)
        self.delete_all_button(page).on_click(None)
        self.dialog_click(page, "Delete all")
        assert page.dialog is None
        assert main.WORDS == {}
        conn = vocab_db.connect(str(db_file))
        assert vocab_db.count_words(conn) == 0
        conn.close()

    def test_after_delete_all_games_ask_for_words(self, page):
        self.open(page)
        self.delete_all_button(page).on_click(None)
        click(page, "Back to menu")
        click(page, "Start 30s game")
        assert heading(page) == "Vocabulary Match"  # still on the menu
        assert "Add at least 6 words to play. Your list has 0." in texts(page)
        assert page.tasks == []  # no countdown started

    def test_empty_list_stays_empty_after_restart(self, page):
        self.open(page)
        self.delete_all_button(page).on_click(None)
        again = FakePage()
        main.main(again)
        assert main.WORDS == {}
        assert heading(again) == "Vocabulary Match"

    def test_menu_message_cleared_when_returning(self, page):
        self.open(page)
        self.delete_all_button(page).on_click(None)
        click(page, "Back to menu")
        click(page, "Start 30s game")
        click(page, "Vocab management")
        click(page, "Back to menu")
        assert not any(t.startswith("Add at least") for t in texts(page) if t)

    def test_buttons_in_two_sections(self, page):
        self.open(page)
        layout = [
            "---" if isinstance(c, ft.Divider) else c.content
            for c in current_view(page).controls
            if isinstance(c, (ft.Button, ft.Divider))
        ]
        assert layout == [
            "Add word",
            "Upload list via CSV",
            "Download list as CSV",
            "---",
            "Backup database to file",
            "Restore database from file",
            "Delete all words",
            "---",
        ]
        controls = current_view(page).controls
        assert controls.index(find_first(page, ft.DataTable)) > max(
            i for i, c in enumerate(controls) if isinstance(c, ft.Divider)
        )

    def test_backup_saves_the_whole_database(self, page, monkeypatch, db_file):
        calls = {}

        async def save_file(**kwargs):
            calls.update(kwargs)
            return "/somewhere/backup.db"

        monkeypatch.setattr(page.services[0], "save_file", save_file)
        self.open(page)
        click(page, "Backup database to file")
        assert calls["file_name"].startswith("vocab_match_backup_")
        assert calls["file_name"].endswith(".db")
        assert calls["allowed_extensions"] == ["db"]
        other = vocab_db.connect(":memory:")
        vocab_db.restore_from_bytes(other, calls["src_bytes"])
        assert vocab_db.load_words(other) == main.WORDS
        other.close()
        assert "Backup saved to /somewhere/backup.db" in texts(page)

    def test_backup_cancelled(self, page, monkeypatch):
        async def save_file(**kwargs):
            return None

        monkeypatch.setattr(page.services[0], "save_file", save_file)
        self.open(page)
        click(page, "Backup database to file")
        assert "Backup cancelled." in texts(page)

    def restore_file(self, tmp_path, words, method="time"):
        conn = vocab_db.connect(":memory:")
        for word, meaning in words:
            vocab_db.add_meanings(conn, word, [meaning])
        vocab_db.set_setting(conn, "choice_method", method)
        path = tmp_path / "backup.db"
        path.write_bytes(vocab_db.backup_bytes(conn))
        conn.close()
        return path

    def pick_restore(self, page, monkeypatch, path):
        class Picked:
            pass

        picked = Picked()
        picked.path = str(path)

        async def pick_files(**kwargs):
            assert kwargs["allowed_extensions"] == ["db"]
            return [picked]

        monkeypatch.setattr(page.services[0], "pick_files", pick_files)
        click(page, "Restore database from file")

    WORDS_IN_BACKUP = [("Uno", "One"), ("Dos", "Two"), ("Tres", "Three"),
                       ("Cuatro", "Four"), ("Cinco", "Five"), ("Seis", "Six")]

    def test_restore_small_list_without_warning(self, page, monkeypatch, tmp_path):
        self.open(page)
        self.pick_restore(page, monkeypatch, self.restore_file(tmp_path, self.WORDS_IN_BACKUP))
        assert page.dialog is None
        assert set(main.WORDS) == {w for w, _ in self.WORDS_IN_BACKUP}
        assert sorted(table_words(page)) == sorted(main.WORDS)
        assert "Restored the backup: 6 word(s)." in texts(page)

    def test_restore_brings_back_settings(self, page, monkeypatch, tmp_path):
        self.open(page)
        self.pick_restore(
            page, monkeypatch, self.restore_file(tmp_path, self.WORDS_IN_BACKUP, method="rank")
        )
        click(page, "Back to menu")
        click(page, "Settings")
        assert find_first(page, ft.RadioGroup).value == "rank"

    def test_restore_warns_from_50_words(self, db_file, monkeypatch, tmp_path):
        page = self.big_list(db_file)
        self.open(page)
        self.pick_restore(page, monkeypatch, self.restore_file(tmp_path, self.WORDS_IN_BACKUP))
        assert page.dialog.title.value == "Replace your whole list?"
        assert len(main.WORDS) == 50
        self.dialog_click(page, "Cancel")
        assert len(main.WORDS) == 50

    def test_confirmed_restore_replaces_big_list(self, db_file, monkeypatch, tmp_path):
        page = self.big_list(db_file)
        self.open(page)
        self.pick_restore(page, monkeypatch, self.restore_file(tmp_path, self.WORDS_IN_BACKUP))
        self.dialog_click(page, "Restore")
        assert len(main.WORDS) == 6
        conn = vocab_db.connect(str(db_file))
        assert vocab_db.count_words(conn) == 6
        conn.close()

    def test_restore_of_wrong_file_shows_message(self, page, monkeypatch, tmp_path):
        bad = tmp_path / "notes.db"
        bad.write_bytes(b"not a database")
        self.open(page)
        self.pick_restore(page, monkeypatch, bad)
        assert "That file isn't a Vocab Match backup." in texts(page)
        assert main.WORDS == main.SEED_WORDS

    def test_reopening_resets_search_and_form(self, page):
        self.open(page)
        self.search(page, "zzz")
        click(page, "Add word")
        click(page, "Back to menu")
        self.open(page)
        assert find_first(page, ft.TextField).value == ""
        assert not self.form(page).visible
        assert len(table_words(page)) == len(main.SEED_WORDS)

    def test_new_word_can_appear_in_game(self, page):
        self.open(page)
        click(page, "Add word")
        word, english = self.fields(page)
        word.value, english.value = "Nube", "Cloud"
        click(page, "Save")
        click(page, "Back to menu")

        seen = set()
        for _ in range(50):
            click(page, "Start 30s game")
            seen.update(b.data for b in bubbles(page)[0])
            click(page, "Back to menu")
        assert "Nube" in seen


class TestDownload:
    def test_saves_csv(self, page, monkeypatch):
        picker = page.services[0]
        calls = {}

        async def save_file(**kwargs):
            calls.update(kwargs)
            return "/somewhere/vocab_list.csv"

        monkeypatch.setattr(picker, "save_file", save_file)
        click(page, "Vocab management")
        click(page, "Download list as CSV")

        assert calls["file_name"] == "vocab_list.csv"
        assert calls["allowed_extensions"] == ["csv"]
        text = calls["src_bytes"].decode("utf-8-sig")
        assert "Banco,Bank\nBanco,Bench\n" in text
        assert calls["src_bytes"].startswith(b"\xef\xbb\xbf")  # BOM for Excel
        assert "Saved to /somewhere/vocab_list.csv" in texts(page)

    def test_cancelled(self, page, monkeypatch):
        async def save_file(**kwargs):
            return None

        monkeypatch.setattr(page.services[0], "save_file", save_file)
        click(page, "Vocab management")
        click(page, "Download list as CSV")
        assert "Download cancelled." in texts(page)


class TestUpload:
    def open(self, page):
        click(page, "Vocab management")
        click(page, "Upload list via CSV")
        assert heading(page) == "Upload a vocabulary"

    def checkbox(self, page):
        return find_first(page, ft.Checkbox)

    def pick(self, page, monkeypatch, path, clean=False):
        """Choose a file. clean sets the "Start clean database" box, if it's shown."""
        class Picked:
            pass

        picked = Picked()
        picked.path = str(path)

        async def pick_files(**kwargs):
            return [picked] if path else None

        monkeypatch.setattr(page.services[0], "pick_files", pick_files)
        box = self.checkbox(page)
        if box.visible:
            box.value = clean
            box.on_change(None)
        click(page, "Choose file")

    def page_with_words(self, db_file, count):
        """Start the app with exactly count words, numbered."""
        conn = vocab_db.connect(str(db_file))
        vocab_db.set_setting(conn, "seeded", "1")
        for i in range(count):
            vocab_db.add_meanings(conn, f"Palabra{i}", [f"Word{i}"])
        conn.commit()
        conn.close()
        page = FakePage()
        main.main(page)
        return page

    def test_clean_box_offered_to_new_users_ticked(self, page):
        self.open(page)
        box = self.checkbox(page)
        assert box.visible and box.value is True
        assert box.label == "Start clean database"
        assert any("will be replaced" in t for t in texts(page))

    def test_clean_box_hidden_for_50_or_more_words(self, db_file):
        page = self.page_with_words(db_file, main.NEW_USER_WORDS)
        self.open(page)
        assert not self.checkbox(page).visible
        assert any("Nothing is removed" in t for t in texts(page))

    def test_clean_box_shown_for_49_words(self, db_file):
        page = self.page_with_words(db_file, main.NEW_USER_WORDS - 1)
        self.open(page)
        assert self.checkbox(page).visible

    def test_unticking_changes_the_note(self, page):
        self.open(page)
        box = self.checkbox(page)
        box.value = False
        box.on_change(None)
        assert any("Nothing is removed" in t for t in texts(page))
        assert not any("will be replaced" in t for t in texts(page))

    def test_clean_upload_replaces_the_list(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.csv"
        upload.write_text("Nube,Cloud\nHola,Hi\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload, clean=True)
        assert main.WORDS == {"Hola": ("Hi",), "Nube": ("Cloud",)}
        assert (
            "Started a clean list with 2 pair(s) from the file. "
            f"Removed the {len(main.SEED_WORDS)} word(s) you had before."
        ) in texts(page)

    def test_clean_upload_of_unreadable_file_keeps_the_list(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.csv"
        upload.write_text("nothing useful\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload, clean=True)
        assert main.WORDS == main.SEED_WORDS

    def test_box_ticked_again_when_screen_reopens(self, page):
        self.open(page)
        box = self.checkbox(page)
        box.value = False
        click(page, "Back")
        click(page, "Upload list via CSV")
        assert self.checkbox(page).value is True

    def test_imports_file(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.csv"
        upload.write_text("Nube,Cloud\nHola,Hello\n", encoding="utf-8-sig")
        self.open(page)
        self.pick(page, monkeypatch, upload)

        assert main.WORDS["Nube"] == ("Cloud",)
        assert (
            "Added 1 new pair(s) to your list. Skipped 1 already in it. "
            "Nothing was removed."
        ) in texts(page)

    def test_reports_bad_lines(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.csv"
        upload.write_text("a\nb\nc\nd\nNube,Cloud\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload)

        message = next(t for t in texts(page) if t.startswith("Added"))
        assert message == (
            "Added 1 new pair(s) to your list. Skipped 0 already in it. "
            "Nothing was removed. "
            "Could not read 4 line(s): Line 1: a; Line 2: b; Line 3: c"
        )

    def test_screen_says_uploading_adds(self, db_file):
        page = self.page_with_words(db_file, main.NEW_USER_WORDS)
        self.open(page)
        assert any("Uploading adds to your list" in t and "Nothing is removed" in t
                   for t in texts(page))

    def test_upload_keeps_existing_words_and_history(self, page, monkeypatch, tmp_path, db_file):
        conn = vocab_db.connect(str(db_file))
        vocab_db.record_guess(conn, "Hola", True)
        before = vocab_db.load_stats(conn, "Hola")
        conn.close()

        upload = tmp_path / "list.csv"
        upload.write_text("Nube,Cloud\nHola,Hi\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload)

        assert set(main.SEED_WORDS) < set(main.WORDS)
        assert main.WORDS["Hola"] == ("Hello", "Hi")
        conn = vocab_db.connect(str(db_file))
        assert vocab_db.load_stats(conn, "Hola") == before
        conn.close()

    def test_cancelled_pick_changes_nothing(self, page, monkeypatch):
        self.open(page)
        self.pick(page, monkeypatch, None)
        assert main.WORDS == main.SEED_WORDS
        assert all(not t.startswith("Added") for t in texts(page) if t)

    def test_back_returns_to_vocab_screen_with_new_words(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.txt"
        upload.write_text("Nube|Cloud\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload)
        click(page, "Back")
        assert heading(page) == "Vocab management"
        assert "Nube" in table_words(page)

    def test_message_cleared_when_reopened(self, page, monkeypatch, tmp_path):
        upload = tmp_path / "list.csv"
        upload.write_text("Nube,Cloud\n", encoding="utf-8")
        self.open(page)
        self.pick(page, monkeypatch, upload)
        click(page, "Back")
        click(page, "Upload list via CSV")
        assert all(not t.startswith("Added") for t in texts(page) if t)
