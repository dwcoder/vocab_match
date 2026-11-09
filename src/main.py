import asyncio
import os
import random

import flet as ft

import choice_function
import vocab_db


# Filled from the database when the app starts.
# Each word (in any language) maps to a tuple of its English meanings.
WORDS = {}

# Loaded into the database the first time the app runs, when the database is empty.
SEED_WORDS = {
    "Hola": ("Hello",),
    "Casa": ("House",),
    "Perro": ("Dog",),
    "Agua": ("Water",),
    "Gato": ("Cat",),
    "Adiós": ("Goodbye",),
    "Gracias": ("Thank you",),
    "Por favor": ("Please",),
    "Sí": ("Yes",),
    "Libro": ("Book",),
    "Mesa": ("Table",),
    "Silla": ("Chair",),
    "Puerta": ("Door",),
    "Ventana": ("Window",),
    "Comida": ("Food",),
    "Pan": ("Bread",),
    "Leche": ("Milk",),
    "Café": ("Coffee",),
    "Manzana": ("Apple",),
    "Sol": ("Sun",),
    "Luna": ("Moon",),
    "Escuela": ("School",),
    "Amigo": ("Friend",),
    "Coche": ("Car",),
    "Familia": ("Family",),
    "Banco": ("Bank", "Bench"),
    "Tiempo": ("Time", "Weather"),
    "Pluma": ("Pen", "Feather"),
}

ON_SCREEN = 5
# The game needs a full board plus one spare word to replace a matched one.
MIN_WORDS = ON_SCREEN + 1

NORMAL = ft.Colors.BLUE_50
SELECTED = ft.Colors.BLUE_100
MATCHED = ft.Colors.GREEN_300
WRONG = ft.Colors.RED_300
BUTTON_WIDTH = 260
# The game board: two columns of bubbles. The bubbles shrink to fit narrow
# phone screens, up to this width in total on wider screens.
BOARD_GAP = 20
BOARD_MAX_WIDTH = 2 * 170 + BOARD_GAP
# The vocab screen is a centred panel no wider than this.
VOCAB_MAX_WIDTH = 600


def shown(text):
    """How a word or meaning appears on screen: always lower case.

    Mixed capitals would give pairs away ("Yo" goes with "I"), so the board
    and the list show everything in lower case. The database keeps the
    spelling as entered.
    """
    return text.lower()


def clock(seconds):
    """Format a countdown like a stopwatch: 30 -> "0:30", 60 -> "1:00"."""
    return f"{seconds // 60}:{seconds % 60:02d}"


def database_path():
    # Where the app keeps its data. In a built app (Android/iOS/desktop) Flet sets
    # FLET_APP_STORAGE_DATA to a writable, app-private folder. When running with
    # `flet run` during development it may be unset, so fall back to ~/vocab_match.
    folder = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
        os.path.expanduser("~"), "vocab_match"
    )
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "vocab.db")


