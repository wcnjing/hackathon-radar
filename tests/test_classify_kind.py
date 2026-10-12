"""The keyword fallback's kind classifier, and how a kind decides the bar an
event is judged against when Claude is not doing the scoring."""

import tomllib
from pathlib import Path

import pytest
from scoring_fakes import CONFIG, INTERESTS, _ScoresEverything, make_event

from hackathon_radar import cli, filtering
from hackathon_radar.cli import _select
from hackathon_radar.filtering import classify_kind, classify_kind_with_signal
from hackathon_radar.scoring import (
    ScoredEvent,
    ScoreResult,
    score_events,
)
from hackathon_radar.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Classification uses every available signal, and anchors properly
# ---------------------------------------------------------------------------


class TestClassifyKind:
    @pytest.mark.parametrize(
        "title,expected",
        [
            # the review's confirmed misclassifications
            ("Social Impact Hackathon 2026", "hackathon"),
            ("Hackathon Demo Day", "hackathon"),
            ("AI Workshop: Build an Agent", "hackathon"),
            ("Antler Residency", "program"),
            # anchoring: "sprint" must not fire inside "Sprinter"
            ("Sprinter Van Expo", "networking"),
            # still-correct classifications that must not regress
            ("Founders Networking Meetup", "networking"),
            ("AI Builders Mixer", "networking"),
            ("Fireside Chat with a Founder", "networking"),
            ("Startup Accelerator 2026", "program"),
            ("NUS Datathon 2026", "hackathon"),
            ("Hack&Roll 2026", "hackathon"),
            ("GenAI Buildathon", "hackathon"),
            ("Global Game Jam", "hackathon"),
            ("AWS DeepRacer Student League", "hackathon"),
            ("AI Singapore Apprenticeship Programme", "program"),
        ],
    )
    def test_titles(self, title, expected):
        assert classify_kind(make_event(title=title)) == expected

    @pytest.mark.parametrize("source", ["devpost", "mlh"])
    def test_hackathon_only_sources_win_over_the_title(self, source):
        """MHacks and TartanHacks carry no token a regex can safely anchor on,
        but the source they came from only ever publishes hackathons."""
        assert classify_kind(make_event(title="MHacks 2026", source=source)) == "hackathon"
        assert classify_kind(make_event(title="TartanHacks", source=source)) == "hackathon"

    def test_tags_are_searched_as_well_as_the_title(self):
        """MLH stamps every event with this tag; Devpost carries its themes."""
        event = make_event(title="Spring Season Kickoff", tags=["student hackathon"])
        assert classify_kind(event) == "hackathon"

    def test_location_is_not_searched(self):
        """A venue called "The Hive Social" is not a networking event."""
        event = make_event(title="GenAI Buildathon", location="The Hive Social, Singapore")
        assert classify_kind(event) == "hackathon"

    def test_hackathon_beats_networking_when_a_title_carries_both(self):
        assert classify_kind(make_event(title="Hackathon Mixer and Demo Day")) == "hackathon"

    def test_ambiguous_titles_report_no_signal(self):
        kind, matched = classify_kind_with_signal(make_event(title="Tuesday Evening"))
        assert (kind, matched) == ("networking", False)

    def test_matched_titles_report_a_signal(self):
        assert classify_kind_with_signal(make_event(title="AI Buildathon"))[1] is True
        assert classify_kind_with_signal(make_event(title="Founders Meetup"))[1] is True

    @pytest.mark.parametrize(
        "title", ["Sprinter Van Expo", "Shack Party", "Jamboree Festival", "Hackney Community Day"]
    )
    def test_word_boundaries_prevent_false_hackathons(self, title):
        assert classify_kind(make_event(title=title)) != "hackathon"


class TestKindSignalBonus:
    """A confident build-event classification counts as one keyword hit, so the
    classifier regexes and `interests.keywords` cannot silently drift apart."""

    def test_a_datathon_no_keyword_knows_still_clears_the_bar(self):
        event = make_event(title="NUS Datathon 2026")
        result = score_events([event], CONFIG, None)[event.key]
        assert event.kind == "hackathon"
        assert result.score >= INTERESTS["min_score"], "classified but scored as irrelevant"

    def test_networking_gets_no_bonus(self):
        plain = make_event(title="Tuesday Evening", external_id="1")
        mixer = make_event(title="Founders Networking Mixer", external_id="2")
        scores = score_events([plain, mixer], CONFIG, None)
        assert mixer.kind == "networking"
        # the mixer's extra points come from keyword hits only, never a bonus
        assert "networking title" not in scores[mixer.key].reason

    def test_the_bonus_is_recorded_in_the_reason(self):
        event = make_event(title="GenAI Buildathon")
        result = score_events([event], CONFIG, None)[event.key]
        assert "+hackathon title" in result.reason

    def test_the_bonus_never_pushes_past_ten(self):
        event = make_event(title="AI Student Hackathon Meetup Networking", location="Singapore")
        result = score_events([event], CONFIG, None)[event.key]
        assert result.score <= 10.0

    def test_an_unsignalled_event_gets_no_bonus(self):
        event = make_event(title="Tuesday Evening")
        result = score_events([event], CONFIG, None)[event.key]
        assert "+" not in result.reason


