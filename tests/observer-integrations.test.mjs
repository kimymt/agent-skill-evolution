import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createObserver as piObserver } from "../integrations/pi/index.js";
import { createObserver as openCodeObserver } from "../integrations/opencode.js";
import { invoke } from "../integrations/bridge.mjs";

test("Pi records outcomes without shell rows and keeps recording errors visible", async () => {
  const state = await mkdtemp(join(tmpdir(), "observer-silent-"));
  process.env.AGENT_EVOLUTION_OBSERVER_STATE = state;
  try {
    let tool;
    piObserver({ on() {}, registerTool: value => { tool = value; } }, invoke, {});
    const ctx = { cwd: state, sessionManager: { getSessionId: () => "silent" } };
    for (const status of ["passed", "failed", "unverified", "cancelled"]) {
      await invoke("pi", { cwd: state, session_id: "silent", turn_id: status,
        hook_event_name: "UserPromptSubmit", prompt: "task" });
      const result = await tool.execute("id", { status, summary: "確認結果", skill: "verify-changes" }, null, null, ctx);
      assert.deepEqual(tool.renderCall({}, {}, { isError: false }).render(80), []);
      assert.deepEqual(tool.renderResult(result, {}, {}, { isError: false }).render(80), []);
      const stop = await invoke("pi", { cwd: state, session_id: "silent", hook_event_name: "Stop" });
      assert.equal(Boolean(stop.notice), status === "failed");
    }
    const error = { content: [{ type: "text", text: "記録できません" }] };
    assert.ok(tool.renderCall({}, {}, { isError: true }).render(80).length);
    assert.deepEqual(tool.renderResult(error, {}, {}, { isError: true }).render(80), ["記録できません"]);
    await assert.rejects(tool.execute("id", { status: "passed", summary: "cancel" }, { aborted: true }, null, ctx));
    let broken;
    piObserver({ on() {}, registerTool: value => { broken = value; } }, async () => ({}), {});
    await assert.rejects(broken.execute("id", { status: "passed", summary: "bad result" }, null, null, ctx));
  } finally {
    delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    await rm(state, { recursive: true, force: true });
  }
});

function piHarness(approved) {
  const handlers = new Map();
  const sent = [], decisions = [], dialogs = [], events = [];
  const pi = { on: (name, fn) => handlers.set(name, fn), sendUserMessage: text => sent.push(text), sendMessage: (...args) => sent.push(args) };
  const ctx = { cwd: "/tmp/work", sessionManager: { getSessionId: () => "session" }, hasUI: true,
    ui: { confirm: async (...args) => { dialogs.push(args); return approved; }, notify: () => {} } };
  const call = async (agent, payload, operation) => {
    if (operation) { decisions.push(operation); return { instruction: "read skill; prepare only" }; }
    events.push(payload);
    if (payload.hook_event_name === "Stop") return { notice: "a".repeat(32), question: "起動しますか？" };
    return { hookSpecificOutput: { additionalContext: "record final outcome" } };
  };
  piObserver(pi, call);
  return { handlers, ctx, sent, decisions, dialogs, events };
}

test("Pi asks only after settlement; decline triggers no model turn", async () => {
  const h = piHarness(false);
  await h.handlers.get("before_agent_start")({ prompt: "task" }, h.ctx);
  await h.handlers.get("tool_result")({ toolName: "bash", input: { command: "test" }, isError: true }, h.ctx);
  await h.handlers.get("agent_before_settle")({ outcome: "completed" }, h.ctx);
  assert.equal(h.dialogs.length, 0);
  await h.handlers.get("agent_settled")({}, h.ctx);
  assert.equal(h.dialogs.length, 1);
  assert.equal(h.decisions[0].at(-1), "decline");
  assert.equal(h.sent.length, 0);
});

test("Pi explicit acceptance starts preparation after recording consent", async () => {
  const h = piHarness(true);
  await h.handlers.get("agent_settled")({}, h.ctx);
  assert.equal(h.decisions[0].at(-1), "approve");
  assert.deepEqual(h.sent, ["read skill; prepare only"]);
});

