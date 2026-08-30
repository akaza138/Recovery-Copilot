from seed.case_catalog import generate_cases

DATASET_ONLY_KEYS = {
    "external_payment_id",
    "order_id",
    "amount",
    "currency",
    "failure_code",
    "failure_reason",
    "failure_description",
    "retry_count",
    "failed_at",
    "raw_payload",
    "customer",
}


def test_batch_has_at_least_fifty_records():
    cases = generate_cases()
    assert len(cases) >= 50


def test_batch_has_both_easy_and_hard_cases():
    cases = generate_cases()
    categories = {case["ground_truth"]["category"] for case in cases}
    assert categories == {"easy", "hard"}


def test_dataset_record_never_leaks_ground_truth():
    """The dataset the engine sees must not contain the answer: no
    expected_* fields, no category, no template_key."""
    cases = generate_cases()
    for case in cases:
        assert set(case["dataset_record"].keys()) == DATASET_ONLY_KEYS


def test_exactly_one_canonical_instance_per_demo_case():
    cases = generate_cases()
    canonical = [case["ground_truth"] for case in cases if case["ground_truth"]["canonical_demo_case"]]
    labels = [c["canonical_demo_case"] for c in canonical]
    assert sorted(labels) == ["case_a", "case_b", "case_c"]


def test_case_a_is_transient_failure_retry_success():
    cases = generate_cases()
    gt = next(c["ground_truth"] for c in cases if c["ground_truth"]["canonical_demo_case"] == "case_a")
    assert gt["expected_action"] == "retry"
    assert gt["expected_final_status"] == "confirmed_recovered"
    assert gt["expected_confidence_band"] == "high"


def test_case_b_skips_retry_for_non_retryable_failure():
    cases = generate_cases()
    gt = next(c["ground_truth"] for c in cases if c["ground_truth"]["canonical_demo_case"] == "case_b")
    assert gt["expected_action"] == "payment_link"
    assert gt["expected_final_status"] == "confirmed_recovered"


def test_case_c_escalates_high_value_uncertain_payment():
    cases = generate_cases()
    dataset_by_id = {c["dataset_record"]["external_payment_id"]: c["dataset_record"] for c in cases}
    gt = next(c["ground_truth"] for c in cases if c["ground_truth"]["canonical_demo_case"] == "case_c")

    assert gt["expected_action"] == "human_review"
    assert gt["expected_confidence_band"] == "medium"
    assert gt["representative_model_confidence"] is not None
    assert gt["representative_model_confidence"] < 0.95  # below any plausible auto-action threshold

    record = dataset_by_id[gt["external_payment_id"]]
    assert record["amount"] >= 50_00_000  # high-value: at least ₹50,000


def test_dnd_optout_and_contact_limit_customers_exist():
    cases = generate_cases()
    templates = {case["ground_truth"]["template_key"] for case in cases}
    assert "customer_opted_out" in templates
    assert "contact_limit_reached" in templates

    dnd_case = next(c for c in cases if c["ground_truth"]["template_key"] == "customer_opted_out")
    assert dnd_case["dataset_record"]["customer"]["dnd_opt_out"] is True

    limit_case = next(c for c in cases if c["ground_truth"]["template_key"] == "contact_limit_reached")
    customer = limit_case["dataset_record"]["customer"]
    assert customer["contact_count"] >= customer["max_contact_attempts"]


def test_retry_cap_case_already_at_max():
    cases = generate_cases()
    case = next(c for c in cases if c["ground_truth"]["template_key"] == "retry_cap_already_reached")
    assert case["dataset_record"]["retry_count"] == 3
    assert case["ground_truth"]["expected_stop_reason"] == "max_attempts_reached"
    assert case["ground_truth"]["expected_final_status"] == "unresolved"


def test_generation_is_deterministic_for_a_given_seed():
    first = generate_cases(seed=7)
    second = generate_cases(seed=7)
    assert [c["dataset_record"]["external_payment_id"] for c in first] == [
        c["dataset_record"]["external_payment_id"] for c in second
    ]