# ---------------------------------------------------------------------------
# Guard the acceptance criterion against the REAL config.toml
# ---------------------------------------------------------------------------


class TestAgainstTheRealConfig:
    """Issue #4's AC1 says "under default config". Hand-written test interests
    can drift from config.toml without CI noticing, so assert on the shipped
    file itself."""

    @pytest.fixture(scope="class")
    @classmethod
    def real(cls):
        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            return tomllib.load(f)["interests"]

    def _outcome(self, event, interests):
        result = score_events([event], {"interests": interests, "scoring": {}}, None)[event.key]
        threshold = interests.get("min_score_by_kind", {}).get(
            event.kind, interests.get("min_score", 6)
        )
        return result.score >= threshold, event.kind, result.score

    def test_the_shipped_config_still_raises_the_networking_bar(self, real):
        """If this ever stops being true the rest of the class proves nothing."""
        assert real["min_score_by_kind"]["networking"] > real["min_score"]

    @pytest.mark.parametrize(
        "title",
        [
            "Founders Networking Meetup",  # the issue's own example
            "AI Builders Mixer & Demo Day",
            "Startup Founders' Breakfast",
            "Fireside Chat with a Startup Founder",
            "Tech Talk: Scaling to a Million Users",
        ],
    )
    def test_networking_events_do_not_post_under_the_shipped_config(self, real, title):
        posts, kind, score = self._outcome(make_event(title=title, location="Singapore"), real)
        assert kind == "networking", f"{title!r} classified {kind!r}"
        assert not posts, f"{title!r} posted at {score}"

    @pytest.mark.parametrize(
        "title",
        [
            # every title the review named as wrongly blocked
            "AI Agents Hackathon 2026",
            "Anthropic Builder Workshop Singapore",
            "Women Who Code Workshop: LLM Fine-tuning",
            "AI Singapore Apprenticeship Programme",
            "AWS DeepRacer Student League",
            "Build with AI: Gemini Developer Day",
            "NUS Datathon 2026",
            "Hack&Roll 2026",
            # and a few more of the same shape
            "GenAI Buildathon SG",
            "MHacks 2026",
            "Social Impact Hackathon 2026",
            "Antler Residency",
        ],
    )
    def test_wanted_events_still_post_under_the_shipped_config(self, real, title):
        """The review's Critical finding was that the fix blocked 16 of 17
        events the profile explicitly asks for. Guard the other direction too."""
        posts, kind, score = self._outcome(make_event(title=title, location="Singapore"), real)
        assert posts, f"{title!r} blocked at {score} as {kind!r}"

    def test_the_build_boundary_separates_building_from_mingling(self, real):
        """`build` is what lets "Build with AI" through. Its closing
        boundary is what keeps "AI Builders Mixer" out. Both halves matter, so
        both are asserted together -- widening one silently breaks the other."""
        builds, _, _ = self._outcome(make_event(title="Build with AI: Gemini Day"), real)
        mingles, kind, score = self._outcome(make_event(title="AI Builders Mixer & Demo Day"), real)
        assert builds is True
        assert mingles is False, f"a mixer posted at {score} as {kind!r}"


# ---------------------------------------------------------------------------
# One taxonomy, imported by both users
# ---------------------------------------------------------------------------


class TestKindTaxonomyIsShared:
    def test_scored_event_uses_the_filtering_alias(self):
        from typing import get_args

        from hackathon_radar.filtering import Kind

        assert set(get_args(ScoredEvent.model_fields["kind"].annotation)) == set(get_args(Kind))

    def test_classify_kind_only_returns_taxonomy_members(self):
        from typing import get_args

        from hackathon_radar.filtering import Kind

        titles = ["Hackathon", "Meetup", "Accelerator", "Tuesday Evening", "Sprinter Van"]
        assert {classify_kind(make_event(title=t)) for t in titles} <= set(get_args(Kind))


