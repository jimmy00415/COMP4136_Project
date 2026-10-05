# Exact-Target Extractive Qualitative Answer Design

## Outcome

For exactly one resolved movie, qualitative-only and mixed qualitative answers must
never return model-authored prose. Vertex may choose which eligible same-movie PDF
passages matter, but the server renders every qualitative line as a bounded verbatim
span from those selected passages. Canonical facts remain metadata-only.

## Selected approach

Three approaches were considered:

1. Use Vertex only as an ordered PDF citation-ID selector, then render safe spans on
   the server. This is selected because it preserves semantic passage selection while
   making invented prose structurally impossible.
2. Ignore Vertex and rank every retrieved PDF passage on the server. This is simpler,
   but changes the existing relevance boundary and loses the selector signal.
3. Ask Vertex for offsets or quoted spans. This adds schema and validation complexity
   while still requiring the server to prove that every span is verbatim.

## Authority and data flow

The singular exact-target authority partition remains the first gate. Metadata passages
answer canonical intents; only PDF passages for the one resolved movie are eligible for
qualitative selection. A qualitative query that resolves two or more movie titles
declines this singular branch and keeps the existing general/cross-movie generation and
citation contract. Multi-title canonical-only queries remain deterministic metadata;
multi-title mixed or canonically ambiguous queries return a controlled response before
generation.

For an exact-target qualitative branch:

1. Send only eligible same-movie PDF passages to Vertex.
2. Validate only `citation_ids`: the response must be a `GeneratedAnswer`, IDs must be
   non-empty, unique, ordered, and an exact subset of the eligible PDF passage IDs.
3. Ignore `answer_markdown` completely. It cannot influence returned text or failure.
4. For each selected passage, normalize CJK layout whitespace, segment deterministically
   at line and sentence boundaries, and retain only exact spans from that normalized
   passage.
5. Reject metadata/header/field-labelled segments, runtime claims, URI/path noise, and
   empty or oversized segments. Do not blanket-reject four-digit years, canonical
   names, or genre words: legitimate analysis may mention dates such as 1984/1997 and
   phrases such as `喜劇張力`; verbatim extraction prevents model invention.
6. Score surviving segments by normalized qualitative question terms, using selected
   passage order and source segment order as deterministic tie-breakers.
7. Return at most one bounded excerpt per selected passage and at most two cited lines.
   Each line ends with that exact passage ID.

Mixed replies render the deterministic metadata answer first and the extractive PDF
lines second. If no safe qualitative span survives, qualitative-only requests return a
fixed controlled insufficiency response with no unsupported citation; mixed requests
retain the canonical metadata answer and add the same fixed qualifier.

## Query normalization

`_canonical_metadata_intents()` normalizes the question, exact-title aliases, and field
intent aliases with the same query normalizer before removal and matching. This makes
simplified queries for titles such as `临时演员` deterministic for canonical intents
including `上映日期` and `主演`, without treating canonical words inside the title as an
intent.

## Limits and failure handling

- At most eight eligible passages enter the existing query boundary.
- At most two qualitative lines are rendered.
- At most one excerpt is rendered per selected passage.
- Each excerpt is bounded to 240 characters after normalization.
- Unknown, duplicate, empty, metadata, or cross-movie selected IDs raise
  `GroundingError`.
- Direct selector/extractor calls reject evidence spanning more than one movie, while
  the query service routes valid multi-title comparisons around the singular branch.
- No safe excerpt is a controlled insufficiency, not permission to fall back to model
  prose or metadata as qualitative evidence.

## Tests

Regression tests must prove:

- invented Vertex prose containing `虛構者`, 1988, or another unsupported claim is
  absent while a safe verbatim PDF span is returned;
- a mixed answer composes canonical metadata and safe PDF excerpts with distinct
  citations;
- an unsafe-only PDF passage fails closed with the fixed insufficiency response;
- unknown, duplicate, and non-PDF selected citation IDs fail;
- the real `富貴逼人` genre fixture `喜劇、家庭、奇幻` does not suppress a safe
  `喜劇張力` span;
- simplified `临时演员` queries for `上映日期` and `主演` remain metadata-only and do
  not invoke generation;
- multi-title qualitative comparisons retain the existing general generation path,
  canonical-only queries stay metadata-only, and mixed queries fail controlled;
- non-exact/general generation continues to validate and return model prose under the
  existing contract.

Cloud resources, release data, manifests, and deployment behavior are out of scope.
