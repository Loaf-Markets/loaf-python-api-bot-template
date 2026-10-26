"""Competition leaderboard (public)."""

from __future__ import annotations

from typing import Any

from .base import Resource


class LeaderboardResource(Resource):
    def get(self) -> Any:
        """``GET /leaderboard`` — the current competition leaderboard. (public)

        Returns ``{roundNumber, roundName, roundRules, roundStatus,
        bottomCullPercent, volumeMultiplierTiers, volumeExponent, pnlMultiplier,
        entries, totalParticipants, newAssetMarket}``, where
        ``bottomCullPercent`` is the share (0-100) of lowest-ranked participants
        dropped when the round ends. An entry is ``rank``, ``handle``,
        ``walletAddress`` and ``points``, rounded to two decimals:
        ``a * volume ** volumeExponent + pnlMultiplier * pnl``, where ``a`` is
        the multiplier of the highest ``volumeMultiplierTiers`` tier the volume
        reaches (1 below the first).

        The board is live while a round is ``ACTIVE``, and while it is
        ``ENDING`` until its board is frozen; then it is the newest frozen board.
        During the next round's PREPARED that is still the previous round's, so
        check ``roundNumber``. Raises :class:`~loaf.exceptions.LoafNotFoundError`
        (404 ``No active competition round``) when there is no board.

        ``entries`` is truncated to the top N the server is configured to serve,
        so take the participant count from ``totalParticipants`` (everyone
        scored on the full board) rather than ``len(entries)``, and your OWN
        standing from ``competition.queue_position()['leaderboardPosition']`` —
        that ranks you against the full board, so it is set even when you place
        below the served cut.

        ``totalParticipants`` can be ABSENT between rounds. The break-period
        board is the round's snapshot replayed as it was frozen, so a round
        frozen before the field existed has no such key — read it defensively
        and treat a miss as unknown, NOT as ``len(entries)`` (which is the
        served cut, not the field's meaning)::

            board = loaf.leaderboard.get()
            total = board.get("totalParticipants")   # None on such a snapshot

        For live updates, prefer subscribing to the ``leaderboard`` WebSocket
        channel (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_leaderboard`)
        over polling — the server pushes a ``leaderboard_update`` whenever the
        board changes (no frame on subscribe: seed from this read).
        """
        return self._client.get("/leaderboard", auth=False)
