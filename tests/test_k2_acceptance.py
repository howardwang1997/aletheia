"""K2 live-acceptance scoring (:mod:`aletheia.scheduler.k2_acceptance`).

The regression these lock down: a multi-round campaign that only ever recorded ``no_demonstration``
(priors seeded, predictions pre-registered, credences persisted, a go/no-go pivot — but ZERO
harness verdicts, ZERO belief updates, null calibration) used to be mis-scored as a FULL PASS,
because the spine invariant ``len(updates) == len(confirm_verdicts)`` is vacuously true at 0. The
fix adds a positive-evidence gate: FULL now requires >=1 confirm-split verdict that moved a
calibrated belief. The real 2026-06-09 run `99c2…` is exactly this pattern.
"""

from __future__ import annotations

import pytest

from aletheia.db import create_all
from aletheia.events.store import list_run_events
from aletheia.memory.service import list_credences
from aletheia.scheduler.k2_acceptance import score_k2


def _evt(etype: str, **payload):
    return {"type": etype, "payload": payload}


def _zero_verdict_multi_round_stream() -> list[dict]:
    """The 99c2 shape: 3 lineages, predictions pre-registered, 3 no_demonstration rounds, a pivot —
    but no demonstration ever computed, so no verdict / update / calibration."""
    qks = ["lineage-a", "lineage-b", "lineage-c"]
    events: list[dict] = []
    for qk in qks:
        events.append(_evt("belief_prior", question_key=qk, mean=0.58, weak_prior=True))
    for qk in qks:
        events.append(_evt("belief_prediction", question_key=qk, predicted_p_holds=0.58))
    events.append(_evt("campaign_plan", round=1, **{"continue": True}))
    events.append(_evt("campaign_reason", round=1, reason="no_demonstration", recoverable=False))
    events.append(_evt("campaign_plan", round=2, **{"continue": True}))
    events.append(_evt("campaign_reason", round=2, reason="no_demonstration", recoverable=True))
    events.append(_evt("campaign_reason", round=3, reason="no_demonstration", recoverable=True))
    events.append(_evt("campaign_finished", calibration=None, n_belief_updates=0))
    return events


_CREDS_3 = [{"question_key": q, "alpha": 1.0, "beta": 1.0, "n_updates": 0}
            for q in ("lineage-a", "lineage-b", "lineage-c")]


def test_zero_verdict_multi_round_is_partial_not_full():
    result = score_k2(_zero_verdict_multi_round_stream(), _CREDS_3)
    assert result.verdict == "partial"  # the regression: must NOT be "full"
    assert result.n_reasons == 3
    assert result.n_updates == 0
    assert result.n_confirm_verdicts == 0
    assert result.calibration is None
    # the spine checks are vacuously true (belief correctly never moved) — that is intended; the
    # verdict gate, not the spine check, is what withholds FULL.
    by_name = {c.name: c.ok for c in result.checks}
    assert by_name["credence moves ONLY on harness confirm-split verdicts (spine intact)"] is True


def test_full_pass_requires_a_verdict_that_moved_a_calibrated_belief():
    qk = "lineage-a"
    events = [
        _evt("belief_prior", question_key=qk, mean=0.55, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.55),
        _evt("demonstration", computed=True, exploration_applied=True, holds=True),
        _evt("belief_update", question_key=qk, realized=1.0, surprise=0.45, mean=0.7, n_updates=1),
        _evt("campaign_plan", round=1, **{"continue": True}),
        _evt("campaign_reason", round=1, reason="generalized", recoverable=True),
        _evt("campaign_reason", round=2, reason="did_not_generalize", recoverable=True),
        _evt("campaign_finished", calibration=0.2, n_belief_updates=1),
    ]
    creds = [{"question_key": qk, "alpha": 2.0, "beta": 1.0, "n_updates": 1}]
    result = score_k2(events, creds)
    assert result.verdict == "full"
    assert result.n_updates == 1 and result.n_confirm_verdicts == 1 and result.calibration == 0.2