def main(page: ft.Page):
    page.title = "Vocabulary Match"
    page.padding = 25
    page.bgcolor = ft.Colors.WHITE
    page.scroll = ft.ScrollMode.AUTO

    conn = vocab_db.connect(database_path())
    vocab_db.seed_if_empty(conn, SEED_WORDS)

    # Guess statistics for each word, used to choose which words to show.
    stats = {}

    def reload_words():
        WORDS.clear()
        WORDS.update(vocab_db.load_words(conn))
        stats.clear()
        stats.update(vocab_db.load_all_stats(conn))

    reload_words()

    # How words are scored: one of choice_function.METHODS (memory by default).
    def saved_choice_method():
        method = vocab_db.get_setting(conn, "choice_method", choice_function.DEFAULT_METHOD)
        return method if method in choice_function.METHODS else choice_function.DEFAULT_METHOD

    settings = {"choice_method": saved_choice_method()}

    # Words on screen. Left = the words, right = one English meaning for each.
    # The right column is shuffled separately, so slots don't line up.
    words = {"left": [], "right": []}
    # Words waiting to appear. Matched words go to the back.
    pool = []
    busy = {"left": [False] * ON_SCREEN, "right": [False] * ON_SCREEN}
    selected = {"left": None, "right": None}
    state = {
        "limit": 30,
        "time_left": 30,
        "matches": 0,
        "running": False,
        "game": 0,
    }

    # ---------- Word selection ----------

    def meaning_keys(word):
        # A word's meanings, compared ignoring capitals.
        return {vocab_db.match_key(m) for m in WORDS[word]}

    def meanings_on_screen(skip_index=None):
        # All English meanings of the words on screen, except one slot.
        return {
            m
            for k, w in enumerate(words["left"])
            if k != skip_index
            for m in meaning_keys(w)
        }

    def pick_from_pool(skip_index=None):
        # Pick a pool word whose meanings don't clash with the words on screen,
        # so each English word on the right belongs to exactly one word.
        # Words are weighted by choice_function: overdue and often-missed
        # words come up more.
        taken = meanings_on_screen(skip_index)
        candidates = [w for w in pool if not (meaning_keys(w) & taken)]
        if not candidates:
            candidates = list(pool)  # rare fallback if every word clashes
        word = choice_function.choose(
            candidates, choice_function.scores(stats, settings["choice_method"])
        )
        pool.remove(word)
        return word

    def deal():
        words["left"][:] = []
        pool[:] = list(WORDS)
        for _ in range(ON_SCREEN):
            words["left"].append(pick_from_pool())
        words["right"][:] = [random.choice(WORDS[w]) for w in words["left"]]
        random.shuffle(words["right"])
        for side in ("left", "right"):
            for i in range(ON_SCREEN):
                show_word(side, i)
                paint(side, i)

    # ---------- Game screen ----------

    # The game screen shows only a countdown, a green match counter and the
    # board. The only message is "Time's up!" at the end.
    timer_text = ft.Text("", size=24, weight=ft.FontWeight.BOLD)
    score_text = ft.Text("", size=24, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_600)
    message_text = ft.Text(
        "Time's up!", size=20, weight=ft.FontWeight.BOLD, visible=False
    )

    def make_bubble(side, index):
        return ft.Container(
            content=ft.Text(
                "",
                size=17,
                weight=ft.FontWeight.BOLD,
                color=ft.Colors.BLUE_900,
                text_align=ft.TextAlign.CENTER,
            ),
            height=65,  # width comes from the board column, see BOARD_MAX_WIDTH
            padding=8,
            alignment=ft.Alignment.CENTER,
            bgcolor=NORMAL,
            border_radius=15,
            opacity=1,
            animate_opacity=ft.Animation(duration=400, curve=ft.AnimationCurve.EASE_OUT),
            offset=ft.Offset(0, 0),
            animate_offset=ft.Animation(duration=60, curve=ft.AnimationCurve.EASE_IN_OUT),
            on_click=lambda e: choose(side, index),
        )

    bubbles = {
        "left": [make_bubble("left", i) for i in range(ON_SCREEN)],
        "right": [make_bubble("right", i) for i in range(ON_SCREEN)],
    }

    def show_word(side, index):
        # data keeps the stored spelling; the bubble shows it in lower case.
        bubbles[side][index].data = words[side][index]
        bubbles[side][index].content.value = shown(words[side][index])

    def paint(side, index):
        if busy[side][index]:
            return
        bubbles[side][index].bgcolor = SELECTED if selected[side] == index else NORMAL

    async def on_correct(li, ri, gid):
        for side, i in (("left", li), ("right", ri)):
            bubbles[side][i].bgcolor = MATCHED
        page.update()
        await asyncio.sleep(0.5)
        if gid != state["game"]:
            return

        for side, i in (("left", li), ("right", ri)):
            bubbles[side][i].opacity = 0
        page.update()
        await asyncio.sleep(0.4)
        if gid != state["game"]:
            return

        old_word = words["left"][li]
        new_word = pick_from_pool(li)
        pool.append(old_word)
        words["left"][li] = new_word
        words["right"][ri] = random.choice(WORDS[new_word])

        for side, i in (("left", li), ("right", ri)):
            show_word(side, i)
            busy[side][i] = False
            bubbles[side][i].opacity = 1
            paint(side, i)
        page.update()

    async def on_wrong(li, ri, gid):
        for side, i in (("left", li), ("right", ri)):
            bubbles[side][i].bgcolor = WRONG
        page.update()
        await asyncio.sleep(0.4)
        if gid != state["game"]:
            return

        for dx in (-0.08, 0.08, -0.08, 0.08, 0):
            for side, i in (("left", li), ("right", ri)):
                bubbles[side][i].offset = ft.Offset(dx, 0)
            page.update()
            await asyncio.sleep(0.08)
        if gid != state["game"]:
            return

        for side, i in (("left", li), ("right", ri)):
            busy[side][i] = False
            paint(side, i)
        page.update()

    def choose(side, index):
        if not state["running"] or busy[side][index]:
            return

        previous = selected[side]
        selected[side] = None if previous == index else index
        if previous is not None:
            paint(side, previous)
        paint(side, index)

        li, ri = selected["left"], selected["right"]
        if li is None or ri is None:
            page.update()
            return

        selected["left"] = selected["right"] = None
        paint("left", li)
        paint("right", ri)
        gid = state["game"]

        busy["left"][li] = True
        busy["right"][ri] = True
        word = words["left"][li]
        english = words["right"][ri]
        correct = vocab_db.match_key(english) in meaning_keys(word)
        vocab_db.record_guess(conn, word, correct)
        stats[word] = vocab_db.load_stats(conn, word)
        if correct:
            state["matches"] += 1
            score_text.value = str(state["matches"])
            page.run_task(on_correct, li, ri, gid)
        else:
            page.run_task(on_wrong, li, ri, gid)
        page.update()

    async def countdown(gid):
        while state["time_left"] > 0:
            await asyncio.sleep(1)
            if gid != state["game"]:
                return
            state["time_left"] -= 1
            timer_text.value = clock(state["time_left"])
            page.update()
        end_game()

    def end_game():
        state["running"] = False
        selected["left"] = selected["right"] = None
        for side in ("left", "right"):
            for i in range(ON_SCREEN):
                paint(side, i)
        message_text.visible = True
        play_again_button.visible = True
        page.update()

    def start_game(seconds):
        state["game"] += 1
        gid = state["game"]
        state["limit"] = seconds
        state["time_left"] = seconds
        state["matches"] = 0
        state["running"] = True
        for side in busy:
            busy[side] = [False] * ON_SCREEN
        selected["left"] = selected["right"] = None

        deal()
        timer_text.value = clock(seconds)
        score_text.value = "0"
        message_text.visible = False
        play_again_button.visible = False
        show(view_game, centred=True, app_bar=back_bar)
        page.run_task(countdown, gid)

    play_again_button = ft.Button(
        "Play again",
        on_click=lambda e: start_game(state["limit"]),
        visible=False,
        width=BUTTON_WIDTH,
    )

    # A plain bar with only a small back arrow, so the arrow sits in the
    # screen's top-left corner and stays there while the screen scrolls.
    def make_back_bar(tooltip, go_back):
        return ft.AppBar(
            leading=ft.IconButton(
                ft.Icons.ARROW_BACK,
                tooltip=tooltip,
                on_click=lambda e: go_back(),
            ),
            automatically_imply_leading=False,
            bgcolor=ft.Colors.WHITE,
            elevation=0,
            elevation_on_scroll=0,  # stay plain white when the list scrolls under it
        )

    # Game, vocab and settings screens go back to the menu.
    back_bar = make_back_bar("Back to menu", lambda: show_home())
    # The upload screen goes back to the vocab screen.
    upload_back_bar = make_back_bar("Back", lambda: show_vocab())

    view_game = ft.Column(
        [
            ft.Row(
                [
                    ft.Row(
                        [ft.Icon(ft.Icons.TIMER_OUTLINED, size=26), timer_text],
                        spacing=6,
                    ),
                    score_text,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=40,
            ),
            ft.Row(
                [
                    ft.Column(
                        bubbles[side],
                        spacing=12,
                        expand=1,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    )
                    for side in ("left", "right")
                ],
                spacing=BOARD_GAP,
                max_width=BOARD_MAX_WIDTH,
            ),
            message_text,
            play_again_button,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=20,
    )

    # ---------- Vocab management screen ----------

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    search_field = ft.TextField(
        label="Search word or English",
        on_change=lambda e: refresh_table(),
        expand=True,
    )
    vocab_message = ft.Text("", size=15)
    vocab_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Word", weight=ft.FontWeight.BOLD)),
            ft.DataColumn(ft.Text("English", weight=ft.FontWeight.BOLD)),
        ],
        rows=[],
    )

    # The add/edit form. editing["original"] is the word being edited,
    # or None when adding a new word.
    editing = {"original": None}
    form_title = ft.Text("", size=20, weight=ft.FontWeight.BOLD)
    word_field = ft.TextField(label="Word")
    english_field = ft.TextField(label="English meanings (separate with commas)")
    form_message = ft.Text("", color=ft.Colors.RED_700)

    def show_form(title, word="", english=""):
        form_title.value = title
        word_field.value = word
        english_field.value = english
        form_message.value = ""
        delete_button.visible = editing["original"] is not None
        form.visible = True
        page.update()

    def start_add(e=None):
        editing["original"] = None
        show_form("Add word")

    def start_edit(word):
        editing["original"] = word
        show_form("Edit word", word, ", ".join(WORDS[word]))

    def cancel_form(e=None):
        form.visible = False
        page.update()

    def save_form(e=None):
        word = (word_field.value or "").strip()
        meanings = [
            m.strip() for m in (english_field.value or "").split(",") if m.strip()
        ]
        if not word or not meanings:
            form_message.value = "Enter a word and at least one English meaning."
            page.update()
            return

        try:
            if editing["original"] is None:
                added = vocab_db.add_meanings(conn, word, meanings)
                conn.commit()
                # "hola" adds to an existing "Hola"; name the word as stored.
                word = vocab_db.stored_spelling(conn, word)
                vocab_message.value = (
                    f"Added {added} new meaning(s) to '{word}'."
                    if added
                    else f"'{word}' already has those meanings."
                )
            else:
                vocab_db.update_word(conn, editing["original"], word, meanings)
                vocab_message.value = f"Saved '{word}'."
        except ValueError as err:
            form_message.value = str(err)
            page.update()
            return

        reload_words()
        refresh_table()
        form.visible = False
        page.update()

    def ask_delete(e=None):
        word = editing["original"]
        if len(WORDS) <= MIN_WORDS:
            form_message.value = (
                f"The game needs at least {MIN_WORDS} words, "
                "so this one can't be deleted."
            )
            page.update()
            return
        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Delete '{word}'?"),
                content=ft.Text(
                    "This removes the word, its meanings and your practice "
                    "history for it. It can't be undone."
                ),
                actions=[
                    ft.TextButton("Cancel", on_click=lambda e: page.pop_dialog()),
                    ft.TextButton(
                        "Delete",
                        on_click=lambda e: confirm_delete(word),
                        style=ft.ButtonStyle(color=ft.Colors.RED_700),
                    ),
                ],
            )
        )

    def confirm_delete(word):
        page.pop_dialog()
        vocab_db.delete_word(conn, word)
        reload_words()
        refresh_table()
        form.visible = False
        vocab_message.value = f"Deleted '{word}'."
        page.update()

    delete_button = ft.Button(
        "Delete",
        icon=ft.Icons.DELETE_OUTLINE,
        color=ft.Colors.RED_700,
        icon_color=ft.Colors.RED_700,
        on_click=ask_delete,
        visible=False,
    )

    form = ft.Container(
        content=ft.Column(
            [
                form_title,
                word_field,
                english_field,
                form_message,
                ft.Row(
                    [
                        ft.Button("Save", on_click=save_form),
                        ft.Button("Cancel", on_click=cancel_form),
                        delete_button,
                    ],
                    spacing=10,
                    wrap=True,
                ),
            ],
            spacing=10,
        ),
        visible=False,
        padding=15,
        bgcolor=ft.Colors.BLUE_50,
        border_radius=10,
    )

    def refresh_table():
        query = vocab_db.sort_key(search_field.value or "").strip()
        rows = []
        for word, meanings in WORDS.items():
            if query and not (
                query in vocab_db.sort_key(word)
                or any(query in vocab_db.sort_key(m) for m in meanings)
            ):
                continue
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(shown(word))),
                        ft.DataCell(ft.Text(", ".join(shown(m) for m in meanings))),
                    ],
                    data=word,  # the stored spelling
                    on_select_change=lambda e, w=word: start_edit(w),
                )
            )
        vocab_table.rows = rows
        page.update()

    async def export_words(e=None):
        text = vocab_db.export_csv(conn)
        path = await file_picker.save_file(
            file_name="vocab_list.csv",
            allowed_extensions=["csv"],
            src_bytes=text.encode("utf-8-sig"),
        )
        vocab_message.value = f"Saved to {path}" if path else "Download cancelled."
        page.update()

    # ---------- Upload a vocabulary screen ----------

    upload_message = ft.Text("", size=15)

    async def choose_upload_file(e=None):
        files = await file_picker.pick_files(
            allow_multiple=False, allowed_extensions=["csv", "txt"]
        )
        if not files:
            return
        with open(files[0].path, encoding="utf-8-sig") as f:
            text = f.read()

        result = vocab_db.import_text(conn, text)
        reload_words()
        refresh_table()

        message = (
            f"Added {result['added']} new pair(s) to your list. "
            f"Skipped {result['skipped']} already in it. Nothing was removed."
        )
        if result["errors"]:
            message += (
                f" Could not read {len(result['errors'])} line(s): "
                + "; ".join(result["errors"][:3])
            )
        upload_message.value = message
        vocab_message.value = message
        page.update()

    view_upload = ft.Column(
        [
            ft.Text("Upload a vocabulary", size=30, weight=ft.FontWeight.BOLD),
            ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.BLUE_700),
                        ft.Text(
                            "Uploading adds to your list. Your current words, and "
                            "how well you know them, are kept. Nothing is removed.",
                            expand=True,
                        ),
                    ],
                    spacing=10,
                ),
                padding=12,
                bgcolor=ft.Colors.BLUE_50,
                border_radius=10,
            ),
            ft.Text(
                "Make a CSV file (.csv) with one word pair on each row, "
                "separated by a comma:"
            ),
            ft.Container(
                content=ft.Text(
                    "Hola,Hello\nCasa,House\nBanco,Bank\nBanco,Bench",
                    font_family="monospace",
                ),
                padding=12,
                bgcolor=ft.Colors.BLUE_50,
                border_radius=10,
            ),
            ft.Text("• The first column is the foreign word, the second column is its English meaning."),
            ft.Text("• A word with more than one meaning can go on several rows, as Banco does above."),
            ft.Text("• Pairs already in your list are skipped, so uploading the same file twice is safe."),
            ft.Text("• Plain .txt files with word|meaning lines also work."),
            ft.Text("Making the file in Excel", size=20, weight=ft.FontWeight.BOLD),
            ft.Text("1. Type the foreign words in column A and their English meanings in column B, one pair per row. No header row."),
            ft.Text("2. Choose File > Save As. Pick the CSV UTF-8 (Comma delimited) type and save it."),
            ft.Text("3. Upload that file with Choose file below."),
            ft.Row(
                [
                    ft.Button("Choose file", on_click=choose_upload_file),
                ],
                spacing=10,
            ),
            upload_message,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.START,
        spacing=12,
    )

    def show_upload():
        upload_message.value = ""
        show(view_upload, app_bar=upload_back_bar)

    view_vocab = ft.Column(
        [
            ft.Text("Vocab management", size=30, weight=ft.FontWeight.BOLD),
            ft.Row([search_field]),
            ft.Row(
                [
                    ft.Button("Add word", on_click=start_add),
                    ft.Button("Upload a vocabulary", on_click=lambda e: show_upload()),
                    ft.Button("Download list", on_click=export_words),
                ],
                wrap=True,
                spacing=10,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            vocab_message,
            form,
            vocab_table,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=15,
        max_width=VOCAB_MAX_WIDTH,
    )

    # ---------- Settings screen ----------

    def change_choice_method(e=None):
        method = choice_method_group.value
        if method not in choice_function.METHODS:
            return
        settings["choice_method"] = method
        vocab_db.set_setting(conn, "choice_method", method)
        page.update()

    choice_method_group = ft.RadioGroup(
        content=ft.Column(
            [
                ft.Radio(value=choice_function.RANK, label="By order"),
                ft.Text(
                    "Words you matched longest ago come up most. Only the order "
                    "you matched them in counts, not how long ago it was."
                ),
                ft.Radio(value=choice_function.TIME, label="By time"),
                ft.Text(
                    "Words you matched longest ago come up most. A word's chance "
                    f"halves for every {choice_function.HALF_LIFE_HOURS} hours "
                    "after the oldest word's."
                ),
                ft.Radio(value=choice_function.MEMORY, label="By memory strength (default)"),
                ft.Text(
                    "Each word has its own memory strength, which doubles when you "
                    "get it right and halves when you get it wrong. Words you are "
                    "most likely to have forgotten come up most, so words you know "
                    "well come up less and less often."
                ),
            ],
            spacing=6,
        ),
        value=settings["choice_method"],
        on_change=change_choice_method,
    )

    view_settings = ft.Column(
        [
            ft.Text("Settings", size=30, weight=ft.FontWeight.BOLD),
            ft.Text("Choosing the next word", size=20, weight=ft.FontWeight.BOLD),
            choice_method_group,
            ft.Text(
                "Whichever you pick, words you often get wrong and words you have "
                "never matched come up more."
            ),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.START,
        spacing=15,
    )

    # ---------- Home screen ----------

    view_home = ft.Column(
        [
            ft.Text("Vocabulary Match", size=30, weight=ft.FontWeight.BOLD),
            ft.Button(
                "Start 30s game",
                on_click=lambda e: start_game(30),
                width=BUTTON_WIDTH,
            ),
            ft.Button(
                "Start 1min game",
                on_click=lambda e: start_game(60),
                width=BUTTON_WIDTH,
            ),
            ft.Button(
                "Vocab management",
                on_click=lambda e: show_vocab(),
                width=BUTTON_WIDTH,
            ),
            ft.Button(
                "Settings",
                on_click=lambda e: show_settings(),
                width=BUTTON_WIDTH,
            ),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=20,
    )

    # ---------- Switching screens ----------

    def show(view, centred=False, across=False, app_bar=None):
        # centred puts the screen in the middle of the page, both ways.
        # across centres it left to right only, keeping it at the top, which
        # suits long lists. Otherwise the screen starts at the top left.
        # app_bar is shown above the screen (the back arrow).
        page.controls.clear()
        page.appbar = app_bar
        page.horizontal_alignment = (
            ft.CrossAxisAlignment.CENTER if centred or across else ft.CrossAxisAlignment.START
        )
        page.vertical_alignment = (
            ft.MainAxisAlignment.CENTER if centred else ft.MainAxisAlignment.START
        )
        page.add(view)
        page.update()

    def stop_game():
        # Invalidates the running timer and animations, and stops clicks.
        state["game"] += 1
        state["running"] = False

    def show_home():
        stop_game()
        show(view_home, centred=True)

    def show_settings():
        stop_game()
        choice_method_group.value = settings["choice_method"]
        show(view_settings, app_bar=back_bar)

    def show_vocab():
        stop_game()
        form.visible = False
        search_field.value = ""
        refresh_table()
        show(view_vocab, across=True, app_bar=back_bar)

    deal()
    show_home()


if __name__ == "__main__":
    ft.run(main)
