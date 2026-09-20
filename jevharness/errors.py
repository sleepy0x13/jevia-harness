"""Exception types. Every failure the harness can produce is one of these."""
from __future__ import annotations


class HarnessError(Exception):
    """Base class. Carries an HTTP-ish status for the API layer."""

    status = 500
    code = "harness_error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"error": {"code": self.code, "message": self.message}}
        if self.detail is not None:
            out["error"]["detail"] = self.detail
        return out

    def public_dict(self, secrets=()) -> dict:
        """What may be shown or saved: no raw upstream body, nothing secret.

        ``detail`` often holds a provider's whole error response, which can
        echo headers back; it never leaves the process.
        """
        from .redact import redact

        return {"error": {"code": self.code, "message": redact(self.message, secrets)[:600]}}


class ConfigError(HarnessError):
    status = 400
    code = "config_error"


class SchemaError(HarnessError):
    """The caller's decision schema is invalid."""

    status = 400
    code = "schema_error"


class ProviderError(HarnessError):
    """Upstream API failed."""

    status = 502
    code = "provider_error"


class RateLimitError(ProviderError):
    status = 429
    code = "rate_limited"


class CompileError(HarnessError):
    """Could not turn a natural-language task into a typed Jev schema."""

    status = 422
    code = "compile_error"
