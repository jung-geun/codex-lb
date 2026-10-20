from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.quota_estimates.batch import SourceIdentity, source_identity_groups, source_scope_models
from app.modules.quota_estimates.estimator import Observation, RequestUsage, estimate_quota

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
RESET = NOW + timedelta(hours=2)
LEFT = NOW - timedelta(minutes=20)


def observation(at=NOW, ratio=0.3, reset=RESET):
    return Observation(at, ratio, reset)


def call(*, start=None, end=None, model="upstream-model", input_tokens=800, output_tokens=200, **kwargs):
    return RequestUsage(
        started_at=start if start is not None else LEFT + timedelta(minutes=1),
        completed_at=end if end is not None else LEFT + timedelta(minutes=2),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        **kwargs,
    )


def infer(*, observations=None, requests=None, **kwargs):
    return estimate_quota(
        window_key="five_hour",
        model=kwargs.pop("model", None),
        now=NOW,
        observations=observations if observations is not None else [observation(LEFT, 0.1), observation()],
        requests=requests if requests is not None else [call()],
        **kwargs,
    ).model_dump(mode="json", by_alias=True)


def assert_no_cap(result, reason):
    assert result["status"] in {"unknown", "insufficient"}
    assert result["reason"] == reason
    assert result["estimatedTotalTokens"] is None
    assert result["remainingTokens"] is None
    assert result["totalTokensLow"] is None and result["totalTokensHigh"] is None
    assert result["estimateKind"] is None


def test_consumer_contract_valid_delta_and_remaining():
    result = infer()
    assert result["status"] == "estimated" and result["reason"] == "valid"
    assert result["estimatedTotalTokens"] == pytest.approx(5000)
    assert result["remainingTokens"] == pytest.approx(3500)
    assert result["observedTokens"] == 1000
    assert result["sampleCount"] == 1 and result["observationCount"] == 2
    assert result["requestCount"] == 1
    assert result["usedRatioDelta"] == pytest.approx(0.2)
    assert result["evidenceStart"] == "2026-10-19T11:40:00Z"
    assert result["evidenceEnd"] == "2026-10-19T12:00:00Z"
    assert result["currentUsedRatio"] == 0.3
    assert result["basis"] == "observed_gateway_input_plus_output_tokens"
    assert result["estimateKind"] == "empirical_estimate"
    assert any("not an official" in caveat for caveat in result["caveats"])
    assert any("Outside-gateway" in caveat for caveat in result["caveats"])


@pytest.mark.parametrize(
    "observations,reason",
    [
        ([], "no_observations"),
        ([observation(LEFT, 0.3), observation()], "insufficient_delta"),
        ([observation(LEFT, 0.29), observation()], "insufficient_delta"),
        ([observation(LEFT, 0.1, RESET - timedelta(days=7)), observation()], "insufficient_delta"),
        ([observation(LEFT, 0.1, None), observation(reset=None)], "missing_reset"),
        ([observation(LEFT, 0.1, NOW), observation(reset=NOW)], "reset_elapsed"),
        ([observation(LEFT, 0.4), observation()], "inconsistent_observations"),
        ([observation(LEFT, 0.1), observation(NOW - timedelta(minutes=6))], "current_stale"),
        ([observation(LEFT, 0.1), observation(NOW + timedelta(seconds=1))], "current_future"),
        ([observation(LEFT, 0.1), observation(ratio=float("nan"))], "inconsistent_observations"),
        ([observation(NOW - timedelta(days=31), 0.1), observation()], "insufficient_delta"),
    ],
)
def test_no_invented_capacity_for_invalid_or_insufficient_evidence(observations, reason):
    assert_no_cap(infer(observations=observations), reason)


def test_explicit_future_current_is_not_replaced_by_older_history():
    assert_no_cap(infer(current=observation(NOW + timedelta(seconds=1))), "current_future")


@pytest.mark.parametrize(
    "usage_request",
    [
        call(input_tokens=None),
        call(output_tokens=None),
        call(output_tokens=None, reasoning_tokens=200),
        call(input_tokens=-1),
        call(output_tokens=True),
        call(success=False),
        RequestUsage(None, LEFT + timedelta(minutes=2), "upstream-model", 800, 200),
    ],
)
def test_missing_actual_usage_or_duration_is_not_zero_or_reasoning_fallback(usage_request):
    assert_no_cap(infer(requests=[usage_request]), "incomplete_usage")


@pytest.mark.parametrize(
    "usage_request",
    [
        call(start=LEFT - timedelta(seconds=1)),
        call(start=NOW - timedelta(seconds=1), end=NOW + timedelta(seconds=1)),
        call(start=LEFT),
        call(end=NOW),
    ],
)
def test_crossing_or_timestamp_equal_calls_are_not_full_bracketing_evidence(usage_request):
    # The pure estimator sees a call ending after the sample if now is later
    # than that sample; the adapter fetches all completions through read time.
    current_at = NOW - timedelta(minutes=1)
    if usage_request.completed_at > NOW:
        usage_request = call(start=current_at - timedelta(seconds=1), end=current_at + timedelta(seconds=1))
        observations = [observation(LEFT, 0.1), observation(current_at)]
    else:
        observations = None
    assert_no_cap(infer(requests=[usage_request], observations=observations), "boundary_overlap")


