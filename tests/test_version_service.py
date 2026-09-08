"""Tests for VersionService, incl. the Phase 2 `subdir` / `source_ref` additions
and the Phase 12A persistence hardening (atomic write, backup, corruption
recovery, in-process concurrency, tz-aware timestamps)."""

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import pytest

from app.services.version_service import VersionPersistenceError, VersionService


def test_subdir_isolates_storage(isolated_output_dir):
    brd = VersionService(project_id="p1")
    hld = VersionService(project_id="p1", subdir="hld")

    brd.add_version(content="brd body", source="initial")
    hld.add_version(content="hld body", source="initial")

    assert (isolated_output_dir / "p1" / "versions.json").exists()
    assert (isolated_output_dir / "p1" / "hld" / "versions.json").exists()

    # Each instance only ever sees its own stream.
    assert len(brd.get_all_versions()) == 1
    assert len(hld.get_all_versions()) == 1
    assert brd.get_all_versions()[0].content == "brd body"
    assert hld.get_all_versions()[0].content == "hld body"


def test_default_path_unchanged_for_brd(isolated_output_dir):
    """Existing BRD callers pass no subdir and must land in the same place as before."""
    VersionService(project_id="p2").add_version(content="x", source="initial")
    assert (isolated_output_dir / "p2" / "versions.json").exists()
    assert not (isolated_output_dir / "p2" / "hld").exists()


def test_append_only_and_deterministic_numbering():
    svc = VersionService(project_id="p3", subdir="hld")
    v1 = svc.add_version(content="one", source="initial")
    v2 = svc.add_version(content="two", source="manual_edit")
    v3 = svc.add_version(content="three", source="ai_refine")

    assert [v1.version, v2.version, v3.version] == [1, 2, 3]
    all_versions = svc.get_all_versions()
    assert [v.content for v in all_versions] == ["one", "two", "three"]
    # earlier versions are never mutated
    assert all_versions[0].content == "one"


def test_mark_final_is_exclusive_and_locks():
    svc = VersionService(project_id="p4", subdir="hld")
    svc.add_version(content="one", source="initial")
    svc.add_version(content="two", source="manual_edit")

    svc.mark_final(1)
    svc.mark_final(2)  # switching final must clear the previous one

    finals = [v for v in svc.get_all_versions() if v.is_final]
    assert len(finals) == 1
    assert finals[0].version == 2
    assert finals[0].is_locked is True
    assert svc.get_version(1).is_final is False
    assert svc.get_version(1).is_locked is False


def test_unlock_final_keeps_is_final():
    svc = VersionService(project_id="p5", subdir="hld")
    svc.add_version(content="one", source="initial")
    svc.mark_final(1)

    svc.unlock_final()

    final = svc.get_final_version()
    assert final.version == 1
    assert final.is_final is True
    assert final.is_locked is False


def test_source_ref_round_trips():
    svc = VersionService(project_id="p6", subdir="hld")
    svc.add_version(content="hld", source="initial", source_ref="brd_v3")

    reloaded = VersionService(project_id="p6", subdir="hld").get_version(1)
    assert reloaded.source_ref == "brd_v3"


def test_source_ref_defaults_to_none():
    svc = VersionService(project_id="p7")
    svc.add_version(content="brd", source="initial")
    assert svc.get_version(1).source_ref is None


# ==========================================================================
# Phase 12A — persistence hardening
# ==========================================================================

_SVC_LOGGER = "app.services.version_service"


def _seed(svc, *contents):
    for c in contents:
        svc.add_version(content=c, source="initial")


def _svc_logs_visible(monkeypatch, caplog, level="CRITICAL"):
    """The app logger sets propagate=False; re-enable it so caplog can see it
    (same pattern as tests/test_test_case_service.py)."""
    monkeypatch.setattr(logging.getLogger(_SVC_LOGGER), "propagate", True)
    return caplog.at_level(level, logger=_SVC_LOGGER)


# --- 1. atomic persistence: happy path ------------------------------------