def test_single_round_with_a_verdict_is_partial_not_full():
    # an honest converge-in-one-round campaign: has a verdict, but does NOT exercise cross-round learning
    qk = "lineage-a"
    events = [
        _evt("belief_prior", question_key=qk, mean=0.55, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.55),
        _evt("demonstration", computed=True, exploration_applied=True, holds=False),
        _evt("belief_update", question_key=qk, realized=0.0, surprise=0.55, mean=0.4, n_updates=1),
        _evt("campaign_reason", round=1, reason="did_not_generalize", recoverable=True),
        _evt("campaign_finished", calibration=0.3, n_belief_updates=1),
    ]
    creds = [{"question_key": qk, "alpha": 1.0, "beta": 2.0, "n_updates": 1}]
    result = score_k2(events, creds)
    assert result.verdict == "partial"  # only 1 round -> the K2 cross-round thesis isn't exercised


def test_strict_full_requires_seal_fresh_batches_final_and_external_replication():
    qk = "lineage-a"
    events = [
        _evt("campaign_split_sealed", split_algo_version=2),
        _evt("external_validation_sealed", dataset_fingerprint="external"),
        _evt("belief_prior", question_key=qk, mean=0.55, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.55),
        _evt("hypothesis_attempt", attempt_id=1, phase="confirmation", status="registered",
             split_hash="batch-a", alpha=0.01),
        _evt("experiment", round=1, exp_id="exp-1"),
        _evt("demonstration", computed=True, exploration_applied=True, holds=True),
        _evt("belief_update", question_key=qk, realized=1.0, surprise=0.45, mean=0.7,
             n_updates=1),
        _evt("hypothesis_attempt", attempt_id=1, phase="confirmation", status="evaluated"),
        _evt("campaign_plan", round=1, **{"continue": True}),
        _evt("campaign_reason", round=1, reason="generalized", recoverable=True),
        _evt("hypothesis_attempt", attempt_id=2, phase="confirmation", status="registered",
             split_hash="batch-b", alpha=0.01),
        _evt("campaign_reason", round=2, reason="did_not_generalize", recoverable=True),
        _evt("hypothesis_attempt", attempt_id=2, phase="confirmation", status="evaluated"),
        _evt("campaign_finished", calibration=0.2, n_belief_updates=1),
        _evt("final_holdout", evaluated=True, holds=True),
        # A negative replication is still a completed, honest K2 evaluation. Run-level result
        # support is rejected elsewhere; acceptance here checks that it was not skipped/tuned away.
        _evt("external_replication", evaluated=True, holds=False),
    ]
    creds = [{"question_key": qk, "alpha": 2.0, "beta": 1.0, "n_updates": 1}]
    strict = score_k2(
        events, creds, require_seal_v2=True, require_external=True
    )
    assert strict.verdict == "full"
    missing_external = score_k2(
        events[:-1], creds, require_seal_v2=True, require_external=True
    )
    assert missing_external.verdict == "partial"


def test_resume_references_do_not_retimestamp_original_seals():
    qk = "lineage-a"
    events = [
        _evt("campaign_split_sealed", split_algo_version=2, reused=False),
        _evt("external_validation_sealed", dataset_fingerprint="external", reused=False),
        _evt("belief_prior", question_key=qk, mean=0.5, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.5),
        # Historical versions emitted these event types again on resume. They only
        # referenced the immutable ledger and therefore must not become a later seal.
        _evt("campaign_split_sealed", split_algo_version=2, reused=True),
        _evt("external_validation_sealed", dataset_fingerprint="external", reused=True),
        _evt("hypothesis_attempt", attempt_id=1, phase="confirmation", status="registered",
             split_hash="batch-a", alpha=0.01),
        _evt("experiment", round=1, exp_id="exp-1"),
        _evt("demonstration", computed=True, exploration_applied=True, holds=True),
        _evt("belief_update", question_key=qk, realized=1.0, surprise=0.5, n_updates=1),
        _evt("hypothesis_attempt", attempt_id=1, phase="confirmation", status="evaluated"),
        _evt("campaign_plan", round=1, **{"continue": True}),
        _evt("campaign_reason", round=1, reason="generalized", recoverable=True),
        _evt("hypothesis_attempt", attempt_id=2, phase="confirmation", status="registered",
             split_hash="batch-b", alpha=0.01),
        _evt("campaign_reason", round=2, reason="did_not_generalize", recoverable=True),
        _evt("hypothesis_attempt", attempt_id=2, phase="confirmation", status="evaluated"),
        _evt("campaign_finished", calibration=0.2, n_belief_updates=1),
        _evt("final_holdout", evaluated=True, holds=True),
        _evt("external_replication", evaluated=True, holds=False),
    ]
    result = score_k2(
        events,
        [{"question_key": qk, "alpha": 2.0, "beta": 1.0, "n_updates": 1}],
        require_seal_v2=True,
        require_external=True,
    )
    assert result.verdict == "full"


