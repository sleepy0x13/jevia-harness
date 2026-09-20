---
name: Extraction
description: Pulling specific values out of material into a fixed shape.
when: The task asks for particular fields, a table, a list, or structured output from unstructured material.
---

Return exactly the fields asked for, in the order asked for.

**Copy values, do not normalise them** unless told to. A date written "2 Jan"
stays "2 Jan". Preserve the original spelling of names.

**Mark absence explicitly.** A field with no value in the material is `null` or
"not stated" — never an empty string, never a guess, never a plausible default.
This is the single most important rule: an invented value is worse than a
missing one because nobody can tell it apart.

**Never infer.** If the material says "shipped last week" and the task wants a
date, the answer is "not stated" unless a date appears.

When the material contains several candidates for one field, return the one the
task's wording points at and note the ambiguity in one line.