def test_atomic_write_leaves_valid_json_and_no_temp_files(isolated_output_dir):
    svc = VersionService(project_id="atomic1")
    svc.add_version(content="one", source="initial")
    svc.add_version(content="two", source="manual_edit")

    d = isolated_output_dir / "atomic1"
    assert [p.name for p in d.iterdir() if ".tmp" in p.name] == []  # no leftover temp files

    text = (d / "versions.json").read_text(encoding="utf-8")
    # on-disk shape is unchanged: a 2-space-indented JSON array, no trailing newline
    assert text == json.dumps(json.loads(text), indent=2)
    assert [r["content"] for r in json.loads(text)] == ["one", "two"]
    assert [v.content for v in svc.get_all_versions()] == ["one", "two"]


# --- 2. backup creation / replacement behaviour --------------------------

def test_backup_absent_on_first_write_then_trails_primary_by_one(isolated_output_dir):
    svc = VersionService(project_id="bak1", subdir="hld")
    bak = isolated_output_dir / "bak1" / "hld" / "versions.json.bak"

    svc.add_version(content="one", source="initial")
    assert not bak.exists()  # nothing known-good to back up yet

    svc.add_version(content="two", source="initial")
    assert [r["content"] for r in json.loads(bak.read_text("utf-8"))] == ["one"]

    svc.add_version(content="three", source="initial")
    assert [r["content"] for r in json.loads(bak.read_text("utf-8"))] == ["one", "two"]

    # primary stays complete + current the whole time
    assert [v.content for v in svc.get_all_versions()] == ["one", "two", "three"]


def test_backup_refreshed_on_mark_final_and_unlock(isolated_output_dir):
    svc = VersionService(project_id="bak2", subdir="lld")
    _seed(svc, "one", "two")
    bak = isolated_output_dir / "bak2" / "lld" / "versions.json.bak"

    svc.mark_final(2)
    pre = json.loads(bak.read_text("utf-8"))  # pre-mark_final primary: nothing final yet
    assert all(r["is_final"] is False for r in pre)

    svc.unlock_final()
    pre2 = json.loads(bak.read_text("utf-8"))  # pre-unlock primary: v2 final AND locked
    assert [r for r in pre2 if r["is_final"]][0]["is_locked"] is True


def test_reads_never_create_a_backup(isolated_output_dir):
    svc = VersionService(project_id="bak3")
    svc.add_version(content="only", source="initial")
    bak = isolated_output_dir / "bak3" / "versions.json.bak"
    assert not bak.exists()

    for _ in range(3):
        svc.get_all_versions()
        svc.get_latest_version()
        svc.get_final_version()
    assert not bak.exists()  # a pure read must not write anything


# --- 3. recovery from corrupted primary using a valid backup ------------

def test_recovers_corrupt_primary_from_valid_backup(isolated_output_dir, caplog, monkeypatch):
    svc = VersionService(project_id="rec1")
    _seed(svc, "one", "two")  # bak == [one]
    primary = isolated_output_dir / "rec1" / "versions.json"
    bak = isolated_output_dir / "rec1" / "versions.json.bak"
    good_bak_text = bak.read_text("utf-8")

    primary.write_text("{ not valid json at all", encoding="utf-8")  # simulate a partial write

    with _svc_logs_visible(monkeypatch, caplog):
        recovered = VersionService(project_id="rec1").get_all_versions()

    assert [v.content for v in recovered] == ["one"]  # backup state, NOT an empty history
    assert any(
        r.levelname == "CRITICAL" and "recovered" in r.message.lower()
        for r in caplog.records
    )
    assert primary.read_text("utf-8") == good_bak_text  # primary healed from the backup

    # the stream keeps working, numbering continuing from the recovered state
    v2 = VersionService(project_id="rec1").add_version(content="three", source="initial")
    assert v2.version == 2
    assert [v.content for v in VersionService(project_id="rec1").get_all_versions()] == [
        "one", "three",
    ]


def test_recovery_survives_a_pydantic_validation_failure(isolated_output_dir, caplog, monkeypatch):
    svc = VersionService(project_id="rec1b")
    _seed(svc, "one", "two")  # bak == [one]
    primary = isolated_output_dir / "rec1b" / "versions.json"
    # valid JSON, but a record is missing required fields -> ValidationError
    primary.write_text(json.dumps([{"version": 1}], indent=2), encoding="utf-8")

    with _svc_logs_visible(monkeypatch, caplog):
        recovered = VersionService(project_id="rec1b").get_all_versions()

    assert [v.content for v in recovered] == ["one"]
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


