---
name: paper-summary
description: Summarize scholarly papers (mostly arXiv, cs/ML domain) into a fixed structured markdown note — architecture, inputs, outputs, loss functions, training procedure, evaluation, contributions over prior work, limitations, future work. Use when the user asks to summarize a paper, take paper notes, or add a paper to the notes database.
---

# Paper Summary Skill

Produce a dense, structured markdown note for one paper. The style is tuned for
**incremental papers** — work that builds on a known base method — where the
interesting content is the delta over the base, not the field-level background.

## Workflow

1. **Get the full text.** Use the `papers` MCP server when available:
   - `papers_get_paper_by_url` with the arXiv URL, or `papers_get_paper_abstract` for metadata.
   - `papers_get_paper_pdf_text` for the full text (needed for loss equations, training details, limitations).
   - Fallback: fetch `https://arxiv.org/abs/<id>` for metadata and `https://arxiv.org/html/<id>` for full text.
   - Never summarize from the abstract alone unless the user explicitly asks for a shallow pass — losses, training procedure, and limitations live in the body and appendix.
2. **Extract** every item in the section checklist below. Method sections and
   the appendix ("More Implementation Details", "Training Details") are where
   most of the content hides — read them fully.
3. **Write** the note following the template and style rules.
4. Save to the path the user gives; if none, save as `<paper-short-name>.md`
   (lowercase, no spaces) in the user's notes directory.

## Template

```markdown
# <Short Name>: <Full Paper Title>

<arXiv URL, bare, on its own line>

## Reason for existing

- the gap or problem that motivated the paper
- for incremental papers: what the base method could not do, concretely
- bullets only, no paragraph of prose

## Architecture

- one top-level bullet per major component
  - details as nested child bullets, not parenthetical clauses
- include shapes/sizes (token counts, dims, patch size), initialization
  (pretrained from what), and structural equations where they exist

## Inputs

- what the model consumes (modalities, whether camera info is required,
  sequence lengths / resolutions it operates on)
- optional/conditional inputs as child bullets

## Outputs

- one top-level bullet per output, in a fixed naming form where possible
  (`X̂_self, C_self` — short label — then child bullets explaining it)
- state the coordinate frame / units of each output

## Loss functions

- one top-level bullet per loss, bolded loss name
  - family/style ("MASt3R-style") as child bullet
  - formula as its own child bullet
  - normalization / conditioning details (when metric GT is available, etc.)
- if a design seems redundant but is deliberate, add a trailing claim bullet
  with the reasons as children

## Training procedure

- **Data**
  - dataset count + coverage axes (static/dynamic, indoor/outdoor, real/synthetic…)
  - representative dataset names inline, "…" for the tail
  - special handling rules (partial annotations, single-view stacking, masking)
- **Curriculum** (or **Stages**)
  - one bullet per stage in order, compressed to one line each
- **Optimization**
  - optimizer, schedule, notable augmentation choices
  - omit hardware and minor hyperparameters (lr values, GPU counts) unless
    the paper stresses them

## Evaluation data

- benchmarks/datasets evaluated on, and what task each covers
- only if the paper reports experiments; otherwise omit the section

## Key contributions over previous methods

- bullets only, each naming what is new **and against which baseline**
  ("vs. Spann3R: …", "first to …")
- for incremental papers, lead with the delta over the direct base method
- this replaces a prose "summary" section — there is no summary section

## Limitations

- bullets, terse, from the paper's own limitations section plus evident ones
- skip anything already stated as a contribution

## Future work

- the directions the authors name (global alignment, generative formulation, …)
- omit the section if the paper names none

<!-- Optional extra sections for novel content, placed after Architecture (or
     before Limitations). E.g. "State update rule" for TTT3R, "Querying the
     state", "Memory mechanism", "Inference procedure". Only add a section when
     the paper's novelty does not fit the fixed sections above. -->
```

## Style rules

These come from diffing a verbose draft against the refined final note. Follow
them as far as possible.

1. **No prose summary.** The note opens with title + bare URL, then goes
   straight into `Reason for existing`. The contributions section carries the
   "what's new" narrative.
2. **Minimal header.** No author list, no badges, no link lists. Just the bare
   arXiv URL line under the title.
3. **Nested bullets, not parentheses.** Anything that would sit inside
   parentheses or after a dash-clause becomes a child bullet. Keep each bullet
   a fragment, not a sentence. Drop trailing periods.
4. **Bold sparingly.** Bold only component names, loss names, and section-label
   bullets (`**Data**`, `**Curriculum**`). Never bold whole phrases for emphasis.
5. **Formulas on their own bullets.** Do not bury equations inside prose.
6. **Compress, don't dump.** Lists of many items become a few representative
   names + "…". Long enumerations from the paper (e.g. 32 datasets) must not be
   transcribed in full.
7. **Limited repetition.** Each fact appears in exactly one section. If a
   mechanism is introduced in Architecture, the Training and Outputs sections
   refer to it by name, not re-describe it. (Compact cross-pointers like
   "used as a query, and during training as a randomly substituted observation"
   are fine; re-explanations are not.)
8. **Omit trivia.** Hardware, exact learning rates, GPU counts, citations of
   the paper's own acknowledgements. Keep choices that change behavior
   (freeze-encoder stages, sequence-level vs per-image augmentation, masking
   probabilities).
9. **Keep the deliberate-design notes.** When the paper explains *why* a
   seemingly redundant or odd choice exists, keep that reasoning — it is the
   part readers can't recover from the architecture alone.

## Handling papers that don't fit the mold

- **Novel content:** if the paper's core novelty has no home in the fixed
  sections (a new update rule, a memory mechanism, a sampling scheme, a theory
  result), add a dedicated section near where it conceptually belongs — after
  Architecture, or before Limitations. Do not force it into Inputs/Outputs.
- **Different output types:** an LLM paper's "Outputs" may be tokens/rewards, a
  generative model's may be latents/videos — keep the section, adapt content.
- **No loss function / no training** (analysis or survey papers): omit those
  sections rather than writing "N/A".
- **Non-incremental papers:** still write `Reason for existing` and
  `Key contributions over previous methods`, but compare against the closest
  prior work the paper itself positions against (related-work section).
- **Multiple base methods:** in `Key contributions`, name the baseline each
  bullet compares against explicitly.
- **Equations that matter** (update rules, objectives): keep them in the note
  even if the fixed template doesn't name a slot for them.
