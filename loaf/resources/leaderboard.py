"""Competition leaderboard (public)."""

from __future__ import annotations

from typing import Any

from .base import Resource


class LeaderboardResource(Resource):
    def get(self) -> Any:
        """``GET /leaderboard`` — the current competition leaderboard. (public)

        Returns ``{roundNumber, roundName, roundRules, roundStatus,
        bottomCullPercent, volumeMultiplierTiers, entries, totalParticipants,
        newAssetProperty}``, where ``bottomCullPercent`` is the share (0-100) of
        lowest-ranked participants dropped when the round ends. An entry is
        ``rank``, ``handle``, ``walletAddress`` and ``points`` (``points`` is
        rounded to two decimals). Serves a live board while a round is ``ACTIVE``
        or the last round's frozen snapshot between rounds. Raises 404 when no
        round/snapshot exists.

        ``entries`` is truncated to the top N the server is configured to serve,
        so take the participant count from ``totalParticipants`` (everyone
        scored on the full board) rather than ``len(entries)``, and your OWN
        standing from ``competition.queue_position()['leaderboardPosition']`` —
        that ranks you against the full board, so it is set even when you place
        below the served cut.

        For live updates, prefer subscribing to the ``leaderboard`` WebSocket
        channel (:meth:`loaf.ws.client.LoafWebSocketClient.subscribe_leaderboard`)
        over polling — the server pushes a ``leaderboard_update`` whenever the
        board changes.
        """
        return self._client.get("/leaderboard", auth=False)
