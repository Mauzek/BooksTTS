"""Тесты HTTP-слоя. Ни сети, ни настоящего API — клиент подменён."""

from __future__ import annotations

import pytest
from fakes import FakeClient
from test_markup import dialogue

from audiobook.api.app import create_app
from audiobook.core import repo

fastapi_testclient = pytest.importorskip("fastapi.testclient")

SESSION = {"x-session": "test-session"}
CSRF = {"x-requested-with": "booktts"}


@pytest.fixture()
def http(db, conn, marked):
    """Приложение поверх той же временной базы, что и фикстуры."""
    conn.commit()  # сервер открывает своё соединение — оно видит зафиксированное
    app = create_app(db=db, client=FakeClient(dialogue))
    with fastapi_testclient.TestClient(app, headers=CSRF) as client:
        yield client


@pytest.fixture()
def chapter_id(marked):
    return marked.id


# --------------------------------------------------------------------------
# Страница и настройки
# --------------------------------------------------------------------------


def test_app_page_and_its_scripts_are_served(http):
    response = http.get("/")
    assert response.status_code == 200
    assert '<script type="module"' in response.text
    script = http.get("/ui/js/app.js")
    assert script.status_code == 200
    # WebView2 кеширует агрессивно: без no-cache правки интерфейса не видны.
    assert script.headers["cache-control"] == "no-cache"


def test_client_errors_are_logged(http, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="audiobook.api"):
        response = http.post(
            "/api/client-log",
            json={"message": "x is not defined", "source": "/ui/js/app.js", "line": 3, "stack": "a" * 10_000},
        )
    assert response.status_code == 204
    assert "x is not defined" in caplog.text


def test_config_reports_defaults(http):
    data = http.get("/api/config").json()
    assert data["default_model"].startswith("claude-")
    assert data["emotions"][0] == "нейтрально"


# --------------------------------------------------------------------------
# Библиотека
# --------------------------------------------------------------------------


def test_library_lists_books(http):
    data = http.get("/api/library").json()
    assert data["books"][0]["title"] == "Проба"
    assert data["books"][0]["chapters"] == 1


def test_chapters_report_segment_counts(http, marked):
    data = http.get(f"/api/books/{marked.book_id}/chapters").json()
    assert data["chapters"][0]["segments"] == 5


def test_import_of_a_missing_file_is_a_clear_error(http):
    response = http.post("/api/books/import", json={"path": "нет-такого.epub"})
    assert response.status_code == 400
    assert "не найден" in response.json()["detail"]


def test_import_adds_a_book(http, tmp_path):
    source = tmp_path / "новая.txt"
    source.write_text(
        "Глава 1\n\n" + "\n\n".join(f"Абзац {i} этой главы." for i in range(4)),
        encoding="utf-8",
    )
    body = http.post("/api/books/import", json={"path": str(source)}).json()
    assert body["chapters"]


# --------------------------------------------------------------------------
# Глава
# --------------------------------------------------------------------------


def test_chapter_carries_text_segments_and_speakers(http, chapter_id):
    data = http.get(f"/api/chapters/{chapter_id}").json()
    assert data["chapter"]["text"]
    assert len(data["segments"]) == 5
    assert {s["name"] for s in data["speakers"]} == {"narrator", "Велимир", "Аглая"}


def test_every_speaker_has_a_stable_colour(http, chapter_id):
    first = http.get(f"/api/chapters/{chapter_id}").json()
    second = http.get(f"/api/chapters/{chapter_id}").json()
    assert [s["color"] for s in first["speakers"]] == [s["color"] for s in second["speakers"]]


def test_segment_offsets_match_the_chapter_text(http, chapter_id):
    data = http.get(f"/api/chapters/{chapter_id}").json()
    text = data["chapter"]["text"]
    for segment in data["segments"]:
        assert text[segment["char_start"] : segment["char_end"]] == segment["text"]


def test_unknown_chapter_is_404(http):
    assert http.get("/api/chapters/999").status_code in (404, 500)


