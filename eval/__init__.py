"""The evaluation harness.

A first-class module, not a script: it is the part of this project that turns a demo into something
defensible, and every number in the README is produced by it.

``items``    the golden set and the multi-turn set, and the rule that an unlabelled draft cannot be
             scored.
``metrics``  every metric in the brief's section 5.2, computed from trace events and from nothing
             else.
``report``   ``summary.json`` and ``report.md``.
``judges``   versioned judge prompts, as files, with the judge model recorded in the results.
``harness``  running one item and collecting its trace.
``run``      the command-line entry point.  Requires a live API key and is never invoked by pytest.

It lives at the top level rather than inside ``consilium/`` because it is not part of the package a
user installs: it depends on the package, on the golden set, and on a live provider, and shipping it
in the wheel would ship the last two by implication.  The consequence is that ``consilium eval``
imports it lazily and says so when it is missing.
"""
