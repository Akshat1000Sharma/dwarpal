"""Credential factory: a mock Trusted Surface, Credential Provider and Shopping Agent.

The Credential Provider is out of scope for this project and is mocked, as the README states. This
module is that mock, and it is deliberately shared by three callers so they cannot drift apart:

    - the test suite
    - the adversarial and benign corpora
    - the interop driver

Every credential it produces is validated against the published AP2 JSON Schemas before it goes on
the wire, so the corpus cannot pass by feeding Dwarpal something the specification would reject.

The ``Tamper`` options exist because the adversarial corpus has to forge, replay and re-bind
credentials at the byte level.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.ap2.jose import (
    KeyPair,
    b64url_decode,
    b64url_encode,
    canonical_json,
    decode_jws_unverified,
    generate_keypair,
    sha256_b64url,
    sign_jws,
)
from app.ap2.schema_validation import assert_conforms
from app.ap2.sdjwt import SD, attach_key_binding, issue, parse
from app.ap2.vocabulary import (
    CHECKOUT_JWT_TYP,
    CONFIRMATION_JWT_TYP,
    EXTENSION_CONSTRAINTS_CLAIM,
    NATURAL_LANGUAGE_CONSTRAINT,
    PRESENCE_JWT_TYP,
    CheckoutConstraint,
    PaymentConstraint,
    Vct,
)
from app.db.base import utcnow
from app.settings import settings
from app.trust.registry import register_runtime_key
from app.verification.pipeline import PresentedCredentials

DEFAULT_ISSUER = "did:web:trusted-surface.dwarpal.test"
SANDBOX_ISSUER = "did:web:sandbox-wallet.dwarpal.test"
REGULATED_ISSUER = "did:web:bank.dwarpal.test"
UNKNOWN_ISSUER = "did:web:not-in-the-registry.test"

DEFAULT_INSTRUMENT = {"id": "pi_dwarpal_test_card", "type": "CARD", "description": "Test card"}


@dataclass
class Principals:
    """The parties in a human-not-present flow, minus the merchant."""

    issuer: KeyPair
    agent: KeyPair
    issuer_id: str = DEFAULT_ISSUER
    agent_id: str = "agent:dwarpal-reference-shopper"

    @classmethod
    def create(
        cls,
        *,
        issuer_id: str = DEFAULT_ISSUER,
        agent_id: str = "agent:dwarpal-reference-shopper",
        register: bool = True,
    ) -> Principals:
        # A distinct kid per principal, so a registry holding many mock authorities can
        # select the right key directly instead of trying each in turn.
        unique = secrets.token_hex(4)
        principals = cls(
            issuer=generate_keypair(f"{issuer_id}#key-{unique}"),
            agent=generate_keypair(f"{agent_id}#key-{unique}"),
            issuer_id=issuer_id,
            agent_id=agent_id,
        )
        if register:
            principals.register()
        return principals

    def register(self) -> None:
        """Publish the mock authority's key into the trust registry it is declared in."""
        register_runtime_key(self.issuer_id, self.issuer.public_jwk())


@dataclass
class Tamper:
    """Mutations the adversarial corpus applies. Every field defaults to no tampering."""

    forge_issuer_signature: bool = False
    wrong_agent_key: bool = False
    drop_key_binding: bool = False
    expired: bool = False
    not_yet_valid: bool = False
    clock_skew_seconds: int = 0
    unknown_issuer: bool = False
    altered_checkout_jwt: str | None = None
    altered_checkout_hash: str | None = None
    payment_amount_minor: int | None = None
    payment_transaction_id: str | None = None
    payment_instrument: dict[str, Any] | None = None
    payee: dict[str, Any] | None = None
    omit_line_item_constraint: bool = False
    signing_algorithm: str | None = None
    duplicate_disclosure: bool = False
    mutate_disclosure: bool = False
    key_binding_audience: str | None = None
    key_binding_nonce: str | None = None
    key_binding_age_seconds: int = 0
    payment_currency: str | None = None
    pisp: dict[str, Any] | None = None
    checkout_jwt_from_stranger: bool = False
    presence_age_seconds: int = 0
    presence_checkout_hash: str | None = None
    presence_issuer_id: str | None = None
    forge_presence_signature: bool = False


