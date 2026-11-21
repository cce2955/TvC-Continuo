from types import SimpleNamespace
import time
import json

from tvcgui.features.analysis.store import AnalysisStore, compare_attempts, load_archived_matches, matchup_report
import tvcgui.features.analysis.store as analysis_module


def edge(seq, kind, slot="P1-C1", **changes):
    fields = dict(
        sequence=seq, timestamp_ns=1_000_000_000 + seq * 16_666_667,
        kind=kind, slot=slot, char_id=1, action_id=0,
        action_frame=0, held=0, pressed=0, damage=0,
        current_hp=10000, combo_count=0, hitstun_remaining=0,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


def snapshots(meter=10000, baroque=20):
    return {
        "P1-C1": {"name": "Ken", "meter": meter, "baroque_red_pct_max": baroque,
                  "cur": 10000, "x": 1, "attA": 0x100, "mv_label": "5A"},
        "P2-C1": {"name": "Ryu", "meter": 0, "cur": 10000, "x": 4},
    }


def test_opt_in_combo_history_preserves_lead_in_and_labels():
    store = AnalysisStore()
    store.ingest((edge(1, "DAMAGE", damage=300),), snapshots(), now=1.0)
    assert not store.snapshot()["activity"]
    store.start(1)
    store.ingest((
        edge(2, "INPUT", pressed=1),
        edge(3, "ACTION", action_id=0x100),
        edge(4, "COMBO_BEGIN", slot="P2-C1", combo_count=1),
        edge(5, "DAMAGE", slot="P2-C1", damage=300),
        edge(6, "COMBO_END", slot="P2-C1"),
    ), snapshots(), now=1.0, label_for=lambda char_id, action_id: "5A" if action_id == 0x100 else None)
    result = store.snapshot()
    assert result["attempts"][0]["starter"] == "5A"
    assert result["attempts"][0]["damage"] == 300
    assert [item["kind"] for item in result["attempts"][0]["timeline"][:2]] == ["INPUT", "ACTION"]
    assert result["move_attempts"] == {"Ken · 5A": 1}
    assert result["damage_by_source"] == {"Unknown source": 300}
    assert store.set_note(result["attempts"][0]["id"], "Try C earlier")
    assert store.snapshot()["attempts"][0]["note"] == "Try C earlier"


def test_resource_accounting_and_saved_history(tmp_path):
    store = AnalysisStore()
    store.start()
    store.ingest((), snapshots(meter=10000), now=1.0)
    store.ingest((edge(1, "MEGACRASH_BEGIN"),), snapshots(meter=22000), now=1.05)
    store.ingest((), snapshots(meter=12000), now=1.10)
    result = store.snapshot()
    assert result["meter_gain"]["P1"] == 12000
    assert result["meter_spent"]["P1"] == 10000
    assert result["megacrashes"]["P1"] == 1
    assert result["meter_seconds"]["P1"][2] > 0
    path = tmp_path / "history.json"
    store.stop()
    store.save(path)
    loaded = AnalysisStore()
    loaded.load(path)
    assert loaded.snapshot()["megacrashes"]["P1"] == 1


def test_compare_attempts_reports_delay_after_confirmed_hit():
    hit = {"kind": "DAMAGE", "time_ns": 1_000_000_000}
    early = {"kind": "ACTION", "time_ns": 1_000_000_000 + 13 * 16_666_667, "action_id": 0x103}
    late = {"kind": "ACTION", "time_ns": 1_000_000_000 + 16 * 16_666_667, "action_id": 0x103}
    result = compare_attempts({"timeline": [hit, early]}, {"timeline": [hit, late]})
    assert result["kind"] == "timing_difference"
    assert result["left"]["after_hit_frames"] == 13.0
    assert result["right"]["after_hit_frames"] == 16.0


def test_first_hit_preceding_combo_begin_is_included():
    store = AnalysisStore()
    store.start()
    store.ingest((
        edge(1, "ACTION", action_id=0x100),
        edge(2, "DAMAGE", slot="P2-C1", damage=700),
        edge(3, "COMBO_BEGIN", slot="P2-C1", combo_count=1),
        edge(4, "COMBO_END", slot="P2-C1"),
    ), snapshots(), now=1.0)
    attempt = store.snapshot()["attempts"][0]
    assert attempt["damage"] == 700
    assert len(attempt["damage_events"]) == 1


def test_manual_round_review_uses_deltas():
    store = AnalysisStore()
    store.start()
    store.ingest((edge(1, "DAMAGE", slot="P2-C1", damage=400),), snapshots(), now=1.0)
    store.mark_round_end()
    store.ingest((edge(2, "DAMAGE", slot="P2-C1", damage=200),), snapshots(), now=1.1)
    store.mark_round_end()
    rounds = store.snapshot()["rounds"]
    assert rounds[0]["damage_by_victim"] == {"Ryu": 200}
    assert rounds[1]["damage_by_victim"] == {"Ryu": 400}


def test_assist_report_counts_only_offscreen_partner_attack_bursts():
    store = AnalysisStore()
    store.start()
    partner = "P1-C2"
    store.ingest((
        edge(1, "ACTION", slot=partner, action_id=0x100, point_active=False),
        edge(2, "ACTION", slot=partner, action_id=0x101, point_active=False),
        edge(3, "ACTION", slot=partner, action_id=0x20, point_active=False),
        edge(4, "ACTION", slot="P1-C1", action_id=0x100, point_active=True),
    ), {**snapshots(), partner: {"name": "Jun", "meter": 10000, "cur": 10000}}, now=1.0)
    report = store.snapshot()
    assert report["assist_entries"] == {"Jun": 1}
    assert "Jun · 0x0020" not in report["move_attempts"]


def test_defense_source_requires_unique_clean_contact():
    store = AnalysisStore()
    store.start()
    stamp = time.monotonic_ns()
    damage = edge(1, "DAMAGE", slot="P2-C1", damage=500, timestamp_ns=stamp)
    store.ingest((damage,), snapshots(), now=time.monotonic())
    result = SimpleNamespace(sequence=1, kind="hit", clean=True, contact_count=1,
                             action_name="j.C", action_id=0x10B,
                             attacker_name="Ken", attacker_slot="P1-C1",
                             defender_slot="P2-C1", damage=500)
    store.ingest_timing([result])
    report = store.snapshot()
    assert report["damage_by_source"] == {"Likely: Ken · j.C": 500}
    assert report["defense_events"][0]["source"] == "Likely: Ken · j.C"


def test_match_report_counts_only_verified_result_and_preserves_start_role():
    store = AnalysisStore()
    store.start()
    store.set_match_tracking(True)
    live = {
        "P1-C1": {"base": 0x90001000, "cur": 10000, "name": "Ryu"},
        "P2-C1": {"base": 0x90002000, "cur": 10000, "name": "Karas"},
        "P2-C2": {"base": 0x90003000, "cur": 10000, "name": "Batsu"},
    }
    point = {"P1-C1": True, "P2-C1": True, "P2-C2": False}
    store.observe_match(4, live, point)
    store.observe_match(4, live, point)
    assert not store.snapshot()["match_records"]
    lost = {**live, "P1-C1": {**live["P1-C1"], "cur": 0}}
    store.observe_match(4, lost, point)
    store.observe_match(9, lost, point)
    report = matchup_report(store.snapshot()["match_records"], "P1")
    assert report["teams"][0]["losses"] == 1
    assert report["teams"][0]["wins"] == 0
    roles = {(row["opponent_character"], row["start_role"]): row for row in report["opponent_roles"]}
    assert roles[("Karas", "starting point")]["losses"] == 1
    assert roles[("Batsu", "starting partner")]["losses"] == 1


def test_archive_match_history_deduplicates_records(tmp_path):
    match = {"id": "match-1", "winner": "P1", "ended_at": 5,
             "rosters": {"P1": ["Ryu"], "P2": ["Karas"]}}
    payload = {"format": "continuo-analysis", "version": 1,
               "data": {"match_records": [match]}}
    for index in (1, 2):
        (tmp_path / f"analysis-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
    matches = load_archived_matches(tmp_path)
    assert len(matches) == 1
    assert matchup_report(matches, "P1")["teams"][0]["wins"] == 1
    assert load_archived_matches(tmp_path, reset_before=6) == []


def test_matchup_move_counts_are_per_match_not_per_hit():
    record = {"winner": "P2", "rosters": {"P1": ["Ryu"], "P2": ["Karas"]},
              "damage_taken_by_move": {"P1": {"Likely: Karas · 5A": 600}},
              "hits_taken_by_move": {"P1": {"Likely: Karas · 5A": 3}}}
    row = matchup_report([record], "P1")["moves"][0]
    assert (row["hits_taken"], row["matches_hit"], row["losses_when_hit"]) == (3, 1, 1)


def test_point_exposure_outcomes_keep_seen_and_unseen_separate():
    base = {"rosters": {"P1": ["Ryu"], "P2": ["Karas"]}}
    records = [
        {**base, "winner": "P1", "point_seconds": {"P2": {"Karas": 21.0}}},
        {**base, "winner": "P2", "point_seconds": {"P2": {}}},
    ]
    row = matchup_report(records, "P1")["point_exposure"][0]
    assert row["wins_seen_point"] == 1
    assert row["losses_no_point_observed"] == 1


def test_unresolved_transition_does_not_become_a_loss():
    store = AnalysisStore()
    store.start()
    store.set_match_tracking(True)
    live = {
        "P1-C1": {"base": 0x90001000, "cur": 10000, "name": "Ryu"},
        "P2-C1": {"base": 0x90002000, "cur": 10000, "name": "Karas"},
    }
    point = {"P1-C1": True, "P2-C1": True}
    store.observe_match(4, live, point)
    store.observe_match(4, live, point)
    for _ in range(4):
        store.observe_match(9, {}, {})
    store.observe_match(4, live, point)
    store.observe_match(4, live, point)
    records = store.snapshot()["match_records"]
    assert len(records) == 1
    assert records[0]["winner"] == ""
    assert not matchup_report(records, "P1")["teams"]


def test_stopping_combo_capture_keeps_background_match_tracking():
    store = AnalysisStore()
    store.start()
    store.set_match_tracking(True)
    live = {"P1-C1": {"base": 0x90001000, "cur": 10000, "name": "Ryu"},
            "P2-C1": {"base": 0x90002000, "cur": 10000, "name": "Karas"}}
    store.observe_match(4, live)
    store.observe_match(4, live)
    store.stop()
    assert not store.snapshot()["match_records"]
    store.shutdown()
    match = store.snapshot()["match_records"][0]
    assert match["winner"] == ""
    assert match["proof"] == "app_closed_before_result"


def test_match_background_path_records_result_without_combo_capture():
    store = AnalysisStore()
    assert store.match_tracking is True
    assert store.enabled is False
    live = {
        "P1-C1": {"base": 0x90001000, "cur": 10000, "name": "Ryu"},
        "P2-C1": {"base": 0x90002000, "cur": 10000, "name": "Karas"},
    }
    point = {"P1-C1": True, "P2-C1": True}
    store.observe_match(4, live, point)
    store.observe_match(4, live, point)
    store.ingest_match_background(
        (edge(1, "DAMAGE", slot="P1-C1", damage=500),), live, point, now=1.0,
    )
    store.observe_match(4, {**live, "P1-C1": {**live["P1-C1"], "cur": 0}}, point)
    result = store.snapshot()
    assert result["match_records"][0]["winner"] == "P2"
    assert result["match_records"][0]["damage_taken_by_point"]["P1"] == {"Karas": 500}
    assert not result["attempts"]


def test_stop_tracking_and_reset_persist_without_erasing_archives(tmp_path, monkeypatch):
    prefs = tmp_path / "match_stats.json"
    monkeypatch.setattr(analysis_module, "match_stats_path", lambda: prefs)
    store = AnalysisStore()
    store.enable_persistence()
    store.set_match_tracking(False)
    assert json.loads(prefs.read_text(encoding="utf-8"))["tracking"] is False
    before = store.reset_before
    store.reset_match_stats()
    assert store.reset_before > before
    restored = AnalysisStore()
    restored.enable_persistence()
    assert restored.match_tracking is False
    assert restored.reset_before == store.reset_before


def test_combo_recording_does_not_clear_background_match_results():
    store = AnalysisStore()
    store.match_records.appendleft({"id": "prior", "winner": "P1", "ended_at": 1})
    store.start(cursor=20)
    assert store.snapshot()["match_records"][0]["id"] == "prior"
    store.stop()
    assert store.match_tracking is True


def test_opt_out_ignores_background_events_and_resumes_at_new_cursor():
    store = AnalysisStore()
    store.set_match_tracking(False, cursor=20)
    store.ingest_match_background((edge(21, "DAMAGE", damage=500),), snapshots())
    assert store.cursor == 20
    assert not store.snapshot()["match_records"]
    store.set_match_tracking(True, cursor=30)
    assert store.cursor == 30
