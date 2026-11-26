import test from "node:test";
import assert from "node:assert/strict";
import { copyFile, mkdtemp, rm, readFile, writeFile, mkdir, readdir, symlink, chmod, stat } from "node:fs/promises";
import { realpathSync } from "node:fs";
import { EventEmitter } from "node:events";
import { spawnSync } from "node:child_process";
import { PassThrough } from "node:stream";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import piDefault, { createObserver as piObserver } from "../integrations/pi/index.js";
import { createObserver as openCodeObserver } from "../integrations/opencode.js";
import { invoke, createInvoker, sessionRoute, ObserverError } from "../integrations/bridge.mjs";

const runtimeConfig = JSON.parse(await readFile(new URL("../observer-runtime.json", import.meta.url), "utf8"));

test("Pi default loader registers its outcome schema without external packages", async () => {
  const handlers = new Map();
  const registered = [];
  await piDefault({ on: (event, handler) => handlers.set(event, handler), registerTool: tool => registered.push(tool) });
  assert.equal(registered.length, 1);
  const tool = registered[0];
  assert.equal(tool.name, "agent_evolution_outcome");
  assert.equal(tool.parameters.type, "object");
  assert.deepEqual(tool.parameters.required, ["status", "summary"]);
  assert.deepEqual(tool.parameters.properties.status.anyOf.map(value => value.const),
    ["passed", "failed", "unverified", "cancelled"]);
  assert.equal(tool.parameters.properties.summary.type, "string");
  assert.equal(tool.parameters.properties.skill.type, "string");
  assert.ok(handlers.has("agent_settled"));
});

