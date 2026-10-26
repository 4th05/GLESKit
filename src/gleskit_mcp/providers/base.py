from __future__ import annotations

from dataclasses import dataclass
import re


class ProviderError(RuntimeError):
    """Raised when an upstream provider request fails."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        retryable: bool = False,
        suggested_next_step: str = "",
        diagnostic: str = "",
    ) -> None:
        super().__init__(message)
        embedded_code = re.search(r"error_code=([A-Za-z0-9_.-]+)", str(message), flags=re.IGNORECASE)
        self.error_code = str(error_code or (embedded_code.group(1) if embedded_code else "provider_error"))
        self.retryable = bool(retryable)
        self.suggested_next_step = str(
            suggested_next_step or "Inspect the diagnostic and correct the inputs before retrying."
        )
        self.diagnostic = str(diagnostic or message)

    def __str__(self) -> str:
        base = super().__str__()
        return (
            f"{base} [error_code={self.error_code}, retryable={'true' if self.retryable else 'false'}, "
            f"suggested_next_step={self.suggested_next_step}, diagnostic={self.diagnostic}]"
        )

    def structured(self) -> dict[str, object]:
        """Return the compact error contract exposed by analysis services."""
        return {
            "error_code": self.error_code or "provider_error",
            "retryable": self.retryable,
            "suggested_next_step": self.suggested_next_step,
            "diagnostic": self.diagnostic,
        }


@dataclass(slots=True)
class ProviderResponse:
    provider: str
    summary: str
    payload: dict
