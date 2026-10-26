// A logic-free bridge from World Model's conformance runner to the Python engine.
//
// It holds no World Model logic. It starts one Python process running
// `python -I -m stemma.register.wm.rpc` (a generic RPC server over the very
// package the claim register imports) and forwards every constructor call,
// method call and property read, on the module or on any object a call returns,
// by name. Values cross as JSON; Python errors are rethrown as JavaScript Errors.
//
// The runner calls some methods synchronously, so calls block: a worker thread
// owns the Python process's pipes, and the main thread waits on a
// SharedArrayBuffer with Atomics.wait and takes each reply with
// receiveMessageOnPort.
//
// Generic rule for function-valued arguments (the runner passes a clock): a
// function is sent as {"$fn": id}; when Python calls it, the server sends a
// "callback" message, this side runs the function with the given arguments and
// sends its return value back, and Python's call returns that value. Dates cross
// as {"$date": ISO}. The same rule applies to any function, anywhere in the args.
//
// Interpreter: $STEMMA_PYTHON, default <repo>/.venv/bin/python.

import path from "node:path";
import { fileURLToPath } from "node:url";
import { MessageChannel, receiveMessageOnPort, Worker } from "node:worker_threads";

const here = path.dirname(fileURLToPath(import.meta.url));
const python = process.env.STEMMA_PYTHON ?? path.join(here, "..", ".venv", "bin", "python");
const RESERVED = new Set(["$ref", "$fn", "$date", "$undef", "$map"]);
const WAIT_MS = 1000; // resource limit: how long one Atomics.wait sleeps before re-checking

const WORKER = `
const { workerData } = require("node:worker_threads");
const { spawn } = require("node:child_process");
const readline = require("node:readline");
const { port, sab, python, args } = workerData;
const signal = new Int32Array(sab);
const post = (msg) => { port.postMessage(msg); Atomics.add(signal, 0, 1); Atomics.notify(signal, 0); };
const child = spawn(python, args, { stdio: ["pipe", "pipe", "inherit"] });
child.on("error", (err) => post(JSON.stringify({ op: "exit", detail: String(err) })));
child.on("exit", (code, sig) => post(JSON.stringify({ op: "exit", detail: "python exited with " + (sig ?? code) })));
readline.createInterface({ input: child.stdout }).on("line", (line) => post(line));
port.on("message", (line) => child.stdin.write(line + "\\n"));
`;

const sab = new SharedArrayBuffer(4);
const signal = new Int32Array(sab);
const { port1, port2 } = new MessageChannel();
const worker = new Worker(WORKER, {
  eval: true,
  workerData: { port: port2, sab, python, args: ["-I", "-m", "stemma.register.wm.rpc"] },
  transferList: [port2],
});
worker.unref();

const functions = new Map<number, Function>();
let nextFn = 1;

function receive(): any {
  for (;;) {
    const seen = Atomics.load(signal, 0);
    const got = receiveMessageOnPort(port1);
    if (got) return JSON.parse(got.message);
    Atomics.wait(signal, 0, seen, WAIT_MS);
  }
}

// Send one request and block until its reply, serving callbacks meanwhile.
function request(message: object): any {
  port1.postMessage(JSON.stringify(message));
  for (;;) {
    const reply = receive();
    if (reply.op === "result") return decode(reply.value);
    if (reply.op === "error") {
      const err = new Error(reply.error.message);
      err.name = reply.error.type;
      (err as any).pythonTraceback = reply.error.traceback;
      throw err;
    }
    if (reply.op === "callback") {
      let answer: object;
      try {
        const fn = functions.get(reply.fn)!;
        answer = { op: "return", value: encode(fn(...reply.args.map(decode))) };
      } catch (e) {
        answer = { op: "throw", error: { name: (e as Error).name, message: (e as Error).message } };
      }
      port1.postMessage(JSON.stringify(answer));
      continue;
    }
    throw new Error(`bridge: Python process ended (${reply.detail ?? JSON.stringify(reply)})`);
  }
}

function encode(value: any): any {
  if (value === undefined) return null;
  if (value === null || typeof value !== "object") {
    if (typeof value === "function") {
      const id = nextFn++;
      functions.set(id, value);
      return { $fn: id };
    }
    return value;
  }
  if (value instanceof Date) return { $date: value.toISOString() };
  if (Array.isArray(value)) return value.map(encode);
  if (refIds.has(value)) return { $ref: refIds.get(value) };
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(value)) if (v !== undefined) out[k] = encode(v);
  const keys = Object.keys(out);
  return keys.length === 1 && RESERVED.has(keys[0]) ? { $map: out } : out;
}

function decode(value: any): any {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map(decode);
  const keys = Object.keys(value);
  if (keys.length === 1 && RESERVED.has(keys[0])) {
    const inner = value[keys[0]];
    if (keys[0] === "$map") return Object.fromEntries(Object.entries(inner).map(([k, v]) => [k, decode(v)]));
    if (keys[0] === "$date") return new Date(inner);
    if (keys[0] === "$undef") return undefined;
    if (keys[0] === "$ref") return remote(inner.id, new Set(inner.methods));
    if (keys[0] === "$fn") return functions.get(inner);
  }
  return Object.fromEntries(keys.map((k) => [k, decode(value[k])]));
}

const refIds = new WeakMap<object, number>();
const released = new FinalizationRegistry<number>((id) => {
  try { port1.postMessage(JSON.stringify({ op: "release", ids: [id] })); receive(); } catch { /* exiting */ }
});

// A Python object: callable (and constructible), its methods forwarded by name,
// any other property read forwarded as a "get".
function remote(id: number, methods: Set<string>): any {
  const call = (name: string | null, args: any[]) =>
    request({ op: "call", target: id, name, args: args.map(encode) });
  const proxy: any = new Proxy(function () {} as any, {
    apply: (_t, _self, args) => call(null, args),
    construct: (_t, args) => call(null, args),
    get: (_t, prop) => {
      if (typeof prop !== "string") return undefined;
      if (methods.has(prop)) return (...args: any[]) => call(prop, args);
      if (prop === "then") return undefined; // not a promise
      return request({ op: "get", target: id, name: prop });
    },
  });
  refIds.set(proxy, id);
  if (id !== 0) released.register(proxy, id);
  return proxy;
}

const root = remote(0, new Set()); // the package: every attribute read is a "get"

// The names the runner imports. Each is the Python package attribute of that name,
// read once here; classes and functions arrive as callable proxies.
export const WorldModel = root.WorldModel;
export const entityIdFromName = root.entityIdFromName;
export const normalizeName = root.normalizeName;
export const decide = root.decide;