def test_bounded_boundary_usage_yields_point_and_conservative_range():
    current_at = NOW - timedelta(seconds=10)
    result = infer(
        observations=[observation(LEFT, 0.1), observation(current_at)],
        requests=[
            call(),
            call(start=LEFT - timedelta(seconds=1), end=LEFT + timedelta(seconds=1), input_tokens=10, output_tokens=0),
            call(
                start=current_at - timedelta(seconds=1),
                end=current_at + timedelta(seconds=1),
                input_tokens=10,
                output_tokens=0,
            ),
        ],
    )
    assert result["status"] == "estimated"
    assert result["estimatedTotalTokens"] == pytest.approx(5050)
    assert result["remainingTokens"] == pytest.approx(3535)
    assert result["totalTokensLow"] == pytest.approx(5000)
    assert result["totalTokensHigh"] == pytest.approx(5100)
    assert result["observedTokens"] == 1010 and result["requestCount"] == 2
    assert result["boundaryUncertaintyTokens"] == 20


def test_boundary_error_accumulates_a_longer_interval_before_reusing_any_evidence():
    middle = LEFT + timedelta(minutes=10)
    current_at = NOW - timedelta(seconds=10)
    result = infer(
        observations=[observation(LEFT, 0.1), observation(middle, 0.15), observation(current_at, 0.2)],
        requests=[
            call(start=LEFT - timedelta(seconds=1), end=LEFT + timedelta(seconds=1), input_tokens=10, output_tokens=0),
            call(input_tokens=100, output_tokens=0),
            call(
                start=middle - timedelta(seconds=1),
                end=middle + timedelta(seconds=1),
                input_tokens=100,
                output_tokens=0,
            ),
            call(
                start=middle + timedelta(minutes=1),
                end=middle + timedelta(minutes=2),
                input_tokens=1000,
                output_tokens=0,
            ),
            call(
                start=current_at - timedelta(seconds=1),
                end=current_at + timedelta(seconds=1),
                input_tokens=10,
                output_tokens=0,
            ),
        ],
    )
    assert result["status"] == "estimated"
    assert result["sampleCount"] == 1 and result["requestCount"] == 4
    assert result["observedTokens"] == 1210
    assert result["estimatedTotalTokens"] == pytest.approx(12100)
    assert result["totalTokensLow"] == pytest.approx(12000)
    assert result["totalTokensHigh"] == pytest.approx(12200)
    assert result["usedRatioDelta"] == pytest.approx(0.1)


def test_missing_usage_yields_only_recorded_minimum_with_unknown_upper():
    result = infer(requests=[call(), call(start=LEFT - timedelta(seconds=1), input_tokens=None)])
    assert result["status"] == "estimated" and result["reason"] == "incomplete_usage"
    assert result["estimateKind"] == "recorded_lower_bound"
    assert result["estimatedTotalTokens"] == pytest.approx(5000)
    assert result["remainingTokens"] == pytest.approx(3500)
    assert result["totalTokensLow"] == pytest.approx(5000) and result["totalTokensHigh"] is None
    assert result["observedTokens"] == 1000


def test_complete_interval_is_preferred_over_large_incomplete_recorded_minimum():
    middle = LEFT + timedelta(minutes=10)
    result = infer(
        observations=[observation(LEFT, 0.1), observation(middle, 0.2), observation()],
        requests=[
            call(input_tokens=100000, output_tokens=0),
            call(input_tokens=None),
            call(start=middle + timedelta(minutes=1), end=middle + timedelta(minutes=2)),
        ],
    )
    assert result["estimateKind"] == "empirical_estimate" and result["reason"] == "valid"
    assert result["estimatedTotalTokens"] == pytest.approx(10000)
    assert result["observedTokens"] == 1000 and result["sampleCount"] == 1
    assert result["totalTokensHigh"] == pytest.approx(10000)


def test_completed_before_left_is_not_counted_and_after_right_is_not_included():
    current_at = NOW - timedelta(minutes=1)
    result = infer(
        observations=[observation(LEFT, 0.1), observation(current_at)],
        requests=[
            call(),
            call(start=LEFT - timedelta(minutes=2), end=LEFT - timedelta(minutes=1)),
            call(start=NOW - timedelta(seconds=20), end=NOW - timedelta(seconds=10)),
        ],
    )
    assert result["observedTokens"] == 1000 and result["requestCount"] == 1


def test_cache_and_reasoning_subtotals_are_not_added_to_normalized_actual_tokens():
    result = infer(requests=[call(cached_input_tokens=400, cache_creation_input_tokens=300, reasoning_tokens=100)])
    assert result["observedTokens"] == 1000
    assert result["estimatedTotalTokens"] == pytest.approx(5000)