test("Pi records outcomes without shell rows and keeps recording errors visible", async () => {
  const state = realpathSync(await mkdtemp(join(tmpdir(), "observer-silent-")));
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

function decisionResult(operation) {
  const approved = operation.at(-1) === "approve";
  return { notice: operation[operation.indexOf("--notice") + 1], status: approved ? "approved" : "declined",
    model_called: false, evaluation_authorized: false, promotion_authorized: false,
    ...(approved ? { instruction: "[agent-evolution-approved] prepare only" } : {}) };
}

function piHarness(approved) {
  const handlers = new Map();
  const sent = [], decisions = [], dialogs = [], events = [];
  const pi = { on: (name, fn) => handlers.set(name, fn), sendUserMessage: text => sent.push(text), sendMessage: (...args) => sent.push(args) };
  const ctx = { cwd: realpathSync(tmpdir()), sessionManager: { getSessionId: () => "session" }, hasUI: true,
    ui: { confirm: async (...args) => { dialogs.push(args); return approved; }, notify: () => {} } };
  const call = async (agent, payload, operation) => {
    if (operation) { decisions.push(operation); return decisionResult(operation); }
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
  assert.deepEqual(h.sent, ["[agent-evolution-approved] prepare only"]);
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
  const plugin = openCodeObserver({ directory: realpathSync(tmpdir()), client: {
    tui: { showToast: async (value) => toasts.push(value) }, app: { log: async () => {} },
    session: { prompt: () => { throw new Error("must not call model"); } },
  } }, async (agent, payload, operation) => {
    if (operation) { decisions.push(operation); return decisionResult(operation); }
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
  const state = realpathSync(await mkdtemp(join(tmpdir(), "observer-integration-")));
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
    const decision = await invoke("pi", null, ["--cwd", state, "--session", "bridge", "decide", "--notice", result.notice, "--decision", "decline"]);
    assert.equal(decision.model_called, false);
    const stored = await readFile(join(state, "notices", result.notice + ".json"), "utf8");
    assert.ok(!stored.includes("private command"));
  } finally {
    delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    await rm(state, { recursive: true, force: true });
  }
});

test("Pi pins event, outcome and consent to the first cwd and state despite tool cwd changes", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-pi-route-")));
  const initial = join(root, "initial"), changed = join(root, "changed");
  await mkdir(initial); await mkdir(changed);
  const oldOverride = process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  try {
    const handlers = new Map(), routes = [], sent = [];
    let tool;
    const pi = { on: (name, fn) => handlers.set(name, fn), registerTool: value => { tool = value; },
      sendUserMessage: value => sent.push(value) };
    const ctx = { cwd: initial, sessionManager: { getSessionId: () => "pinned" }, hasUI: true,
      ui: { confirm: async () => false, notify: message => assert.fail(message) } };
    piObserver(pi, async (agent, payload, operation, route) => {
      routes.push(route);
      return invoke(agent, payload, operation, route);
    }, {});
    await handlers.get("session_start")({}, ctx);
    ctx.cwd = changed;
    await handlers.get("before_agent_start")({ prompt: "task" }, ctx);
    process.env.AGENT_EVOLUTION_OBSERVER_STATE = join(changed, "override");
    await handlers.get("tool_result")({ toolName: "bash", input: { workdir: changed }, isError: true }, ctx);
    await tool.execute("id", { status: "failed", summary: "remaining failure" }, null, null, ctx);
    await handlers.get("agent_settled")({}, ctx);
    assert.equal(sent.length, 0);
    assert.equal(routes.length, 5);
    assert.ok(routes.every(route => route === routes[0]));
    assert.equal(routes[0].state, join(initial, ".evolution-observer"));
    assert.deepEqual(await readdir(changed), []);
    const notices = await readdir(join(routes[0].state, "notices"));
    assert.equal(notices.length, 1);
    const notice = JSON.parse(await readFile(join(routes[0].state, "notices", notices[0]), "utf8"));
    assert.equal(notice.status, "declined");
  } finally {
    if (oldOverride === undefined) delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    else process.env.AGENT_EVOLUTION_OBSERVER_STATE = oldOverride;
    await rm(root, { recursive: true, force: true });
  }
});

test("Pi keeps queued work and consent with its original session when the active session changes", async () => {
  const handlers = new Map(), calls = [], sent = [], warnings = [];
  let activeSession = "old", releaseConfirm;
  const ctx = { cwd: realpathSync(tmpdir()), sessionManager: { getSessionId: () => activeSession }, hasUI: true,
    ui: { confirm: () => new Promise(resolve => { releaseConfirm = resolve; }), notify: text => warnings.push(text) } };
  piObserver({ on: (name, fn) => handlers.set(name, fn), sendUserMessage: value => sent.push(value) },
    async (_agent, payload, operation, route) => {
      calls.push({ payload, operation, route });
      if (operation) return decisionResult(operation);
      if (payload.hook_event_name === "Stop") return { notice: "a".repeat(32), question: "approve?" };
      return {};
    });
  await handlers.get("before_agent_start")({ prompt: "task" }, ctx);
  const settling = handlers.get("agent_settled")({}, ctx);
  while (!releaseConfirm) await new Promise(resolve => setImmediate(resolve));
  activeSession = "new";
  const next = handlers.get("before_agent_start")({ prompt: "new task" }, ctx);
  releaseConfirm(true);
  await Promise.all([settling, next]);
  assert.equal(calls.find(call => call.operation)?.route.session, "old");
  assert.equal(calls.at(-1).route.session, "new");
  assert.equal(sent.length, 0);
  assert.equal(warnings.length, 1);
});

test("OpenCode binds next-turn decisions to each session's explicit state and rejects cross-session notices", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-opencode-route-")));
  const oldOverride = process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  try {
    const calls = [], toasts = [], logs = [];
    const plugin = openCodeObserver({ directory: root, client: {
      app: { log: async value => logs.push(value) }, tui: { showToast: async value => toasts.push(value) },
      session: { prompt: () => assert.fail("must not invoke a model") },
    } }, async (agent, payload, operation, route) => {
      calls.push({ payload, operation, route });
      return invoke(agent, payload, operation, route);
    });
    const parts = (text, id) => ({ message: { id }, parts: [{ type: "text", text }] });
    process.env.AGENT_EVOLUTION_OBSERVER_STATE = join(root, "state-a");
    await plugin["chat.message"]({ sessionID: "a" }, parts("task a", "turn-a"));
    const routeA = calls.at(-1).route;
    process.env.AGENT_EVOLUTION_OBSERVER_STATE = join(root, "state-b");
    await plugin["chat.message"]({ sessionID: "b" }, parts("task b", "turn-b"));
    const routeB = calls.at(-1).route;
    for (const route of [routeA, routeB]) {
      await invoke("opencode", null, ["outcome", "--status", "failed", "--summary", "remaining failure"], route);
      await plugin.event({ event: { type: "session.idle", properties: { sessionID: route.session } } });
    }
    const noticeA = (await readdir(join(routeA.state, "notices")))[0].replace(/\.json$/, "");
    const noticeB = (await readdir(join(routeB.state, "notices")))[0].replace(/\.json$/, "");
    await plugin["chat.message"]({ sessionID: "b" }, parts(`evolution approve ${noticeA}`, "wrong"));
    assert.equal(logs.length, 1);
    assert.equal(calls.at(-1).route, routeB);
    process.env.AGENT_EVOLUTION_OBSERVER_STATE = join(root, "unused");
    await plugin["chat.message"]({ sessionID: "a" }, parts(`evolution decline ${noticeA}`, "decide-a"));
    await plugin["chat.message"]({ sessionID: "b" }, parts(`evolution approve ${noticeB}`, "decide-b"));
    const decisions = calls.filter(call => call.operation?.[0] === "decide");
    assert.deepEqual(decisions.map(call => call.route), [routeB, routeA, routeB]);
    assert.equal(logs.length, 1);
    assert.equal(toasts.length, 2);
    const system = { system: [] };
    await plugin["experimental.chat.system.transform"]({ sessionID: "b" }, system);
    assert.ok(system.system.some(value => value.includes("[agent-evolution-approved]")));
    assert.equal((await readdir(root)).includes("unused"), false);
  } finally {
    if (oldOverride === undefined) delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    else process.env.AGENT_EVOLUTION_OBSERVER_STATE = oldOverride;
    await rm(root, { recursive: true, force: true });
  }
});

function fakeProcess(run, capture = {}) {
  return (binary, args) => {
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.kill = () => { capture.killed = true; child.emit("close", null); };
    capture.args = args;
    capture.binary = binary;
    let input = "";
    child.stdin.on("data", chunk => { input += chunk; });
    child.stdin.on("finish", () => { capture.input = input; run(child); });
    return child;
  };
}

test("Bridge exposes only fixed diagnostic codes, including successful-process event errors", async () => {
  const route = sessionRoute(realpathSync(tmpdir()), "errors");
  for (const [stderr, stdout, exit, expected] of [
    ["agent-evolution: [E_PERMISSION] SECRET /private/path", "", 2, "E_PERMISSION"],
    ["SECRET invalid password /private/path", "", 2, "E_PROCESS"],
    ["", JSON.stringify({ systemMessage: "SECRET", error: { code: "E_STATE_MISSING", operation: "event" } }), 0, "E_STATE_MISSING"],
    ["", JSON.stringify({ error: { code: "SECRET" } }), 0, "E_RESULT"],
    ["", "SECRET not json", 0, "E_RESULT"],
  ]) {
    const call = createInvoker({ spawnProcess: fakeProcess(child => {
      child.stderr.write(stderr); child.stdout.write(stdout); child.emit("close", exit);
    }) });
    await assert.rejects(call("pi", { hook_event_name: "Stop" }, undefined, route), error => {
      assert.ok(error instanceof ObserverError);
      assert.equal(error.code, expected);
      assert.ok(!error.message.includes("SECRET"));
      assert.ok(!error.message.includes("/private/path"));
      return true;
    });
  }
});

test("Bridge bounds hung processes and oversized output without exposing runtime text", async () => {
  const route = sessionRoute(realpathSync(tmpdir()), "bounds");
  for (const [run, expected] of [[() => {}, "E_TIMEOUT"], [child => child.stdout.write("x".repeat(1_000_001)), "E_OUTPUT_TOO_LARGE"]]) {
    const capture = {};
    const call = createInvoker({ spawnProcess: fakeProcess(run, capture), timeoutMs: 10 });
    await assert.rejects(call("pi", { hook_event_name: "Stop" }, undefined, route), error => error.code === expected);
    assert.equal(capture.killed, true);
  }
  const call = createInvoker({ spawnProcess: () => { throw new Error("SECRET executable path"); } });
  await assert.rejects(call("pi", {}, undefined, route), error => error.code === "E_PROCESS" && !error.message.includes("SECRET"));
});

test("Bridge supplies explicit stable route and does not parse summary text as options", async () => {
  const route = sessionRoute(realpathSync(tmpdir()), "route");
  const capture = {};
  const call = createInvoker({ spawnProcess: fakeProcess(child => {
    child.stdout.write('{"recorded":"passed"}'); child.emit("close", 0);
  }, capture) });
  await call("pi", null, ["outcome", "--status", "passed", "--summary", "--state"], route);
  assert.equal(capture.binary, runtimeConfig.python);
  assert.deepEqual(capture.args.slice(0, 6), ["-I", "-S", "-B",
    fileURLToPath(new URL("../scripts/observe.py", import.meta.url)), "--agent", "pi"]);
  assert.deepEqual(capture.args.slice(6), ["--state", route.state, "--cwd", route.cwd, "--session", route.session,
    "outcome", "--status", "passed", "--summary", "--state"]);
  assert.equal(capture.input, "");
  await assert.rejects(call("pi", null, ["--session", "different", "decide", "--notice", "a".repeat(32), "--decision", "approve"], route),
    error => error.code === "E_INPUT");
});

test("Relocated runtime uses only its own scripts and explicit skill path", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-relocated-")));
  try {
    for (const file of ["observer-runtime.json", "scripts/observe.py", "src/agent_evolution/__init__.py",
      "src/agent_evolution/observer.py", "src/agent_evolution/util.py", "src/agent_evolution/state.py", "integrations/bridge.mjs",
      "integrations/opencode.js", "integrations/pi/index.js"]) {
      const destination = join(root, file);
      await mkdir(dirname(destination), { recursive: true });
      await copyFile(new URL(`../${file}`, import.meta.url), destination);
    }
    const configuredSkill = join(root, "explicit-shared-skill", "SKILL.md");
    await writeFile(join(root, "observer-runtime.json"), JSON.stringify({ ...runtimeConfig, skill: configuredSkill }));
    const bridge = await import(pathToFileURL(join(root, "integrations/bridge.mjs")).href);
    const route = Object.freeze({ cwd: root, session: "relocated", state: join(root, "state") });
    const start = await bridge.invoke("codex", { hook_event_name: "UserPromptSubmit", prompt: "task" }, undefined, route);
    const guidance = start.hookSpecificOutput.additionalContext;
    assert.ok(guidance.includes(`${runtimeConfig.python} -I -S -B ${join(root, "scripts/observe.py")}`));
    assert.ok(!guidance.includes(".venv"));
    await bridge.invoke("codex", null, ["outcome", "--status", "failed", "--summary", "relocation check"], route);
    const stop = await bridge.invoke("codex", { hook_event_name: "Stop" }, undefined, route);
    const decision = await bridge.invoke("codex", null,
      ["decide", "--notice", stop.notice, "--decision", "approve"], route);
    assert.ok(decision.instruction.includes(configuredSkill));
    assert.equal(decision.model_called, false);
    assert.equal(decision.evaluation_authorized, false);
    const pi = await import(pathToFileURL(join(root, "integrations/pi/index.js")).href);
    let registered;
    await pi.default({ on() {}, registerTool: tool => { registered = tool; } });
    assert.equal(registered.name, "agent_evolution_outcome");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Python and bridge reject missing or malformed release config without a development fallback", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-config-")));
  try {
    for (const file of ["scripts/observe.py", "src/agent_evolution/__init__.py", "src/agent_evolution/observer.py",
      "src/agent_evolution/util.py", "src/agent_evolution/state.py", "integrations/bridge.mjs"]) {
      const destination = join(root, file);
      await mkdir(dirname(destination), { recursive: true });
      await copyFile(new URL(`../${file}`, import.meta.url), destination);
    }
    const invalid = [undefined, "not json", JSON.stringify({ ...runtimeConfig, version: true }),
      JSON.stringify({ ...runtimeConfig, version: 2 }), JSON.stringify({ ...runtimeConfig, python: "python3" }),
      JSON.stringify({ ...runtimeConfig, skill: "" }), JSON.stringify({ ...runtimeConfig, skill: "/bad\u0000path" }),
      JSON.stringify({ ...runtimeConfig, extra: "ignored?" })];
    for (const [index, config] of invalid.entries()) {
      if (config !== undefined) await writeFile(join(root, "observer-runtime.json"), config);
      await assert.rejects(import(`${pathToFileURL(join(root, "integrations/bridge.mjs")).href}?case=${index}`),
        error => error.code === "E_RUNTIME_CONFIG" && !error.message.includes("ignored?"));
      const python = spawnSync(runtimeConfig.python, ["-I", "-S", "-B", join(root, "scripts/observe.py"),
        "--agent", "codex", "list"], { encoding: "utf8" });
      assert.equal(python.status, 2);
      assert.equal(python.stdout, "");
      assert.ok(python.stderr.startsWith("agent-evolution: [E_RUNTIME_CONFIG]"));
      assert.ok(!python.stderr.includes("Traceback"));
      assert.ok(!python.stderr.includes("ignored?"));
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});


