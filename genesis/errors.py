"""Typed application errors.

Each error carries the copy the *user* should see separately from the detail the
operator should see, so the chat surface never leaks stack traces and the logs
never lose the cause.
"""

from __future__ import annotations


class GenesisError(Exception):
    """Base error with user-facing copy."""

    user_message = "Something went wrong while handling that request."

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.user_message)
        self.detail = detail or self.user_message


class PlanningError(GenesisError):
    user_message = (
        "I could not turn that into a reliable agent plan. "
        "I can still try it once without deploying anything — send the request again if you want that."
    )


class UnsupportedRequest(GenesisError):
    user_message = "That request needs a capability I do not have."


class DeploymentError(GenesisError):
    user_message = "Live deployment is unavailable right now, so I answered without deploying."

    def __init__(self, detail: str = "", stage: str = "deploy", retryable: bool = False) -> None:
        super().__init__(detail)
        self.stage = stage
        self.retryable = retryable


class AuthError(GenesisError):
    user_message = "The platform rejected Genesis credentials; an operator needs to check them."


class QuotaError(GenesisError):
    user_message = "You have reached the usage limit for now. Please try again later."


class SessionExpired(GenesisError):
    user_message = "That request expired. Send it again and I will restart the flow."


class GatewayCallError(RuntimeError):
    """Raised inside the tool gateway client; ``retryable`` drives engine retries."""

    def __init__(self, message: str, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