def test_global_sums_all_models_but_scoped_only_proven_exact_aliases():
    requests = [call(), call(model="unrelated", input_tokens=1600, output_tokens=400)]
    assert infer(requests=requests)["estimatedTotalTokens"] == pytest.approx(15000)
    scoped = infer(requests=requests, model="public-alias", matching_models=frozenset({"upstream-model"}))
    assert scoped["estimatedTotalTokens"] == pytest.approx(5000)
    assert scoped["requestCount"] == 1 and scoped["model"] == "public-alias"
    assert_no_cap(infer(model="family-name", matching_models=frozenset()), "unsupported_scope")


def test_unknown_model_keeps_scoped_total_unknown_but_preserves_proven_minimum():
    result = infer(
        model="upstream-model", matching_models=frozenset({"upstream-model"}), requests=[call(), call(model=None)]
    )
    assert result["estimateKind"] == "recorded_lower_bound" and result["reason"] == "incomplete_usage"
    assert result["observedTokens"] == 1000
    assert result["estimatedTotalTokens"] == pytest.approx(5000)
    assert result["totalTokensHigh"] is None


def test_matching_is_exact_not_substring_or_family_guess():
    assert_no_cap(
        infer(model="sonnet", matching_models=frozenset({"sonnet"}), requests=[call(model="claude-sonnet-unproven")]),
        "no_matched_usage",
    )


def test_small_rounded_samples_accumulate_once_before_capacity_is_inferred():
    samples = [observation(LEFT + timedelta(minutes=index), 0.1 + index * 0.01) for index in range(7)]
    result = infer(observations=samples + [observation(NOW, 0.16)])
    assert result["sampleCount"] == 1
    assert result["observedTokens"] == 1000
    assert result["usedRatioDelta"] == pytest.approx(0.05)
    assert result["estimatedTotalTokens"] == pytest.approx(20000)


def test_ratio_weighted_median_rejects_outlier_without_reusing_overlapping_evidence():
    samples = [
        observation(NOW - timedelta(minutes=30 - 10 * index), ratio) for index, ratio in enumerate([0.1, 0.3, 0.5, 0.6])
    ]
    requests = [
        call(
            start=samples[index].observed_at + timedelta(minutes=1),
            end=samples[index].observed_at + timedelta(minutes=2),
            input_tokens=tokens,
            output_tokens=0,
        )
        for index, tokens in enumerate([1000, 1000, 10000])
    ]
    result = infer(observations=samples, requests=requests)
    assert result["sampleCount"] == 3 and result["requestCount"] == 3
    assert result["observedTokens"] == 12000
    assert result["estimatedTotalTokens"] == pytest.approx(5000)
    assert result["totalTokensLow"] == pytest.approx(5000)
    assert result["totalTokensHigh"] == pytest.approx(100000)


def test_duplicate_observations_are_not_duplicate_evidence_and_conflicts_are_unknown():
    result = infer(observations=[observation(LEFT, 0.1), observation(LEFT, 0.1), observation(), observation()])
    assert result["observationCount"] == 2 and result["sampleCount"] == 1
    assert_no_cap(
        infer(observations=[observation(LEFT, 0.1), observation(LEFT, 0.2), observation()]), "inconsistent_observations"
    )


def test_zero_remaining_requires_actual_supported_capacity_evidence():
    result = infer(observations=[observation(LEFT, 0.8), observation(ratio=1)])
    assert result["status"] == "estimated" and result["remainingTokens"] == 0
    assert_no_cap(infer(observations=[observation(LEFT, 1), observation(ratio=1)]), "insufficient_delta")


def test_shared_principal_and_group_aliases_are_transitive_and_provider_scoped():
    groups, ambiguous = source_identity_groups(
        [
            SourceIdentity("a", "claude", "group-a", "seat", "org"),
            SourceIdentity("b", "claude", "group-b", "seat", "org"),
            SourceIdentity("c", "claude", "group-b"),
            SourceIdentity("d", "other", "group-b"),
        ]
    )
    assert groups["a"] == groups["b"] == groups["c"] == frozenset({"a", "b", "c"})
    assert groups["d"] == frozenset({"d"}) and not ambiguous


def test_conflicting_principals_and_unknown_subscription_aliases_do_not_mix():
    groups, ambiguous = source_identity_groups(
        [
            SourceIdentity("a", "claude", "shared", "seat-a", "org"),
            SourceIdentity("b", "claude", "shared", "seat-b", "org"),
            SourceIdentity("c", "other"),
            SourceIdentity("d", "other"),
        ]
    )
    assert ambiguous == frozenset({"a", "b", "c", "d"})
    assert groups["c"] != groups["d"]
    assert_no_cap(infer(unavailable_reason="identity_ambiguous"), "identity_ambiguous")


def test_public_alias_reused_for_different_upstream_models_does_not_join_scopes():
    assert source_scope_models("alias", [{"alias": "actual"}, {"alias": "actual"}]) == frozenset({"actual"})
    assert source_scope_models("alias", [{"alias": "actual"}, {"alias": "other"}]) == frozenset()
    assert source_scope_models("actual", [{"public": "actual"}]) == frozenset({"actual"})
