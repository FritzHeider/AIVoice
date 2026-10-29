"""Request and response bodies of the MFA endpoints (specs/04-auth-tenancy-rbac.md §4.1, §4.5)."""

from __future__ import annotations

from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

__all__ = [
    "MFA_VERIFY_CHOICE_MESSAGE",
    "MfaCodeRequest",
    "MfaDisableResult",
    "MfaEnableResult",
    "MfaSetupResult",
    "MfaVerifyRequest",
    "MfaVerifyResult",
]

MFA_VERIFY_CHOICE_MESSAGE: Final = "Send either code or recovery_code."

TotpCodeField = Annotated[str, Field(min_length=1, max_length=32, repr=False)]
VerifyCodeField = Annotated[str | None, Field(default=None, min_length=1, max_length=64, repr=False)]

# The pyotp documentation sample (base32 of "Hello!" + 0xDEADBEEF) twice: a well-known sample, not a secret.
_SECRET_EXAMPLE: Final = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # noqa: S105 - documentation example
_RECOVERY_CODES_EXAMPLE: Final[list[JsonValue]] = [
    "k7mq-2vxa-p4tc",
    "w3nd-b6yr-h2qe",
    "c5zj-t7lu-m4fo",
    "r2ga-x6pk-e3vn",
    "y4hs-q7bw-d5ci",
    "n6fe-u2tz-a7mr",
    "l3vo-g5kd-s6wj",
    "b7ix-m2nc-z4hy",
    "t5qa-j3re-f6lp",
    "e2wu-o7sy-k5gb",
]


class MfaCodeRequest(BaseModel):
    """``{code}``: a 6-digit TOTP code (whitespace ignored) for enable and disable."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"code": "492039"}]})

    code: TotpCodeField


class MfaVerifyRequest(BaseModel):
    """``{code}`` or ``{recovery_code}``, exactly one."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"code": "492039"}, {"recovery_code": "k7mq-2vxa-p4tc"}]},
    )

    code: VerifyCodeField = None
    recovery_code: VerifyCodeField = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError(MFA_VERIFY_CHOICE_MESSAGE)
        return self


class MfaSetupResult(BaseModel):
    """The pending TOTP secret, its provisioning URI, and the URI as an inline SVG QR code."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "secret_base32": _SECRET_EXAMPLE,
                    "otpauth_uri": (
                        f"otpauth://totp/VoiceAgents:jane%40acme-dental.test?secret={_SECRET_EXAMPLE}"
                        "&issuer=VoiceAgents"
                    ),
                    "qr_svg": '<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196">…</svg>',
                }
            ]
        }
    )

    secret_base32: str
    otpauth_uri: str
    qr_svg: str


class MfaEnableResult(BaseModel):
    """MFA is on; the 10 recovery codes are shown this once."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"mfa_enabled": True, "recovery_codes": _RECOVERY_CODES_EXAMPLE}]}
    )

    mfa_enabled: bool
    recovery_codes: list[str]


class MfaVerifyResult(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"mfa_passed": True, "recovery_codes_remaining": 10}]}
    )

    mfa_passed: bool
    recovery_codes_remaining: int


class MfaDisableResult(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"mfa_enabled": False}]})

    mfa_enabled: bool
