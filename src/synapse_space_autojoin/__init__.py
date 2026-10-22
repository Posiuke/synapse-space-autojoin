"""Space membership propagation using Synapse's public module API."""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

from synapse.api.errors import SynapseError
from synapse.config import ConfigError
from synapse.module_api import run_as_background_process
from synapse.types import UserID

logger = logging.getLogger(__name__)
MEMBER = "m.room.member"
CREATE = "m.room.create"
CHILD = "m.space.child"
PARENT = "m.space.parent"


class SpaceAutoJoin:
    """Invite, then join local users to the verified children of a Space."""

    @staticmethod
    def parse_config(config: dict[str, Any]) -> dict[str, Any]:
        allowed = {"inviter", "recursive", "max_rooms"}
        if set(config) - allowed:
            raise ConfigError(f"Unknown space-autojoin options: {set(config) - allowed}")
        inviter = config.get("inviter")
        if not isinstance(inviter, str) or not UserID.is_valid(inviter):
            raise ConfigError("space-autojoin: inviter must be a full Matrix user ID")
        recursive = config.get("recursive", True)
        if type(recursive) is not bool:
            raise ConfigError("space-autojoin: recursive must be a boolean")
        max_rooms = config.get("max_rooms", 1000)
        if type(max_rooms) is not int or max_rooms < 1:
            raise ConfigError("space-autojoin: max_rooms must be a positive integer")
        return {"inviter": inviter, "recursive": recursive, "max_rooms": max_rooms}

    def __init__(self, config: dict[str, Any], api: Any):
        self.api = api
        self.config = self.parse_config(config)
        self.inviter = self.config["inviter"]
        if not api.is_mine(self.inviter):
            raise ConfigError("space-autojoin: inviter must belong to this homeserver")
        self._pending: set[tuple[str, str]] = set()
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

    @staticmethod
    def _content(state: Any, event_type: str, key: str = "") -> Any:
        event = state.get((event_type, key))
        return event.content if event is not None else {}

    def _membership(self, state: Any, user: str) -> str | None:
        return self._content(state, MEMBER, user).get("membership")

    async def _state(self, room: str, user: str) -> Any:
        return await self.api.get_room_state(
            room,
            [(CREATE, ""), (CHILD, None), (PARENT, None),
             (MEMBER, user), (MEMBER, self.inviter)],
        )

    async def on_new_event(self, event: Any, state_events: Any) -> None:
        if event.type != MEMBER or event.content.get("membership") != "join":
            return
        user = event.state_key
        if user == self.inviter or not self.api.is_mine(user):
            return
        # Profile changes also produce membership=join events.
        if event.unsigned.get("prev_content", {}).get("membership") == "join":
            return
        if self._content(state_events, CREATE).get("type") != "m.space":
            return
        key = (event.room_id, user)
        if key in self._pending:
            return
        self._pending.add(key)
        run_as_background_process("space_autojoin", self._sync, event.room_id, user)

    async def _sync(self, root: str, user: str) -> None:
        owned = {(root, user)}
        try:
            # Leave the event-persistence callback before generating new events.
            await self.api.sleep(0)
            queue = deque([root])
            visited = {root}
            while queue:
                parent = queue.popleft()
                state = await self._state(parent, user)
                if self._membership(state, user) != "join":
                    continue
                if self._membership(state, self.inviter) != "join":
                    logger.warning("Skipping Space %s: inviter is not a member", parent)
                    continue
                children = sorted(
                    key for (kind, key), event in state.items()
                    if kind == CHILD and isinstance(event.content.get("via"), (list, tuple))
                    and event.content["via"]
                )
                for child in children:
                    if child in visited:
                        continue
                    if len(visited) - 1 >= self.config["max_rooms"]:
                        logger.warning("Room limit reached for %s in %s", user, root)
                        return
                    visited.add(child)
                    # Stop if the user has left the originating Space meanwhile.
                    if self._membership(await self._state(root, user), user) != "join":
                        return
                    try:
                        child_state = await self._state(child, user)
                        # A Space may list arbitrary rooms. Require a backlink from
                        # the child before granting access through this relationship.
                        if not self._content(child_state, PARENT, parent).get("via"):
                            logger.warning("Skipping %s: no backlink to %s", child, parent)
                            continue
                        key = (child, user)
                        if key not in self._pending:
                            self._pending.add(key)
                            owned.add(key)
                        joined = await self._join(child, user, child_state)
                        if (joined and self.config["recursive"] and
                                self._content(child_state, CREATE).get("type") == "m.space"):
                            queue.append(child)
                    except Exception:
                        # A private/unavailable child must not block other children.
                        logger.exception("Could not auto-join %s to %s", user, child)
        except Exception:
            logger.exception("Space autojoin failed for %s in %s", user, root)
        finally:
            self._pending.difference_update(owned)

    async def _join(self, room: str, user: str, state: Any) -> bool:
        membership = self._membership(state, user)
        if membership == "join":
            return True
        # Preserve explicit leaves, kicks and bans, including on Space re-entry.
        if membership in {"leave", "ban", "knock"}:
            logger.info("Skipping %s in %s: membership=%s", user, room, membership)
            return False
        if self._membership(state, self.inviter) != "join":
            logger.warning("Skipping %s: inviter is not a member", room)
            return False
        if membership != "invite":
            await self._update_membership(
                sender=self.inviter, target=user, room_id=room, new_membership="invite"
            )
        # No global auto_accept_invites is needed. Synapse checks normal join rules.
        try:
            await self._update_membership(
                sender=user, target=user, room_id=room, new_membership="join"
            )
        except Exception:
            # Another module may already have accepted the invitation.
            if self._membership(await self._state(room, user), user) != "join":
                raise
        logger.info("Auto-joined %s to %s", user, room)
        return True

    async def _update_membership(self, **kwargs: Any) -> Any:
        for attempt in range(4):
            try:
                return await self.api.update_room_membership(**kwargs)
            except SynapseError as error:
                if attempt == 3 or (error.code != 429 and error.code < 500):
                    raise
                retry_ms = getattr(error, "retry_after_ms", None)
                delay = max(1.0, retry_ms / 1000) if retry_ms is not None else 2 ** attempt
                logger.warning("Retrying membership update for %s in %s after %.1fs",
                               kwargs["target"], kwargs["room_id"], delay)
                await self.api.sleep(delay)