def _retag_algorithm(token: str, algorithm: str) -> str:
    """Rewrite the issuer JWT header to claim a different algorithm.

    Everything downstream is left untouched, which is the attack: a verifier that trusts the header
    over its own policy would either skip the check entirely (``none``) or verify a symmetric MAC
    against a public value it treats as a secret.
    """
    issuer_jwt, separator, remainder = token.partition("~")
    header_segment, payload_segment, signature_segment = issuer_jwt.split(".")
    header = json.loads(b64url_decode(header_segment))
    header["alg"] = algorithm
    rebuilt = ".".join(
        [
            b64url_encode(canonical_json(header)),
            payload_segment,
            "" if algorithm == "none" else signature_segment,
        ]
    )
    return rebuilt + separator + remainder


def _rewrite_disclosures(token: str, mutate: bool, duplicate: bool) -> str:
    """Tamper with the disclosure list without disturbing the issuer signature.

    ``mutate`` re-salts one disclosure, so it is still well formed JSON but hashes to a digest the
    issuer never committed to. ``duplicate`` presents one twice. Both are the holder assembling a
    presentation from parts, which RFC 9901 requires a verifier to reject.
    """
    parts = token.split("~")
    issuer_jwt, disclosures = parts[0], [d for d in parts[1:] if d]
    if not disclosures:
        return token
    if mutate:
        decoded = json.loads(b64url_decode(disclosures[0]))
        decoded[0] = b64url_encode(secrets.token_bytes(16))
        disclosures[0] = b64url_encode(canonical_json(decoded))
    if duplicate:
        disclosures.append(disclosures[0])
    return issuer_jwt + "~" + "".join(d + "~" for d in disclosures)


def _apply_token_tamper(token: str, tamper: Tamper) -> str:
    if tamper.signing_algorithm:
        token = _retag_algorithm(token, tamper.signing_algorithm)
    if tamper.mutate_disclosure or tamper.duplicate_disclosure:
        token = _rewrite_disclosures(token, tamper.mutate_disclosure, tamper.duplicate_disclosure)
    return token


def _resign_checkout_as_stranger(checkout_jwt: str) -> tuple[str, str]:
    """Re-sign the merchant's Checkout with a key that is not the merchant's.

    The hash is recomputed so it still covers the token it claims to. Without that the binding
    check refuses on the hash and the merchant-key check is never reached, which would leave this
    technique proving something weaker than it says.
    """
    _header, payload = decode_jws_unverified(checkout_jwt)
    forged = sign_jws(payload, generate_keypair("stranger-merchant#key"), typ=CHECKOUT_JWT_TYP)
    return forged, sha256_b64url(forged.encode("ascii"))


@dataclass
class MandateSpec:
    """What the human authorised."""

    allowed_merchant_ids: list[str] = field(default_factory=lambda: [settings.MERCHANT_ID])
    line_items: list[dict[str, Any]] = field(default_factory=list)
    natural_language: list[str] = field(default_factory=list)
    amount_cap_minor: int = 5_000_000
    amount_min_minor: int | None = None
    budget_minor: int | None = None
    currency: str = "INR"
    max_occurrences: int | None = None
    validity_seconds: int = 3600
    instrument: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_INSTRUMENT))
    execution_not_before: str | None = None
    execution_not_after: str | None = None
    allowed_pisps: list[dict[str, Any]] | None = None


def line_item_requirement(
    requirement_id: str, skus: list[tuple[str, str]], quantity: int
) -> dict[str, Any]:
    return {
        "id": requirement_id,
        "acceptable_items": [{"id": sku, "title": title} for sku, title in skus],
        "quantity": quantity,
    }


def _window(spec: MandateSpec, tamper: Tamper, now: datetime) -> tuple[int, int]:
    iat = int(now.timestamp())
    exp = iat + spec.validity_seconds
    if tamper.expired:
        # Comfortably outside the configured skew tolerance, so the refusal is unambiguous.
        exp = iat - (settings.CREDENTIAL_CLOCK_SKEW_SECONDS + 600)
        iat = exp - spec.validity_seconds
    elif tamper.not_yet_valid:
        iat = int(now.timestamp()) + settings.CREDENTIAL_CLOCK_SKEW_SECONDS + 600
        exp = iat + spec.validity_seconds
    elif tamper.clock_skew_seconds:
        iat += tamper.clock_skew_seconds
        exp += tamper.clock_skew_seconds
    return iat, exp


