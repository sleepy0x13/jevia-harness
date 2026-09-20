---
name: Code review
description: Judging a diff, a pull request, or a proposed change.
when: The material is code, a diff, a patch, or a description of a change to be judged before it lands.
---

Review in this order, and stop at the first category that has findings worth
raising — a style note next to a data-loss bug buries the bug.

1. **Correctness.** Does it do what it claims for the inputs it will actually
   see? Off-by-one, wrong branch, unhandled `None`, swapped arguments, a
   condition that can never be true.
2. **Blast radius.** What breaks for existing callers? Renamed or removed public
   names, changed defaults, changed error types, anything persisted in a new
   shape.
3. **Failure handling.** What happens when the network is down, the file is
   missing, the response is empty, the input is hostile? An unhandled case that
   throws is better than one that silently continues.
4. **Tests.** Is the changed behaviour covered by something that would fail if
   the change were reverted?
5. **Simplification.** Only after the above: dead code, a branch that cannot be
   reached, a helper used once.

For each finding give the location, what goes wrong, and the concrete input that
makes it go wrong. "Consider adding error handling" is not a finding. "A 404
from the pricing endpoint raises here instead of returning an empty list, so a
missing model takes the whole page down" is.

Say plainly when the change is fine. A review that manufactures findings to look
thorough trains people to ignore reviews.
