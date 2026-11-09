"""Unit tests for choice_function.py."""

import random
from collections import Counter
from datetime import datetime, timedelta

import pytest

import choice_function as cf


OLDEST = datetime(2026, 11, 1, 12, 0, 0)


def hours_after(hours):
    return OLDEST + timedelta(hours=hours)


class TestTimeScore:
    def test_oldest_scores_one(self):
        assert cf.time_score(OLDEST, OLDEST) == 1.0

    @pytest.mark.parametrize("hours, expected", [(24, 0.5), (48, 0.25), (12, 0.5 ** 0.5)])
    def test_halves_every_half_life(self, hours, expected):
        assert cf.time_score(hours_after(hours), OLDEST) == pytest.approx(expected)

    def test_custom_half_life(self):
        assert cf.time_score(hours_after(2), OLDEST, half_life_hours=1) == pytest.approx(0.25)

    def test_never_seen_scores_one(self):
        assert cf.time_score(None, OLDEST) == 1.0
        assert cf.time_score(None, None) == 1.0

    def test_never_negative_hours(self):
        assert cf.time_score(OLDEST - timedelta(hours=5), OLDEST) == 1.0

    def test_very_old_gap_does_not_crash(self):
        assert 0.0 <= cf.time_score(hours_after(24 * 3650), OLDEST) < 1e-300


class TestMistakeScore:
    @pytest.mark.parametrize(
        "recent, expected",
        [
            ("", 0.0),
            (None, 0.0),
            ("1111111111", 0.0),
            ("0000000000", 1.0),
            ("1100", 0.2),
            ("0", 0.1),
            ("0101010101", 0.5),
        ],
    )
    def test_fraction_wrong_out_of_ten(self, recent, expected):
        assert cf.mistake_score(recent) == pytest.approx(expected)

    def test_only_last_ten_count(self):
        assert cf.mistake_score("000001111111111") == 0.0


class TestTimeMethod:
    def test_oldest_scores_one_and_later_words_decay(self):
        result = cf.scores(
            method=cf.TIME,
            stats={
                "Old": {"last_seen": OLDEST, "recent_results": "1"},
                "Day": {"last_seen": hours_after(24), "recent_results": "1"},
                "Two": {"last_seen": hours_after(48), "recent_results": "1"},
            }
        )
        assert result == pytest.approx({"Old": 1.0, "Day": 0.5, "Two": 0.25})

    def test_reads_iso_strings(self):
        result = cf.scores(
            method=cf.TIME,
            stats={
                "Old": {"last_seen": OLDEST.isoformat(), "recent_results": ""},
                "Day": {"last_seen": hours_after(24).isoformat(), "recent_results": ""},
            }
        )
        assert result == pytest.approx({"Old": 1.0, "Day": 0.5})

    def test_never_seen_scores_one(self):
        result = cf.scores(
            method=cf.TIME,
            stats={
                "New": {"last_seen": None, "recent_results": ""},
                "Recent": {"last_seen": hours_after(240), "recent_results": "1"},
                "Old": {"last_seen": OLDEST, "recent_results": "1"},
            }
        )
        assert result["New"] == 1.0
        assert result["Old"] == 1.0
        assert result["Recent"] < 1e-2

    def test_all_wrong_matches_the_oldest(self):
        result = cf.scores(
            method=cf.TIME,
            stats={
                "Old": {"last_seen": OLDEST, "recent_results": "1111"},
                "Missed": {"last_seen": hours_after(240), "recent_results": "0000000000"},
            }
        )
        assert result["Missed"] == result["Old"] == 1.0

    def test_mistakes_are_a_floor_not_added(self):
        result = cf.scores(
            method=cf.TIME,
            stats={
                "Old": {"last_seen": OLDEST, "recent_results": ""},
                # Time score 0.5, mistake score 0.3: the higher one wins.
                "A": {"last_seen": hours_after(24), "recent_results": "000"},
                # Time score ~0, mistake score 0.5.
                "B": {"last_seen": hours_after(500), "recent_results": "00000"},
            }
        )
        assert result["A"] == pytest.approx(0.5)
        assert result["B"] == pytest.approx(0.5)

    def test_single_word_scores_one(self):
        assert cf.scores({"Hola": {"last_seen": OLDEST, "recent_results": "1"}}, cf.TIME) == {
            "Hola": 1.0
        }

    def test_empty(self):
        assert cf.scores({}, cf.TIME) == {}


