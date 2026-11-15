# 0018. Similarity merging is a preview the operator confirms, never an automatic rewrite

- **Status:** Accepted
- **Date:** 2026-11-15

## Context

Last.fm is community-tagged, so one genre arrives under several spellings. Measured on a
live 6,583-item library, the artist genre-entity list carries 39 entries, including
`Dark Wave`/`Darkwave` and `Goth`/`Gothic` as distinct pairs — one genre occupying several
entries in every genre list Jellyfin shows.

The request was to "merge genres/styles by similarity, example: hip hop with hip-hop, ignore
case". The mechanism is easy; the *decision to act on it* is not, and ADR 0015 already
settled the adjacent question for destructive paths: matching there is exact and
case-insensitive, because a matcher that guessed had already been shown to delete 67
albums' worth of curation for a request naming a genre occurring once.

Similarity is a different question — "do these two categories *read* the same?" — but it
fails in the same direction. `Rock` and `Rockabilly` share a prefix; `Pop` and `Pop Rock`
share a word; `R&B` and `Rhythm & Blues` are the same genre under different words. A rule
strong enough to merge the third is a rule that eventually merges the first.

## Decision

**Similarity finds and explains; only an explicit confirmation writes.** The preview
(`/api/bulk/merge-similar/preview`) is read-only and returns groups with the reason they
group. The apply path requires a job id from a diff over *named* groups, so a merge can
never be a side effect of asking what the library looks like. A group the rule would not
have produced is refused, not merged.

**The rule is lexical, and stated: two values are the same genre when they are identical
after Unicode NFC normalisation, case folding, and removing every token separator.**
`Hip Hop`, `hip-hop`, `HipHop`, `HIP HOP`, `hip_hop`, `hip.hop` and `Hip  Hop` all squash
to `hiphop`. Separator differences are orthography, not meaning, and `#`-free `Dark Wave`
versus `Darkwave` is exactly the redundancy being removed.

**The rule does not guess at semantics, and does not pretend to.** `Rock`/`Rockabilly`,
`Pop`/`Pop Rock`, `Rock`/`Gothic Rock`, `Hip Hop`/`Trip Hop` and `R&B`/`Rhythm & Blues` do
not group. Merging the last pair would require an alias table, which is a different feature
with a different kind of evidence: an operator's assertion, not a spelling difference.
`Drum & Bass` does not merge with `Drum and Bass` either, because treating `and` as a
separator is an abbreviation rule, and abbreviation is where a similarity function starts
inventing genres.

**The normal write path is untouched.** `TagPolicy.classify` still keeps `hip hop` and
`hip-hop` distinct when both arrive from Last.fm, ADR 0015 stands, and nothing consumes the
similarity rule automatically. A test asserts this, so the guarantee cannot be lost by a
later refactor that "reuses" the grouping function.

## Consequences

- An operator sees `Rock` never grouped with `Rockabilly` *before* anything is rewritten,
  and the group carries its reason rather than an assertion.
- The representative choice is deterministic (most occurrences, then most spaces, then
  capitalised, then fewest separator runs, then ordinal), because a preview that describes
  one merge and an apply that performs another is worse than no preview.
- A keep list and per-spelling exceptions narrow the proposal without writing; an exception
  matching nothing is *reported*, because silence there reads as a successful exclusion.
- The merge runs through the same reviewed-batch lifecycle as a genre removal: snapshot
  before every write, etag check, per-item failure isolation, revertible by batch id.
- Reading the vocabulary is a bounded scan, and `truncated` says so: a preview over part of
  a library is a lower bound, stated rather than implied.
- Automatic merging remains available as a *future* feature, and would be a new decision
  with an alias table and its own ADR — not a flag on this one.

## Alternatives rejected

- **Merge automatically on the write path.** A judgement about genre identity applied to
  every item in a library, unattended, is the class of edit that cannot be reviewed after
  the fact — and it would contradict ADR 0015 in the same code path that relies on it.
- **Merge on a shared word or shared prefix.** Collapses half a vocabulary into `rock`,
  which is the failure ADR 0015 was written to prevent.
- **Semantic matching with a bundled genre taxonomy.** Same objection ADR 0006 records
  against a bundled genre list: an opinionated classifier with its own maintenance burden,
  for a problem the operator can settle by confirming groups.
- **Case-only merging.** Already done by `normalize_tag` everywhere, and does not merge
  `Hip Hop` with `hip-hop`, which is the case that was asked for.
- **Treating `&`/`and`/`n` as equivalent.** An abbreviation rule disguised as a separator
  rule; it merges `Drum & Bass` with `Drum and Bass` and, by the same argument, invites
  `R&B` with `RnB` with `Rhythm and Blues`.
