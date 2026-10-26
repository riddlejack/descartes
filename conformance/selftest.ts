// Checks of the bridge's transport itself (not of World Model): synchronous
// returns, callbacks into JavaScript (the clock rule), Python errors rethrown,
// reserved-key escaping, Dates, and sub-object method calls.
//   ./node_modules/.bin/tsx selftest.ts
import * as wm from "./bridge.ts";

let failures = 0;
const check = (name: string, ok: boolean, detail = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"} ${name}${detail ? " - " + detail : ""}`);
  if (!ok) failures++;
};

const src = (at: string) => ({ kind: "manual", at });
let clockCalls = 0;
const world = new wm.WorldModel({
  storage: "memory",
  clock: () => {
    clockCalls++;
    return new Date("2030-01-02T03:04:05.678Z");
  },
});

const e = world.upsertEntity({ type: "person", name: "Jerome Example abc", source: src("2026-01-01T00:00:00Z") });
check("sync return of a record", e.id === wm.entityIdFromName("person", "Jerome Example abc"), e.id);
world.upsertEntity({ type: "person", name: "Jerome Example x", source: src("2026-01-01T00:00:00Z") });
const queue = world.mergeQueue();
check("review band reaches the queue", queue.length === 1 && queue[0].status === "pending");
const done = world.approveMerge(queue[0].id, "operator-1");
check("JS clock called back from Python", clockCalls === 1 && done.resolvedAt === "2030-01-02T03:04:05.678Z", `${clockCalls} ${done.resolvedAt}`);

let thrown: any;
try {
  world.relate({ from: e.id, type: "t", to: "missing", source: src("2026-01-01T00:00:00Z") });
} catch (err) {
  thrown = err;
}
check("Python error rethrown as Error with its type", thrown instanceof Error && thrown.name === "UnknownRecordError", thrown?.name);

const odd = world.upsertEntity({ type: "x", name: "odd", attrs: { $ref: { id: 0 } }, source: src("2026-01-01T00:00:00Z") });
check("reserved key round-trips as data", JSON.stringify(odd.attrs) === '{"$ref":{"id":0}}', JSON.stringify(odd.attrs));

const at = new Date("2026-02-03T00:00:00.000Z");
const dated = world.upsertEntity({ type: "x", name: "dated", source: { kind: "manual", at } });
check("Date argument crosses as a timestamp", dated.createdAt === "2026-02-03T00:00:00.000Z", dated.createdAt);

const graph = world.view("graph");
check("sub-object methods forwarded", graph.entity(e.id)?.id === e.id);
check("missing attribute reads as undefined", (world as any).noSuchThing === undefined);
check("namespace attribute read forwarded", world.namespace === null);
check("decide is the Python function", wm.decide(0.85) === "auto-merge" && wm.decide(0.81) === "new");

console.log(failures === 0 ? "\n  bridge selftest: all passed" : `\n  bridge selftest: ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
