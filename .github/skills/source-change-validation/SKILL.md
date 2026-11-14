---
name: source-change-validation
description: Mandatory final validation for every coding task that changes source code. Run Black and the full test suite before declaring the task finished.
---

# Source change validation

Use this skill whenever a task changes source code, regardless of whether the
change also includes documentation, configuration, or tests.

Before declaring the task finished:

1. Run `black --check .` from the repository root.
2. Run the complete test suite with `pytest -q` from the repository root.
3. If Black reports formatting issues in files changed for the task, format
   those files with Black and rerun the check. Do not reformat unrelated files.
4. Fix failures caused by the task, then rerun both commands.

Do not report validation as passing unless both commands succeed. If either
command cannot run or still fails for a reason unrelated to the change, state
the exact command and failure clearly rather than claiming the task is fully
validated. Do not skip the full suite in favor of only targeted tests.
