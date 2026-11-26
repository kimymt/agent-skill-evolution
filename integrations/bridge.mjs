import { spawn } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const project = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const messages = {
  E_PERMISSION: "監視記録の保存先に書き込めません。",
  E_STATE_MISSING: "開始時の監視記録がありません。",
  E_TASK_CLOSED: "この作業の監視は終了済み、または抑止されています。",
  E_TASK_EXPIRED: "作業の監視記録が期限切れです。",
  E_INPUT: "監視への入力が不正です。",
  E_INPUT_TOO_LARGE: "監視への入力が大きすぎます。",
  E_STATE_CORRUPT: "保存された監視記録を読み取れません。",
  E_UNSAFE_PATH: "監視記録の保存先を安全に使用できません。",
  E_IO: "監視記録の読み書きに失敗しました。",
  E_NOTICE_DECIDED: "この通知への判断は記録済みです。",
  E_NOTICE_EXPIRED: "この通知は期限切れです。",
  E_PROCESS: "監視用プロセスを実行できません。",
  E_TIMEOUT: "監視用プロセスが時間内に終了しませんでした。",
  E_OUTPUT_TOO_LARGE: "監視用プロセスの出力が大きすぎます。",
  E_RESULT: "監視用プロセスの結果を確認できません。",
  E_RUNTIME_CONFIG: "監視用ランタイムの設定がないか不正です。配布物を確認してください。",
};

export class ObserverError extends Error {
  constructor(code) {
    const known = Object.hasOwn(messages, code) ? code : "E_RESULT";
    super(`agent-evolution: [${known}] ${messages[known]} 評価は起動していません。`);
    this.code = known;
  }
}

