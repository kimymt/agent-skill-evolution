import { invoke, queue } from "./bridge.mjs";

export function createObserver({ client, directory }, call = invoke) {
  const serial = queue();
  const context = new Map();
  const observed = new Set();
  const payload = (session, event, fields = {}) => ({
    cwd: directory, session_id: session, hook_event_name: event, ...fields,
  });
  const safe = (work) => serial(work).catch(async () => {
    await client.app.log({ body: { service: "agent-evolution", level: "warn", message: "Observer failed; no evaluation started" } }).catch(() => {});
  });
  return {
    "chat.message": async (input, output) => safe(async () => {
      const text = output.parts.filter(p => p.type === "text").map(p => p.text).join("\n");
      const reply = /^evolution (approve|decline) ([a-f0-9]{32})$/.exec(text.trim());
      let instruction = "";
      if (reply) {
        const result = await call("opencode", null, ["decide", "--notice", reply[2], "--decision", reply[1]]);
        instruction = result.instruction || "起動を見送りました。改善処理は開始しません。";
      }
      const result = await call("opencode", payload(input.sessionID, "UserPromptSubmit", {
        prompt: instruction.startsWith("[agent-evolution-approved]") ? instruction : text,
        turn_id: input.messageID || output.message.id,
      }));
      context.set(input.sessionID, [result.hookSpecificOutput?.additionalContext, instruction].filter(Boolean).join("\n"));
    }),
    "experimental.chat.system.transform": async (input, output) => {
      const guidance = context.get(input.sessionID);
      if (guidance) output.system.push(guidance);
    },
    event: async ({ event }) => safe(async () => {
      const props = event.properties;
      if (event.type === "message.part.updated") {
        const part = props.part;
        if (part.type !== "tool" || !["completed", "error"].includes(part.state.status)) return;
        const key = `${part.sessionID}:${part.callID}:${part.state.status}`;
        if (observed.has(key)) return;
        observed.add(key);
        if (observed.size > 2000) observed.delete(observed.values().next().value);
        await call("opencode", payload(part.sessionID, "PostToolUse", {
          tool_name: part.tool, tool_input: part.state.input,
          tool_response: { isError: part.state.status === "error", exit_code: part.state.metadata?.exit },
        }));
      } else if (event.type === "session.error" && props.error?.name === "MessageAbortedError") {
        await call("opencode", payload(props.sessionID, "Interrupt"));
      } else if (event.type === "session.idle") {
        const result = await call("opencode", payload(props.sessionID, "Stop"));
        if (result.notice) {
          await client.tui.showToast({ body: { title: "agent-evolutionの起動確認", variant: "warning", duration: 30000,
            message: `${result.question}\n承認: evolution approve ${result.notice}\n見送り: evolution decline ${result.notice}` } });
        }
      } else if (event.type === "session.deleted") {
        context.delete(props.info.id);
      }
    }),
  };
}

export default async function (ctx) { return createObserver(ctx); }
