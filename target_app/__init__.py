"""A deliberately legacy-hostile back-office app for the agent to drive.

This is the *target*, not part of the system under test. Nothing in `src/`
imports it. It exists so Phase 1 runs against a surface with the properties the
brief cares about — server-rendered HTML, table-based layout, no test IDs,
labels that are adjacent table cells rather than `<label for>`, and content
inside an iframe — plus a fault-injection switch for the error paths.
"""
