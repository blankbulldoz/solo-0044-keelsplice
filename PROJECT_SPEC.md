# Keelsplice

Keelsplice is a local Python HTTP service for preparing self-contained relational fixtures from existing SQLite databases. A database engineer gives it named tables and typed primary-key tuples, chooses which foreign keys to traverse in reverse, and receives a standalone SQLite subdatabase with a traceable selection and a certificate. Parent dependencies are always followed. The default deployment binds to loopback and reads only administrator-registered database files.

## Workflows

1. Inspect a registered source to see canonical table and column names, primary-key order, and resolved foreign keys.
2. Register a named immutable recipe version against that source schema. Versions pin schema content and seed specification.
3. Retire a version to stop new captures while retaining its description and captures.
4. Preview the relationship closure and budget usage without storing rows.
5. Capture DDL, selected row values and witnesses inside one source read transaction and seal the record.
6. Trace one captured row to its direct causes, original seed positions and outgoing dependencies.
7. Build an empty destination schema, insert the saved rows, and check foreign keys before storing the artifact.
8. Reopen and certify the resulting artifact against the sealed schema and typed row contents.
9. Compare two captures with identical schema, reporting row additions, removals and modifications.
10. Deliver the certified SQLite bytes with their SHA-256 digest.

The HTTP transport invokes the service coordinator. Storage owns read-only source transactions and writable local recipe/artifact state. Catalog inspection follows SQLite's primary-key and foreign-key naming semantics. The recipe module normalizes request inputs. Capture owns graph traversal and snapshot provenance. The value codec preserves SQLite storage classes and typed identities. Bundle rebuilds a database from captured facts and provides independent post-build checks.

## Domain contracts

- The source is a main-schema SQLite file containing ordinary tables with declared primary keys. Selected rows need non-NULL primary-key components. SQL views and triggers are outside the handoff contract. Virtual/shadow tables and main databases with no ordinary tables are rejected. Use built-in SQLite affinity, collations and generated expressions.
- Partial and expression indexes are preserved when their SQL uses SQLite built-ins. PK and UNIQUE constraints declared in tables, and separately created indexes, are preserved. Tables with no selected rows retain their definitions.
- Parent lookup resolves composite and implicit targets using SQLite comparison semantics. A child key with any NULL component creates no required dependency. Explicit traversal toward children repeats to a fixed point over the requested edge set. Each row is expanded once and all direct witnesses are retained.
- A capture is sealed only after the graph and row budgets succeed. Its recipe version and source schema identity are part of the stored result. Source writes can resume without changing an already sealed result.
- Rebuilding and certifying may be retried. Only certified bytes are exposed by download. Certification checks the destination file's structure and rows against sealed data. It doesn't make the service a backup of SQLite metadata, deleted pages, triggers or live application side effects.

The examples exercise the production route with disposable source and repository files. Automated tests are deferred for this baseline.
