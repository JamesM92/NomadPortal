"""Who may browse, submit forms to, or download from which node.

One module owns the rule so the route handlers, the page renderer and
the browser all ask the same question. Nothing here imports Flask: a
policy is built from plain values (the viewer's role, the operator's
settings, the hosted-site hash, a blocklist predicate), so it can be
tested with no app, no login and no network.

Rules:

- An empty node hash is always denied.
- ``super_admin`` is always allowed.
- ``admin`` / ``user``: allowed everywhere, unless that role's own
  default-lock is on, then only the *allowed set* (the hosted site plus
  the operator's prescribed nodes).
- ``guest``, browse / download: same shape, using the guest lock.
- ``guest``, interact: ALWAYS confined to the allowed set, whatever the
  lock says. Guests may browse anywhere when the lock is off, but can
  only submit forms on the operator's own nodes. This is long-standing
  behaviour, kept on purpose and pinned by
  ``tests/test_access_characterisation.py``.
- The node blocklist is checked after the role rule and applies to
  everyone, super admins included.

A denied guest gets 401 (signing in could help); any other denied
viewer gets 403.
"""

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

ROLES = ("guest", "user", "admin", "super_admin")

BROWSE = "browse"
INTERACT = "interact"
DOWNLOAD = "download"

BLOCKED_MESSAGE = "This node has been blocked by the administrator."

# (message for a guest asked to log in, message for a signed-in viewer)
_MESSAGES = {
    BROWSE:   ("Login required to browse external nodes",
               "Browsing external nodes is restricted by the administrator"),
    INTERACT: ("Login required to submit forms on external nodes",
               "Form submissions to external nodes are restricted by the administrator"),
    DOWNLOAD: ("Login required to fetch files from external nodes",
               "Fetching from external nodes is restricted by the administrator"),
}


def role_of(user) -> str:
    """Map a flask_login-style user (or anonymous user) to a role."""
    if getattr(user, "super_admin", False):
        return "super_admin"
    if getattr(user, "is_admin", False):
        return "admin"
    if getattr(user, "is_authenticated", False):
        return "user"
    return "guest"


@dataclass(frozen=True)
class Denial:
    status: int
    error: str


def _norm(node_hash) -> str:
    return (node_hash or "").strip().lower()


@dataclass(frozen=True)
class PolicyConfig:
    """Everything the policy needs, as plain values.

    ``prescribed`` is the operator's list of nodes visitors are steered
    to; the first entry is the landing page. ``hosted`` is this
    portal's own site. ``locks`` maps a role to its default-lock flag.
    """
    prescribed: tuple = ()
    hosted: str = ""
    locks: Mapping = field(default_factory=dict)
    is_blocked: Callable[[str], bool] = lambda _h: False

    @property
    def allowed(self) -> frozenset:
        return frozenset(h for h in (_norm(self.hosted),
                                     *(_norm(p) for p in self.prescribed)) if h)

    @property
    def landing(self) -> Optional[str]:
        return _norm(self.prescribed[0]) if self.prescribed else None

    @classmethod
    def from_settings(
        cls,
        settings: Mapping,
        hosted: str = "",
        is_blocked: Callable[[str], bool] = lambda _h: False,
    ) -> "PolicyConfig":
        """Build from the stored UI settings. The only place that knows
        the legacy keys: a ``default_node`` becomes a one-entry
        prescribed list, and an explicit ``prescribed_nodes`` list (when
        present) takes over."""
        listed = settings.get("prescribed_nodes") or []
        if isinstance(listed, str):
            listed = [listed]
        prescribed = [_norm(p) for p in listed if _norm(p)]
        if not prescribed:
            legacy = _norm(settings.get("default_node", ""))
            prescribed = [legacy] if legacy else []
        seen, unique = set(), []
        for p in prescribed:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return cls(
            prescribed=tuple(unique),
            hosted=_norm(hosted),
            locks={
                "guest": bool(settings.get("guests_default_lock")),
                "user":  bool(settings.get("users_default_lock")),
                "admin": bool(settings.get("admins_default_lock")),
            },
            is_blocked=is_blocked,
        )


class AccessPolicy:
    """A policy bound to one viewer. Cheap to build; build one per request."""

    def __init__(self, config: PolicyConfig, role: str):
        if role not in ROLES:
            raise ValueError(f"unknown role: {role!r}")
        self._cfg = config
        self._role = role

    @property
    def role(self) -> str:
        return self._role

    @property
    def landing(self) -> Optional[str]:
        return self._cfg.landing

    @property
    def prescribed(self) -> tuple:
        return tuple(_norm(p) for p in self._cfg.prescribed)

    def _confined(self, action: str) -> bool:
        """Is this viewer limited to the allowed set for this action?"""
        if self._role == "super_admin":
            return False
        if self._role == "guest" and action == INTERACT:
            return True
        return bool(self._cfg.locks.get(self._role))

    def allows(self, node_hash, action: str = BROWSE) -> bool:
        """The role rule alone (ignores the blocklist)."""
        nh = _norm(node_hash)
        if not nh:
            return False
        if self._role == "super_admin":
            return True
        if self._confined(action):
            return nh in self._cfg.allowed
        return True

    def check(self, node_hash, action: str = BROWSE) -> Optional[Denial]:
        """None when allowed, otherwise the response to send.

        Order matters and is part of the contract: the role rule first,
        then the blocklist, so a viewer who may not go there at all is
        told that, not that the node is blocked."""
        if action not in _MESSAGES:
            raise ValueError(f"unknown action: {action!r}")
        if not self.allows(node_hash, action):
            login_msg, restricted_msg = _MESSAGES[action]
            if self._role == "guest":
                return Denial(401, login_msg)
            return Denial(403, restricted_msg)
        if self._cfg.is_blocked(_norm(node_hash)):
            return Denial(403, BLOCKED_MESSAGE)
        return None

    def link_filter(self) -> Callable[[str], bool]:
        """Predicate for the page renderer: may this viewer follow a link
        to this node? Used to turn off-limits links into plain text."""
        return lambda node_hash: self.allows(node_hash, BROWSE)

    def view(self) -> dict:
        """JSON-safe snapshot for the browser, so it renders what the
        server decided instead of keeping its own copy of the rules.
        ``nodes`` is the allowed list when this viewer is confined for
        browsing, or None when unrestricted. The server still enforces."""
        confined = self._confined(BROWSE)
        return {
            "role": self._role,
            "read_only": self._role == "guest",
            "landing": self.landing,
            "nodes": sorted(self._cfg.allowed) if confined else None,
        }


def policy_for(user, settings: Mapping, hosted: str = "",
               is_blocked: Callable[[str], bool] = lambda _h: False) -> AccessPolicy:
    """Convenience for the Flask layer: one call from the request's user
    and the stored settings to a ready policy."""
    return AccessPolicy(PolicyConfig.from_settings(settings, hosted, is_blocked),
                        role_of(user))
