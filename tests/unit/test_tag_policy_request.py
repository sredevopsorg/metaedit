"""The API layer's translation of configuration into a ``TagPolicy``.

Small module, load-bearing detail: ``LASTFM_GENRE_LIMIT`` and ``LASTFM_STYLE_LIMIT`` were
declared on ``Settings`` and read by nothing before 0.3.0, because ``TagPolicyRequest``
carried its own hard-coded defaults. An operator could set either variable and see no
change in any diff. These tests pin the wiring in both directions -- configuration reaching
the policy, and an explicit request value still winning.

The distinction that matters throughout: ``None`` means "the request did not say", ``0``
means "the request said zero". Collapsing those two is how a deliberate "promote nothing"
becomes "use the default".
"""

from __future__ import annotations

import pytest

from metaedit.api.items import TagPolicyRequest, tag_policy_from
from metaedit.config import Settings
from metaedit.domain.tags import TagPolicy


def settings_with(genre: int, style: int) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        LASTFM_GENRE_LIMIT=genre,
        LASTFM_STYLE_LIMIT=style,
    )


def limits(policy: TagPolicy) -> tuple[int, int]:
    return (policy.genre_limit, policy.style_limit)


def test_no_request_uses_the_configured_defaults() -> None:
    assert limits(tag_policy_from(None, settings_with(4, 6))) == (4, 6)


def test_a_request_that_names_no_limits_uses_the_configured_defaults() -> None:
    """A body carrying only a blacklist must not reset the limits to literals.

    This is the case that made the env vars look ignored: every caller that set
    ``extra_blacklist`` was implicitly restating 5/10.
    """
    policy = tag_policy_from(TagPolicyRequest(extra_blacklist=["seen live"]), settings_with(2, 3))
    assert limits(policy) == (2, 3)


def test_an_explicit_limit_wins_over_configuration() -> None:
    policy = tag_policy_from(TagPolicyRequest(genre_limit=12, style_limit=1), settings_with(5, 5))
    assert limits(policy) == (12, 1)


def test_the_two_limits_are_independent() -> None:
    """Setting one must not drag the other back to a default."""
    assert limits(tag_policy_from(TagPolicyRequest(genre_limit=9), settings_with(5, 5))) == (
        9,
        5,
    )
    assert limits(tag_policy_from(TagPolicyRequest(style_limit=9), settings_with(5, 5))) == (
        5,
        9,
    )


def test_an_explicit_zero_is_honoured_not_treated_as_missing() -> None:
    """``0`` means "promote nothing into this field", which is expressible on purpose.

    Written as a test because the natural implementation -- ``request.genre_limit or
    default`` -- turns a deliberate zero into the configured default, and the operator
    sees five genres they explicitly asked not to have.
    """
    policy = tag_policy_from(TagPolicyRequest(genre_limit=0, style_limit=0), settings_with(5, 5))
    assert limits(policy) == (0, 0)


def test_without_settings_the_policy_falls_back_to_its_own_defaults() -> None:
    """The test-only path stays usable, and is explicit about what it costs.

    ``settings`` is optional so pure unit tests need not construct one. That path cannot
    consult the environment, so it uses ``TagPolicy``'s defaults; anything reachable by a
    request passes settings.
    """
    assert limits(tag_policy_from(None)) == (5, 5)


@pytest.mark.parametrize("value", [-1, 51])
def test_out_of_range_limits_are_refused(value: int) -> None:
    with pytest.raises(ValueError):
        TagPolicyRequest(genre_limit=value)


def test_the_blacklist_is_folded_for_matching() -> None:
    policy = tag_policy_from(TagPolicyRequest(extra_blacklist=["Seen Live"]), None)
    assert "seen live" in policy.effective_blacklist()