test("Pi without a dialog never approves or starts a turn", async () => {
  const h = piHarness(true);
  h.ctx.hasUI = false;
  await h.handlers.get("agent_settled")({}, h.ctx);
  assert.equal(h.decisions.length, 0);
  assert.equal(h.sent[0][1].triggerTurn, false);
});

test("Pi records cancellation before settlement", async () => {
  const h = piHarness(false);
  await h.handlers.get("agent_before_settle")({ outcome: "aborted" }, h.ctx);
  assert.equal(h.events[0].hook_event_name, "Interrupt");
});

test("Pi completion does not collect final answer text", async () => {
  const h = piHarness(false);
  assert.equal(h.handlers.has("agent_end"), false);
  await h.handlers.get("agent_settled")({}, h.ctx);
  assert.equal("last_assistant_message" in h.events.at(-1), false);
});

test("OpenCode idle asks via UI without calling session.prompt", async () => {
  const toasts = [], decisions = [], events = [];
  const plugin = openCodeObserver({ directory: "/tmp/work", client: {
    tui: { showToast: async (value) => toasts.push(value) }, app: { log: async () => {} },
    session: { prompt: () => { throw new Error("must not call model"); } },
  } }, async (agent, payload, operation) => {
    if (operation) { decisions.push(operation); return { instruction: "[agent-evolution-approved] prepare only" }; }
    events.push(payload);
    if (payload.hook_event_name === "Stop") return { notice: "b".repeat(32), question: "起動しますか？" };
    return { hookSpecificOutput: { additionalContext: "record outcome" } };
  });
  const parts = text => ({ message: { id: "message" }, parts: [{ type: "text", text }] });
  await plugin["chat.message"]({ sessionID: "s" }, parts("task"));
  const system = { system: ["original"] };
  await plugin["experimental.chat.system.transform"]({ sessionID: "s" }, system);
  assert.deepEqual(system.system, ["original", "record outcome"]);
  const part = { type: "tool", sessionID: "s", callID: "call", tool: "bash", state: { status: "error", input: { command: "test" } } };
  await plugin.event({ event: { type: "message.part.updated", properties: { part } } });
  await plugin.event({ event: { type: "message.part.updated", properties: { part } } });
  assert.equal(events.filter(e => e.hook_event_name === "PostToolUse").length, 1);
  await plugin.event({ event: { type: "session.idle", properties: { sessionID: "s" } } });
  assert.equal(toasts.length, 1);
  assert.equal("last_assistant_message" in events.at(-1), false);
  assert.equal(decisions.length, 0);
  await plugin["chat.message"]({ sessionID: "s" }, parts("yes"));
  assert.equal(decisions.length, 0);
  await plugin["chat.message"]({ sessionID: "s" }, parts("evolution approve " + "b".repeat(32)));
  assert.equal(decisions[0].at(-1), "approve");
  assert.ok(events.at(-1).prompt.startsWith("[agent-evolution-approved]"));
});

test("JS bridge runs real Python lifecycle without model or raw-log storage", async () => {
  const state = await mkdtemp(join(tmpdir(), "observer-integration-"));
  process.env.AGENT_EVOLUTION_OBSERVER_STATE = state;
  try {
    const base = { cwd: state, session_id: "bridge", turn_id: "turn" };
    await invoke("pi", { ...base, hook_event_name: "UserPromptSubmit", prompt: "task" });
    await invoke("pi", { ...base, hook_event_name: "PostToolUse", tool_name: "bash", tool_input: { command: "private command" }, tool_response: { isError: true } });
    const assessment = await invoke("pi", { ...base, hook_event_name: "Stop" });
    assert.equal(assessment.notice, undefined);
    await invoke("pi", null, ["--cwd", state, "--session", "bridge", "outcome", "--status", "failed", "--summary", "check still fails"]);
    const result = await invoke("pi", { ...base, hook_event_name: "Stop" });
    assert.ok(result.notice);
    assert.deepEqual(await invoke("pi", { ...base, hook_event_name: "Stop" }), {});
    const decision = await invoke("pi", null, ["decide", "--notice", result.notice, "--decision", "decline"]);
    assert.equal(decision.model_called, false);
    const stored = await readFile(join(state, "notices", result.notice + ".json"), "utf8");
    assert.ok(!stored.includes("private command"));
  } finally {
    delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    await rm(state, { recursive: true, force: true });
  }
});