def build_open_checkout_mandate(
    principals: Principals,
    spec: MandateSpec,
    *,
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> str:
    tamper = tamper or Tamper()
    moment = now or utcnow()
    iat, exp = _window(spec, tamper, moment)

    constraints: list[Any] = [
        {
            "type": CheckoutConstraint.ALLOWED_MERCHANTS.value,
            "allowed": [
                SD({"id": merchant_id, "name": settings.MERCHANT_NAME})
                for merchant_id in spec.allowed_merchant_ids
            ],
        }
    ]
    if not tamper.omit_line_item_constraint:
        constraints.append(
            {
                "type": CheckoutConstraint.LINE_ITEMS.value,
                "items": [
                    {
                        "id": requirement["id"],
                        "quantity": requirement["quantity"],
                        "acceptable_items": [SD(item) for item in requirement["acceptable_items"]],
                    }
                    for requirement in spec.line_items
                ],
            }
        )

    claims: dict[str, Any] = {
        "vct": Vct.OPEN_CHECKOUT_MANDATE.value,
        "iss": UNKNOWN_ISSUER if tamper.unknown_issuer else principals.issuer_id,
        "sub": principals.agent_id,
        "constraints": constraints,
        "iat": iat,
        "exp": exp,
    }
    if spec.natural_language:
        claims[EXTENSION_CONSTRAINTS_CLAIM] = [
            {"type": NATURAL_LANGUAGE_CONSTRAINT, "text": text} for text in spec.natural_language
        ]

    signer = generate_keypair("forged#key") if tamper.forge_issuer_signature else principals.issuer
    return issue(claims, signer, holder_jwk=principals.agent.public_jwk())


def build_open_payment_mandate(
    principals: Principals,
    spec: MandateSpec,
    *,
    open_checkout_digest: str,
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> str:
    tamper = tamper or Tamper()
    moment = now or utcnow()
    iat, exp = _window(spec, tamper, moment)

    constraints: list[Any] = [
        {
            "type": PaymentConstraint.REFERENCE.value,
            "conditional_transaction_id": open_checkout_digest,
        },
        {
            "type": PaymentConstraint.AMOUNT_RANGE.value,
            "currency": spec.currency,
            "max": spec.amount_cap_minor,
            **({"min": spec.amount_min_minor} if spec.amount_min_minor is not None else {}),
        },
        {
            "type": PaymentConstraint.ALLOWED_PAYEES.value,
            "allowed": [SD({"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME})],
        },
        {
            "type": PaymentConstraint.ALLOWED_PAYMENT_INSTRUMENTS.value,
            "allowed": [SD(dict(spec.instrument))],
        },
    ]
    if spec.budget_minor is not None:
        constraints.append(
            {
                "type": PaymentConstraint.BUDGET.value,
                "max": spec.budget_minor,
                "currency": spec.currency,
            }
        )
    if spec.max_occurrences is not None:
        constraints.append(
            {
                "type": PaymentConstraint.AGENT_RECURRENCE.value,
                "frequency": "ON_DEMAND",
                "max_occurrences": spec.max_occurrences,
            }
        )
    if spec.allowed_pisps is not None:
        constraints.append(
            {
                "type": PaymentConstraint.ALLOWED_PISPS.value,
                "allowed": [SD(dict(entry)) for entry in spec.allowed_pisps],
            }
        )
    if spec.execution_not_before or spec.execution_not_after:
        constraints.append(
            {
                "type": PaymentConstraint.EXECUTION_DATE.value,
                **({"not_before": spec.execution_not_before} if spec.execution_not_before else {}),
                **({"not_after": spec.execution_not_after} if spec.execution_not_after else {}),
            }
        )

    claims = {
        "vct": Vct.OPEN_PAYMENT_MANDATE.value,
        "iss": UNKNOWN_ISSUER if tamper.unknown_issuer else principals.issuer_id,
        "sub": principals.agent_id,
        "constraints": constraints,
        "iat": iat,
        "exp": exp,
    }
    signer = generate_keypair("forged#key") if tamper.forge_issuer_signature else principals.issuer
    return issue(claims, signer, holder_jwk=principals.agent.public_jwk())


def build_closed_checkout_mandate(
    principals: Principals,
    *,
    checkout_jwt: str,
    checkout_hash: str,
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> str:
    tamper = tamper or Tamper()
    moment = now or utcnow()
    iat = int(moment.timestamp())
    claims = {
        "vct": Vct.CLOSED_CHECKOUT_MANDATE.value,
        "checkout_jwt": tamper.altered_checkout_jwt or checkout_jwt,
        "checkout_hash": tamper.altered_checkout_hash or checkout_hash,
        "iat": iat,
        "exp": iat + 900,
    }
    assert_conforms("checkout_mandate", claims)
    signer = generate_keypair("stranger#key") if tamper.wrong_agent_key else principals.agent
    return issue(claims, signer)


def build_closed_payment_mandate(
    principals: Principals,
    spec: MandateSpec,
    *,
    transaction_id: str,
    amount_minor: int,
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> str:
    tamper = tamper or Tamper()
    moment = now or utcnow()
    iat = int(moment.timestamp())
    claims = {
        "vct": Vct.CLOSED_PAYMENT_MANDATE.value,
        "transaction_id": tamper.payment_transaction_id or transaction_id,
        "payee": tamper.payee or {"id": settings.MERCHANT_ID, "name": settings.MERCHANT_NAME},
        "payment_amount": {
            "amount": tamper.payment_amount_minor
            if tamper.payment_amount_minor is not None
            else amount_minor,
            "currency": tamper.payment_currency or spec.currency,
        },
        "payment_instrument": tamper.payment_instrument or dict(spec.instrument),
        "iat": iat,
        "exp": iat + 900,
    }
    if tamper.pisp is not None:
        claims["pisp"] = dict(tamper.pisp)
    assert_conforms("payment_mandate", claims)
    signer = generate_keypair("stranger#key") if tamper.wrong_agent_key else principals.agent
    return issue(claims, signer)


def issue_presence_attestation(
    principals: Principals,
    *,
    checkout_hash: str,
    method: str = "surface_confirmation",
    subject: str = "human:dwarpal-demo-principal",
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> str:
    """What a trusted surface signs when a person is actually at it.

    Bound to one Checkout and stamped with the moment the person was observed, because presence is
    a claim about a specific cart at a specific instant. It grants nothing on its own: the open
    mandates still carry the authority, and the kernel still evaluates every limit.
    """
    tamper = tamper or Tamper()
    moment = now or utcnow()
    observed_at = int(moment.timestamp()) - tamper.presence_age_seconds
    claims = {
        "iss": tamper.presence_issuer_id or principals.issuer_id,
        "sub": subject,
        "checkout_hash": tamper.presence_checkout_hash or checkout_hash,
        "method": method,
        "nonce": secrets.token_hex(8),
        "iat": observed_at,
        "exp": observed_at + 900,
    }
    signer = generate_keypair("forged-surface#key") if tamper.forge_presence_signature else (
        principals.issuer
    )
    return sign_jws(claims, signer, typ=PRESENCE_JWT_TYP)


def digest_of(token: str) -> str:
    return sha256_b64url(parse(token).presentation.encode("ascii"))


@dataclass
class IssuedMandates:
    """Open mandates as the human signed them, before any presentation.

    Issuance and presentation are separate because a human signs an open mandate once and the
    agent presents it repeatedly. Re-issuing per attempt would give each presentation a fresh
    digest and silently reset every per-mandate counter.
    """

    open_checkout: str
    open_payment: str
    open_checkout_digest: str
    spec: MandateSpec
    principals: Principals


@dataclass
class Presentation:
    """A full credential set, ready to hand to the merchant."""

    credentials: PresentedCredentials
    open_checkout_digest: str
    nonce: str
    spec: MandateSpec
    principals: Principals


def issue_open_mandates(
    principals: Principals,
    spec: MandateSpec,
    *,
    tamper: Tamper | None = None,
    now: datetime | None = None,
) -> IssuedMandates:
    """Sign the pair of open mandates once, as a trusted surface would."""
    tamper = tamper or Tamper()
    moment = now or utcnow()
    # Token-level tampering happens before the digest is taken, so the payment mandate's reference
    # constraint still points at the token that is actually presented. Otherwise every one of these
    # techniques would refuse on the reference mismatch instead of on what it means to test.
    open_checkout = _apply_token_tamper(
        build_open_checkout_mandate(principals, spec, tamper=tamper, now=moment), tamper
    )
    open_checkout_digest = digest_of(open_checkout)
    open_payment = _apply_token_tamper(
        build_open_payment_mandate(
            principals, spec, open_checkout_digest=open_checkout_digest, tamper=tamper, now=moment
        ),
        tamper,
    )
    return IssuedMandates(
        open_checkout=open_checkout,
        open_payment=open_payment,
        open_checkout_digest=open_checkout_digest,
        spec=spec,
        principals=principals,
    )


def present_issued(
    issued: IssuedMandates,
    *,
    checkout_jwt: str,
    checkout_hash: str,
    amount_minor: int,
    audience: str | None = None,
    nonce: str = "dwarpal-nonce-1",
    tamper: Tamper | None = None,
    now: datetime | None = None,
    human_present: bool = False,
) -> Presentation:
    """Present already-issued open mandates against one specific merchant Checkout."""
    tamper = tamper or Tamper()
    moment = now or utcnow()
    aud = audience or settings.PUBLIC_BASE_URL
    principals = issued.principals
    spec = issued.spec
    open_checkout = issued.open_checkout
    open_payment = issued.open_payment
    open_checkout_digest = issued.open_checkout_digest

    binder = generate_keypair("stranger#key") if tamper.wrong_agent_key else principals.agent
    kb_iat = int(moment.timestamp()) - tamper.key_binding_age_seconds
    kb_audience = tamper.key_binding_audience or aud
    kb_nonce = tamper.key_binding_nonce or nonce
    if not tamper.drop_key_binding:
        open_checkout = attach_key_binding(
            open_checkout, binder, audience=kb_audience, nonce=kb_nonce, issued_at=kb_iat
        )
        open_payment = attach_key_binding(
            open_payment, binder, audience=kb_audience, nonce=kb_nonce, issued_at=kb_iat
        )

    if tamper.checkout_jwt_from_stranger:
        checkout_jwt, checkout_hash = _resign_checkout_as_stranger(checkout_jwt)

    closed_checkout = build_closed_checkout_mandate(
        principals,
        checkout_jwt=checkout_jwt,
        checkout_hash=checkout_hash,
        tamper=tamper,
        now=moment,
    )
    bound_hash = tamper.altered_checkout_hash or checkout_hash
    closed_payment = build_closed_payment_mandate(
        principals,
        spec,
        transaction_id=bound_hash,
        amount_minor=amount_minor,
        tamper=tamper,
        now=moment,
    )

    presence = (
        issue_presence_attestation(
            principals, checkout_hash=checkout_hash, tamper=tamper, now=moment
        )
        if human_present
        else None
    )

    return Presentation(
        credentials=PresentedCredentials(
            open_checkout=open_checkout,
            closed_checkout=closed_checkout,
            open_payment=open_payment,
            closed_payment=closed_payment,
            nonce=nonce,
            presence=presence,
        ),
        open_checkout_digest=open_checkout_digest,
        nonce=nonce,
        spec=spec,
        principals=principals,
    )


def present(
    principals: Principals,
    spec: MandateSpec,
    *,
    checkout_jwt: str,
    checkout_hash: str,
    amount_minor: int,
    audience: str | None = None,
    nonce: str = "dwarpal-nonce-1",
    tamper: Tamper | None = None,
    now: datetime | None = None,
    human_present: bool = False,
) -> Presentation:
    """Issue and present in one step, for a single-shot purchase."""
    issued = issue_open_mandates(principals, spec, tamper=tamper, now=now)
    return present_issued(
        issued,
        checkout_jwt=checkout_jwt,
        checkout_hash=checkout_hash,
        amount_minor=amount_minor,
        audience=audience,
        nonce=nonce,
        tamper=tamper,
        now=now,
        human_present=human_present,
    )


def spec_for_cart(
    lines: list[tuple[str, str, int]],
    *,
    amount_cap_minor: int = 5_000_000,
    natural_language: list[str] | None = None,
    **kwargs: Any,
) -> MandateSpec:
    """Build a mandate that authorises exactly the given (sku, title, quantity) cart."""
    return MandateSpec(
        line_items=[
            line_item_requirement(f"req-{index + 1}", [(sku, title)], quantity)
            for index, (sku, title, quantity) in enumerate(lines)
        ],
        amount_cap_minor=amount_cap_minor,
        natural_language=natural_language or [],
        **kwargs,
    )


def expires_at(seconds: int) -> str:
    return (utcnow() + timedelta(seconds=seconds)).isoformat()


def sign_confirmation(
    principals: Principals,
    *,
    escalation_id: str,
    checkout_hash: str,
    decision: str,
    now: datetime | None = None,
) -> str:
    """The answer a present person gives to an escalation, signed by the surface they are at.

    An approval has to be something only the human could have produced. A boolean in the request
    body would be an approval the agent grants itself.
    """
    moment = now or utcnow()
    issued_at = int(moment.timestamp())
    claims = {
        "iss": principals.issuer_id,
        "escalation_id": escalation_id,
        "checkout_hash": checkout_hash,
        "decision": decision,
        "iat": issued_at,
        "exp": issued_at + 900,
    }
    return sign_jws(claims, principals.issuer, typ=CONFIRMATION_JWT_TYP)
