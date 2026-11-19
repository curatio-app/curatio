# Tagging architecture audit — 2026-08-04

## Export reviewed

- Generated: `2026-08-04T22:37:29Z`
- Metadata: 365,212 titles
- Tag assignments: 3,303,348 rows across all historical versions
- Current-version title/type pairs: 103,103
- Vocabulary: 517 canonical tags
- Tag observations: 183,466 strings / 2,195,648 occurrences
- Universal catalogs: 40 curated definitions, 0 synthesized rows
- Personal catalogs: 209 rows across 13 profiles
- Admin settings were intentionally excluded, so the live quality-floor overrides
  cannot be reconstructed from the export. Admin Center's preflight is authoritative
  for the final eligible-title count.

## Findings

### 1. The taxonomy was not the primary failure

The current vocabulary is broad, canonical assignments are structurally valid, and
there were no unknown tag ids, null confidence values, or duplicate assignments in
the current version. The main defect was how the vocabulary was presented and
validated.

The tagger built one union of every title's shortlist for a 20-title batch, then
validated every title against that union. In the export, 387,692 of 966,543 model
assignments (40.1%) were outside the affected title's own shortlist, and 99,681 of
103,103 titles had at least one such assignment. This explains combinations such as
`Age of Sail` on *Mad Max* and `War's Aftermath` on *The Bear*.

### 2. Some source facts were attached to the wrong title

MDBList batch results used to be matched through its top-level `id`, which is an
MDBList id rather than a TMDB id. The code was corrected previously, but the source
version was never bumped, so corrupted rows remained marked current. *The Matrix*,
for example, held MDBList facts belonging to *Donnie Darko*.

TMDB detail payloads also contained correct canonical dates and titles that the
enrichment worker never copied back to `media_metadata`. This produced wrong eras:
*The Godfather* was stored with a 2022 release date and *The Matrix* with 2018.

### 3. The observation queue overstated taxonomy gaps

Only 5.3% of observation occurrences were canonical names, 18.3% were already-known
aliases, and 76.4% were genuinely novel strings. All were labelled `unmapped`, so the
admin queue made known concepts such as Reality, Competition, Talk, and Game Show
look like vocabulary gaps.

### 4. The customer-facing category layer was stale

The export had 40 curated rows and no synthesized rows. Only 33 had content, the
median row contained two titles, seven were empty, and 194 of 500 stored memberships
failed the shipped default quality gate. Personal rows existed, but 188 of 209 were
older than 24 hours because their refresh was coupled to the disabled-by-default
paid daily-update scheduler.

## Implemented redesign

- Every title now receives compact, title-specific allowed tag indexes in the prompt.
- Server validation independently enforces that same title-specific shortlist.
- TMDB enrichment version `1.2.0` repairs canonical metadata from already-stored
  detail payloads before tagging; this is local and adds no AI cost.
- MDBList version `1.2.0` invalidates potentially mis-associated facts once.
- The tagger version includes both source versions, so repaired facts cannot leave
  falsely-current tag assignments behind.
- Known canonical and aliased observations are reclassified out of the unmapped QA
  queue on startup and as new observations arrive.
- Synthesis reads only high-quality, current-tagger assignments with meaningful
  normalized weight (`>= 0.15`). Generated formulas enforce the same constraints.
- Initial builds, daily updates, the one-button upgrade, and manual catalog
  regeneration synthesize first and materialize generated rows only.
- The former 40 static rows remain inactive compatibility records for cached ids;
  new manifests, dashboard rows, discovery sampling, diagnostics, and regeneration
  use synthesized rows only.
- Personal rows refresh daily through a separate local scheduler even when
  `DAILY_UPDATE_ENABLED=false`. This scheduler does not call Gemini.

## Retag decision

Yes: perform one new quality-eligible retag after deploying this code. Existing tags
cannot be repaired safely with an offline deletion rule: pruning every assignment
outside the old shortlist would leave 22,476 titles below the minimum useful tag
count, and some outside-shortlist assignments are genuinely correct. The corrected
title-scoped prompt and source repair need to make the decision again.

Do not tag the full 365,212-title acquisition library. Leave the Admin Center minimum
votes box empty and let the shared quality gate select the recommendation corpus. The
previous live preflight selected 105,985 titles and excluded 248,454; the next live
preflight may differ because admin quality settings were not included in this export.

## Safe Admin Center runbook

1. Deploy and restart the updated server.
2. Open **Admin Center → Tagging → One-button upgrade**.
3. Confirm all six steps are selected: TMDB details, MDBList facts, re-tag,
   relevance, catalogs, and users.
4. First enter a limit of **1,000**, leave minimum votes blank, and choose
   **Run Selected Steps**. This is a resumable pilot and should cost only cents.
5. Check the pilot's missing-result and too-few-valid-tag rates. The catastrophic
   rate should be gone, and the unmapped observation queue should no longer be
   dominated by known canonical/alias names.
6. Run the same six steps again with the limit empty. Completed source rows and
   successfully tagged titles are skipped, so the pilot is not paid for twice.
7. If preflight estimates slightly above the default `$20` ceiling, set the hard
   ceiling to `$25` before the full run. The ceiling is protection, not a target;
   measured spend remains based on actual tokens.
8. After the catalog and user steps complete, inspect **Catalog rows**. It should
   list synthesized rows only; personal rows will then continue cycling daily with
   no Gemini spend.

The exact target version shown by the deployed Admin Center is authoritative because
model and tagging settings participate in its hash. With repository defaults, this
revision computes `3-37914a720e3a`.