# ---------------------------------------------------------------------------
# Weak build words must not outrank explicit networking words
# ---------------------------------------------------------------------------


class TestStrongAndWeakBuildSignals:
    """Ranking every build word above networking fixed "Social Impact
    Hackathon" and broke "Build Club Mixer". Strong words name the format
    outright and earn the precedence; weak ones merely suggest building, and a
    mixer will happily call itself a jam.
    """

    @pytest.mark.parametrize(
        "title",
        [
            # a strong word plus a networking word: the strong word wins
            "Hackathon Demo Day",
            "Social Impact Hackathon 2026",
            "Datathon Networking Session",
            "Hack&Roll Mixer",
        ],
    )
    def test_strong_words_beat_networking_words(self, title):
        assert classify_kind(make_event(title=title)) == "hackathon"

    @pytest.mark.parametrize(
        "title",
        [
            # a weak word plus a networking word: networking wins
            "Build Club Mixer",
            "Founders Mixer: Build Night",
            "Pitch Competition & Networking Night",
            "Startup Networking Challenge Night",
            "Demo Day Jam Session",
            "Founders Breakfast: Build in Public",
            "AI Builders Mixer",
            "Workshop and Networking Evening",
        ],
    )
    def test_networking_words_beat_weak_build_words(self, title):
        kind = classify_kind(make_event(title=title))
        assert kind == "networking", f"{title!r} classified {kind!r} and would post at the base bar"

    @pytest.mark.parametrize(
        "title",
        [
            # a weak word and no networking word: the weak word is enough
            "Build with AI: Gemini Developer Day",
            "AWS DeepRacer Student League",
            "AI Workshop: Build an Agent",
            "Anthropic Builder Workshop Singapore",
            "Women Who Code Workshop: LLM Fine-tuning",
            "Global Game Jam",
            "Weekend Build Sprint",
        ],
    )
    def test_weak_build_words_still_classify_when_nothing_contradicts_them(self, title):
        assert classify_kind(make_event(title=title)) == "hackathon"

    def test_a_networking_format_outranks_a_program_word(self):
        """A demo day about an accelerator is a demo day. This used to assert
        the opposite, and "Accelerator Demo Day" posted at the base bar."""
        assert classify_kind(make_event(title="Accelerator Demo Day")) == "networking"

    def test_a_program_word_still_outranks_social(self):
        """ "social" is as often an adjective as a format, so it alone does not
        turn a program into a networking event."""
        assert classify_kind(make_event(title="Social Impact Accelerator")) == "program"
        assert classify_kind(make_event(title="Social Enterprise Fellowship")) == "program"

    def test_the_two_tiers_do_not_overlap(self):
        """A word in both would make the precedence meaningless."""
        for probe in ("hackathon", "datathon", "buildathon", "hack"):
            assert filtering.STRONG_HACKATHON_RE.search(probe)
            assert not filtering.WEAK_BUILD_RE.search(probe)
        for probe in (
            "build",
            "jam",
            "league",
            "challenge",
            "competition",
            "workshop",
            "sprint",
            "hacks",
        ):
            assert filtering.WEAK_BUILD_RE.search(probe)
            assert not filtering.STRONG_HACKATHON_RE.search(probe)

    def test_source_still_wins_over_a_networking_title(self):
        """Devpost and MLH publish nothing but hackathons, whatever the title."""
        assert classify_kind(make_event(title="Build Club Mixer", source="mlh")) == "hackathon"

    def test_no_mingling_title_clears_the_bar_under_the_shipped_config(self):
        """The end-to-end claim, against the real config rather than fixtures."""
        import tomllib

        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            interests = tomllib.load(f)["interests"]
        leaked = []
        for title in (
            "Build Club Mixer",
            "Founders Mixer: Build Night",
            "Pitch Competition & Networking Night",
            "Startup Networking Challenge Night",
            "Demo Day Jam Session",
            "Founders Breakfast: Build in Public",
        ):
            event = make_event(title=title, source="luma")
            result = score_events([event], {"interests": interests, "scoring": {}}, None)[event.key]
            threshold = interests["min_score_by_kind"].get(event.kind, interests["min_score"])
            if result.score >= threshold:
                leaked.append(f"{title} ({event.kind} {result.score})")
        assert leaked == []


# ---------------------------------------------------------------------------
# A kind nothing matched is a default, and must not earn the raised bar
# ---------------------------------------------------------------------------


