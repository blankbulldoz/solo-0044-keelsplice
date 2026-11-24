"""Python 3.11 -> 3.14 upgrade workbench for Keelsplice.

The modules in this package are the deliverable upgrade artifacts:

* ``runtime_probe``  - inventory of one interpreter, split into Python runtime
  facts and SQLite library/capability facts, with a two-report comparator.
* ``http_form_probe`` - exact wire-byte checks of the browser form surface and
  the JSON parity endpoint, runnable under either interpreter.
* ``fixture_probe``  - independent SQLite re-reads of certified fixture bytes
  and of the sealed repository state.
* ``legacy_repo``    - phase 1: build the legacy repository with a real 3.11.
* ``switch``         - phases 2-5: freeze, verify, cutover, new capture and
  executable rollback.
* ``run_upgrade``    - end-to-end driver chaining every phase as subprocesses
  under the two real interpreters.

Every entry point works with ``python -I``; scripts insert the project root
onto ``sys.path`` themselves because isolated mode ignores PYTHONPATH and the
script directory.
"""