def test_recovery_returns_data_even_when_primary_healing_write_fails(
    isolated_output_dir, caplog, monkeypatch
):
    """Backup is valid but rewriting the primary DURING recovery fails: the
    recovered versions are still returned (never raised, never []), the failed
    heal is logged, the corrupt primary is left as-is, and the next normal write
    self-heals it without ever copying the corrupt primary into the backup."""
    svc = VersionService(project_id="heal1")
    _seed(svc, "one", "two")  # real writes -> bak == [one]
    d = isolated_output_dir / "heal1"
    primary = d / "versions.json"
    bak = d / "versions.json.bak"
    good_bak_text = bak.read_text("utf-8")
    primary.write_text("{{ corrupt", encoding="utf-8")

    real_atomic = VersionService._atomic_write_text
    state = {"primary_writes": 0}

    def flaky_primary_write(self, target, text):
        # Fail ONLY the very first primary write (the recovery heal); every
        # later write (backup refresh + the self-healing save) works normally.
        # No monkeypatch.undo() -> the isolated_output_dir patch is untouched.
        if target.name == "versions.json":
            state["primary_writes"] += 1
            if state["primary_writes"] == 1:
                raise OSError("simulated: cannot rewrite the primary during recovery")
        return real_atomic(self, target, text)

    monkeypatch.setattr(VersionService, "_atomic_write_text", flaky_primary_write)

    with _svc_logs_visible(monkeypatch, caplog):
        recovered = VersionService(project_id="heal1").get_all_versions()

    assert [v.content for v in recovered] == ["one"]  # recovered, not raised, not []
    assert primary.read_text("utf-8") == "{{ corrupt"  # heal did NOT touch the primary
    assert any(
        r.levelname == "CRITICAL" and "could not be restored" in r.getMessage().lower()
        for r in caplog.records
    )

    # next normal write self-heals (heal write works this time); the corrupt
    # primary is never copied into the backup
    v = VersionService(project_id="heal1").add_version(content="three", source="initial")
    assert v.version == 2  # numbering continued from the recovered state
    assert [
        x.content for x in VersionService(project_id="heal1").get_all_versions()
    ] == ["one", "three"]
    assert bak.read_text("utf-8") == good_bak_text  # corrupt primary never entered the backup


# --- 4. failure when both primary and backup are unusable --------------

def test_raises_when_primary_corrupt_and_no_backup(isolated_output_dir, caplog, monkeypatch):
    svc = VersionService(project_id="rec2")
    svc.add_version(content="only", source="initial")  # single write -> no backup yet
    (isolated_output_dir / "rec2" / "versions.json").write_text("<garbage>", encoding="utf-8")

    with _svc_logs_visible(monkeypatch, caplog):
        with pytest.raises(VersionPersistenceError):
            VersionService(project_id="rec2").get_all_versions()
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


def test_raises_when_primary_and_backup_both_corrupt(isolated_output_dir, caplog, monkeypatch):
    svc = VersionService(project_id="rec3")
    _seed(svc, "one", "two")  # bak == [one]
    d = isolated_output_dir / "rec3"
    (d / "versions.json").write_text("nope", encoding="utf-8")
    (d / "versions.json.bak").write_text("also nope", encoding="utf-8")

    with _svc_logs_visible(monkeypatch, caplog):
        with pytest.raises(VersionPersistenceError):
            VersionService(project_id="rec3").get_all_versions()
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


# --- 5. healthy existing JSON loads unchanged --------------------------

def test_healthy_existing_json_loads_unchanged_and_makes_no_backup(isolated_output_dir):
    d = isolated_output_dir / "compat1"
    d.mkdir(parents=True)
    legacy = [
        {
            "version": 1, "content": "legacy one", "source": "initial",
            "created_at": "2026-01-01T00:00:00", "note": "seed",
            "is_final": True, "is_locked": True, "source_ref": "brd_v2",
        },
        {
            "version": 2, "content": "legacy two", "source": "ai_refine",
            "created_at": "2026-01-02T00:00:00", "note": "",
            "is_final": False, "is_locked": False, "source_ref": None,
        },
    ]
    (d / "versions.json").write_text(json.dumps(legacy, indent=2), encoding="utf-8")

    svc = VersionService(project_id="compat1")
    vs = svc.get_all_versions()
    assert [v.version for v in vs] == [1, 2]
    assert vs[0].content == "legacy one"
    assert vs[0].created_at == "2026-01-01T00:00:00"  # naive legacy string preserved verbatim
    assert vs[0].is_final is True and vs[0].is_locked is True
    assert vs[0].source_ref == "brd_v2"
    assert not (d / "versions.json.bak").exists()  # a pure read never writes