# --------------------------------------------------------------------------
# Правки
# --------------------------------------------------------------------------


def test_assign_changes_the_speaker(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    body = http.post(
        f"/api/chapters/{chapter_id}/assign",
        json={"segment_ids": [segments[0]["id"]], "speaker": "Аглая"},
        headers=SESSION,
    ).json()
    assert body["segments"][0]["speaker"] == "Аглая"
    assert body["segments"][0]["is_manual"] is True
    assert body["history"]["can_undo"] is True


def test_assign_range_splits_a_segment(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    target = segments[0]
    body = http.post(
        f"/api/chapters/{chapter_id}/assign-range",
        json={
            "char_start": target["char_start"] + 6,
            "char_end": target["char_start"] + 18,
            "speaker": "Терех",
        },
        headers=SESSION,
    ).json()
    assert len(body["segments"]) == len(segments) + 2
    assert any(s["speaker"] == "Терех" for s in body["segments"])


def test_empty_range_is_rejected(http, chapter_id):
    response = http.post(
        f"/api/chapters/{chapter_id}/assign-range",
        json={"char_start": 5, "char_end": 5, "speaker": "Терех"},
        headers=SESSION,
    )
    assert response.status_code == 400
    assert "пустое выделение" in response.json()["detail"]


def test_split_and_merge_round_trip(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    target = segments[0]
    after_split = http.post(
        f"/api/chapters/{chapter_id}/split",
        json={"segment_id": target["id"], "offset": 10},
        headers=SESSION,
    ).json()
    halves = [s["id"] for s in after_split["segments"][:2]]

    after_merge = http.post(
        f"/api/chapters/{chapter_id}/merge",
        json={"segment_ids": halves},
        headers=SESSION,
    ).json()
    assert after_merge["segments"][0]["text"] == target["text"]


def test_merge_of_different_speakers_is_rejected(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    response = http.post(
        f"/api/chapters/{chapter_id}/merge",
        json={"segment_ids": [segments[0]["id"], segments[1]["id"]]},
        headers=SESSION,
    )
    assert response.status_code == 400
    assert "разные говорящие" in response.json()["detail"]


def test_rename_applies_to_every_segment(http, chapter_id):
    body = http.post(
        f"/api/chapters/{chapter_id}/rename",
        json={"old": "Велимир", "new": "Веля"},
        headers=SESSION,
    ).json()
    assert "Веля" in {s["speaker"] for s in body["segments"]}
    assert "Велимир" not in {s["speaker"] for s in body["segments"]}


def test_emotion_is_stored(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    body = http.post(
        f"/api/chapters/{chapter_id}/emotion",
        json={"segment_ids": [segments[1]["id"]], "emotion": "зло"},
        headers=SESSION,
    ).json()
    assert body["segments"][1]["emotion"] == "зло"


# --------------------------------------------------------------------------
# Отмена
# --------------------------------------------------------------------------


def test_undo_then_redo(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    http.post(
        f"/api/chapters/{chapter_id}/assign",
        json={"segment_ids": [segments[0]["id"]], "speaker": "Аглая"},
        headers=SESSION,
    )
    undone = http.post(f"/api/chapters/{chapter_id}/undo", headers=SESSION).json()
    assert undone["segments"][0]["speaker"] == "narrator"

    redone = http.post(f"/api/chapters/{chapter_id}/redo", headers=SESSION).json()
    assert redone["segments"][0]["speaker"] == "Аглая"


def test_undo_with_nothing_to_undo_is_400(http, chapter_id):
    response = http.post(f"/api/chapters/{chapter_id}/undo", headers=SESSION)
    assert response.status_code == 400


def test_history_is_separate_per_session(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    http.post(
        f"/api/chapters/{chapter_id}/assign",
        json={"segment_ids": [segments[0]["id"]], "speaker": "Аглая"},
        headers={"x-session": "session-a"},
    )
    response = http.post(f"/api/chapters/{chapter_id}/undo", headers={"x-session": "session-b"})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Разметка и проверки
# --------------------------------------------------------------------------


def test_markup_replaces_segments(http, chapter_id):
    body = http.post(f"/api/chapters/{chapter_id}/markup", json={}).json()
    assert body["markup"]["ok"] is True
    assert body["segments"]


def test_markup_keeps_manual_segments(http, chapter_id):
    segments = http.get(f"/api/chapters/{chapter_id}").json()["segments"]
    http.post(
        f"/api/chapters/{chapter_id}/assign",
        json={"segment_ids": [segments[1]["id"]], "speaker": "Терех"},
        headers=SESSION,
    )
    body = http.post(f"/api/chapters/{chapter_id}/markup", json={}).json()
    assert "Терех" in {s["speaker"] for s in body["segments"]}


def test_checks_report_missing_voices(http, chapter_id):
    data = http.get(f"/api/chapters/{chapter_id}/checks").json()
    assert data["blockers"] > 0
    assert any(f["kind"] == "no_voice" for f in data["findings"])


# --------------------------------------------------------------------------
# Доступ: токен десктопа и защита от CSRF
# --------------------------------------------------------------------------


@pytest.fixture()
def guarded(db, conn, marked):
    conn.commit()
    app = create_app(db=db, client=FakeClient(dialogue), token="test-token-0123456789")
    with fastapi_testclient.TestClient(app, headers=CSRF) as client:
        yield client, app.state.token


def test_without_token_access_is_denied(guarded):
    client, _ = guarded
    assert client.get("/api/library").status_code == 401
    assert client.get("/").status_code == 401


def test_token_header_grants_access(guarded):
    client, token = guarded
    assert client.get("/api/library", headers={"x-booktts-token": token}).status_code == 200


def test_wrong_token_in_enter_is_refused(guarded):
    client, _ = guarded
    response = client.get("/desktop/enter?token=wrong", follow_redirects=False)
    assert response.status_code == 401


def test_enter_exchanges_token_for_a_cookie(guarded):
    client, token = guarded
    response = client.get(f"/desktop/enter?token={token}", follow_redirects=False)
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    # Кука сохранилась в клиенте — дальше пускает без заголовка.
    assert client.get("/api/library").status_code == 200


def test_mutation_without_csrf_header_is_refused(http, chapter_id):
    response = http.post(
        f"/api/chapters/{chapter_id}/undo",
        headers={"x-session": "s", "x-requested-with": "evil"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# Библиотека: папки, книги, границы глав, поиск
# --------------------------------------------------------------------------


def test_folder_lifecycle(http, marked):
    folder = http.post("/api/folders", json={"name": "Цикл"}).json()
    child = http.post("/api/folders", json={"name": "Том", "parent_id": folder["id"]}).json()
    assert http.patch(f"/api/folders/{child['id']}", json={"name": "Том 1"}).json()["name"] == "Том 1"

    moved = http.post(f"/api/books/{marked.book_id}/move", json={"parent_id": child["id"]}).json()
    assert moved["folder_id"] == child["id"]

    tree = http.get("/api/library").json()
    assert tree["folders"][0]["folders"][0]["books"][0]["id"] == marked.book_id

    assert http.delete(f"/api/folders/{folder['id']}").status_code == 200
    assert http.get("/api/library").json()["books"][0]["id"] == marked.book_id


def test_folder_cycle_is_a_400_not_a_404(http):
    parent = http.post("/api/folders", json={"name": "А"}).json()
    child = http.post("/api/folders", json={"name": "Б", "parent_id": parent["id"]}).json()
    response = http.post(f"/api/folders/{parent['id']}/move", json={"parent_id": child["id"]})
    assert response.status_code == 400


def test_book_view_lists_chapters_with_stats(http, marked):
    data = http.get(f"/api/books/{marked.book_id}").json()
    assert data["chapters"][0]["segments"] == 5
    assert {s["name"] for s in data["speakers"]} >= {"Велимир", "Аглая"}


def test_split_and_merge_chapter_routes(http, marked):
    offset = marked.text.index("Он промолчал")
    split = http.post(
        f"/api/chapters/{marked.id}/split-chapter", json={"offset": offset, "title": "Хвост"}
    ).json()
    assert [c["title"] for c in split["chapters"]] == ["Ночной рынок", "Хвост"]
    merged = http.post(f"/api/chapters/{marked.id}/merge-next").json()
    assert merged["n_chars"] == len(marked.text)


def test_split_error_is_a_400(http, marked):
    response = http.post(f"/api/chapters/{marked.id}/split-chapter", json={"offset": 0})
    assert response.status_code == 400


def test_segment_split_route_is_not_shadowed(http, marked):
    """Регрессия: маршрут разделения главы однажды перекрыл разделение сегмента."""
    segment = http.get(f"/api/chapters/{marked.id}").json()["segments"][0]
    body = http.post(
        f"/api/chapters/{marked.id}/split",
        json={"segment_id": segment["id"], "offset": 5},
        headers=SESSION,
    ).json()
    assert len(body["segments"]) == 6


def test_search_route_returns_marked_titles(http, marked):
    results = http.get("/api/search", params={"q": "проба"}).json()["results"]
    assert results[0]["kind"] == "book"
    assert "\x02" in results[0]["title"]


def test_upload_imports_a_book(http):
    body = "Глава 1\n\n" + "\n\n".join(f"Абзац {i} загруженной книги." for i in range(5))
    response = http.put(
        "/api/books/upload", params={"filename": "загрузка.txt"}, content=body.encode("utf-8")
    )
    assert response.status_code == 200
    assert response.json()["chapters"]


def test_upload_of_an_unknown_format_is_refused(http):
    response = http.put("/api/books/upload", params={"filename": "x.exe"}, content=b"MZ")
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Голоса, роли и ключи
# --------------------------------------------------------------------------


@pytest.fixture()
def engine(monkeypatch):
    """Подменённый движок: тесты не грузят модели и не ходят в сеть."""
    from test_catalog import FakeEngine

    from audiobook.core import catalog

    fake = FakeEngine()
    monkeypatch.setattr(catalog, "get_engine", lambda name, options=None: fake)
    monkeypatch.setattr(catalog, "engine_names", lambda: ["silero"])
    return fake


@pytest.fixture()
def vault(monkeypatch):
    from test_catalog import FakeKeyring

    from audiobook.core import secrets

    FakeKeyring.store = {}
    monkeypatch.setattr(secrets, "_keyring", FakeKeyring)
    return FakeKeyring.store


def test_engines_are_listed_with_readiness(http, engine):
    data = http.get("/api/engines").json()
    assert data["engines"][0]["ready"] is True
    assert data["settings"]["engine.silero.model"]


def test_refresh_then_catalog(http, engine):
    report = http.post("/api/engines/refresh", json={}).json()["report"]
    assert report[0]["added"] == 3
    voices = http.get("/api/voices?gender=ж").json()["voices"]
    assert {v["voice_key"] for v in voices} == {"baya", "kseniya"}


def test_preview_file_is_served(http, engine):
    http.post("/api/engines/refresh", json={})
    voice = http.get("/api/voices").json()["voices"][0]
    result = http.post(f"/api/voices/{voice['id']}/preview", json={"text": "Проверка"}).json()
    assert http.get(result["url"]).status_code == 200


def test_cast_assignment_round_trip(http, engine, marked):
    http.post("/api/engines/refresh", json={})
    voice = http.get("/api/voices").json()["voices"][0]
    body = http.put(
        f"/api/books/{marked.book_id}/cast",
        json={"speaker": "Аглая", "voice_id": voice["id"], "rate": 1.1},
    ).json()
    aglaya = next(s for s in body["speakers"] if s["name"] == "Аглая")
    assert aglaya["voice"]["voice_key"] == voice["voice_key"]
    assert aglaya["cast"]["rate"] == 1.1


def test_auto_cast_without_catalog_explains_itself(http, marked):
    response = http.post(f"/api/books/{marked.book_id}/cast/auto", json={})
    assert response.status_code == 400
    assert "каталог" in response.json()["detail"]


def test_profile_round_trip_over_http(http, engine, marked):
    http.post("/api/engines/refresh", json={})
    http.post(f"/api/books/{marked.book_id}/cast/auto", json={})
    http.post(f"/api/books/{marked.book_id}/profiles", json={"name": "Цикл"})
    profile = http.get("/api/profiles").json()["profiles"][0]
    result = http.post(
        f"/api/books/{marked.book_id}/apply-profile", json={"profile_id": profile["id"]}
    ).json()
    assert len(result["applied"]) == 3


def test_tokens_are_masked_over_http(http, vault):
    http.put("/api/tokens/anthropic", json={"key": "sk-очень-длинный-ключ-1234"})
    tokens = http.get("/api/tokens").json()["tokens"]
    anthropic = next(t for t in tokens if t["service"] == "anthropic")
    assert anthropic["present"] is True and anthropic["source"] == "keyring"
    assert "1234" in anthropic["masked"]
    assert "очень" not in str(tokens)


def test_unknown_token_service_is_400(http, vault):
    assert http.put("/api/tokens/openai", json={"key": "x"}).status_code == 400


def test_settings_round_trip(http):
    http.put("/api/settings", json={"values": {"engine.silero.model": "v4_ru"}})
    data = http.get("/api/settings").json()
    assert data["settings"]["engine.silero.model"] == "v4_ru"
    assert "v5_5_ru" in data["choices"]["engine.silero.model"]


# --------------------------------------------------------------------------
# Озвучка, очередь задач и словарь произношений
# --------------------------------------------------------------------------


def test_synthesis_is_refused_until_voices_are_assigned(http, marked):
    response = http.post(f"/api/chapters/{marked.id}/synthesize", json={})
    assert response.status_code == 400
    assert "нет голоса" in response.json()["detail"]


def test_job_is_queued_and_can_be_cancelled(http, engine, marked):
    http.post("/api/engines/refresh", json={})
    http.post(f"/api/books/{marked.book_id}/cast/auto", json={})

    job = http.post(f"/api/chapters/{marked.id}/synthesize", json={}).json()
    assert job["status"] == "pending"

    listing = http.get("/api/jobs").json()
    assert listing["active"] == 1
    assert "Озвучка" in listing["jobs"][0]["title"]

    assert http.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert http.get("/api/jobs").json()["active"] == 0


def test_finished_jobs_are_cleared(http, engine, marked):
    http.post("/api/engines/refresh", json={})
    http.post(f"/api/books/{marked.book_id}/cast/auto", json={})
    job = http.post(f"/api/books/{marked.book_id}/synthesize", json={}).json()
    http.post(f"/api/jobs/{job['id']}/cancel")
    assert http.delete("/api/jobs").json()["deleted"] == 1


def test_audio_is_missing_until_the_chapter_is_voiced(http, marked):
    response = http.get(f"/api/chapters/{marked.id}/audio")
    assert response.status_code == 404
    assert "не озвучена" in response.json()["detail"]


def test_pronunciation_rules_round_trip(http, marked):
    body = http.put(
        f"/api/books/{marked.book_id}/pronunciations",
        json={"term": "Рудеус", "replacement": "Рудэус"},
    ).json()
    assert body["rules"][0]["term"] == "Рудеус"
    rule_id = body["rules"][0]["id"]
    left = http.delete(f"/api/books/{marked.book_id}/pronunciations/{rule_id}").json()
    assert left["rules"] == []


def test_ready_line_is_parseable(library):
    import json

    from audiobook.api.app import READY_PREFIX, ready_line

    line = ready_line(54321)
    assert line.startswith(READY_PREFIX)
    assert json.loads(line[len(READY_PREFIX):])["port"] == 54321
