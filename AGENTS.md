# Agent Engineering Guidelines

These rules apply to every AI agent working in this repository.

## Code Quality and Design

Write concise, adaptable code where every abstraction serves a concrete purpose.
Avoid speculative plumbing, unused indirection, and duplicate boilerplate.

In Rust (`pitwall` and Tauri code), factor repeated patterns into shared
traits and generalizable declarative macros rather than copy-pasting per-metric
or per-channel logic.  Prefer functional paradigms: pure functions, iterator
combinators, immutable data pipelines, and explicit algebraic types for units
and states so invalid states fail to compile.

## Comments

Place one concise architectural block comment at the top of each non-trivial
module stating its role, coordinate or unit conventions, and invariants.  Keep
inline comments terse and factual, documenting only non-trivial invariants,
preconditions, return semantics, or algorithmic constraints.

Do not write conversational narration, restatements of obvious syntax, or
self-congratulatory rationales ("We do X so that Y works cleanly!").  If the
code already says *what* it does, omit the comment.

## Documentation

Never use bullet points or ordered lists in `README.md` files.  Write READMEs
in concise technical prose paragraphs and raw code blocks.

## Testing and Validation

Never write circular "echo tests" that merely re-assert internal assumptions or
plumbing (for example, checking that a prior constant is positive or that a flag
persists in a database without exercising the solver).

Every behavioral change—especially in telemetry ingestion, physics models,
constraints, or proposal generation—must be validated against a concrete,
expected outcome on real or end-to-end inputs:

- **Solver / toggle changes:** Run the full planner or optimiser with the flag
  OFF vs. ON and assert the expected physical direction and non-degeneracy of
  the proposed setup changes.
- **Physics / simulation models:** Validate predictions on holdout pairs of
  real LMU `.duckdb` recordings (calibrate on stint A, predict stint B's
  measured `ua`, `a_lat`, `v_min`, or straight-line speed).  A model term that
  does not improve real-data holdout accuracy does not ship.
- **Telemetry ingestion:** Test against real LMU channel layouts (including
  fixed-rate channels without `ts` columns) so silent `KeyError` fallbacks are
  impossible.
- **Silent constraint collapses:** Optimiser gates must expose rejection counts
  so a broken constraint cannot masquerade as an empty "already optimal" result.

## Git and Commit Conventions

Never commit directly to `main`.  Create a branch using `<type>/<kebab-case>`
naming matching the repository history (`fix/...`, `feat/...`, `improve/...`).

Write commit messages with a concise imperative subject line (under 72
characters) followed by a blank line and one or two short technical paragraphs
stating what changed and why.  Do not commit scratch notes or planning markdown
files alongside code fixes unless explicitly requested.

