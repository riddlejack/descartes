# World Model conformance: result

World Model's own conformance runner, unmodified, passes all eight of its checks
against the Python engine that the claim register imports (`stemma.register.wm`),
through a bridge that holds no World Model logic.

Local absolute paths in the recorded output below are replaced by placeholders
(`<REPO>` for this repository, `<TMPDIR>` for the system temporary folder).

## The command

From the repository root:

    sh conformance/run.sh

which runs, inside `conformance/`:

    WORLD_MODEL_IMPL=<abs path>/conformance/bridge.ts ./node_modules/.bin/tsx vendor/runner.ts

Environment of the recorded run: Node v24.14.1, tsx 4.23.0 (pinned in
`package.json`, locked in `package-lock.json`), Python 3.12.13 from `.venv/bin/python`
(override with `STEMMA_PYTHON`).

## The runner is the upstream file

`vendor/runner.ts` is copied byte for byte from the Meterless repository at commit
1742dec56a66e58eef9dda2d2b865823758ca8cd, path
`engines/world-model/conformance/runner.ts` (see `vendor/NOTICE`; licence in
`vendor/LICENSE`, Apache-2.0).

    $ git -C <meterless clone> show 1742dec56a66e58eef9dda2d2b865823758ca8cd:engines/world-model/conformance/runner.ts | shasum -a 256
    7cd3b80d62d45d16f3342c62a91c89abf819c9b46970187d2f36fb9e3050da82  -
    $ shasum -a 256 conformance/vendor/runner.ts
    7cd3b80d62d45d16f3342c62a91c89abf819c9b46970187d2f36fb9e3050da82  conformance/vendor/runner.ts

## Passing output (exit code 0)

    
    World Model conformance: implementation = <REPO>/conformance/bridge.ts
    
      PASS sid-001 stable ids: diacritics and NBSP normalize identically (NFKD + strip marks)
      PASS sid-002 stable ids: distinct names stay distinct (no over-normalization)
      PASS band-001 merge bands at the boundaries: 0.85 auto, 0.84 review, 0.82 review, 0.81 new
      PASS prov-001 writes without provenance are rejected
      PASS idem-001 ingest idempotency: same batch twice produces an empty diff
      PASS idem-002 the idempotency check can fail: a mutated batch changes the shape
      PASS bitemp-001 bitemporal edges: a superseding edge closes the old one; point-in-time queries respect validity
      PASS audit-001 every mutation writes an audit entry in order
    
      8/8 checks passed

## Why this is the production code passing

The bridge (`bridge.ts`) imports only Node built-ins (`node:path`, `node:url`,
`node:worker_threads`, and in its worker `node:child_process` and `node:readline`).
It contains no World Model rule: no id formula, no band, no provenance check, no
log. It starts `python -I -m stemma.register.wm.rpc`, a generic JSON-lines server
in the same package, which looks up whatever name it is sent on
`stemma.register.wm` (or on an object an earlier call returned) and calls it. So
the runner's `new WorldModel(...)`, `normalizeName(...)`,
`world.view("graph").neighborsOf(...)` are the Python package's own `WorldModel`,
`normalizeName` and `GraphView.neighborsOf`: the module the register imports. The
only names the bridge spells out are the four exports the runner requires
(ES modules need static export names); each is read from the Python package by
that name.

How the transport works: the runner calls some methods synchronously, so every
call blocks. A worker thread owns the Python process's pipes; the main thread
posts a request on a MessagePort and sleeps in `Atomics.wait` on a
SharedArrayBuffer until the worker signals a reply, then takes it with
`receiveMessageOnPort`. Python objects that are not JSON data (the WorldModel,
its views) come back as references and become JavaScript proxies that forward
method calls and property reads by name. Python exceptions come back with their
type and message and are rethrown as JavaScript `Error`s (`name` is the Python
type).

The generic rule for function-valued arguments: any JavaScript function, anywhere
in the arguments, is sent as `{"$fn": id}`. When Python calls it, the server sends
a `callback` message and blocks; the bridge runs the function with the given
arguments and sends back its return value (a `Date` crosses as
`{"$date": ISO}`), and the Python call returns that. This is how the runner's
`clock: () => new Date(...)` reaches the engine. The runner's eight checks never
trigger an operator action, so they never call the clock; `selftest.ts` exercises
the callback path (an `approveMerge` whose `resolvedAt` comes from a JavaScript
clock), plus error rethrow, reserved-key escaping, Dates and sub-object calls:

    $ cd conformance && ./node_modules/.bin/tsx selftest.ts
      PASS sync return of a record - ent_14c164fe5e7e6bb3
      PASS review band reaches the queue
      PASS JS clock called back from Python - 1 2030-01-02T03:04:05.678Z
      PASS Python error rethrown as Error with its type - UnknownRecordError
      PASS reserved key round-trips as data - {"$ref":{"id":0}}
      PASS Date argument crosses as a timestamp - 2026-02-03T00:00:00.000Z
      PASS sub-object methods forwarded
      PASS missing attribute reads as undefined
      PASS namespace attribute read forwarded
      PASS decide is the Python function
    
      bridge selftest: all passed

## The runner fails when the Python engine is broken

Each breakage below was a temporary local edit to the Python engine, reverted with
`git checkout` straight after the run; the bridge was not touched. Each makes
exactly the check that guards that rule fail, and the runner exits 1.

