// Trusted execution runtime. No model providers, tools, credentials or policy decisions.
import { createInterface } from "node:readline";
import { BACKGROUND_CONTEXT as ctx } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import { Harness, SessionFailed, StorageRequestError, createRegistry, defineDoc, defineExtension, defineTask } from "@earendil-works/pi-durable";
import { SqliteStorage } from "@earendil-works/pi-durable/storage/sqlite";
import { openNodeSqliteDatabase } from "@earendil-works/pi-durable/storage/sqlite/node";

type Input = { attemptId: string; digest: string };
// A fixed vocabulary: the controller classifies by reason and never reads free text.
type Reason = "storage" | "bridge" | "inputs_changed" | "engine";
class BridgeError extends Error {
  readonly reason: Reason;
  constructor(reason: Reason, message: string) { super(message); this.reason = reason; }
}
function reasonOf(error: unknown): Reason {
  if (error instanceof BridgeError) return error.reason;
  if (error instanceof SessionFailed || error instanceof StorageRequestError) return "storage";
  return "engine";
}
type Observation = { status: string };
const lines = createInterface({ input: process.stdin });
const iterator = lines[Symbol.asyncIterator]();
const write = (value: object) => process.stdout.write(JSON.stringify(value) + "\n");
async function read(): Promise<Record<string, unknown>> {
  const line = await iterator.next();
  if (line.done || line.value.length > 65536) throw new BridgeError("bridge", "Invalid runtime input");
  let value: unknown;
  try { value = JSON.parse(line.value); } catch { throw new BridgeError("bridge", "Invalid runtime input"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new BridgeError("bridge", "Invalid runtime input");
  return value as Record<string, unknown>;
}

let sequence = 0;
async function call(operation: "observe" | "execute", input: Input): Promise<Observation> {
  const id = ++sequence;
  write({ version: 1, type: "request", id, operation, ...input });
  const reply = await read();
  if (reply.version !== 1 || reply.id !== id || typeof reply.status !== "string") throw new BridgeError("bridge", "Invalid runtime response");
  return { status: reply.status };
}

const Attempts = defineDoc<{ items: Record<string, { taskId: number; digest: string }> }>({
  kind: "factory.dispatches", version: 1, scope: "session", initial: () => ({ items: {} }),
  checkpointWhen: () => false,
});
const Dispatch = defineTask<Input, { phase: "execute" }, Observation>({
  name: "factory.cli-dispatch", version: 1, initial: () => ({ phase: "execute" }),
  phases: {
    execute: async (task, runtime, context) => {
      // Reconcile both stores before an effect. Never replay an active native CLI.
      let result = await call("observe", task.input);
      if (result.status === "leased") result = await call("execute", task.input);
      await runtime.commit(() => ({
        status: "terminal",
        outcome: result.status === "succeeded"
          ? { status: "completed", result }
          : { status: "failed", error: { message: `Controller attempt is ${result.status}` } },
      }), context);
    },
  },
  abort: async (_task, runtime, context) => {
    await runtime.commit(() => ({ status: "terminal", outcome: { status: "aborted" } }), context);
  },
});

async function openHarness(path: string): Promise<Harness> {
  const registry = createRegistry();
  registry.install(defineExtension({ name: "factory-cli-runtime", tasks: [Dispatch] }));
  const database = await openNodeSqliteDatabase(path);
  // The application requires power-failure durability beyond upstream's NORMAL default.
  await database.exec("PRAGMA synchronous = FULL");
  return Harness.open(await SqliteStorage.open(database), { models: createModels(), registry }, ctx);
}

let harness: Harness | undefined;
try {
  if (process.argv.includes("--preflight")) {
    // Exercise the exact imports and startup path, never a controller database.
    harness = await openHarness(":memory:");
    await harness.root(ctx);
    write({ version: 1, type: "ready" });
  } else {
    const init = await read();
    if (init.version !== 1 || typeof init.database !== "string" ||
        typeof init.attemptId !== "string" || !/^att_[a-f0-9]+$/.test(init.attemptId) ||
        typeof init.digest !== "string" || !/^[a-f0-9]{64}$/.test(init.digest)) throw new BridgeError("bridge", "Invalid dispatch identity");
    harness = await openHarness(init.database);
    const root = await harness.root(ctx);
    const attemptId = init.attemptId;
    const digest = init.digest;
    const taskId = await root.commit(async (tx) => {
      const doc = await tx.doc(Attempts);
      const existing = doc.items[attemptId];
      if (existing) {
        if (existing.digest !== digest) throw new BridgeError("inputs_changed", "Dispatch inputs changed");
        return existing.taskId;
      }
      const id = await tx.createTask(Dispatch, { attemptId, digest }, { ownership: { kind: "conversation" } });
      doc.items[attemptId] = { taskId: id, digest };
      return id;
    }, ctx);
    // Do not resume unrelated tasks: the bridge grants authority for this attempt only.
    const task = await harness.waitForTask(taskId as Parameters<Harness["waitForTask"]>[0], ctx);
    write({ version: 1, type: "result", attemptId, digest, outcome: task.state.outcome.status });
  }
} catch (error) {
  write({ version: 1, type: "error", reason: reasonOf(error), message: "Durable dispatch failed; inspect controller evidence" });
  process.exitCode = 2;
} finally {
  await harness?.close(ctx);
  lines.close();
}
