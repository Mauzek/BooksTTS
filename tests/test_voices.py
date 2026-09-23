"""Тесты назначения голосов. Ни синтеза, ни сети."""

from __future__ import annotations

import pytest

from audiobook.core.voices import (
    CHARACTER_POOL,
    NARRATOR_SPEAKER,
    SILERO_SPEAKERS,
    Voice,
    VoiceMap,
    VoicesError,
)

YAML = """\
narrator: { engine: silero, speaker: aidar, rate: 1.0 }
Рудеус:   { engine: silero, speaker: eugene, rate: 1.05 }
Роксана:  baya
"""


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


def test_defaults_are_valid():
    voice = Voice()
    assert voice.engine == "silero"
    assert voice.speaker in SILERO_SPEAKERS


def test_unknown_engine_is_rejected():
    with pytest.raises(VoicesError, match="движок"):
        Voice(engine="yandex")


def test_unknown_silero_speaker_is_rejected():
    with pytest.raises(VoicesError, match="диктора"):
        Voice(speaker="василий")


@pytest.mark.parametrize("rate", [0.1, 3.0])
def test_rate_out_of_range_is_rejected(rate):
    with pytest.raises(VoicesError, match="темп"):
        Voice(rate=rate)


def test_elevenlabs_speaker_is_not_checked_against_silero():
    # У elevenlabs произвольные voice_id — свой список проверять нечем.
    assert Voice(engine="elevenlabs", speaker="21m00Tcm4TlvDq8ikWAM").speaker


def test_shorthand_is_just_a_speaker_name():
    assert Voice.from_dict("baya") == Voice(speaker="baya")


def test_key_separates_rate():
    assert Voice(speaker="baya", rate=1.0).key != Voice(speaker="baya", rate=1.2).key


def test_bad_yaml_value_names_the_role():
    with pytest.raises(VoicesError, match="Рудеус"):
        Voice.from_dict({"speaker": "нет-такого"}, where="voices.yaml, роль 'Рудеус'")


# --------------------------------------------------------------------------
# Чтение и запись
# --------------------------------------------------------------------------


def test_load_reads_all_forms(tmp_path):
    path = tmp_path / "voices.yaml"
    path.write_text(YAML, encoding="utf-8")
    voices = VoiceMap.load(path)
    assert voices.entries["Рудеус"].speaker == "eugene"
    assert voices.entries["Рудеус"].rate == 1.05
    assert voices.entries["Роксана"].speaker == "baya"


def test_missing_file_is_an_empty_map(tmp_path):
    voices = VoiceMap.load(tmp_path / "нет.yaml")
    assert len(voices) == 0


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "voices.yaml"
    original = VoiceMap({"Аглая": Voice(speaker="xenia", rate=1.1)})
    original.save(path)
    assert VoiceMap.load(path).entries["Аглая"] == Voice(speaker="xenia", rate=1.1)


def test_saved_file_explains_itself(tmp_path):
    path = tmp_path / "voices.yaml"
    VoiceMap({"Аглая": Voice()}).save(path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "aidar" in text  # список дикторов в шапке


def test_broken_yaml_names_the_file(tmp_path):
    path = tmp_path / "voices.yaml"
    path.write_text("роль: [незакрытая", encoding="utf-8")
    with pytest.raises(VoicesError, match="voices.yaml"):
        VoiceMap.load(path)


# --------------------------------------------------------------------------
# Подсказки и проверка назначений
# --------------------------------------------------------------------------


def test_suggestion_is_stable_between_runs():
    assert VoiceMap().suggest("Велимир") == VoiceMap().suggest("Велимир")


def test_narrator_gets_the_narrator_voice():
    assert VoiceMap().suggest("narrator").speaker == NARRATOR_SPEAKER


def test_suggestion_avoids_taken_voices():
    taken = set(CHARACTER_POOL[:-1])
    assert VoiceMap().suggest("Аглая", taken=taken).speaker == CHARACTER_POOL[-1]


def test_suggestion_wraps_when_pool_is_exhausted():
    voice = VoiceMap().suggest("Аглая", taken=set(SILERO_SPEAKERS))
    assert voice.speaker in SILERO_SPEAKERS  # круг начинается заново, а не падает


def test_suggestions_spread_across_the_pool():
    names = ["Аглая", "Велимир", "Терех"]
    chosen = VoiceMap().suggestions_for(names)
    assert len({v.speaker for v in chosen.values()}) == len(names)


def test_suggestions_keep_assigned_voices():
    voices = VoiceMap({"Аглая": Voice(speaker="kseniya")})
    assert voices.suggestions_for(["Аглая", "Терех"])["Аглая"].speaker == "kseniya"


def test_unassigned_lists_roles_without_a_voice():
    voices = VoiceMap({"narrator": Voice()})
    assert voices.unassigned(["narrator", "Аглая", "Терех"]) == ["Аглая", "Терех"]


def test_default_voice_covers_everyone():
    voices = VoiceMap({}, default=Voice(speaker="baya"))
    assert voices.unassigned(["Аглая"]) == []
    assert voices.get("кто-угодно").speaker == "baya"


def test_resolve_falls_back_to_a_suggestion():
    assert VoiceMap().resolve("Аглая").speaker in SILERO_SPEAKERS
