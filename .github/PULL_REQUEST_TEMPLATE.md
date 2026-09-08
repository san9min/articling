## What does this change?

<!-- One or two sentences: what problem does this solve, and how? -->

## Checklist

- [ ] Read [CONTRIBUTING.md](../CONTRIBUTING.md) and [AGENTS.md](../AGENTS.md)
- [ ] Added/updated tests that verify a real graph invariant, format-specific
      edge case, or regression (not a restatement of library behavior)
- [ ] `pytest` passes locally
- [ ] If an extractor changed: ran `scaffold.check_invariants`
      (`articling.cli --check`) on affected output
- [ ] Updated `README.md` and, if user-facing behavior changed (CLI flags,
      SDK signatures, export shape), the packaged usage skill at
      `articling/.agents/skills/articling/SKILL.md`

## Anything else reviewers should know?

<!-- Design tradeoffs, follow-up work, things you're unsure about. -->
