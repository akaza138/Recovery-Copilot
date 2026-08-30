"""The synthetic failed-payment case catalog: template definitions for the
easy and deliberately-hard scenarios the batch must cover, plus the
generator that expands them into concrete records.

Pure functions only — no DB, no FastAPI, no pydantic — so this can run
(and be inspected) without the full app dependency stack installed.

Each template carries the *expected* outcome for the ground-truth file. That
expectation is never written into the dataset record itself: the dataset is
what the engine is allowed to see, and the engine has to arrive at the same
answer blind.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from faker import Faker

from seed.payloads import AMBIGUOUS_PROFILES, KNOWN_PROFILES, ErrorProfile, build_failed_payment_payload, rid

NORMAL_AMOUNT_RANGE = (5_000, 15_00_000)  # paise: ₹50 – ₹15,000
HIGH_VALUE_AMOUNT_RANGE = (50_00_000, 2_00_00_000)  # paise: ₹50,000 – ₹2,00,000


@dataclass
class CustomerSpec:
    dnd_opt_out: bool = False
    max_contact_attempts: int = 3
    contact_count: int = 0
    prior_recovery_attempts: int = 0
    prior_recovery_successes: int = 0


@dataclass
class CaseTemplate:
    template_key: str
    category: str  # "easy" | "hard"
    profile: ErrorProfile
    amount_range: tuple[int, int] = NORMAL_AMOUNT_RANGE
    customer: CustomerSpec = field(default_factory=CustomerSpec)
    retry_count_so_far: int = 0
    expected_root_cause: str = ""  # defaults to profile.key if unset
    expected_confidence_band: str = "high"  # "high" | "medium" | "low"
    representative_confidence: float | None = None  # only meaningful when Claude-diagnosed
    expected_action: str = "retry"  # "retry" | "payment_link" | "human_review" | "stand_down"
    expected_final_status: str = "confirmed_recovered"
    stop_reason: str | None = None
    notes: str = ""


EASY_TEMPLATES = [
    CaseTemplate(
        template_key="issuer_timeout_retry_success",
        category="easy",
        profile=KNOWN_PROFILES["issuer_timeout"],
        expected_confidence_band="high",
        expected_action="retry",
        expected_final_status="confirmed_recovered",
        notes="Transient issuer timeout; retry should succeed in Razorpay test mode.",
    ),
    CaseTemplate(
        template_key="expired_card_payment_link_success",
        category="easy",
        profile=KNOWN_PROFILES["expired_card"],
        expected_confidence_band="high",
        expected_action="payment_link",
        expected_final_status="confirmed_recovered",
        notes="Non-retryable; skip straight to a payment link instead of a pointless retry.",
    ),
    CaseTemplate(
        template_key="invalid_cvv_payment_link_success",
        category="easy",
        profile=KNOWN_PROFILES["invalid_cvv"],
        expected_confidence_band="high",
        expected_action="payment_link",
        expected_final_status="confirmed_recovered",
        notes="Non-retryable card-detail error; payment link lets the customer re-enter correct details.",
    ),
    CaseTemplate(
        template_key="incorrect_otp_retry_success",
        category="easy",
        profile=KNOWN_PROFILES["incorrect_otp"],
        expected_confidence_band="high",
        expected_action="retry",
        expected_final_status="confirmed_recovered",
        notes="Customer authentication slip; immediately retryable.",
    ),
    CaseTemplate(
        template_key="insufficient_funds_retry_success",
        category="easy",
        profile=KNOWN_PROFILES["insufficient_funds"],
        expected_confidence_band="high",
        expected_action="retry",
        expected_final_status="confirmed_recovered",
        notes="Retry after a cooldown window; balance may have been topped up.",
    ),
    CaseTemplate(
        template_key="hard_decline_payment_link_success",
        category="easy",
        profile=KNOWN_PROFILES["card_declined_do_not_honor"],
        expected_confidence_band="high",
        expected_action="payment_link",
        expected_final_status="confirmed_recovered",
        notes="Permanent hold code on this card; retrying the same card is pointless, payment link works.",
    ),
]

HARD_TEMPLATES = [
    CaseTemplate(
        template_key="conflicting_signals_human_review",
        category="hard",
        profile=AMBIGUOUS_PROFILES["conflicting_soft_decline"],
        expected_confidence_band="medium",
        representative_confidence=0.68,
        expected_action="human_review",
        expected_final_status="escalated",
        notes="Issuer text suggests 'retry', error class suggests permanent failure. Genuinely conflicting; Claude call "
        "warranted (Day 3). Day 2's rule-based-only fallback can't yet distinguish this from a low-confidence case — "
        "it deterministically scores unfamiliar reasons as LOW and stands down rather than guessing MEDIUM.",
    ),
    CaseTemplate(
        template_key="unfamiliar_reason_low_confidence_stand_down",
        category="hard",
        profile=AMBIGUOUS_PROFILES["unknown_error"],
        expected_confidence_band="low",
        representative_confidence=0.42,
        expected_action="stand_down",
        expected_final_status="escalated",
        notes="No rule matches; diagnosis itself is low-confidence. Must stand down, not guess.",
    ),
    CaseTemplate(
        template_key="abandoned_3ds_medium_confidence",
        category="hard",
        profile=AMBIGUOUS_PROFILES["3ds_authentication_abandoned"],
        expected_confidence_band="medium",
        representative_confidence=0.74,
        expected_action="human_review",
        expected_final_status="escalated",
        notes="Could be a customer drop-off (retryable) or a risk-avoidance signal; not clear-cut enough for auto-action. "
        "Same Day-2-vs-Day-3 caveat as conflicting_signals_human_review.",
    ),
    CaseTemplate(
        template_key="retry_cap_already_reached",
        category="hard",
        profile=KNOWN_PROFILES["issuer_timeout"],
        retry_count_so_far=3,
        expected_confidence_band="high",
        expected_action="stand_down",
        expected_final_status="unresolved",
        stop_reason="max_attempts_reached",
        notes="Diagnosis looks easy, but this payment already hit the max-3 retry cap. Stopping rule wins over diagnosis.",
    ),
    CaseTemplate(
        template_key="customer_opted_out",
        category="hard",
        profile=KNOWN_PROFILES["issuer_timeout"],
        customer=CustomerSpec(dnd_opt_out=True),
        expected_confidence_band="high",
        expected_action="stand_down",
        expected_final_status="escalated",
        stop_reason="dnd_opt_out",
        notes="Customer has opted out of contact. Compliance gate blocks any automated action regardless of diagnosis.",
    ),
    CaseTemplate(
        template_key="contact_limit_reached",
        category="hard",
        profile=KNOWN_PROFILES["expired_card"],
        customer=CustomerSpec(max_contact_attempts=3, contact_count=3),
        expected_confidence_band="high",
        expected_action="stand_down",
        expected_final_status="escalated",
        stop_reason="contact_limit_reached",
        notes="Would otherwise send a payment link, but the customer's contact cap for this period is already used up.",
    ),
    CaseTemplate(
        template_key="high_value_uncertain_escalation",
        category="hard",
        profile=AMBIGUOUS_PROFILES["conflicting_soft_decline"],
        amount_range=HIGH_VALUE_AMOUNT_RANGE,
        expected_confidence_band="medium",
        representative_confidence=0.71,
        expected_action="human_review",
        expected_final_status="escalated",
        stop_reason="high_value_uncertain_escalation",
        notes="Uncertain diagnosis (~71% model-reported confidence) on a high-value payment; policy's threshold for "
        "auto-action at this size is far higher (e.g. 95%). Escalates to human review rather than guessing.",
    ),
    CaseTemplate(
        template_key="previously_failed_serial_customer",
        category="hard",
        profile=KNOWN_PROFILES["issuer_timeout"],
        customer=CustomerSpec(prior_recovery_attempts=6, prior_recovery_successes=0),
        expected_confidence_band="high",
        expected_action="human_review",
        expected_final_status="escalated",
        notes="Diagnosis alone looks easy, but this customer has 6 prior recovery attempts and 0 successes. Track "
        "record *should* be a decision factor eventually, but it is intentionally NOT part of Day 2's policy engine "
        "(out of scope per that day's explicit requirement list) — today's implementation correctly RETRYs this case "
        "on diagnosis alone. This expectation is a Day-2+ target, not a bug to fix today.",
    ),
    CaseTemplate(
        template_key="risk_blocked_never_auto",
        category="hard",
        profile=KNOWN_PROFILES["risk_blocked"],
        expected_confidence_band="high",
        expected_action="human_review",
        expected_final_status="escalated",
        stop_reason="risk_block_requires_human_review",
        notes="Risk-engine blocks are never auto-retried, no matter how confident the diagnosis is. Hard safety rule, not a confidence call.",
    ),
]

EASY_INSTANCES_PER_TEMPLATE = 4
HARD_INSTANCES_PER_TEMPLATE = 4

# Exactly one instance of these templates is flagged as the canonical demo
# case in the ground-truth file (see README "three demo cases").
CANONICAL_CASE_TEMPLATE = {
    "issuer_timeout_retry_success": "case_a",
    "expired_card_payment_link_success": "case_b",
    "high_value_uncertain_escalation": "case_c",
}


def _build_customer(fake: Faker, spec: CustomerSpec) -> dict:
    return {
        "external_customer_id": rid("cust"),
        "name": fake.name(),
        "email": fake.email(),
        "phone": fake.msisdn()[:10],
        "dnd_opt_out": spec.dnd_opt_out,
        "max_contact_attempts": spec.max_contact_attempts,
        "contact_count": spec.contact_count,
        "prior_recovery_attempts": spec.prior_recovery_attempts,
        "prior_recovery_successes": spec.prior_recovery_successes,
    }


def generate_cases(*, seed: int = 42) -> list[dict]:
    """Returns a flat list of case dicts, each with a `dataset_record` (what
    the engine sees) and a `ground_truth` (what we expect it to conclude)."""
    fake = Faker()
    Faker.seed(seed)
    random.seed(seed)

    cases: list[dict] = []
    now = datetime.now(timezone.utc)

    for templates, instances_per in ((EASY_TEMPLATES, EASY_INSTANCES_PER_TEMPLATE), (HARD_TEMPLATES, HARD_INSTANCES_PER_TEMPLATE)):
        for template in templates:
            for i in range(instances_per):
                amount = random.randint(*template.amount_range)
                customer = _build_customer(fake, template.customer)
                failed_at = now - timedelta(hours=random.randint(1, 72))

                payload = build_failed_payment_payload(
                    amount=amount,
                    currency="INR",
                    email=customer["email"],
                    contact=customer["phone"],
                    profile=template.profile,
                )
                payment = payload["payload"]["payment"]["entity"]

                dataset_record = {
                    "external_payment_id": payment["id"],
                    "order_id": payment["order_id"],
                    "amount": amount,
                    "currency": "INR",
                    "failure_code": template.profile.error_code,
                    "failure_reason": template.profile.error_reason,
                    "failure_description": template.profile.error_description,
                    "retry_count": template.retry_count_so_far,
                    "failed_at": failed_at.isoformat(),
                    "raw_payload": payload,
                    "customer": customer,
                }

                is_canonical = i == 0 and template.template_key in CANONICAL_CASE_TEMPLATE

                ground_truth = {
                    "external_payment_id": payment["id"],
                    "template_key": template.template_key,
                    "category": template.category,
                    "canonical_demo_case": CANONICAL_CASE_TEMPLATE[template.template_key] if is_canonical else None,
                    "expected_root_cause": template.expected_root_cause or template.profile.key,
                    "expected_confidence_band": template.expected_confidence_band,
                    "representative_model_confidence": template.representative_confidence,
                    "expected_action": template.expected_action,
                    "expected_final_status": template.expected_final_status,
                    "expected_stop_reason": template.stop_reason,
                    "notes": template.notes,
                }

                cases.append({"dataset_record": dataset_record, "ground_truth": ground_truth})

    return cases
