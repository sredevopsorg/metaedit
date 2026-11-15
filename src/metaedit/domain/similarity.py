"""Grouping genre and style values that are the same genre spelled differently.

Last.fm is community-tagged, so one genre arrives under several spellings: ``Hip Hop``,
``hip-hop`` and ``HipHop`` are one genre written three ways, and a library that carries all
three offers the operator three entries in every genre list. This module decides which
values belong together.

**Nothing here writes, and nothing that uses it may write on this module's authority.**
Guessing that two values are the same genre is a judgement, and a wrong judgement applied
automatically is a library-wide rename nobody asked for. So the only consumer is a
*preview*: :func:`build_preview` states what it would merge, an operator confirms, and the
apply path re-verifies against the preview. ADR 0015 already settled that matching for the
destructive paths is exact and case-insensitive; this is a different question -- "do these
two categories read the same?" -- and it is answered for a human rather than for a write.

The rule, in one sentence: **two values are the same genre when they are identical after
Unicode NFC normalisation, case folding, and removing every token separator.**

    ``Hip Hop``, ``hip-hop``, ``HipHop``, ``HIP HOP``, ``hip_hop``, ``hip.hop`` and
    ``Hip  Hop`` all squash to ``hiphop`` and therefore group together.

Separator differences are *lexical*, not semantic: a hyphen, an underscore, a dot, a slash,
an ampersand, a plus and a space are all ways of joining the same two words, so treating
them as differences would leave the library's genre list with one entry per tagger. The
same rule merges ``Dark Wave`` with ``Darkwave``, and the live genre-entity list carries
both as distinct entries -- which is exactly the redundancy this is for.

What the rule deliberately does **not** do is guess at *different* genres that merely share
words:

* ``Rock`` and ``Rockabilly`` squash to ``rock`` and ``rockabilly`` -- different keys.
  ``Rockabilly`` is a genre in its own right, not a spelling of rock.
* ``Pop`` and ``Pop Rock``, ``Rock`` and ``Gothic Rock``, ``Hip Hop`` and ``Trip Hop``: the
  keys differ, so they never group. A rule that merged on a shared word, or on one key
  being a prefix of another, would collapse half a genre vocabulary into ``rock``.
* ``R&B`` and ``Rhythm & Blues`` squash to ``rb`` and ``rhythmblues`` -- different keys.
  They *are* the same genre, and this rule does not claim to know that. Merging them would
  need an alias table, which is a different feature with a different kind of evidence (an
  operator's assertion, not a spelling difference).

The two cases the rule refuses to separate are worth stating because they look like
mistakes:

* ``Drum & Bass`` and ``Drum and Bass`` squash to ``drumbass`` and ``drumandbass``. The
  ampersand is a *separator* here and the word ``and`` is not, so the two do not group.
  Translating ``and``/``&``/``n`` into each other is an abbreviation rule, not a separator
  rule, and abbreviation is where a similarity function starts inventing genres.
* A bare number or a single letter is refused as a key rather than grouped: see
  :func:`squash_key`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from metaedit.adapters.lastfm.canonical import normalize_tag

# Characters that join the words of one genre, and therefore mean nothing when deciding
# whether two spellings are the same genre. Compared after `normalize_tag`, which has
# already casefolded, NFC-normalised and collapsed whitespace -- so this set only has to
# name the non-space joiners, and `squash_key` drops whitespace separately. Running order
# `Hip  Hop` matters here: `normalize_tag` collapses it to one space, and the squash then
# removes that space, which is what makes a double space group with a hyphen.
#
# This is a deliberate, closed list rather than "every non-alphanumeric character": the
# live vocabulary contains values such as `UK Garage / Bassline` (two genres, which the
# tag policy splits before this ever sees them), `Café Tacvba` (an artist name that is a
# tag) and `90s`/`2000s` (decades, not genres). A rule that stripped punctuation in
# general would turn `90s` and `90` into one key while leaving `Café` and `Cafe` apart,
# which is the opposite of what a spelling-variant rule should do.
SEPARATORS = frozenset({"-", "_", ".", "/", "\\", "&", "+"})

# The shortest normalised value that may take part in a comparison.
#
# A value whose *entire* content is a separator (`-`) or a bare punctuation mark squashes to
# nothing and must be refused rather than grouped with every other value that also squashes
# to nothing. The floor is measured on the value *before* the separators are removed, not
# after: `EBM` squashes to three characters, but a two-word genre like `Dark Wave` squashes
# to `darkwave` only because the space is gone -- and applying a length floor to the
# squashed form would silently rule on the key rather than on the value. Two is the shortest
# value any separator-stripping can leave meaningful; nothing real is shorter.
_MIN_VALUE_LENGTH = 2


def squash_key(value: str | None) -> str:
    """The merge-comparison key: NFC, casefolded, separators removed.

    Returns ``""`` for a value with no key -- blank, or nothing but separators -- and the
    caller must treat that as "not comparable" rather than as a group. Two values that
    both squashed to ``""`` would otherwise be reported as the same genre, which is how a
    list of junk values becomes one "merge".

    Built on :func:`metaedit.domain.genre_removal.normalise` (``normalize_tag``) rather
    than on an NFC+casefold of its own, so "the same genre" case-folds the same way here
    as it does in the blacklist, the archive's ``tag_name_norm`` and the removal tool.

    ``str.casefold`` (through ``normalize_tag``) runs *before* the separators are removed,
    which is what makes ``HIP-HOP`` and ``Hip Hop`` land on the same key. Removing first
    and folding afterwards would work for ASCII too, but only by accident: casefolding can
    change length (``ß`` -> ``ss``) and a fold applied to a half-split string is the kind
    of order-dependence that is silent until someone's language needs it.
    """
    key = _identity(value)
    return key if len(key) >= _MIN_VALUE_LENGTH else ""


def _identity(value: str | None) -> str:
    """The separator-free, whitespace-free form of a value: the comparison key's body.

    Returns ``""`` for a blank value. The length floor lives in :func:`squash_key` and
    because it decides whether two values may be *compared* -- not
    whether this value appeared twice in a list.
    """
    return "".join(
        character
        for character in normalize_tag(value)
        if character not in SEPARATORS and not character.isspace()
    )


@dataclass(frozen=True, slots=True)
class SpellingVariant:
    """One spelling in a group: the value, how often it occurs, and how it is counted."""

    value: str
    count: int
    # ``True`` when a value was offered more than once, which for an explicit list is the
    # caller's way of weighting it and for a vocabulary is a server quirk. Reported rather
    # than collapsed so a preview can explain why this spelling was chosen.
    repeated: bool

    def as_dict(self) -> dict[str, object]:
        return {"value": self.value, "count": self.count, "repeated": self.repeated}


@dataclass(frozen=True, slots=True)
class SimilarityGroup:
    """Values that a preview proposes merging into one spelling.

    ``representative`` is what the merge would keep; ``merge`` is ``{from: to}`` pairs for
    the values that would be rewritten, which is the shape a confirmed merge needs. Both
    are computed once, here, so a preview and an apply cannot disagree about the proposal
    by recomputing it in two places.

    ``squashed`` is retained because it is the *reason* the group exists, and an operator
    reviewing ``Bay Area`` + ``Bay-Area`` should be able to see that the reason is a
    separator rather than a judgement about genres.
    """

    squashed: str
    representative: str
    variants: tuple[SpellingVariant, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(variant.value for variant in self.variants)

    @property
    def total(self) -> int:
        """How many occurrences the whole group accounts for."""
        return sum(variant.count for variant in self.variants)

    @property
    def proposals(self) -> dict[str, str]:
        """``{from_value: representative}`` for every spelling that is not the winner."""
        return {
            variant.value: self.representative
            for variant in self.variants
            if variant.value != self.representative
        }

    @property
    def needs_merge(self) -> bool:
        """Whether confirming this group would change anything.

        A group of one spelling (which cannot happen: a group of one is not reported) or a
        group whose spellings are all the identical string is not a merge.
        """
        return bool(self.proposals)

    def as_dict(self) -> dict[str, object]:
        return {
            "squashed": self.squashed,
            "representative": self.representative,
            "total": self.total,
            "variants": [variant.as_dict() for variant in self.variants],
            "merge": self.proposals,
        }


def _representative_key(variant: SpellingVariant) -> tuple[int, int, int, int, str]:
    """Sort key for choosing a group's canonical spelling: smaller is better.

    Five clauses, in the order they decide:

    1. **Most occurrences.** The spelling the library already uses most is the one to
       keep; rewriting the common form would change more items for no gain.
    2. **Most spaces.** Among equally common spellings, the one written as separate words
       is the conventional English form (``Hip Hop`` over ``hip-hop``) and reads better in
       a client's genre list.
    3. **Capitalised.** ``Hip Hop`` beats ``hip hop``: Jellyfin shows genre values
       verbatim, and a lower-case category looks like a tag rather than a genre.
    4. **Fewest separator runs.** Left after the clauses above, prefer the spelling that
       uses its separators least oddly (``hip hop`` over ``hip--hop``).
    5. **The value itself, ordinally.** Purely so the result is *deterministic*: two runs
       over the same vocabulary must choose the same representative, or a preview would
       not describe what a confirmed apply does. Nothing good rides on which of two
       otherwise identical spellings wins; it only has to always be the same one.
    """
    value = variant.value
    return (
        -variant.count,
        -sum(1 for character in value if character.isspace()),
        0 if value[:1].isupper() else 1,
        _separator_runs(value),
        value,
    )


def _separator_runs(value: str) -> int:
    """How many maximal runs of non-space separators the value contains."""
    runs = 0
    previous = False
    for character in normalize_tag(value):
        is_separator = character in SEPARATORS
        if is_separator and not previous:
            runs += 1
        previous = is_separator
    return runs


def group_values(
    values: Iterable[str], *, counts: Mapping[str, int] | None = None
) -> tuple[SimilarityGroup, ...]:
    """Group ``values`` by :func:`squash_key`, keeping only the groups worth reporting.

    Redundant spellings collapse first, because any source of values can repeat one:
    ``"Hip Hop"`` and ``"hip hop"`` are the *same* value to a case-insensitive genre list,
    and counting them as two occurrences twice would let a duplicate outvote a genuinely
    common spelling.

    A group is reported only when it has at least two distinct values *and* confirming it
    would change something. A group of one is not a finding; reporting every genre as a
    "group of one" would bury the few that matter.

    Values with no key (blank, separators only) are dropped rather than grouped together --
    they are not comparable to each other, let alone to a genre.

    ``counts`` weights values that were gathered from a library, where the same genre
    occurs on many items; without it every value counts once, which is right for an
    explicit list.

    Ordering is by the representative's own comparison key, so the preview is stable
    across runs and does not depend on the order the vocabulary arrived in.
    """
    # Two de-duplications, and conflating them is how the first version of this grouped
    # nothing at all:
    #
    # * a *spelling* is identified by `normalize_tag`, the archive's `tag_name_norm`, so
    #   `Hip Hop` and `HIP  HOP` are one stored value. Counting them as two occurrences
    #   would let capitalisation outvote a genuinely common spelling.
    # * a *group* is identified by the squash key, which is what collapses `Hip Hop` and
    #   `hip-hop` -- different spellings (and so different keys here) of one genre.
    #
    # Using the squash key for the first would make `Hip Hop` and `hip-hop` one spelling,
    # leaving every group with a single member: a rule that groups perfectly and reports
    # nothing. That is exactly the failure this comment exists to prevent.
    spellings: dict[str, str] = {}
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        spellings.setdefault(normalize_tag(value), value)

    buckets: dict[str, list[SpellingVariant]] = {}
    for display in spellings.values():
        key = squash_key(display)
        if not key:
            continue
        count = (counts or {}).get(display, 1)
        buckets.setdefault(key, []).append(
            SpellingVariant(value=display, count=count, repeated=count > 1)
        )

    groups: list[SimilarityGroup] = []
    for key, variants in buckets.items():
        if len(variants) < 2:
            continue
        ordered = tuple(sorted(variants, key=_representative_key))
        group = SimilarityGroup(
            squashed=key,
            representative=ordered[0].value,
            variants=ordered,
        )
        if group.needs_merge:
            groups.append(group)
    return tuple(sorted(groups, key=lambda group: _representative_key(_as_variant(group))))


def _as_variant(group: SimilarityGroup) -> SpellingVariant:
    """The group's representative, as a variant, so groups sort by the same rule."""
    return group.variants[0]


