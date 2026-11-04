"""Withdrawals: USDC from your spot balance to your own wallet.

``loaf.funds.withdraw(250)`` sends 250 USDC from your spot balance to the
wallet your exchange account belongs to: the address whose first deposit
opened the account. Nothing else can receive it. The withdrawal your trading
key signs names no recipient, and the exchange's contract pays a withdrawal
of your account to that wallet alone, so a signed withdrawal can only ever pay
you, whoever sends it. Keep the trading key secret all the same: it trades
your account.

**What happens.** The exchange journals the withdrawal at once and takes it
from your spot balance (the 202, :class:`~loaf.models.WithdrawalAccepted`),
then settles it on chain in a batch, whose transaction makes it payable to
your wallet, and pays it there. Follow it in
``portfolio.component().transfers``: a ``WITHDRAWAL`` row with its
``journalSeq``, ``PENDING`` until its batch is on chain, then ``CONFIRMED``
with that batch's ``blockchainTxHash``, or ``REJECTED`` if settlement refused
it, which leaves the amount in your account. The WebSocket's
:meth:`~loaf.ws.client.LoafWebSocketClient.on_transfer` pushes each change.

**Units.** ``amount`` is in USDC (``12.5`` is 12.5 USDC). The exchange moves
whole ticks of it: the SDK turns it into ticks with the collateral your
portfolio summary serves, as a perps allocate does
(:meth:`~loaf.resources.perps.PerpsResource.collateral`: ticks = amount x
10^``decimals`` / ``tickSize``), and refuses an amount that is not a whole
number of ticks (nothing is rounded). The 202's ``amount`` is that count of
ticks; a transfer row's ``amount`` is in USDC's base units.

**Signing.** A withdrawal is signed on your machine with your trading key, as
an order is, and sent with a login token your trading key signs (the exchange
checks the account is yours). It spends your account's next nonce, the one
counter perps allocates and exits spend too: the SDK reads it
(``GET /l2/keys``) for each one and sends one at a time per client. A
withdrawal's number is its nonce, so an op sent elsewhere (the web app,
another process) between that read and the send makes the exchange refuse
this one as it refuses an order on a used number: 422, ``code``
``ORDER_INDEX_USED`` (``l2 money: the nonce is not the api key's nonce …``).
Withdraw again: the SDK reads the new nonce. A signature or key the exchange
refuses is 401 ``INVALID_SIGNATURE``, as an order's
(:class:`~loaf.exceptions.LoafAuthError`): your trading key is not the one in
force on the account.

**Refusals** answer as an order's do: each carries a ``code``
(:attr:`~loaf.exceptions.LoafAPIError.code`, one of
:class:`~loaf.enums.OrderErrorCode`), and the exception class follows from the
status and the code. Branch on them, never on the message.
:meth:`FundsResource.withdraw` lists them; none journaled anything.

**Retries.** As for orders: a timeout or a 5xx re-sends the same signed body,
which the exchange journals once at most (it answers a repeat of a journaled
one ``duplicate: True``). A 503 ``NOT_PROCESSED`` or ``TRADING_PAUSED``, or
one that says nothing was done, journaled nothing: re-sent like any 5xx, it is
raised as itself when no earlier attempt may have landed. A re-send refused
422 ``ORDER_INDEX_USED`` is raised as itself even after an attempt whose answer
was lost: the exchange answers a re-send of a journaled withdrawal as its
duplicate before it checks the nonce, so no attempt was journaled, and none
will be (its nonce went to another op): withdraw again. Any error other than
:class:`~loaf.exceptions.WithdrawalOutcomeUnknownError` means nothing was
journaled; on that one, ``resubmit(error.signed_body)``, and never sign the
same withdrawal afresh while it may have been journaled.

**Deposits** are made in the web app: a deposit is signed by your wallet's
own key, which the SDK never holds.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from .. import k1
from .._l2 import _is_int, submit_signed_op
from ..constants import (
    DEFAULT_WITHDRAWAL_TTL_MS,
    ENV_TRADING_KEY,
    L2_BUFFER_MS,
    MAX_WITHDRAWAL_TTL_MS,
    MIN_ORDER_TTL_MS,
    PERPS_QUOTE_ASSET_ID,
)
from ..exceptions import LoafConfigError, _client_validation_error
from ..money import _to_decimal
from .base import Resource
from .perps import _ranged, _ticks

if TYPE_CHECKING:
    from .._l2 import TradingKey

_WITHDRAW_LIMIT = 2**53  # a withdrawal's amount, in ticks, is below it
_SPOT_ROUTE = 1  # the one route a withdrawal takes: your spot balance
_SIGNED_STRS = ("amount", "l2SigS", "l2SigE")
_SIGNED_INTS = (
    "l2ChainId",
    "l2AccountIndex",
    "l2ApiKeyIndex",
    "l2AssetId",
    "l2Route",
    "l2Nonce",
    "l2ExpiryMs",
)


class FundsResource(Resource):
    def withdraw(self, amount: float | Decimal, *, ttl_ms: int | None = None) -> Any:
        """``POST /l2/withdrawals`` — withdraw ``amount`` USDC from your spot
        balance to your own wallet, signed with your trading key.

        Every refusal by the exchange carries ``code``
        (:class:`~loaf.enums.OrderErrorCode`), as an order's does, and
        journaled nothing: withdraw again once its cause is gone.

        Args:
            amount: USDC (a number; a ``Decimal`` for exact cents): a whole
                number of the collateral's ticks (see the module's Units),
                from one tick to 2^53 - 1 of them. Checked once the
                collateral is read; nothing is rounded.
            ttl_ms: how long the exchange takes the signed withdrawal, in
                whole milliseconds, from 60,000 to 86,400,000 (default
                3,600,000, one hour). It rests nowhere: the exchange journals
                it at once or refuses it. It signs ``l2ExpiryMs`` = now + 72
                hours (:data:`~loaf.constants.L2_BUFFER_MS`, the time it
                stays valid for settlement) + ``ttl_ms``.

        Returns:
            ``{journalSeq, accountIndex, assetId, amount, nonce, duplicate,
            status}`` (:class:`~loaf.models.WithdrawalAccepted`): the exchange
            journaled it at ``journalSeq`` and took ``amount`` ticks from your
            spot balance (``duplicate``: a re-send of one journaled before).

        Raises:
            LoafConfigError: no trading key.
            LoafAPIError: 422, refused by the exchange's rules (this class
                itself; ``code`` says which): ``INSUFFICIENT_BALANCE``, more
                than your spot USDC available (your resting orders hold some);
                ``BELOW_MIN_ORDER_VALUE``, less than the minimum withdrawal;
                ``RATE_LIMITED``, too many withdrawals not yet settled on
                chain, yours or everyone's (withdraw again later);
                ``ORDER_INDEX_USED``, the nonce, spent first by an op sent
                elsewhere (withdraw again: the SDK reads the next one), also
                after an attempt whose answer was lost: none was journaled;
                ``ORDER_EXPIRY_INVALID``, ``INVALID_REQUEST`` or
                ``UNKNOWN_REASON``, the message says why.
            LoafRateLimitError: 429 ``RATE_LIMITED``, your account's request
                rate (the withdrawals not yet settled are the 422 above):
                withdraw again after ``retry_after`` seconds.
            LoafAuthError: 401. ``UNAUTHENTICATED``: the exchange refused the
                login token; the message says why after the exchange's text
                (this machine's clock, or your trading key not the one in force
                on its account, a key of another deployment included, refused
                at the nonce read). ``INVALID_SIGNATURE``: it refused the
                withdrawal's own signature or key: your trading key is not the
                one in force on its account, which the message says.
            LoafForbiddenError: 403 ``ORDER_NOT_OWNED``: the account is not
                the login's.
            LoafValidationError: refused locally (``status_code`` 0, nothing
                sent) or by the exchange (400): ``ORDER_EXPIRY_INVALID``, its
                expiry inside the buffer (this machine's clock is behind), or
                ``INVALID_REQUEST``, another L2 chain or a body the exchange
                does not take (``details`` names the fields).
            TradingPausedError: 503 ``TRADING_PAUSED``, the exchange paused:
                send it again once it resumes (``retry_after``).
            LoafServiceUnavailableError: 503 ``NOT_PROCESSED``, the exchange
                did not process it, or ``SERVICE_UNAVAILABLE`` saying nothing
                was done (your account unread before anything was sent on),
                after the SDK's re-sends: withdraw again. Or the collateral
                could not be read just now: nothing was sent.
            LoafServerError: your summary has no usable collateral.
            WithdrawalOutcomeUnknownError: it may or may not have been
                journaled (a timeout, a 503 ``SERVICE_UNAVAILABLE`` that the
                exchange did not decide, or any refusal of a re-send after an
                attempt whose answer was lost but ``ORDER_INDEX_USED``):
                :meth:`resubmit` it, never sign it afresh.
        """
        key = self._credentials()
        value = _to_decimal(amount, "amount")
        if value <= 0:
            raise _client_validation_error(f"amount must be positive (got {amount!r})")
        ttl = _withdrawal_ttl(ttl_ms)
        ticks = _ticks(value, amount, self._client.perps._collateral(), _WITHDRAW_LIMIT)

        def message(chain_id: int, account_index: int, nonce: int, expiry_ms: int) -> bytes:
            return k1.withdraw_message(
                l2_chain_id=chain_id,
                l2_account_index=account_index,
                l2_asset_id=PERPS_QUOTE_ASSET_ID,
                amount=ticks,
                l2_nonce=nonce,
                l2_expiry_ms=expiry_ms,
            )

        terms = {
            "l2ApiKeyIndex": 0,
            "l2AssetId": PERPS_QUOTE_ASSET_ID,
            "l2Route": _SPOT_ROUTE,
            "amount": str(ticks),
        }
        lead = L2_BUFFER_MS + ttl
        return submit_signed_op(
            self._client, key, "/l2/withdrawals", terms, lead, message, "withdrawal"
        )

    def resubmit(self, signed_body: Mapping[str, Any]) -> Any:
        """Re-send a signed withdrawal exactly as it was (the ``signed_body`` of
        a :class:`~loaf.exceptions.WithdrawalOutcomeUnknownError`).

        A 202 settles it: ``duplicate: True``, journaled before; otherwise
        journaled now. A 422 ``ORDER_INDEX_USED`` settles it too, raised as
        itself (a :class:`~loaf.exceptions.LoafAPIError`): the exchange answers
        a re-send of a journaled withdrawal as its duplicate first, so this one
        was never journaled and never will be (its nonce went to another op):
        withdraw again. Anything else raises ``WithdrawalOutcomeUnknownError``
        again, with the reason in ``last_error``. The exchange takes the body
        until its ``l2ExpiryMs`` less 72 hours, by its own clock; once it
        refuses a re-send for that (``ORDER_EXPIRY_INVALID``), your account's
        next nonce settles it (``GET /l2/keys``' ``withdrawalNonce``): still
        the body's ``l2Nonce``, it was never journaled and never will be; past
        it, this withdrawal or another op you signed at that nonce was. Needs
        your trading key, for the login token: it signs nothing new.
        """
        body = signed_body if isinstance(signed_body, Mapping) else {}
        if not (
            all(isinstance(body.get(field), str) for field in _SIGNED_STRS)
            and all(_is_int(body.get(field)) for field in _SIGNED_INTS)
        ):
            raise _client_validation_error(
                "resubmit needs a signed withdrawal body (its fields and signature), e.g. "
                "WithdrawalOutcomeUnknownError.signed_body"
            )
        return self._client._send_signed(
            "/l2/withdrawals", dict(body), ambiguous=True, op="withdrawal"
        )

    def _credentials(self) -> TradingKey:
        """The trading key that signs the withdrawal and the login tokens that carry it."""
        key = self._client._l2_key
        if key is None:
            raise LoafConfigError(
                f"Withdrawing needs your trading key: set ${ENV_TRADING_KEY} to the web app's "
                "Show trading key (README §2), or pass trading_key=..."
            )
        return key


def _withdrawal_ttl(ttl_ms: Any) -> int:
    """``ttl_ms=`` of a withdrawal checked, or the default when omitted."""
    if ttl_ms is None:
        return DEFAULT_WITHDRAWAL_TTL_MS
    return _ranged(
        ttl_ms, "ttl_ms", MIN_ORDER_TTL_MS, MAX_WITHDRAWAL_TTL_MS, "a number of milliseconds"
    )
