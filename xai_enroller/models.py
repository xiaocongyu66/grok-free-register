from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    sso_token: str

    def __repr__(self) -> str:
        return f"SourceRecord(source_id={self.source_id!r})"


@dataclass(frozen=True)
class DeviceFlow:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: float
    token_endpoint: str = "https://auth.x.ai/oauth2/token"

    def __repr__(self) -> str:
        return (
            f"DeviceFlow(user_code={self.user_code!r}, "
            f"verification_url={self.verification_url!r})"
        )


@dataclass(frozen=True)
class OAuthCredential:
    access_token: str
    refresh_token: str
    id_token: Optional[str]
    token_type: Optional[str]
    expires_in: int
    expires_at: str
    last_refresh: str
    subject: Optional[str]
    token_endpoint: str

    def __repr__(self) -> str:
        return "OAuthCredential(<redacted>)"


@dataclass(frozen=True)
class SinkReceipt:
    fingerprint: str


class AuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    NEEDS_BROWSER = "needs_browser"
    NEEDS_INTERACTION = "needs_interaction"


@dataclass(frozen=True)
class AuthorizationResult:
    status: AuthorizationStatus
    reason_code: str


class JobStatus(str, Enum):
    IMPORTED = "imported"
    NEEDS_BROWSER = "needs_browser"
    NEEDS_INTERACTION = "needs_interaction"
    SOURCE_INVALID = "source_invalid"
    OAUTH_DENIED = "oauth_denied"
    OAUTH_EXPIRED = "oauth_expired"
    OAUTH_REJECTED = "oauth_rejected"
    SINK_FAILED = "sink_failed"
    TRANSPORT_FAILED = "transport_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobResult:
    source_id: str
    status: JobStatus
    reason_code: str
    attempt_number: int = 1
    sink_receipt_fingerprint: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"JobResult(source_id={self.source_id!r}, status={self.status.value!r}, "
            f"reason_code={self.reason_code!r}, attempt_number={self.attempt_number})"
        )