Breakage 1: the auto-merge boundary read as `>` instead of `>=`.

    diff --git a/src/stemma/register/wm/resolve.py b/src/stemma/register/wm/resolve.py
    index 46498d8..b57bcb7 100644
    --- a/src/stemma/register/wm/resolve.py
    +++ b/src/stemma/register/wm/resolve.py
    @@ -60,7 +60,7 @@ def decide(
             raise TypeError(f"decide needs a number, got {type(similarity).__name__}")
         if math.isnan(similarity):
             raise ValueError("decide: similarity is NaN")
    -    if autoMergeAt is not None and similarity >= autoMergeAt:
    +    if autoMergeAt is not None and similarity > autoMergeAt:
             return "auto-merge"
         if similarity >= reviewLower:
             return "review"

    
    World Model conformance: implementation = <REPO>/conformance/bridge.ts
    
      PASS sid-001 stable ids: diacritics and NBSP normalize identically (NFKD + strip marks)
      PASS sid-002 stable ids: distinct names stay distinct (no over-normalization)
      FAIL band-001 merge bands at the boundaries: 0.85 auto, 0.84 review, 0.82 review, 0.81 new
           0.85 exactly is auto-merge (>= reading)
      PASS prov-001 writes without provenance are rejected
      PASS idem-001 ingest idempotency: same batch twice produces an empty diff
      PASS idem-002 the idempotency check can fail: a mutated batch changes the shape
      PASS bitemp-001 bitemporal edges: a superseding edge closes the old one; point-in-time queries respect validity
      PASS audit-001 every mutation writes an audit entry in order
    
      7/8 checks passed
    exit=1

Breakage 2: a new open edge no longer closes the old one.

    diff --git a/src/stemma/register/wm/model.py b/src/stemma/register/wm/model.py
    index b1e5aa6..8ce950e 100644
    --- a/src/stemma/register/wm/model.py
    +++ b/src/stemma/register/wm/model.py
    @@ -748,7 +748,7 @@ class WorldModel:
                 rel["validTo"] = valid_to
             closes: list[Relationship] = []
             later: list[Relationship] = []
    -        if not multi and valid_to is None:
    +        if False and not multi and valid_to is None:
                 for other in st.iter_relationships():
                     if (
                         "validTo" in other

    
    World Model conformance: implementation = <REPO>/conformance/bridge.ts
    
      PASS sid-001 stable ids: diacritics and NBSP normalize identically (NFKD + strip marks)
      PASS sid-002 stable ids: distinct names stay distinct (no over-normalization)
      PASS band-001 merge bands at the boundaries: 0.85 auto, 0.84 review, 0.82 review, 0.81 new
      PASS prov-001 writes without provenance are rejected
      PASS idem-001 ingest idempotency: same batch twice produces an empty diff
      PASS idem-002 the idempotency check can fail: a mutated batch changes the shape
      FAIL bitemp-001 bitemporal edges: a superseding edge closes the old one; point-in-time queries respect validity
           open edge should point at team b; got [team:g:a,team:g:b]
      PASS audit-001 every mutation writes an audit entry in order
    
      7/8 checks passed
    exit=1

Breakage 3: a write without provenance is given a default one instead of refused.

    diff --git a/src/stemma/register/wm/validate.py b/src/stemma/register/wm/validate.py
    index 12fceed..d7e132d 100644
    --- a/src/stemma/register/wm/validate.py
    +++ b/src/stemma/register/wm/validate.py
    @@ -80,6 +80,8 @@ def optional_text(data: Mapping[str, Any], key: str, op: str) -> str | None:
     
     def provenance(source: Any, op: str) -> Provenance:
         """No provenance, no write."""
    +    if source is None:
    +        source = {"kind": "manual", "at": "2026-01-01"}
         if source is None:
             raise ProvenanceError(f"{op}: no provenance; every write carries `source` (kind, at)")
         if not isinstance(source, Mapping):

    
    World Model conformance: implementation = <REPO>/conformance/bridge.ts
    
      PASS sid-001 stable ids: diacritics and NBSP normalize identically (NFKD + strip marks)
      PASS sid-002 stable ids: distinct names stay distinct (no over-normalization)
      PASS band-001 merge bands at the boundaries: 0.85 auto, 0.84 review, 0.82 review, 0.81 new
      FAIL prov-001 writes without provenance are rejected
           no provenance, no write
      PASS idem-001 ingest idempotency: same batch twice produces an empty diff
      PASS idem-002 the idempotency check can fail: a mutated batch changes the shape
      PASS bitemp-001 bitemporal edges: a superseding edge closes the old one; point-in-time queries respect validity
      PASS audit-001 every mutation writes an audit entry in order
    
      7/8 checks passed
    exit=1

## The same runner against the file-backed store (26 October 2026)

The runner only ever asks for `storage: "memory"`, so by itself it does not exercise the
file log the register runs on. `STEMMA_WM_MEMORY_AS_FILE=<folder>` (read in
`WorldModel.__init__`, unset in runs) makes every memory world a fresh file log, and
`sh conformance/run-file.sh` runs the unmodified runner that way and fails if no log
was written. Output:

```

World Model conformance: implementation = <REPO>/conformance/bridge.ts

  PASS sid-001 stable ids: diacritics and NBSP normalize identically (NFKD + strip marks)
  PASS sid-002 stable ids: distinct names stay distinct (no over-normalization)
  PASS band-001 merge bands at the boundaries: 0.85 auto, 0.84 review, 0.82 review, 0.81 new
  PASS prov-001 writes without provenance are rejected
  PASS idem-001 ingest idempotency: same batch twice produces an empty diff
  PASS idem-002 the idempotency check can fail: a mutated batch changes the shape
  PASS bitemp-001 bitemporal edges: a superseding edge closes the old one; point-in-time queries respect validity
  PASS audit-001 every mutation writes an audit entry in order

  8/8 checks passed
file logs written: 5 (in <TMPDIR>/wm-conformance-file.DZF3X0)
```
