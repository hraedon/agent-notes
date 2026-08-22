"""Process-level v6 producer identity for review verdicts.

Identity relationship (this is the design, not a convenience):

    The reviewer of a review verdict IS the running producer.

v6 has exactly one identity source for a verdict: the process-level producer
regista resolves from ``REGISTA_PRODUCER_HARNESS`` / ``REGISTA_PRODUCER_MODEL``
/ ``REGISTA_PRODUCER_MODEL_LINEAGE`` and signs into the event's canonical
envelope. agent-notes deliberately adds no reviewer identity of its own — no
``actor_id``/``model_lineage`` parameter, no separate reviewer record. It
does not copy any lineage into the payload. The signed envelope's
``producer.model_lineage`` is the sole v6 reviewer-lineage authority.

Consequence: a process configured without a model (a "no model" producer) can
file, amend, and comment, but it cannot cast a review verdict. That refusal is
deliberate and happens here, before any write, with an actionable message.
"""

from __future__ import annotations

from typing import cast


class ProducerConfigurationError(RuntimeError):
    """The process producer cannot sign a review verdict.

    Raised before any write is attempted, so a refusal never leaves a
    half-written verdict behind.
    """

    code = "PRODUCER_MODEL_LINEAGE_NOT_CONFIGURED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(f"{self.code}: {message}")


def require_reviewer_model_lineage() -> str:
    """Return the canonical model lineage the running producer will sign.

    Resolves the process producer through regista's public
    :func:`regista.resolve_producer` (never an explicit argument — see its
    docstring for why the producer is a process property) and requires a
    non-null ``model`` and a ``model_lineage`` in regista's closed lineage
    registry.

    Raises :class:`ProducerConfigurationError` — naming the environment
    variables to set — when the producer cannot be resolved at all (missing
    harness identity) or resolves without a canonical model lineage. Both are
    agent-notes configuration problems a review cannot proceed past, so they
    surface as this component's own actionable error rather than a regista
    write-time failure.
    """

    import regista

    try:
        producer = regista.resolve_producer()
    except regista.RegistaError as exc:
        raise ProducerConfigurationError(
            "the review verdict is signed by the running producer, but the "
            f"process producer identity is incomplete: {exc}"
        ) from exc
    lineage = producer.model_lineage
    if (
        producer.model is None
        or lineage is None
        or lineage not in regista.MODEL_LINEAGE_FAMILIES
    ):
        raise ProducerConfigurationError(
            "a review verdict requires the running producer to declare both a "
            "model and a canonical model lineage; set REGISTA_PRODUCER_MODEL "
            "and "
            "REGISTA_PRODUCER_MODEL_LINEAGE in the process environment "
            "(the reviewer is the running producer — there is no per-call "
            f"override). Allowed lineages: {sorted(regista.MODEL_LINEAGE_FAMILIES)}"
        )
    return cast(str, lineage)


__all__ = [
    "ProducerConfigurationError",
    "require_reviewer_model_lineage",
]