class TestRankScore:
    def test_oldest_scores_one(self):
        assert cf.rank_score(0, 10) == 1.0

    @pytest.mark.parametrize("position, expected", [(2, 0.5), (4, 0.25), (1, 0.5 ** 0.5)])
    def test_halves_every_fifth_of_the_list(self, position, expected):
        assert cf.rank_score(position, 10) == pytest.approx(expected)

    def test_custom_fraction(self):
        assert cf.rank_score(5, 10, half_fraction=0.5) == pytest.approx(0.5)

    def test_single_word(self):
        assert cf.rank_score(0, 1) == 1.0

    def test_same_shape_for_any_list_size(self):
        assert cf.rank_score(27, 28) == pytest.approx(cf.rank_score(270, 280), rel=0.05)


class TestRankMethod:
    def stats(self, *times, recent=""):
        return {
            f"w{i}": {"last_seen": t, "recent_results": recent} for i, t in enumerate(times)
        }

    def test_only_order_matters(self):
        close = cf.scores(self.stats(OLDEST, hours_after(1), hours_after(2)), cf.RANK)
        far = cf.scores(self.stats(OLDEST, hours_after(100), hours_after(5000)), cf.RANK)
        assert close == pytest.approx(far)

    def test_scores_by_position(self):
        times = [hours_after(h) for h in range(10)]
        result = cf.scores(self.stats(*times), cf.RANK)
        assert result["w0"] == 1.0
        assert result["w2"] == pytest.approx(0.5)
        assert result["w4"] == pytest.approx(0.25)
        assert result["w9"] == pytest.approx(0.5 ** 4.5)

    def test_order_of_input_does_not_matter(self):
        result = cf.scores(
            {
                "new": {"last_seen": hours_after(9), "recent_results": ""},
                "old": {"last_seen": OLDEST, "recent_results": ""},
            },
            cf.RANK,
        )
        assert result["old"] == 1.0
        assert result["new"] < 1.0

    def test_ties_share_a_position(self):
        result = cf.scores(self.stats(OLDEST, OLDEST, hours_after(1), hours_after(1)), cf.RANK)
        assert result["w0"] == result["w1"] == 1.0
        assert result["w2"] == result["w3"] == pytest.approx(cf.rank_score(2, 4))

    def test_never_seen_scores_one_and_is_not_ranked(self):
        result = cf.scores(
            {
                "new": {"last_seen": None, "recent_results": ""},
                "old": {"last_seen": OLDEST, "recent_results": ""},
                "mid": {"last_seen": hours_after(1), "recent_results": ""},
            },
            cf.RANK,
        )
        assert result["new"] == result["old"] == 1.0
        assert result["mid"] == pytest.approx(cf.rank_score(1, 2))

    def test_mistakes_set_a_floor(self):
        times = [hours_after(h) for h in range(10)]
        stats = self.stats(*times)
        stats["w9"]["recent_results"] = "0000000000"
        stats["w8"]["recent_results"] = "00000"
        result = cf.scores(stats, cf.RANK)
        assert result["w9"] == 1.0
        assert result["w8"] == pytest.approx(0.5)

    def test_reads_iso_strings(self):
        result = cf.scores(self.stats(OLDEST.isoformat(), hours_after(5).isoformat()), cf.RANK)
        assert result["w0"] == 1.0


class TestNextHalfLife:
    def test_doubles_when_right(self):
        assert cf.next_half_life(24, True) == 48

    def test_halves_when_wrong(self):
        assert cf.next_half_life(24, False) == 12

    def test_missing_value_starts_from_default(self):
        assert cf.next_half_life(None, True) == cf.START_HALF_LIFE_HOURS * 2

    def test_stays_within_limits(self):
        assert cf.next_half_life(1, False) == cf.MIN_HALF_LIFE_HOURS
        assert cf.next_half_life(cf.MAX_HALF_LIFE_HOURS, True) == cf.MAX_HALF_LIFE_HOURS


class TestMemoryScore:
    NOW = OLDEST + timedelta(days=30)

    def test_never_practised_scores_one(self):
        assert cf.memory_score(None, 24, self.NOW) == 1.0

    def test_just_practised_scores_zero(self):
        assert cf.memory_score(self.NOW, 24, self.NOW) == 0.0

    @pytest.mark.parametrize("hours, expected", [(24, 0.5), (48, 0.75), (72, 0.875)])
    def test_chance_forgotten(self, hours, expected):
        practised = self.NOW - timedelta(hours=hours)
        assert cf.memory_score(practised, 24, self.NOW) == pytest.approx(expected)

    def test_longer_half_life_scores_lower(self):
        practised = self.NOW - timedelta(hours=24)
        assert cf.memory_score(practised, 96, self.NOW) < cf.memory_score(practised, 24, self.NOW)

    def test_missing_half_life_uses_default(self):
        practised = self.NOW - timedelta(hours=cf.START_HALF_LIFE_HOURS)
        assert cf.memory_score(practised, None, self.NOW) == pytest.approx(0.5)


