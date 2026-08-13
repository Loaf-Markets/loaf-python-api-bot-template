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

        Returns ``{rounds, featuredRound, makerFeeBps, takerFeeBps, queueCount}``.
        ``rounds`` is a per-round summary list (``roundNumber``, ``name``,
        ``startsAt``/``endsAt``, ``status``, ``totalPrizePool``,
        ``participantBatchSize``); ``featuredRound`` (or ``null``) adds
        ``rules``, ``startingBalanceUsdl``, ``prizePool``,
        ``volumeMultiplierTiers`` and the round's headline ``newAssetProperty``.
        Round ``status`` is one of :class:`~loaf.enums.CompetitionRoundStatus`.
        """
        return self._client.get("/competition", auth=False)

    def queue_position(self) -> Any:
        """``GET /competition/queue-position`` — your own competition standing.

        Returns ``{position, queueCount, leaderboardPosition, referralCount,
        priorityBoostPlaces, maxBoostsPerUser}``. ``leaderboardPosition`` is
        your rank on the board currently being served — live while a round is
        ``ACTIVE``, your final placement in the round that just ended during
        the break. It ranks you against the FULL board, so it is set even when
        you place below the cut ``leaderboard.get()`` serves, and is ``None``
        when you are not on the board at all. ``position`` is your place in the
        admission queue. Usually only one of the two is set (an admitted trader
        is not queued), but both are during the break if you were bottom-culled
        — back in the queue and still on the frozen board.
        """
        return self._client.get("/competition/queue-position")

    def submit_payout_details(
        self, *, wallet_address: str | None = None, email: str | None = None
    ) -> Any:
        """``POST /competition/payout-details`` — nominate where prize money goes.

        For competition WINNERS only (403 otherwise). Provide EXACTLY ONE of
        ``wallet_address`` (an on-chain address) or ``email``. Re-submitting
        overwrites the prior choice. Returns ``{eligible, roundNumber, place,
        submission}``.
        """
        if (wallet_address is None) == (email is None):
            raise _client_validation_error(
                "Provide exactly one of wallet_address or email"
            )
        return self._client.post(
            "/competition/payout-details",
            json={"walletAddress": wallet_address, "email": email},
        )
