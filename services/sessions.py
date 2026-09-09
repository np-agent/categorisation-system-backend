"""
Session revocation.

Deactivating an account or an organisation has to bite immediately, including
for people who are already signed in. The request-time gate in api/deps.py
rejects their API calls, but their SuperTokens session would otherwise stay
valid; clearing it forces the browser back to the login screen.
"""

import logging
from typing import Iterable

from supertokens_python.recipe.session.asyncio import revoke_all_sessions_for_user

logger = logging.getLogger(__name__)


async def revoke_sessions(supertokens_user_ids: Iterable[str]) -> None:
    """
    Best-effort revocation. A failure here is logged rather than raised: the
    deactivation itself has already been persisted, and the access gate blocks
    the user regardless of whether their stale session was cleaned up.
    """
    for st_user_id in supertokens_user_ids:
        if not st_user_id:
            continue
        try:
            await revoke_all_sessions_for_user(st_user_id)
        except Exception:
            logger.exception(
                "Could not revoke sessions for SuperTokens user %s", st_user_id
            )