class TestMemoryMethod:
    NOW = OLDEST + timedelta(days=30)

    def word(self, hours_ago, half_life, recent=""):
        return {
            "last_seen": None,
            "last_practiced": (self.NOW - timedelta(hours=hours_ago)).isoformat(),
            "half_life_hours": half_life,
            "recent_results": recent,
        }

    def test_is_the_default(self):
        assert cf.DEFAULT_METHOD == cf.MEMORY
        stats = {"a": self.word(24, 24), "b": self.word(3, 48, recent="0")}
        assert cf.scores(stats, now=self.NOW) == cf.scores(stats, cf.MEMORY, now=self.NOW)

    def test_scores_each_word_on_its_own(self):
        result = cf.scores(
            {"a": self.word(24, 24), "b": self.word(24, 96), "c": self.word(0, 24)},
            cf.MEMORY,
            now=self.NOW,
        )
        assert result["a"] == pytest.approx(0.5)
        assert result["b"] == pytest.approx(1 - 0.5 ** 0.25)
        assert result["c"] == 0.0

    def test_adding_other_words_does_not_change_a_score(self):
        alone = cf.scores({"a": self.word(24, 24)}, cf.MEMORY, now=self.NOW)
        crowd = cf.scores(
            {"a": self.word(24, 24), "z": self.word(5000, 1)}, cf.MEMORY, now=self.NOW
        )
        assert alone["a"] == pytest.approx(crowd["a"])

    def test_never_practised_scores_one(self):
        result = cf.scores(
            {"new": {"last_seen": None, "recent_results": ""}}, cf.MEMORY, now=self.NOW
        )
        assert result == {"new": 1.0}

    def test_mistakes_set_a_floor(self):
        result = cf.scores({"a": self.word(0, 24, recent="00000")}, cf.MEMORY, now=self.NOW)
        assert result["a"] == pytest.approx(0.5)

    def test_ignores_last_seen(self):
        word = self.word(24, 24)
        word["last_seen"] = OLDEST.isoformat()
        assert cf.scores({"a": word}, cf.MEMORY, now=self.NOW)["a"] == pytest.approx(0.5)

    def test_now_defaults_to_current_time(self):
        practised = (datetime.now() - timedelta(hours=24)).isoformat()
        stats = {"a": {"last_practiced": practised, "half_life_hours": 24, "recent_results": ""}}
        assert cf.scores(stats, cf.MEMORY)["a"] == pytest.approx(0.5, abs=0.01)


def test_unknown_method_raises():
    with pytest.raises(ValueError, match="Unknown method"):
        cf.scores({"a": {"last_seen": None, "recent_results": ""}}, "fastest")


class TestProbabilities:
    def test_normalises(self):
        result = cf.probabilities({"a": 1.0, "b": 0.5, "c": 0.5})
        assert result == pytest.approx({"a": 0.5, "b": 0.25, "c": 0.25})
        assert sum(result.values()) == pytest.approx(1.0)

    def test_all_zero_gives_equal_chances(self):
        assert cf.probabilities({"a": 0.0, "b": 0.0}) == {"a": 0.5, "b": 0.5}

    def test_empty(self):
        assert cf.probabilities({}) == {}


class TestChoose:
    def test_follows_probabilities(self):
        rng = random.Random(1)
        counts = Counter(
            cf.choose(["a", "b", "c"], {"a": 1.0, "b": 0.5, "c": 0.5}, rng)
            for _ in range(20000)
        )
        assert counts["a"] / 20000 == pytest.approx(0.5, abs=0.02)
        assert counts["b"] / 20000 == pytest.approx(0.25, abs=0.02)
        assert counts["c"] / 20000 == pytest.approx(0.25, abs=0.02)

    def test_only_picks_candidates(self):
        rng = random.Random(2)
        word_scores = {"a": 1.0, "b": 0.001, "c": 0.001}
        picks = {cf.choose(["b", "c"], word_scores, rng) for _ in range(200)}
        assert picks == {"b", "c"}

    def test_missing_scores_count_as_one(self):
        rng = random.Random(3)
        picks = Counter(cf.choose(["new", "seen"], {"seen": 0.0}, rng) for _ in range(100))
        assert picks == {"new": 100}

    def test_all_zero_still_picks(self):
        assert cf.choose(["a"], {"a": 0.0}) == "a"

    def test_no_candidates(self):
        with pytest.raises(ValueError):
            cf.choose([], {})
