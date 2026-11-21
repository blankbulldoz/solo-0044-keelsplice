# Keelsplice

Keelsplice is a Python HTTP service that turns a selection of SQLite rows into a self-contained relational fixture. Give it primary-key roots and the relationships whose child rows you want to include. It resolves required parents, records why each row was included, and hands off a standalone SQLite file after verifying its contents.

## Run locally

Requirements are Python 3.11+ with SQLite 3.37+ and an available loopback port. The service uses the Python standard library. The same command works with multiple `--source ALIAS=FILE` options. The state directory must already exist. Use a disposable example or an operator-managed source file.

```sh
python3 -m compileall -q keelsplice
python3 -m examples.walkthrough all
```

The walkthrough starts a temporary service, loads `examples/microscope.sql` and visits the public HTTP routes. It exports a fixture and independently opens it with SQLite, including after a later source update. You can run a single stage by replacing `all` with its name. Automated tests are deferred.

To run a persistent local instance, first create a sample database in a new directory and then start the service from the project root:

```sh
mkdir keelsplice-local
python3 -c 'from examples.walkthrough import seed; seed("keelsplice-local/microscope.sqlite")'
python3 -m keelsplice --repository keelsplice-local/state.sqlite \
  --source microscope=keelsplice-local/microscope.sqlite --port 8794
```

## HTTP flow

| Method and route | Input and result |
| --- | --- |
| `GET /sources/{alias}/catalog` | Lists tables, ordered PKs, edges and indexes with the pinned schema digest. |
| `POST /recipes/{name}/versions` | Registers a recipe and allocates an immutable version. |
| `GET /recipes/{name}/versions/{version}` | Reads the recipe and its active status. |
| `POST /recipes/{name}/versions/{version}/retire` | Accepts `{}`. Stops future captures; existing captures are available. |
| `POST /preview` | Takes `{"name":"swatch","version":1}`. Runs the selection and returns counts and canonical roots. |
| `POST /captures` | Takes the same version reference. Stores the sealed result and returns its opaque ID. |
| `GET /captures/{id}` | Returns state, schema digest, canonical roots, counts and any certificate. |
| `POST /captures/{id}/trace` | Takes `{"table":"substrate","key":["East","Mica"]}` with the exact stored key. Returns direct causes and parent references, along with zero-based root indexes. |
| `POST /captures/{id}/build` | Accepts `{}`. Rebuilds SQLite from the stored selection. |
| `POST /captures/{id}/certify` | Accepts `{}`. Reads the file independently and certifies counts, schema, sealed result digest and file bytes. |
| `POST /compare` | Takes `{"left":"id-a","right":"id-b"}`. Lists typed primary-key tuples per table marked added, removed or modified. |
| `GET /captures/{id}/database` | Returns certified SQLite file bytes and the quoted SHA-256 `ETag`. |

For example, register a composite-key selection and enable the assay relationship back toward matching child rows:

```json
{
  "source": "microscope",
  "roots": [{"table": "substrate", "key": ["east", "MICA"]}],
  "follow_children": [{"child": "assay", "id": 0}],
  "row_limit": 20
}
```

Send bodies as `application/json`. Key order is the catalog PK order. Integers are signed 64-bit JSON numbers. Use `{"blob":"AAD/"}` for bytes and `{"real":"0x1.a000000000000p+1"}` for a real. Strings remain text. The initial lookup uses SQLite comparison rules, so the canonical root can differ from the supplied spelling or storage class. Capture and trace use the canonical identity. Arrays are retained in input order for roots. Results collapse repeated row identities but preserve root occurrences.

A preview represents one current SQLite read. A later capture reads the source again. A schema change requires a new recipe version even when row values match. The fixed point always includes every selected row's parent dependencies. The `follow_children` list permits traversal toward children on specific foreign keys. See the specification for supported schemas. Sources may use WAL and must remain readable by SQLite in normal read-only mode. The service is meant for an operator-controlled local process and accepts registered source aliases. A fixture contains original selected values.

Requests are limited to 256 KiB. Each recipe can name up to 256 roots and 256 child edge entries with a row limit of 1–5,000. Source query work, witness count and the encoded selection size are also bounded. An over-budget or unavailable source returns an error and leaves capture state untouched. Build or certification retries preserve the stage already reached. Errors return a stable `error` identifier and a human-readable `message`.