function runtimeConfig() {
  try {
    const value = JSON.parse(readFileSync(resolve(project, "observer-runtime.json"), "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)
        || Object.keys(value).sort().join(",") !== "python,skill,version" || value.version !== 1
        || [value.python, value.skill].some(path => typeof path !== "string" || !path.trim()
          || path.includes("\0") || !isAbsolute(path))) throw new Error();
    return Object.freeze(value);
  } catch { throw new ObserverError("E_RUNTIME_CONFIG"); }
}

const runtime = runtimeConfig();

export function observerWarning(error) {
  return error instanceof ObserverError ? error.message : new ObserverError("E_PROCESS").message;
}


export function validatedDecision(result, notice, decision) {
  const approved = decision === "approve";
  if (!result || result.notice !== notice || result.status !== (approved ? "approved" : "declined")
      || result.model_called !== false || result.evaluation_authorized !== false || result.promotion_authorized !== false
      || (approved ? typeof result.instruction !== "string" || !result.instruction.startsWith("[agent-evolution-approved] ")
        : result.instruction !== undefined)) throw new ObserverError("E_RESULT");
  return result;
}

// Capture once per session. Tool workdir and later environment changes must not reroute it.
function statePath(cwd, value) {
  if ([cwd, value].some(path => typeof path !== "string" || !path || path.includes("\0"))) {
    throw new ObserverError("E_INPUT");
  }
  // Do not let path normalization erase a link followed by a parent traversal.
  if ([cwd, value].some(path => path.split("/").includes(".."))) throw new ObserverError("E_UNSAFE_PATH");
  return resolve(cwd, value);
}

export function sessionRoute(cwd, session) {
  if (typeof cwd !== "string" || !cwd || typeof session !== "string" || !session) {
    throw new ObserverError("E_INPUT");
  }
  let canonical;
  try { canonical = realpathSync(cwd); } catch { throw new ObserverError("E_UNSAFE_PATH"); }
  const override = process.env.AGENT_EVOLUTION_OBSERVER_STATE;
  return Object.freeze({ cwd: canonical, session,
    // Identity uses the canonical cwd; storage keeps the original link-bearing
    // path so Python can validate every component before any state operation.
    state: statePath(cwd, override || ".evolution-observer") });
}

function splitOperation(operation) {
  const identity = {};
  let index = 0;
  while (["--state", "--cwd", "--session"].includes(operation[index])) {
    if (typeof operation[index + 1] !== "string" || !operation[index + 1]) throw new ObserverError("E_INPUT");
    identity[operation[index].slice(2)] = operation[index + 1];
    index += 2;
  }
  return { identity, args: operation.slice(index) };
}

// Pass observations through stdin, never interpolate tool text into a shell.
export function createInvoker({ spawnProcess = spawn, timeoutMs = 5000 } = {}) {
  return function invokeObserver(agent, payload, operation = [], route) {
    return new Promise((accept, reject) => {
      let args;
      try {
        const parsed = splitOperation(operation);
        args = parsed.args;
        const supplied = parsed.identity;
        if (!route) {
          const cwd = payload?.cwd || supplied.cwd;
          route = sessionRoute(cwd, payload?.session_id || supplied.session);
          if (supplied.state) route = Object.freeze({ ...route, state: statePath(cwd, supplied.state) });
        }
        if ((supplied.state && statePath(supplied.cwd || route.cwd, supplied.state) !== route.state)
            || (supplied.cwd && realpathSync(supplied.cwd) !== route.cwd)
            || (supplied.session && supplied.session !== route.session)) throw new ObserverError("E_INPUT");
      } catch (error) { reject(error instanceof ObserverError ? error : new ObserverError("E_INPUT")); return; }
      let child;
      try {
        child = spawnProcess(runtime.python, [
          "-I", "-S", "-B", resolve(project, "scripts/observe.py"), "--agent", agent,
          "--state", route.state, "--cwd", route.cwd, "--session", route.session,
          ...(args.length ? args : ["event"]),
        ], { stdio: ["pipe", "pipe", "pipe"] });
      } catch { reject(new ObserverError("E_PROCESS")); return; }
      let output = "", stderrPrefix = "", outputBytes = 0, settled = false;
      const fail = code => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(new ObserverError(code));
      };
      const timer = setTimeout(() => { fail("E_TIMEOUT"); child.kill("SIGKILL"); }, timeoutMs);
      child.stdout.on("data", chunk => {
        if (settled) return;
        outputBytes += Buffer.byteLength(chunk);
        if (outputBytes > 1_000_000) { fail("E_OUTPUT_TOO_LARGE"); child.kill("SIGKILL"); return; }
        output += chunk.toString();
      });
      // Inspect only a bounded fixed-code prefix. Never reflect raw runtime stderr into prompts.
      child.stderr.on("data", chunk => {
        if (stderrPrefix.length < 128) stderrPrefix += chunk.toString().slice(0, 128 - stderrPrefix.length);
      });
      child.stdin.on("error", () => {});
      child.once("error", () => fail("E_PROCESS"));
      child.once("close", code => {
        if (settled) return;
        if (code !== 0) {
          const code = /^agent-evolution: \[(E_[A-Z_]+)\]/.exec(stderrPrefix)?.[1];
          fail(code && Object.hasOwn(messages, code) ? code : "E_PROCESS"); return;
        }
        let result;
        try { result = JSON.parse(output); } catch { fail("E_RESULT"); return; }
        if (!result || typeof result !== "object" || Array.isArray(result)) { fail("E_RESULT"); return; }
        if (result.error) { fail(result.error.code); return; }
        settled = true;
        clearTimeout(timer);
        accept(result);
      });
      try {
        const event = !args.length || ["event", "hook"].includes(args[0]);
        child.stdin.end(event ? JSON.stringify({ ...payload, cwd: route.cwd, session_id: route.session }) : "");
      } catch { fail("E_INPUT"); child.kill("SIGKILL"); }
    });
  };
}

export const invoke = createInvoker();

export function queue() {
  let tail = Promise.resolve();
  return (work) => {
    const next = tail.then(work);
    tail = next.catch(() => {});
    return next;
  };
}
