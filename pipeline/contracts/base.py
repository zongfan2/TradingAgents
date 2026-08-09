"""Shared plumbing for the contract models.

specs/README.md rule 5: every contract schema ships as a strict Pydantic
model — writers reject unknown fields, readers *warn* on them. The strict
side is ``extra="forbid"`` on every model; the reader side is
``parse_lenient``, which logs a warning per unknown field, drops it, and
validates the rest.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from typing import Annotated, Any, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("pipeline.contracts")


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(
            "must be a timezone-aware ISO 8601 instant (e.g. 2026-07-28T09:30:00Z)"
        )
    return value


#: Contract timestamp type: every spec shows timestamps as ISO 8601 UTC
#: (``...Z``) instants, and the ledger's ordering rules ("greatest
#: ``written_at``", the ``decided_at`` outcome anchor) need comparable aware
#: datetimes — naive values are rejected at validation time on every model.
UtcInstant = Annotated[datetime, AfterValidator(_require_aware)]

ModelT = TypeVar("ModelT", bound="ContractModel")


class ContractError(ValueError):
    """A contract violation, carrying the complete list of errors.

    Collectors feed ``errors`` back into the retry prompt (macro collector
    R3), so validators accumulate every failure instead of stopping at the
    first one.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) if self.errors else "contract violation")


class ContractModel(BaseModel):
    """Base for all contract models: strict for writers, lenient for readers."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def parse_lenient(cls: type[ModelT], data: Any) -> ModelT:
        """Reader-mode validation: unknown fields are warned about and
        dropped (a newer writer may have added fields); every other
        validation error still fails.
        """
        data = copy.deepcopy(data)
        while True:
            try:
                return cls.model_validate(data)
            except ValidationError as exc:
                extras = [err["loc"] for err in exc.errors() if err["type"] == "extra_forbidden"]
                if not extras or not isinstance(data, dict):
                    raise
                for loc in extras:
                    _pop_path(data, loc)
                    logger.warning(
                        "%s: ignoring unknown field %r (reader-mode leniency)",
                        cls.__name__,
                        ".".join(str(part) for part in loc),
                    )


def _pop_path(data: Any, loc: tuple[Any, ...]) -> None:
    """Remove the value at a pydantic error location from nested dicts/lists."""
    node = data
    for part in loc[:-1]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return
    if isinstance(node, dict):
        node.pop(loc[-1], None)


def validation_error_messages(exc: ValidationError, prefix: str = "") -> list[str]:
    """Flatten a pydantic ValidationError into contract-style error strings."""
    messages = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        messages.append(f"{prefix}{loc}: {err['msg']}")
    return messages
