"""ERC-8183 job state machine (local view, reconciled with the chain).

Local states:
    seen        job exists on chain with us as provider, not funded yet (OPEN)
    funded      funded on chain; waiting for (or retrying) delivery
    delivering  a worker holds the job (compare-and-swap from funded)
    submitted   deliverable hash is on chain; waiting out the dispute window
    settling    a worker is calling EvaluatorRouter.settle
    completed   JobCompleted on chain (provider paid)          terminal
    rejected    JobRejected on chain                           terminal
    expired     expired / refunded to the client               terminal
    skipped     permanently not ours to deliver (bad quote...) terminal

The chain is the source of truth: `from_chain` maps an on-chain status onto a
local state and never moves a job backwards past a submit, so a job whose
deliverable is already on chain is never submitted twice.
"""

from __future__ import annotations

# IACP.JobStatus on AgenticCommerce
OPEN, FUNDED, SUBMITTED, COMPLETED, REJECTED, EXPIRED = range(6)
CHAIN_STATUS_NAMES = {OPEN: "OPEN", FUNDED: "FUNDED", SUBMITTED: "SUBMITTED", COMPLETED: "COMPLETED",
                      REJECTED: "REJECTED", EXPIRED: "EXPIRED"}

TERMINAL = frozenset({"completed", "rejected", "expired", "skipped"})
ACTIVE = frozenset({"seen", "funded", "delivering", "submitted", "settling"})

ALLOWED: dict[str, frozenset[str]] = {
    "seen": frozenset({"funded", "expired", "skipped", "submitted", "completed", "rejected"}),
    "funded": frozenset({"delivering", "skipped", "expired", "submitted", "completed", "rejected"}),
    "delivering": frozenset({"funded", "submitted", "skipped", "expired", "completed", "rejected"}),
    "submitted": frozenset({"settling", "completed", "rejected", "expired"}),
    "settling": frozenset({"submitted", "completed", "rejected", "expired"}),
    "completed": frozenset(),
    "rejected": frozenset(),
    "expired": frozenset(),
    "skipped": frozenset({"completed", "rejected", "expired", "submitted"}),
}


class IllegalTransition(ValueError):
    pass


def transition(current: str, new: str) -> str:
    if new == current:
        return current
    if new not in ALLOWED.get(current, frozenset()):
        raise IllegalTransition(f"job state {current} -> {new} is not allowed")
    return new


def from_chain(current: str | None, chain_status: int) -> str:
    """The local state implied by an on-chain status, given what we believed."""
    if chain_status == COMPLETED:
        return "completed"
    if chain_status == REJECTED:
        return "rejected"
    if chain_status == EXPIRED:
        return "expired"
    if chain_status == SUBMITTED:
        return "settling" if current == "settling" else "submitted"
    if chain_status == FUNDED:
        if current in ("delivering",):
            return current
        if current == "skipped":
            return "skipped"
        return "funded"
    # OPEN
    if current in (None, "seen"):
        return "seen"
    return current if current in ("skipped",) else "seen"


def may_deliver(local_state: str, chain_status: int) -> bool:
    """Deliver only a job that is funded both locally and on chain."""
    return local_state == "funded" and chain_status == FUNDED


def settle_due(chain_status: int, submitted_at: int, dispute_window: int, now: int, disputed: bool) -> bool:
    """OptimisticPolicy approves by silence once the dispute window has passed."""
    return chain_status == SUBMITTED and not disputed and submitted_at > 0 and now > submitted_at + dispute_window
