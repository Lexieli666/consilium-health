"""Test package.

An ``__init__.py`` so that ``tests.stubs`` has exactly one module name.  Without it, a helper module
inside ``tests/`` is importable as both ``stubs`` and ``tests.stubs``, and mypy refuses to check a
file it can reach under two names.
"""