class TestUnmatchedKindIsJudgedAtTheBaseBar:
    """With no client the keyword score is final (`degraded` is False), so the
    bar it is judged against decides the event for good. A title that names no
    format is "networking" only as the classifier's default; holding it to the
    networking bar of 8 recorded every such event scoring 6-8 as skipped, and
    `_collect` never scores a recorded event again.

    Run against the shipped config.toml, through `_ingest`, because the burial
    happened in `cli._select` and no assertion on the score alone could see it.
    """

    NO_SIGNAL = [
        "AI Agents for Students",
        "Machine Learning Reading Group",
        "Startup Founder Office Hours",
    ]

    def _config(self):
        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            interests = tomllib.load(f)["interests"]
        return {
            "interests": interests,
            "scoring": {},
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }

    def _luma_event(self, title):
        # As `luma.parse_entry` leaves it: no hackathon word in the title, so
        # the source has already stamped its own "networking" default on it.
        return make_event(title=title, source="luma", kind="networking")

    def _ingest_keyword_only(self, tmp_path, monkeypatch, event):
        """One real ingest with no Anthropic credentials. -> (seen, queued)"""
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        cli._ingest(self._config(), store)
        seen, depth = store.is_seen(event), store.queue_depth()
        store.close()
        return seen, depth

    def test_premise_these_titles_match_nothing_and_clear_the_base_bar(self):
        """If the shipped keywords or the classifier regexes ever change so this
        stops holding, the tests below would pass without testing anything."""
        interests = self._config()["interests"]
        raised = interests["min_score_by_kind"]["networking"]
        scores = []
        for title in self.NO_SIGNAL:
            event = self._luma_event(title)
            assert classify_kind_with_signal(event) == ("networking", False), title
            result = score_events([event], self._config(), None)[event.key]
            assert result.score >= interests["min_score"], f"{title!r} scored {result.score}"
            scores.append(result.score)
        assert any(s < raised for s in scores), "nothing here sits between the two bars"

    @pytest.mark.parametrize("title", NO_SIGNAL)
    def test_it_is_queued_on_a_keyword_only_deployment(self, tmp_path, monkeypatch, title):
        seen, depth = self._ingest_keyword_only(tmp_path, monkeypatch, self._luma_event(title))
        assert depth == 1, f"{title!r} was recorded as skipped and can never post"
        assert seen is True

    def test_a_classified_networking_event_is_still_blocked(self, tmp_path, monkeypatch):
        """Issue #4 stays closed: this one *matches* a networking word, so the
        raised bar is policy about a known kind, not a guess."""
        event = self._luma_event("Founders Networking Meetup")
        seen, depth = self._ingest_keyword_only(tmp_path, monkeypatch, event)
        assert depth == 0, "a meetup posted on keyword hits alone"
        assert seen is True

    def test_an_unmatched_event_below_the_base_bar_is_still_recorded(self, tmp_path, monkeypatch):
        event = self._luma_event("Quantum Computing Primer")
        seen, depth = self._ingest_keyword_only(tmp_path, monkeypatch, event)
        assert (seen, depth) == (True, 0)

    def test_the_result_says_whether_the_kind_was_a_guess(self):
        guessed = make_event(title="Tuesday Evening", external_id="1")
        matched = make_event(title="Founders Meetup", external_id="2")
        scores = score_events([guessed, matched], CONFIG, None)
        assert scores[guessed.key].kind_guessed is True
        assert scores[matched.key].kind_guessed is False

    def test_a_kind_claude_assigned_is_never_a_guess(self):
        event = make_event(title="Tuesday Evening")
        result = score_events([event], CONFIG, _ScoresEverything(9, "networking"))[event.key]
        assert result.kind_guessed is False

    def test_a_guess_is_not_held_below_the_raised_bar(self):
        """Two keyword hits, 8.0, and no format word. The hold-down to 7.0 is
        for events classified as networking; this one was only defaulted."""
        event = make_event(title="AI Student Evening")
        result = score_events([event], CONFIG, None)[event.key]
        assert classify_kind_with_signal(event)[1] is False
        assert result.score == 8.0
        assert "held below" not in result.reason

    def test_select_uses_the_base_bar_for_a_guessed_kind(self, tmp_path):
        """The gate itself: same event, same score, same kind -- only the flag
        differs, and only the flag decides."""
        outcomes = {}
        for guessed in (True, False):
            event = make_event(title="Tuesday Evening", kind="networking")
            store = Store(tmp_path / f"{guessed}.db")
            result = ScoreResult(6.5, "keywords: ai", kind_guessed=guessed)
            outcomes[guessed] = _select([event], {event.key: result}, store, CONFIG, 99, False)
            store.close()
        assert len(outcomes[True]) == 1
        assert outcomes[False] == []


