---
name: Bug report
about: Something in articling doesn't work as expected
title: ""
labels: bug
assignees: ""
---

**Describe the bug**
A clear description of what's wrong, and what you expected instead.

**Repro**
- Format: DOCX / PPTX / XLSX / PDF
- Minimal input that reproduces it (attach the file if you can share it, or a
  redacted/synthetic version — see note below)
- Command / code that triggers it, e.g.:
  ```bash
  python -m articling.cli file.xlsx --format json -o out.json
  ```
- Full traceback, if any

**Environment**
- articling version (`pip show articling`)
- Python version
- OS

**⚠️ Please don't attach real/confidential documents.** If the input file is
sensitive, reproduce the issue with a minimal synthetic file instead (see
`tests/fixtures/make_fixtures.py` for how test fixtures are built) and
describe what's structurally special about the original.
