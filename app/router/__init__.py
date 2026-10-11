"""Quota-aware key selection and bounded failover."""
from app.router.classify import Classification, classify
from app.router.exhaustion import PoolExhaustedError, render_exhausted, render_fatal
from app.router.failover import AttemptRecord, FailoverTrace, execute_with_failover
from app.router.rebuild import RebuildHook, identity_rebuild
from app.router.selection import Candidate, NoCandidateError, select, select_candidates

__all__ = [
    "AttemptRecord",
    "Candidate",
    "Classification",
    "FailoverTrace",
    "NoCandidateError",
    "PoolExhaustedError",
    "RebuildHook",
    "classify",
    "execute_with_failover",
    "identity_rebuild",
    "render_exhausted",
    "render_fatal",
    "select",
    "select_candidates",
]