test("Bridge preserves cwd links until Python rejects the storage path", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-linked-cwd-")));
  const oldOverride = process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  try {
    const target = join(root, "target"), link = join(root, "link");
    await mkdir(target);
    await symlink(target, link, "dir");
    const route = sessionRoute(link, "linked");
    assert.equal(route.cwd, target);
    assert.equal(route.state, join(link, ".evolution-observer"));
    for (const agent of ["pi", "opencode"]) {
      await assert.rejects(invoke(agent, { cwd: link, session_id: "linked", hook_event_name: "UserPromptSubmit" }),
        error => error.code === "E_UNSAFE_PATH");
    }
    assert.deepEqual(await readdir(target), []);
  } finally {
    if (oldOverride === undefined) delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    else process.env.AGENT_EVOLUTION_OBSERVER_STATE = oldOverride;
    await rm(root, { recursive: true, force: true });
  }
});

test("Bridge refuses parent traversal before it can erase a storage-path link", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-parent-path-")));
  const oldOverride = process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  try {
    await mkdir(join(root, "child"));
    delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    assert.throws(() => sessionRoute(`${root}/child/..`, "s"), error => error.code === "E_UNSAFE_PATH");
    process.env.AGENT_EVOLUTION_OBSERVER_STATE = "child/../state";
    assert.throws(() => sessionRoute(root, "s"), error => error.code === "E_UNSAFE_PATH");
    delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    const capture = {};
    const call = createInvoker({ spawnProcess: fakeProcess(() => {}, capture) });
    await assert.rejects(call("pi", null, ["--cwd", root, "--session", "s", "--state", "child/../state", "outcome"]),
      error => error.code === "E_UNSAFE_PATH");
    assert.equal(capture.binary, undefined);
    assert.deepEqual(await readdir(root), ["child"]);
  } finally {
    if (oldOverride === undefined) delete process.env.AGENT_EVOLUTION_OBSERVER_STATE;
    else process.env.AGENT_EVOLUTION_OBSERVER_STATE = oldOverride;
    await rm(root, { recursive: true, force: true });
  }
});