def test_belief_moved_without_a_verdict_is_a_fail():
    # spine VIOLATION: a belief_update with no matching harness confirm-split verdict
    qk = "lineage-a"
    events = [
        _evt("belief_prior", question_key=qk, mean=0.55, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.55),
        _evt("belief_update", question_key=qk, realized=1.0, surprise=0.45, mean=0.8, n_updates=1),
        _evt("campaign_reason", round=1, reason="generalized", recoverable=True),
        _evt("campaign_reason", round=2, reason="generalized", recoverable=True),
        _evt("campaign_plan", round=1, **{"continue": True}),
        _evt("campaign_finished", calibration=0.1, n_belief_updates=1),
    ]
    creds = [{"question_key": qk, "alpha": 2.0, "beta": 1.0, "n_updates": 1}]
    result = score_k2(events, creds)
    assert result.verdict == "fail"  # updates(1) != confirm_verdicts(0): the spine check fails


def test_duplicate_demonstration_events_count_as_one_final_round_verdict():
    # Live 160232 shape: the same experiment emits the initial compute demonstration, the
    # reproduction recompute, and then a post-audit re-publication with holds=False. K2 belief moves
    # once on the final experiment outcome, so this is 1 verdict vs 1 update, not 3 verdicts.
    qk = "lineage-a"
    exp_id = "exp-1"
    events = [
        _evt("belief_prior", question_key=qk, mean=0.59, weak_prior=True),
        _evt("belief_prediction", question_key=qk, predicted_p_holds=0.59),
        _evt("experiment", round=1, exp_id=exp_id),
        _evt("demonstration", computed=True, exploration_applied=True, holds=True),
        _evt("demonstration", computed=True, exploration_applied=True, holds=True),
        _evt("demonstration", computed=True, exploration_applied=True, holds=False, audit_refuted=True),
        _evt("belief_update", question_key=qk, exp_id=exp_id, realized=0.0, surprise=0.59,
             mean=0.39, n_updates=1),
        _evt("campaign_reason", round=1, reason="audit_refuted", recoverable=True),
        _evt("campaign_finished", calibration=None, n_belief_updates=1),
    ]
    creds = [{"question_key": qk, "alpha": 1.18, "beta": 1.82, "n_updates": 1}]
    result = score_k2(events, creds)
    assert result.n_confirm_verdicts == 1
    assert result.n_updates == 1
    by_name = {c.name: c.ok for c in result.checks}
    assert by_name["credence moves ONLY on harness confirm-split verdicts (spine intact)"] is True
    assert result.verdict == "partial"  # still not FULL: only one round and no campaign calibration


def test_real_99c2_reference_run_scores_partial_if_present():
    """If the real 2026-06-09 no_demonstration run is in this DB, it must score PARTIAL (it would
    have been FULL under the old gate). Skips on a fresh DB so CI elsewhere is not data-dependent."""
    create_all()
    run_id = "99c2bcbfe54f416290f3d46ea4100c27"
    events = list_run_events(run_id)
    if not events:
        pytest.skip("the 99c2 reference run is not in this DB")
    result = score_k2(events, list_credences(run_id))
    assert result.verdict == "partial"
    assert result.n_updates == 0 and result.n_confirm_verdicts == 0 and result.calibration is None
