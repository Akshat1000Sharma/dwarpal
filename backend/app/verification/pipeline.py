"""Mandate verification.

On an inbound checkout attempt Dwarpal must establish that the agent has authority, in a way a
third party can later re-check. The order below is fixed by specification section 5 and the
pipeline refuses at the first failure, recording which step refused.

    1. the credential is well formed and its signature is valid
    2. the signing key resolves to a known agent identity, and that identity is the subject the
       credential was issued to
    3. the issuing authority resolves in the trust registry, and its tier is recorded
    4. the credential is inside its validity window, with bounded clock-skew tolerance
    5. the credential has not been seen before
    6. the closed Checkout Mandate binds to the merchant's own signed Checkout record, covering
       the cart contents, the total and the policy hash
    7. the closed Checkout Mandate satisfies every constraint in the open Checkout Mandate

Step 2 is the confused-deputy case: an agent presenting a credential issued to a different agent
is refused, and that has its own test. Step 7 is the duty AP2 assigns to the merchant and leaves
unspecified; constraints it cannot decide are marked unresolved and handed to the semantic path,
never treated as satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.ap2 import sdjwt
from app.ap2.constraints import (
    ConstraintResult,
    MandateUsage,
    evaluate_checkout_constraints,
    evaluate_payment_constraints,
)
from app.ap2.jose import JoseError, jwk_thumbprint, public_key_from_jwk, sha256_b64url, verify_jws
from app.ap2.models import Checkout, ClosedCheckoutMandate, ClosedPaymentMandate, Merchant
from app.ap2.schema_validation import SchemaConformanceError, assert_conforms
from app.ap2.vocabulary import EXTENSION_CONSTRAINTS_CLAIM, Vct
from app.db.base import utcnow
from app.db.models import CheckoutSession, OpenMandate
from app.kernel.reasons import ReasonCode
from app.keys import merchant_key
from app.settings import settings
from app.trust.registry import Tier, TrustRegistry, get_registry
from app.verification import nonce


@dataclass(frozen=True)
class PresentedCredentials:
    """Exactly what the agent put on the wire."""

    open_checkout: str
    closed_checkout: str
    open_payment: str | None = None
    closed_payment: str | None = None
    nonce: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "open_checkout_mandate": self.open_checkout,
            "closed_checkout_mandate": self.closed_checkout,
            "open_payment_mandate": self.open_payment,
            "closed_payment_mandate": self.closed_payment,
            "nonce": self.nonce,
        }


@dataclass
class VerificationFailure:
    step: int
    step_name: str
    reason_code: ReasonCode
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "step_name": self.step_name,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class VerifiedAuthority:
    """Everything downstream needs, once authority has been established."""

    agent_id: str
    key_thumbprint: str
    agent_jwk: dict[str, Any]
    issuer_id: str
    tier: Tier
    open_checkout_claims: dict[str, Any]
    closed_checkout_claims: dict[str, Any]
    open_checkout_digest: str
    closed_checkout_digest: str
    checkout: Checkout
    checkout_jwt: str
    checkout_hash: str
    policy_hash: str
    session_row: CheckoutSession
    open_payment_claims: dict[str, Any] | None = None
    closed_payment_claims: dict[str, Any] | None = None
    open_payment_digest: str | None = None
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    steps_passed: list[str] = field(default_factory=list)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "key_thumbprint": self.key_thumbprint,
            "issuer_id": self.issuer_id,
            "tier": self.tier.as_dict(),
            "open_checkout_digest": self.open_checkout_digest,
            "closed_checkout_digest": self.closed_checkout_digest,
            "checkout_hash": self.checkout_hash,
            "policy_hash": self.policy_hash,
            "steps_passed": self.steps_passed,
            "constraints": [c.as_evidence() for c in self.constraint_results],
        }


@dataclass
class VerificationResult:
    authority: VerifiedAuthority | None = None
    failure: VerificationFailure | None = None

    @property
    def ok(self) -> bool:
        return self.authority is not None and self.failure is None


STEP_NAMES = {
    1: "well_formed_and_signed",
    2: "subject_binding",
    3: "issuer_trust",
    4: "validity_window",
    5: "replay",
    6: "checkout_binding",
    7: "constraint_satisfaction",
}


def _fail(step: int, code: ReasonCode, message: str, **detail: Any) -> VerificationResult:
    return VerificationResult(
        failure=VerificationFailure(step, STEP_NAMES[step], code, message, detail)
    )


def _skew() -> int:
    return settings.CREDENTIAL_CLOCK_SKEW_SECONDS


def _candidate_issuer_keys(
    registry: TrustRegistry, issuer_id: str, kid: str | None
) -> list[dict[str, Any]]:
    """Every key the authority publishes, with any kid match tried first.

    An authority legitimately publishes more than one key, during rotation and afterwards.
    Trying only the first would reject credentials signed by any of the others, so all of them
    are candidates and the credential is refused only when none verifies.
    """
    keys = registry.keys_for(issuer_id)
    if not keys:
        return []
    if kid:
        matching = [k for k in keys if k.get("kid") == kid]
        others = [k for k in keys if k.get("kid") != kid]
        return [*matching, *others]
    return list(keys)


def _verify_against_any(
    token: str,
    candidates: list[dict[str, Any]],
    *,
    expected_aud: str | None,
    expected_nonce: str | None,
    require_key_binding: bool,
) -> sdjwt.VerifiedSdJwt:
    """Verify against each candidate key, re-raising the last failure if none works.

    A key-binding failure is raised immediately rather than retried: it means the presenter
    does not hold the mandated key, which no other issuer key can change.
    """
    last: JoseError | None = None
    for candidate in candidates:
        try:
            return sdjwt.verify(
                token,
                candidate,
                expected_aud=expected_aud,
                expected_nonce=expected_nonce,
                require_key_binding=require_key_binding,
            )
        except sdjwt.KeyBindingError:
            raise
        except sdjwt.MissingKeyBindingError:
            raise
        except JoseError as exc:
            last = exc
    raise last or JoseError("no issuer key is registered for this authority")


def _check_key_binding_freshness(
    verified: Any, moment: datetime, label: str
) -> VerificationResult | None:
    """Bound how old a proof of possession may be.

    Applied to both open mandates. The credential's own validity window says nothing about when the
    holder proved possession of the key, so a replayed key binding would otherwise be accepted for
    as long as the mandate itself lives.
    """
    kb_iat = verified.kb_claims.get("iat")
    if kb_iat is None:
        return None
    drift = abs(int(moment.timestamp()) - int(kb_iat))
    if drift > _skew() + settings.INVENTORY_HOLD_TTL_SECONDS:
        return _fail(
            4,
            ReasonCode.CRED_EXPIRED,
            f"the open {label} Mandate key binding proof is too old",
            kb_iat=int(kb_iat),
            drift_seconds=drift,
        )
    return None


def _check_window(claims: dict[str, Any], now: datetime, label: str) -> VerificationResult | None:
    skew = _skew()
    exp = claims.get("exp")
    nbf = claims.get("nbf") or claims.get("iat")
    epoch = int(now.timestamp())
    if exp is not None and epoch > int(exp) + skew:
        return _fail(
            4,
            ReasonCode.CRED_EXPIRED,
            f"{label} expired",
            exp=int(exp),
            now=epoch,
            clock_skew_tolerance_seconds=skew,
        )
    if nbf is not None and epoch + skew < int(nbf):
        return _fail(
            4,
            ReasonCode.CRED_NOT_YET_VALID,
            f"{label} is not yet valid",
            not_before=int(nbf),
            now=epoch,
            clock_skew_tolerance_seconds=skew,
        )
    return None


def verify(
    session: Session,
    credentials: PresentedCredentials,
    *,
    audience: str | None = None,
    now: datetime | None = None,
    record_nonce: bool = True,
) -> VerificationResult:
    """Run the seven steps in order, refusing at the first failure."""
    moment = now or utcnow()
    registry = get_registry()
    steps: list[str] = []

    # ---- step 1: well formed, and the issuer signature verifies -------------------------------
    try:
        open_parsed = sdjwt.parse(credentials.open_checkout)
    except JoseError as exc:
        return _fail(1, ReasonCode.CRED_MALFORMED, f"open Checkout Mandate malformed: {exc}")

    issuer_id = open_parsed.payload.get("iss")
    if not isinstance(issuer_id, str) or not issuer_id:
        return _fail(1, ReasonCode.CRED_MALFORMED, "open Checkout Mandate carries no iss claim")

    candidates = _candidate_issuer_keys(registry, issuer_id, open_parsed.header.get("kid"))
    if not candidates:
        # An unknown authority cannot even have its signature checked, so this surfaces as a
        # trust failure rather than a signature failure.
        return _fail(
            3,
            ReasonCode.CRED_ISSUER_UNKNOWN,
            f"issuer {issuer_id} is not in the trust registry",
            issuer_id=issuer_id,
        )

    try:
        open_verified = _verify_against_any(
            credentials.open_checkout,
            candidates,
            expected_aud=audience,
            expected_nonce=credentials.nonce,
            require_key_binding=True,
        )
    except sdjwt.MissingKeyBindingError as exc:
        return _fail(2, ReasonCode.CRED_KEY_BINDING_MISSING, str(exc))
    except sdjwt.KeyBindingError as exc:
        return _fail(2, ReasonCode.CRED_SUBJECT_MISMATCH, str(exc))
    except JoseError as exc:
        return _fail(1, ReasonCode.CRED_SIGNATURE_INVALID, f"open Checkout Mandate: {exc}")

    open_claims = open_verified.claims
    schema_payload = {k: v for k, v in open_claims.items() if k not in ("iss", "sub", "nbf")}
    try:
        assert_conforms("open_checkout_mandate", schema_payload)
    except SchemaConformanceError as exc:
        return _fail(1, ReasonCode.CRED_SCHEMA_INVALID, str(exc), errors=exc.errors[:5])
    if open_claims.get("vct") != Vct.OPEN_CHECKOUT_MANDATE:
        return _fail(1, ReasonCode.CRED_MALFORMED, "unexpected vct on the open Checkout Mandate")
    steps.append(STEP_NAMES[1])

    # ---- step 2: the presenter holds the key the credential was issued to ---------------------
    agent_jwk = open_verified.cnf_jwk
    if agent_jwk is None:
        return _fail(2, ReasonCode.CRED_KEY_BINDING_MISSING, "open Checkout Mandate has no cnf.jwk")
    try:
        thumbprint = jwk_thumbprint(agent_jwk)
        public_key_from_jwk(agent_jwk)
    except JoseError as exc:
        return _fail(2, ReasonCode.CRED_KEY_BINDING_INVALID, f"cnf.jwk unusable: {exc}")

    agent_id = str(open_claims.get("sub") or thumbprint)

    # The closed mandate must be signed by that same key. A closed mandate signed by any other
    # agent is the confused-deputy case.
    try:
        closed_parsed = sdjwt.parse(credentials.closed_checkout)
    except JoseError as exc:
        return _fail(1, ReasonCode.CRED_MALFORMED, f"closed Checkout Mandate malformed: {exc}")
    try:
        closed_verified = sdjwt.verify(credentials.closed_checkout, agent_jwk)
    except JoseError as exc:
        return _fail(
            2,
            ReasonCode.CRED_SUBJECT_MISMATCH,
            "closed Checkout Mandate is not signed by the key the open mandate was issued to",
            error=str(exc),
        )
    closed_claims = closed_verified.claims
    try:
        assert_conforms(
            "checkout_mandate", {k: v for k, v in closed_claims.items() if k not in ("iss", "sub")}
        )
    except SchemaConformanceError as exc:
        return _fail(1, ReasonCode.CRED_SCHEMA_INVALID, str(exc), errors=exc.errors[:5])
    steps.append(STEP_NAMES[2])

    # ---- step 3: the issuing authority and its tier -------------------------------------------
    tier = registry.tier_for_issuer(issuer_id)
    if tier is None:
        return _fail(3, ReasonCode.CRED_ISSUER_UNKNOWN, f"issuer {issuer_id} has no tier")
    if tier.requires_key_binding and not open_verified.key_bound:
        return _fail(2, ReasonCode.CRED_KEY_BINDING_MISSING, "tier requires key binding")
    steps.append(STEP_NAMES[3])

    # ---- step 4: validity windows -------------------------------------------------------------
    windows = (
        (open_claims, "open Checkout Mandate"),
        (closed_claims, "closed Checkout Mandate"),
    )
    for claims, label in windows:
        failure = _check_window(claims, moment, label)
        if failure is not None:
            return failure
    failure = _check_key_binding_freshness(open_verified, moment, "Checkout")
    if failure is not None:
        return failure
    steps.append(STEP_NAMES[4])

    # ---- step 5: replay -----------------------------------------------------------------------
    open_digest = sha256_b64url(open_parsed.presentation.encode("ascii"))
    closed_digest = sha256_b64url(closed_parsed.presentation.encode("ascii"))
    if record_nonce:
        try:
            nonce.remember(
                session,
                digest=closed_digest,
                kind=str(Vct.CLOSED_CHECKOUT_MANDATE),
                agent_id=agent_id,
                correlation_id=str(closed_claims.get("checkout_hash", ""))[:64],
            )
        except nonce.ReplayDetected:
            return _fail(
                5,
                ReasonCode.CRED_REPLAYED,
                "closed Checkout Mandate has already been presented",
                digest=closed_digest,
            )
    elif nonce.seen(session, closed_digest):
        return _fail(5, ReasonCode.CRED_REPLAYED, "closed Checkout Mandate already seen")
    steps.append(STEP_NAMES[5])

    # ---- step 6: binding to the merchant's own signed Checkout --------------------------------
    try:
        closed_model = ClosedCheckoutMandate.model_validate(closed_claims)
    except Exception as exc:
        return _fail(1, ReasonCode.CRED_MALFORMED, f"closed Checkout Mandate: {exc}")

    computed_hash = sha256_b64url(closed_model.checkout_jwt.encode("ascii"))
    if computed_hash != closed_model.checkout_hash:
        return _fail(
            6,
            ReasonCode.CHECKOUT_BINDING_MISMATCH,
            "checkout_hash does not match the checkout_jwt it claims to cover",
            claimed=closed_model.checkout_hash,
            computed=computed_hash,
        )

    try:
        checkout_payload = verify_jws(closed_model.checkout_jwt, merchant_key().public_key)
    except JoseError as exc:
        return _fail(
            6,
            ReasonCode.CHECKOUT_BINDING_MISMATCH,
            f"the embedded Checkout was not signed by this merchant: {exc}",
        )

    row = session.get(CheckoutSession, str(checkout_payload.get("checkout_id", "")))
    if row is None:
        return _fail(
            6,
            ReasonCode.CHECKOUT_UNKNOWN,
            "the Checkout referenced is not one this merchant issued",
        )
    if row.checkout_hash != closed_model.checkout_hash:
        # The merchant signed a different Checkout than the one presented, which is the cart
        # altered after signing case.
        return _fail(
            6,
            ReasonCode.CART_ALTERED_AFTER_SIGNING,
            "the presented Checkout is not the one the merchant signed for this session",
            stored=row.checkout_hash,
            presented=closed_model.checkout_hash,
        )
    if row.expires_at <= moment:
        return _fail(
            6,
            ReasonCode.CHECKOUT_EXPIRED,
            "the quote has expired",
            expires_at=row.expires_at.isoformat(),
        )

    try:
        checkout = Checkout.model_validate(checkout_payload["checkout"])
        assert_conforms("checkout", checkout_payload["checkout"])
    except Exception as exc:
        return _fail(6, ReasonCode.CHECKOUT_BINDING_MISMATCH, f"embedded Checkout invalid: {exc}")

    if checkout.total_minor() != row.total_minor:
        return _fail(
            6,
            ReasonCode.PRICE_DRIFT,
            "the signed total does not match the quoted total",
            signed_total_minor=checkout.total_minor(),
            quoted_total_minor=row.total_minor,
        )

    acknowledged_policy = str(checkout_payload.get("policy_hash", ""))
    if acknowledged_policy != row.policy_hash:
        return _fail(
            6,
            ReasonCode.POLICY_HASH_MISMATCH,
            "the Checkout acknowledges a different policy hash than the one live at quote time",
            acknowledged=acknowledged_policy,
            live_at_quote=row.policy_hash,
        )
    steps.append(STEP_NAMES[6])

    # ---- step 7: the closed mandate satisfies the open mandate --------------------------------
    merchant = checkout.merchant or Merchant(
        id=settings.MERCHANT_ID, name=settings.MERCHANT_NAME, website=settings.MERCHANT_WEBSITE
    )
    # AP2 constraints and the Dwarpal natural-language extension are evaluated together, but the
    # extension is carried outside the AP2 array so the credential stays schema-conformant.
    all_constraints = [
        *open_claims.get("constraints", []),
        *open_claims.get(EXTENSION_CONSTRAINTS_CLAIM, []),
    ]
    results = evaluate_checkout_constraints(all_constraints, checkout, merchant)

    open_payment_claims: dict[str, Any] | None = None
    closed_payment_claims: dict[str, Any] | None = None
    open_payment_digest: str | None = None

    if credentials.open_payment and credentials.closed_payment:
        payment_outcome = _verify_payment(
            session,
            registry=registry,
            credentials=credentials,
            agent_jwk=agent_jwk,
            checkout=checkout,
            checkout_hash=closed_model.checkout_hash,
            open_checkout_digest=open_digest,
            moment=moment,
            audience=audience,
            record_nonce=record_nonce,
            agent_id=agent_id,
        )
        if isinstance(payment_outcome, VerificationResult):
            return payment_outcome
        (
            open_payment_claims,
            closed_payment_claims,
            open_payment_digest,
            payment_results,
        ) = payment_outcome
        results.extend(payment_results)

    steps.append(STEP_NAMES[7])

    return VerificationResult(
        authority=VerifiedAuthority(
            agent_id=agent_id,
            key_thumbprint=thumbprint,
            agent_jwk=agent_jwk,
            issuer_id=issuer_id,
            tier=tier,
            open_checkout_claims=open_claims,
            closed_checkout_claims=closed_claims,
            open_checkout_digest=open_digest,
            closed_checkout_digest=closed_digest,
            checkout=checkout,
            checkout_jwt=closed_model.checkout_jwt,
            checkout_hash=closed_model.checkout_hash,
            policy_hash=acknowledged_policy,
            session_row=row,
            open_payment_claims=open_payment_claims,
            closed_payment_claims=closed_payment_claims,
            open_payment_digest=open_payment_digest,
            constraint_results=results,
            steps_passed=steps,
        )
    )


def _verify_payment(
    session: Session,
    *,
    registry: TrustRegistry,
    credentials: PresentedCredentials,
    agent_jwk: dict[str, Any],
    checkout: Checkout,
    checkout_hash: str,
    open_checkout_digest: str,
    moment: datetime,
    audience: str | None,
    record_nonce: bool,
    agent_id: str,
) -> VerificationResult | tuple[dict[str, Any], dict[str, Any], str, list[ConstraintResult]]:
    """The Payment Mandate pair, verified with the same ordering as the Checkout pair."""
    assert credentials.open_payment and credentials.closed_payment
    try:
        open_parsed = sdjwt.parse(credentials.open_payment)
    except JoseError as exc:
        return _fail(1, ReasonCode.CRED_MALFORMED, f"open Payment Mandate malformed: {exc}")

    issuer_id = open_parsed.payload.get("iss")
    candidates = _candidate_issuer_keys(registry, str(issuer_id), open_parsed.header.get("kid"))
    if not candidates:
        return _fail(
            3,
            ReasonCode.CRED_ISSUER_UNKNOWN,
            f"payment issuer {issuer_id} is not in the registry",
        )
    try:
        open_verified = _verify_against_any(
            credentials.open_payment,
            candidates,
            expected_aud=audience,
            expected_nonce=credentials.nonce,
            require_key_binding=True,
        )
    except sdjwt.KeyBindingError as exc:
        return _fail(2, ReasonCode.CRED_SUBJECT_MISMATCH, f"open Payment Mandate: {exc}")
    except JoseError as exc:
        return _fail(1, ReasonCode.CRED_SIGNATURE_INVALID, f"open Payment Mandate: {exc}")

    open_claims = open_verified.claims
    if open_verified.cnf_jwk != agent_jwk:
        return _fail(
            2,
            ReasonCode.CRED_SUBJECT_MISMATCH,
            "the Payment Mandate was issued to a different agent than the Checkout Mandate",
        )
    try:
        assert_conforms(
            "open_payment_mandate",
            {k: v for k, v in open_claims.items() if k not in ("iss", "sub", "nbf")},
        )
    except SchemaConformanceError as exc:
        return _fail(1, ReasonCode.CRED_SCHEMA_INVALID, str(exc), errors=exc.errors[:5])

    failure = _check_window(open_claims, moment, "open Payment Mandate")
    if failure is not None:
        return failure

    failure = _check_key_binding_freshness(open_verified, moment, "Payment")
    if failure is not None:
        return failure

    try:
        closed_parsed = sdjwt.parse(credentials.closed_payment)
        closed_verified = sdjwt.verify(credentials.closed_payment, agent_jwk)
    except JoseError as exc:
        return _fail(
            2,
            ReasonCode.CRED_SUBJECT_MISMATCH,
            f"closed Payment Mandate is not signed by the mandated agent key: {exc}",
        )
    closed_claims = closed_verified.claims
    try:
        assert_conforms(
            "payment_mandate", {k: v for k, v in closed_claims.items() if k not in ("iss", "sub")}
        )
    except SchemaConformanceError as exc:
        return _fail(1, ReasonCode.CRED_SCHEMA_INVALID, str(exc), errors=exc.errors[:5])

    failure = _check_window(closed_claims, moment, "closed Payment Mandate")
    if failure is not None:
        return failure

    closed_digest = sha256_b64url(closed_parsed.presentation.encode("ascii"))
    if record_nonce:
        try:
            nonce.remember(
                session,
                digest=closed_digest,
                kind=str(Vct.CLOSED_PAYMENT_MANDATE),
                agent_id=agent_id,
                correlation_id=checkout_hash[:64],
            )
        except nonce.ReplayDetected:
            return _fail(5, ReasonCode.CRED_REPLAYED, "closed Payment Mandate already presented")

    try:
        closed_model = ClosedPaymentMandate.model_validate(closed_claims)
    except Exception as exc:
        return _fail(1, ReasonCode.CRED_MALFORMED, f"closed Payment Mandate: {exc}")

    # The closed Payment Mandate binds to the checkout by carrying its digest.
    if closed_model.transaction_id != checkout_hash:
        return _fail(
            6,
            ReasonCode.CHECKOUT_BINDING_MISMATCH,
            "the Payment Mandate transaction_id does not identify this checkout",
            expected=checkout_hash,
            presented=closed_model.transaction_id,
        )
    if closed_model.payment_amount.amount != checkout.total_minor():
        return _fail(
            6,
            ReasonCode.PRICE_DRIFT,
            "the Payment Mandate authorises a different amount than the signed Checkout total",
            mandate_minor=closed_model.payment_amount.amount,
            checkout_minor=checkout.total_minor(),
        )
    if closed_model.payment_amount.currency != checkout.currency:
        return _fail(
            6, ReasonCode.CONSTRAINT_CURRENCY_MISMATCH, "payment currency differs from the checkout"
        )

    open_payment_digest = sha256_b64url(open_parsed.presentation.encode("ascii"))
    # Prior use is recorded against the open Checkout Mandate, which is the authority record.
    # Keying it on the payment token instead would reset the count on every presentation and
    # make the recurrence and budget constraints unenforceable.
    usage = _mandate_usage(session, open_checkout_digest)
    results = evaluate_payment_constraints(
        open_claims.get("constraints", []),
        amount_minor=closed_model.payment_amount.amount,
        currency=closed_model.payment_amount.currency,
        payee=closed_model.payee,
        instrument_id=closed_model.payment_instrument.id,
        pisp_id=closed_model.pisp.id if closed_model.pisp else None,
        open_checkout_digest=open_checkout_digest,
        usage=usage,
        now=moment,
    )
    return open_claims, closed_claims, open_payment_digest, results


def _mandate_usage(session: Session, digest: str) -> MandateUsage:
    row = session.query(OpenMandate).filter(OpenMandate.digest == digest).one_or_none()
    if row is None:
        return MandateUsage()
    return MandateUsage(total_amount_minor=row.committed_minor, total_uses=row.use_count)


def utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)
