"""Zero-network 'simulated' action executor for batch evaluation runs
(`python -m src.run_batch`, without `--execute-real`). Models a plausible
outcome for an approved RETRY/PAYMENT_LINK decision without touching
Razorpay at all — fast, free, no side effects, good for iterating over a
60-100 record batch.

Every outcome here is action_mode=SIMULATED. The result MAY legitimately be
SUCCEEDED — unlike src/razorpay_action.py's RazorpayActionClient (the REAL
executor used by --execute-real, which never returns SUCCEEDED because it
represents an actual, unconfirmed live call), this executor never made a
live call in the first place, so "succeeded" here is explicitly a MODELED
number for the 'Simulated recovery' metric bucket — never presented as, or
confused with, a confirmed recovery.
"""

from app.models.recovery_attempt import ActionMode, ActionResult
from src.razorpay_action import ActionOutcome

_MODEL_NOTE = (
    "This is a MODELED number for batch evaluation (no live Razorpay call was made), "
    "not a confirmed recovery — use --execute-real for a genuine (still PENDING-only) test-mode call."
)


class SimulatedActionExecutor:
    """Models the outcome of the RETRY/PAYMENT_LINK the policy engine
    approved. The policy engine already gated this to a HIGH-confidence,
    compliant, cap/cooldown-clear case, so the model assumption is that the
    approved intervention succeeds — a simplification appropriate for a
    zero-network evaluation run, documented here rather than hidden."""

    def execute_retry(self, *, amount: int, currency: str, receipt: str) -> ActionOutcome:
        return ActionOutcome(
            action_mode=ActionMode.SIMULATED,
            action_result=ActionResult.SUCCEEDED,
            razorpay_reference=None,
            evidence=f"Simulated retry for {receipt}, modeled as succeeding. {_MODEL_NOTE}",
        )

    def execute_payment_link(self, *, amount: int, currency: str, description: str) -> ActionOutcome:
        return ActionOutcome(
            action_mode=ActionMode.SIMULATED,
            action_result=ActionResult.SUCCEEDED,
            razorpay_reference=None,
            evidence=f"Simulated payment link, modeled as completed by the customer. {_MODEL_NOTE}",
        )
