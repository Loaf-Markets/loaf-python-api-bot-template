"""Trading competition: rounds info, your queue position, prize payout details.

While a competition round is ACTIVE, order placement is restricted to admitted
participants (a non-admitted account gets
:class:`~loaf.exceptions.CompetitionEligibilityError`), so a bot should check
its standing here. Outside an active round trading is unrestricted.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import _client_validation_error
from .base import Resource


class CompetitionResource(Resource):
    def info(self) -> Any:
        """``GET /competition`` — public competition overview.

        Returns ``{nextRoundSoon, rounds, makerFeeBps, takerFeeBps}``.
        ``nextRoundSoon`` is the operator's "a round is coming" announcement
        flag; ``makerFeeBps``/``takerFeeBps`` are the base (lowest volume tier)
        fees. ``rounds`` is EVERY round in full, newest first (``None`` when
        none exist), each with ``roundNumber``, ``name``, ``rules``,
        ``startsAt``/``endsAt``, ``startingBalanceUsdl``,
        ``participantBatchSize`` (how many the queue admits into the round),
        ``status``, ``newAssetProperty`` (the round's headline asset, or
        ``None``), ``prizePool``, ``volumeMultiplierTiers``,
        ``bottomCullPercent`` and ``winners``. See
        :class:`~loaf.models.CompetitionRound`.

        Every round is listed, including ones not yet started and ones long
        finished, so pick the round you mean by ``status`` rather than by
        position — the live one is the (at most one) round with status
        ``ACTIVE``; ``status`` is one of
        :class:`~loaf.enums.CompetitionRoundStatus`::

            rounds = loaf.competition.info().rounds or []
            live = next((r for r in rounds if r.status == "ACTIVE"), None)

        A ``prizePool`` entry is ``{place, amount}`` or, for a BAND,
        ``{place, toPlace, amount}`` — ``toPlace`` is inclusive and ``amount``
        is the whole-USDC prize EACH participant in the band receives, not the
        band's total. So test a finishing place with a range check, never
        equality::

            def prize_for(pool, place):
                for e in pool:
                    if e.place <= place <= e.get("toPlace", e.place):
                        return e.amount
                return None

        ``winners`` names the holder (``place``, ``handle``, ``walletAddress``)
        of each paid place in the top 10 off the round's frozen final board once
        it is ``ENDING``/``ENDED`` — ``None`` while the round is still running,
        and while a finished round's results are pending. It stops at 10 even
        when the pool pays deeper, so it is a podium, not the full paid list.

        This is display-only round metadata behind a short server-side cache,
        and the payload grows with every round ever run — read it when you need
        it, don't poll it on a trading loop. The backend does not distribute
        prizes; see :meth:`submit_payout_details`.
        """
        return self._client.get("/competition", auth=False)

    def queue_position(self) -> Any:
        """``GET /competition/queue-position`` — your own competition standing.

        Returns ``{position, queueCount, leaderboardPosition, referralCount,
        estimatedRoundEntry}``. ``leaderboardPosition`` is your rank on the
        board currently being served — live while a round is ``ACTIVE``, your
        final placement in the round that just ended during the break. It ranks
        you against the FULL board, so it is set even when you place below the
        cut ``leaderboard.get()`` serves, and is ``None`` when you are not on
        the board at all. ``position`` is your place in the admission queue and
        ``estimatedRoundEntry`` the round number that position is on track to
        get you into (both ``None`` once you are admitted, or when you are not
        eligible to queue). Usually only ``position`` or ``leaderboardPosition``
        is set (an admitted trader is not queued), but both are during the break
        if you were bottom-culled — back in the queue and still on the frozen
        board.

        Raises :class:`~loaf.exceptions.LoafServiceUnavailableError` (503) while
        the shared queue snapshot is being rebuilt — transient, so retry.
        """
        return self._client.get("/competition/queue-position")

    def payout_details(self) -> Any:
        """``GET /competition/payout-details`` — have you claimed your prize yet?

        The read-only counterpart of :meth:`submit_payout_details`, for the
        most-recently ENDED round. Returns ``{eligible, roundNumber, place,
        submission}``: a non-``None`` ``submission`` (``payoutType``,
        ``walletAddress``, ``email``, ``submittedAt``) means the prize is
        already claimed and a further POST would 409; ``None`` means still
        claimable. Not being a winner — or no round having ended — answers 200
        with ``eligible: False`` rather than raising.
        """
        return self._client.get("/competition/payout-details")

    def submit_payout_details(
        self, *, wallet_address: str | None = None, email: str | None = None
    ) -> Any:
        """``POST /competition/payout-details`` — nominate where prize money goes.

        For winners of the most-recently ENDED round only. Provide EXACTLY ONE
        of ``wallet_address`` (an on-chain address) or ``email``. Returns
        ``{eligible, roundNumber, place, submission}``.

        A claim is ONE-SHOT — the first nomination stands, and re-submitting
        raises :class:`~loaf.exceptions.LoafConflictError` (409) instead of
        overwriting it (changing it is a support action). Read the stored one
        back with :meth:`payout_details`. Other failures:
        :class:`~loaf.exceptions.LoafForbiddenError` (403) when you did not
        place in a prize position, ``LoafConflictError`` (409) when no round has
        ended, and :class:`~loaf.exceptions.LoafServiceUnavailableError` (503)
        while the ended round's final results are still being frozen (retry).
        """
        if (wallet_address is None) == (email is None):
            raise _client_validation_error(
                "Provide exactly one of wallet_address or email"
            )
        return self._client.post(
            "/competition/payout-details",
            json={"walletAddress": wallet_address, "email": email},
        )