# --- 6. empty / new project still works --------------------------------

def test_empty_new_project_returns_empty_and_writes_nothing(isolated_output_dir):
    svc = VersionService(project_id="fresh1", subdir="test_cases")
    assert svc.get_all_versions() == []
    assert svc.get_latest_version() is None
    assert svc.get_final_version() is None
    assert list((isolated_output_dir / "fresh1" / "test_cases").iterdir()) == []


# --- 7. concurrent / in-process writes do not lose updates ------------

def test_concurrent_add_version_does_not_lose_updates(isolated_output_dir, monkeypatch):
    """N threads each append once to the SAME stream. With the load->modify->save
    critical section serialized per path, every append survives and the version
    numbers are 1..N with no gaps or duplicates."""
    orig_save = VersionService._save_all

    def slow_save(self, versions):  # widen the modify->save window to force interleaving
        time.sleep(0.005)
        return orig_save(self, versions)

    monkeypatch.setattr(VersionService, "_save_all", slow_save)

    n = 12
    errors: list[BaseException] = []

    def worker(i):
        try:
            VersionService(project_id="conc1").add_version(
                content=f"c{i}", source="initial"
            )
        except BaseException as exc:  # pragma: no cover - only trips on a real bug
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    versions = VersionService(project_id="conc1").get_all_versions()
    assert [v.version for v in versions] == list(range(1, n + 1))  # no gaps / dups
    assert sorted(v.content for v in versions) == sorted(f"c{i}" for i in range(n))


def test_reentrant_lock_does_not_deadlock_nested_calls(isolated_output_dir):
    """A public mutator holds the per-path lock while calling _load_all /
    _save_all (which re-acquire it); cross-stream calls take different locks.
    Neither must deadlock."""
    brd = VersionService(project_id="nest1")
    hld = VersionService(project_id="nest1", subdir="hld")

    brd.add_version(content="b1", source="initial")   # RLock re-entry within one call
    hld.add_version(content="h1", source="initial")   # different path, different lock
    brd.mark_final(1)
    brd.unlock_final()
    assert [v.content for v in brd.get_all_versions()] == ["b1"]
    assert [v.content for v in hld.get_all_versions()] == ["h1"]


# --- 8. timestamp remains valid ISO UTC -------------------------------

def test_created_at_is_timezone_aware_utc_iso(isolated_output_dir):
    v = VersionService(project_id="ts1").add_version(content="x", source="initial")

    parsed = datetime.fromisoformat(v.created_at)  # valid ISO 8601
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)  # UTC

    # survives a reload byte-for-byte
    assert VersionService(project_id="ts1").get_version(1).created_at == v.created_at


# --- 9. failure safety: a corrupt primary is never copied over a good backup --

def test_save_does_not_overwrite_good_backup_with_corrupt_primary(isolated_output_dir):
    svc = VersionService(project_id="safe1")
    _seed(svc, "one", "two")  # primary == [one, two]; bak == [one]
    d = isolated_output_dir / "safe1"
    bak = d / "versions.json.bak"
    good_bak_text = bak.read_text("utf-8")

    # primary gets corrupted (e.g. an earlier crash); then a normal write happens
    (d / "versions.json").write_text("!!! corrupt !!!", encoding="utf-8")
    svc.add_version(content="three", source="initial")

    # the corrupt primary must NOT have been promoted into the backup...
    assert bak.read_text("utf-8") == good_bak_text
    assert [r["content"] for r in json.loads(bak.read_text("utf-8"))] == ["one"]
    # ...and the new primary is a clean, complete list rebuilt from the recovered state
    assert [v.content for v in svc.get_all_versions()] == ["one", "three"]