test("Bridge reports nonprivate existing state without changing its permissions", async () => {
  const root = realpathSync(await mkdtemp(join(tmpdir(), "observer-shared-state-")));
  try {
    const state = join(root, "state");
    await mkdir(state);
    await chmod(state, 0o755);
    const route = Object.freeze({ cwd: root, session: "private", state });
    for (const agent of ["pi", "opencode"]) {
      await assert.rejects(invoke(agent, { hook_event_name: "UserPromptSubmit" }, undefined, route),
        error => error.code === "E_PERMISSION");
    }
    assert.equal((await stat(state)).mode & 0o777, 0o755);
    assert.deepEqual(await readdir(state), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Pi never interprets a missing or non-boolean dialog answer as approval", async () => {
  for (const answer of [undefined, "yes"]) {
    const h = piHarness(answer);
    await h.handlers.get("agent_settled")({}, h.ctx);
    assert.equal(h.decisions[0].at(-1), "decline");
    assert.equal(h.sent.length, 0);
  }
});


test("OpenCode discards approved guidance after a later rejected notice", async () => {
  let rejectDecision = false;
  const plugin = openCodeObserver({ directory: realpathSync(tmpdir()), client: { app: { log: async () => {} } } },
    async (_agent, _payload, operation) => {
      if (operation) {
        if (rejectDecision) throw new ObserverError("E_NOTICE_EXPIRED");
        return decisionResult(operation);
      }
      return {};
    });
  const parts = text => ({ message: { id: "m" }, parts: [{ type: "text", text }] });
  await plugin["chat.message"]({ sessionID: "s" }, parts("evolution approve " + "a".repeat(32)));
  const first = { system: [] };
  await plugin["experimental.chat.system.transform"]({ sessionID: "s" }, first);
  assert.ok(first.system[0].startsWith("[agent-evolution-approved]"));
  rejectDecision = true;
  await plugin["chat.message"]({ sessionID: "s" }, parts("evolution approve " + "b".repeat(32)));
  const after = { system: [] };
  await plugin["experimental.chat.system.transform"]({ sessionID: "s" }, after);
  assert.deepEqual(after.system, []);
});

test("Connectors reject malformed consent results without starting preparation or claiming a recorded decision", async () => {
  for (const approved of [true, false]) {
    const handlers = new Map(), sent = [], warnings = [];
    piObserver({ on: (name, fn) => handlers.set(name, fn), sendUserMessage: value => sent.push(value) },
      async (_agent, payload, operation) => operation ? {} : { notice: "a".repeat(32), question: "approve?" });
    const ctx = { cwd: realpathSync(tmpdir()), sessionManager: { getSessionId: () => "malformed" }, hasUI: true,
      ui: { confirm: async () => approved, notify: value => warnings.push(value) } };
    await handlers.get("agent_settled")({}, ctx);
    assert.equal(sent.length, 0);
    assert.ok(warnings[0].includes("[E_RESULT]"));
    const logs = [];
    const plugin = openCodeObserver({ directory: ctx.cwd, client: { app: { log: async value => logs.push(value) } } }, async () => ({}));
    await plugin["chat.message"]({ sessionID: "s" }, { message: { id: "m" },
      parts: [{ type: "text", text: `evolution ${approved ? "approve" : "decline"} ${"b".repeat(32)}` }] });
    const output = { system: [] };
    await plugin["experimental.chat.system.transform"]({ sessionID: "s" }, output);
    assert.deepEqual(output.system, []);
    assert.ok(logs[0].body.message.includes("[E_RESULT]"));
  }
});
