import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const project = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Pass observations through stdin, never interpolate tool text into a shell.
export function invoke(agent, payload, operation = []) {
  return new Promise((accept, reject) => {
    const child = spawn(resolve(project, ".venv/bin/python"), [
      resolve(project, "scripts/observe.py"), "--agent", agent,
      ...(process.env.AGENT_EVOLUTION_OBSERVER_STATE ? ["--state", process.env.AGENT_EVOLUTION_OBSERVER_STATE] : []),
      ...(operation.length ? operation : ["event"]),
    ], { stdio: ["pipe", "pipe", "pipe"] });
    let output = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
    child.stdout.on("data", (chunk) => {
      output += chunk.toString();
      if (output.length > 1_000_000) child.kill("SIGKILL");
    });
    // Runtime error text can contain local paths; do not reflect it into prompts.
    child.stderr.resume();
    child.stdin.on("error", () => {});
    child.once("error", (error) => { clearTimeout(timer); reject(error); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error("Observer process failed; no evaluation started"));
      try { accept(JSON.parse(output)); } catch { reject(new Error("Invalid observer result")); }
    });
    child.stdin.end(operation.length ? "" : JSON.stringify(payload));
  });
}

export function queue() {
  let tail = Promise.resolve();
  return (work) => {
    const next = tail.then(work);
    tail = next.catch(() => {});
    return next;
  };
}