# ------------------------------------------------------------------ merge planning


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What rewriting one field of one item would do.

    Deliberately the same shape of answer as ``genre_removal.RemovalOutcome`` -- before,
    after, what changed, whether the field would be emptied -- because it is reported in the
    same stream to the same screen. It is not that type because the *question* differs: a
    merge has a target value (`to`) rather than a removed one, and an ``emptied`` flag that
    can only ever be false. Sharing the class would mean a field that is always ``None`` on
    one side of a union.
    """

    field: str
    to: str
    before: list[str]
    after: list[str]
    changed: list[int]

    @property
    def changed_any(self) -> bool:
        return bool(self.changed)

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "to": self.to,
            "before": list(self.before),
            "after": list(self.after),
            "changed": list(self.changed),
        }


def rewrite_values(
    values: Iterable[str] | None,
    *,
    representative: str,
    spellings: Iterable[str],
) -> tuple[list[str], list[int]]:
    """Rewrite the named spellings of one group to ``representative``.

    Returns ``(after, changed_indexes)``. Matching is ``normalize_tag`` equality -- the same
    exact, case-insensitive rule as ``genre_removal.matches`` -- so a value that merely
    *looks* similar is never touched. ``spellings`` is the group's own list, which is what
    keeps the rewrite inside the preview: a value the operator did not see grouped cannot
    be rewritten by a merge they confirmed.

    Two deliberate behaviours that a merge must have and that a naive loop gets wrong:

    * **Order is preserved, and the position of the first spelling is kept.** Jellyfin's
      genre order is what clients display; rewriting one word in a list must not reshuffle
      the list.
    * **A representative that is already present collapses the others onto its position**
      rather than leaving a duplicate: `["hip-hop", "Hip Hop", "Ska"]` becomes
      `["Hip Hop", "Ska"]`, not `["Hip Hop", "Hip Hop", "Ska"]`. A duplicate left behind
      looks exactly like the merge having half-failed.
    """
    wanted = {normalize_tag(value) for value in spellings}
    wanted.discard("")
    replacement_key = normalize_tag(representative)

    after: list[str] = []
    changed: list[int] = []
    representative_at: int | None = None
    for value in values or []:
        key = normalize_tag(value)
        if key not in wanted:
            after.append(value)
            continue
        if representative_at is None:
            representative_at = len(after)
            after.append(representative)
            # Reported whenever the spelling is not already the canonical string, because
            # `Hip Hop` -> `Hip Hop` is no edit and saying otherwise would overstate it.
            if key != replacement_key:
                changed.append(representative_at)
            continue
        # A later spelling of the same value merges into the representative's position and
        # disappears. Reported as a change to *that* position -- the index the UI receives
        # must address the list it receives, and the list it receives no longer has the
        # value at this one.
        if representative_at not in changed:
            changed.append(representative_at)
    return after, changed


def rewrite_groups(
    values: Iterable[str] | None,
    *,
    groups: Iterable[Mapping[str, str]],
) -> tuple[list[str], list[int]]:
    """Rewrite several groups in one pass over one field.

    ``groups`` maps each *spelling* to the representative it becomes. Membership is decided
    by :func:`squash_key` -- not by ``normalize_tag`` -- because that is the key the groups
    were formed on: ``HipHop`` and ``hip-hop`` are different ``normalize_tag`` values of one
    genre, and looking them up by the exact key would rewrite one and silently leave the
    other. That is precisely the half-merge this rule exists to prevent, and it would look
    like a partial write rather than like a mismatch.

    One pass rather than one :func:`rewrite_values` call per group: a field is one value, so
    running the groups in sequence would have the second see the first's output, and a
    representative that is also another group's spelling would be rewritten twice.

    Same properties as :func:`rewrite_values` -- order preserved, and the representative
    collapses onto its first occurrence's position so no duplicate is left behind -- and the
    indexes returned address the list returned, so a UI can highlight them without going
    out of bounds.
    """
    # Squashed key -> representative, so a spelling and a differently-joined duplicate of it
    # land on the same entry. A blank key matches nothing and is dropped rather than used as
    # `""`, which would sweep every empty string in the list into a representative.
    replacement: dict[str, str] = {}
    for group in groups:
        for spelling, representative in group.items():
            key = squash_key(spelling) or squash_key(representative)
            if key:
                replacement[key] = representative

    after: list[str] = []
    changed: list[int] = []
    emitted: dict[str, int] = {}
    for value in values or []:
        key = squash_key(value)
        # `squash_key` returns "" for a blank value, and "" is never a group key, so a blank
        # value is simply passed through rather than matched against the map.
        target = replacement.get(key) if key else None
        if target is None:
            after.append(value)
            continue
        if key in emitted:
            # Already written at its first occurrence: this copy merges into it.
            if emitted[key] not in changed:
                changed.append(emitted[key])
            continue
        emitted[key] = len(after)
        after.append(target)
        if value != target:
            changed.append(emitted[key])
    return after, changed


def plan_merge(
    values: Iterable[str] | None,
    *,
    representative: str,
    spellings: Iterable[str],
    field: str,
) -> MergeOutcome:
    """What rewording one field of one item to ``representative`` would do."""
    before = list(values or [])
    after, changed = rewrite_values(before, representative=representative, spellings=spellings)
    return MergeOutcome(
        field=field,
        to=representative,
        before=before,
        after=after,
        changed=changed,
    )


# --------------------------------------------------------------------- the preview


@dataclass(frozen=True, slots=True)
class SimilarityPreview:
    """What a similarity pass found, and what it did not find.

    ``unmatched_exceptions`` exists because a preview that answers "I would merge these"
    without saying "and that exception you typed matches nothing" makes a typo look like a
    deliberate exclusion.
    """

    groups: tuple[SimilarityGroup, ...]
    # How the vocabulary was obtained: an explicit list, or read from the library. Reported
    # so a preview over an implicit selection says what it looked at.
    source: str
    # How many distinct values were considered, and how many items were read to gather
    # them. ``scanned``/``truncated`` are `None`/`False` for an explicit list, where there
    # was nothing to scan.
    values: int
    scanned: int | None = None
    truncated: bool = False
    # Groups held back because the operator excluded one of their spellings.
    withheld: tuple[SimilarityGroup, ...] = ()
    # Words that look like merge decisions already taken (a value and a bare word it
    # contains) plus kept-spelling exceptions that matched nothing.
    hints: tuple[str, ...] = ()

    @property
    def merge(self) -> dict[str, str]:
        """Every proposed rewrite across every group, as one ``{from: to}`` map."""
        merged: dict[str, str] = {}
        for group in self.groups:
            merged.update(group.proposals)
        return merged

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "values": self.values,
            "groups": [group.as_dict() for group in self.groups],
            "merge": self.merge,
            "withheld": [group.as_dict() for group in self.withheld],
            "hints": list(self.hints),
            "scanned": self.scanned,
            "truncated": self.truncated,
        }


def _looks_resolved(value: str, vocabulary: set[str]) -> list[str]:
    """Bare words from ``value`` that are themselves values in the vocabulary.

    ``Hip Hop`` beside a separate ``Hip`` is the case this surfaces: a short list already
    contains the pieces, which is what a merge that *is* working looks like. It is a hint
    rather than a group because merging ``Hip Hop`` into ``Hip`` would be a different genre,
    and the point is to say so rather than to do it.

    Only single words of two or more characters, and only words that are *not* the value
    itself: ``Rock`` and ``Rock, Reggae`` are not a spelling variant, and neither is
    ``Post-Rock`` and ``Rock``.
    """
    normalised = normalize_tag(value)
    if not normalised:
        return []
    found: list[str] = []
    for word in _words(normalised):
        if len(word) < _MIN_VALUE_LENGTH or word == normalised:
            continue
        for candidate in vocabulary:
            if normalize_tag(candidate) == word:
                found.append(candidate)
    return found


def _words(value: str) -> list[str]:
    """The whitespace-separated words of an already-normalised value."""
    return [word for word in value.split(" ") if word]


def build_preview(
    values: Iterable[str],
    *,
    counts: Mapping[str, int] | None = None,
    source: str = "explicit list",
    scanned: int | None = None,
    truncated: bool = False,
    keep: Iterable[str] = (),
    exceptions: Iterable[str] = (),
) -> SimilarityPreview:
    """Group a vocabulary and describe the merge a confirmation would perform.

    ``keep`` refuses whole groups: an operator excluding ``Goth`` from a ``Goth``/``Gothic``
    question must not have that group merged anyway because they excluded only one side.
    ``exceptions`` refuses individual spellings, by ``normalize_tag`` -- a spelling that is
    held back is never rewritten, so the remaining group either shrinks or disappears.
    Neither is a write; they only narrow the proposal.

    An exception that matches nothing is reported in ``hints`` instead of being dropped:
    the operator typed it because they expected it to matter, and silence would let a typo
    pass as an exclusion.
    """
    vocabulary = [value for value in values if value and value.strip()]
    known = {normalize_tag(value) for value in vocabulary}
    held = {normalize_tag(value) for value in exceptions if value and value.strip()}
    refused = {normalize_tag(value) for value in keep if value and value.strip()}

    unmatched = [
        value
        for value in exceptions
        if value and value.strip() and normalize_tag(value) not in known
    ]

    kept_groups: list[SimilarityGroup] = []
    withheld: list[SimilarityGroup] = []
    hints: list[str] = []
    for group in group_values(vocabulary, counts=counts):
        spellings = {normalize_tag(variant.value) for variant in group.variants}
        if spellings & refused:
            withheld.append(group)
            continue
        remaining = tuple(
            variant for variant in group.variants if normalize_tag(variant.value) not in held
        )
        if len(remaining) < 2:
            withheld.append(group)
            continue
        candidate = SimilarityGroup(
            squashed=group.squashed,
            representative=remaining[0].value,
            variants=remaining,
        )
        if not candidate.needs_merge:
            withheld.append(group)
            continue
        kept_groups.append(candidate)

    for value in unmatched:
        hints.append(
            f"exception {value!r} matches no value in this vocabulary or selection; "
            "check the spelling"
        )
    for value in vocabulary:
        for found in _looks_resolved(value, known):
            hints.append(
                f"{value!r} sits beside {found!r}, which is a different genre; "
                "similarity does not merge a value with a word it contains"
            )

    return SimilarityPreview(
        groups=tuple(kept_groups),
        source=source,
        values=len(vocabulary),
        scanned=scanned,
        truncated=truncated,
        withheld=tuple(withheld),
        hints=tuple(hints),
    )


__all__ = [
    "SEPARATORS",
    "MergeOutcome",
    "SimilarityGroup",
    "SimilarityPreview",
    "SpellingVariant",
    "build_preview",
    "group_values",
    "plan_merge",
    "rewrite_groups",
    "rewrite_values",
    "squash_key",
]