# ---------------------------------------------------------------------------
# An explicit networking format beats a program topic and an ambiguous "hacks"
# ---------------------------------------------------------------------------


class TestNetworkingFormatsCannotBypassTheRaisedBar:
    """Program words were checked before networking ones, and a free-standing
    "hacks" counted as strong hackathon evidence. So a meetup *about* an
    accelerator was a program, "AI Life Hacks Meetup" was a hackathon, and both
    posted at the base bar of 6 on a keyword-only deployment.

    The selection tests run the shipped config.toml through `_ingest`: the
    classification is only the cause, and the harm is what gets queued.
    """

    BYPASSED = [
        "Startup Accelerator Networking Meetup",
        "Accelerator Demo Day",
        "AI Life Hacks Meetup",
    ]
    UNMISTAKABLE = [
        "MHacks 2026",
        "TartanHacks",
        "Hack&Roll 2026",
        "AI Agents Hackathon 2026",
        "Hackathon Demo Day",
        "NUS Datathon Networking Session",
    ]

    def _config(self):
        with open(PROJECT_ROOT / "config.toml", "rb") as f:
            interests = tomllib.load(f)["interests"]
        return {
            "interests": interests,
            "scoring": {},
            "scope": {"mode": "global"},
            "enrich": {"enabled": False},
            "notify": {"max_per_day": 15},
        }

    def _ingest_keyword_only(self, tmp_path, monkeypatch, title):
        event = make_event(title=title, source="luma", location="Singapore")
        store = Store(tmp_path / "t.db")
        monkeypatch.setattr(cli, "fetch_all", lambda cfg: [event])
        monkeypatch.setattr(cli, "make_client", lambda: None)
        cli._ingest(self._config(), store)
        seen, depth = store.is_seen(event), store.queue_depth()
        store.close()
        return event, seen, depth

    @pytest.mark.parametrize("title", BYPASSED)
    def test_they_classify_as_networking(self, title):
        assert classify_kind_with_signal(make_event(title=title)) == ("networking", True)

    def test_premise_they_would_clear_the_base_bar(self):
        """If these stopped out-scoring the base bar, the selection test below
        would pass on the score alone and prove nothing about the kind."""
        interests = self._config()["interests"]
        for title in self.BYPASSED:
            event = make_event(title=title, source="luma", location="Singapore")
            result = score_events([event], self._config(), None)[event.key]
            assert result.score >= interests["min_score"], f"{title!r} scored {result.score}"
            assert "held below the networking bar" in result.reason, title

    @pytest.mark.parametrize("title", BYPASSED)
    def test_none_is_selected_under_the_shipped_config(self, tmp_path, monkeypatch, title):
        event, seen, depth = self._ingest_keyword_only(tmp_path, monkeypatch, title)
        assert depth == 0, f"{title!r} was queued as {event.kind!r}"
        assert seen is True

    @pytest.mark.parametrize("title", UNMISTAKABLE)
    def test_unmistakable_hackathons_are_still_selected(self, tmp_path, monkeypatch, title):
        event, _, depth = self._ingest_keyword_only(tmp_path, monkeypatch, title)
        assert event.kind == "hackathon"
        assert depth == 1, f"{title!r} was blocked"

    @pytest.mark.parametrize(
        "title", ["Life Hacks for Students", "Growth Hacks", "Friday Hacks #287"]
    )
    def test_a_free_standing_hacks_is_only_a_weak_signal(self, title):
        """Still a build word when nothing contradicts it -- NUS Hackers'
        "Friday Hacks" is real -- but no longer one that overrides a format."""
        assert not filtering.STRONG_HACKATHON_RE.search(title)
        assert classify_kind(make_event(title=title)) == "hackathon"
        assert classify_kind(make_event(title=f"{title} Meetup")) == "networking"

    @pytest.mark.parametrize("title", ["Lifehacks Meetup", "Lifehacks for Founders"])
    def test_lifehacks_as_one_word_is_not_a_hackathon_name(self, title):
        assert not filtering.STRONG_HACKATHON_RE.search(title)

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Fellowship Info Talk", "networking"),
            ("Bootcamp Graduates Mixer", "networking"),
            ("Incubator Founders' Coffee", "networking"),
            ("Antler Residency", "program"),
            ("Startup Accelerator 2026", "program"),
            ("AI Singapore Apprenticeship Programme", "program"),
        ],
    )
    def test_program_titles_without_a_format_word_are_still_programs(self, title, expected):
        assert classify_kind(make_event(title=title)) == expected
