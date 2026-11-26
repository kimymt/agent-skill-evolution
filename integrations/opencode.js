import { invoke, queue, sessionRoute, observerWarning, validatedDecision } from "./bridge.mjs";

export function createObserver({ client, directory }, call = invoke) {
  const serial = queue();
  const context = new Map();
  const messageVersions = new Map();
  const observed = new Set();
  const sessions = new Map();
  const routeFor = session => {
    if (!sessions.has(session)) sessions.set(session, sessionRoute(directory, session));
    return sessions.get(session);
  };
  const payload = (route, event, fields = {}) => ({
    cwd: route.cwd, session_id: route.session, hook_event_name: event, ...fields,
  });
  const safe = async (session, work) => {
    try {
      const route = routeFor(session);
      return await serial(() => work(route));
    } catch (error) {
      await client.app.log({ body: { service: "agent-evolution", level: "warn", message: observerWarning(error) } }).catch(() => {});
    }
  };
  return {
    "chat.message": async (input, output) => {
      const version = (messageVersions.get(input.sessionID) || 0) + 1;
      messageVersions.set(input.sessionID, version);
      context.delete(input.sessionID);
      return safe(input.sessionID, async route => {
        const text = output.parts.filter(p => p.type === "text").map(p => p.text).join("\n");
        const reply = /^evolution (approve|decline) ([a-f0-9]{32})$/.exec(text.trim());
        let instruction = "";
        if (reply) {
          const result = validatedDecision(await call("opencode", null,
            ["decide", "--notice", reply[2], "--decision", reply[1]], route), reply[2], reply[1]);
          instruction = result.instruction || "起動を見送りました。改善処理は開始しません。";
        }
        const result = await call("opencode", payload(route, "UserPromptSubmit", {
          prompt: instruction.startsWith("[agent-evolution-approved]") ? instruction : text,
          turn_id: input.messageID || output.message.id,
        }), undefined, route);
        if (messageVersions.get(input.sessionID) === version) {
          context.set(input.sessionID, [result.hookSpecificOutput?.additionalContext, instruction].filter(Boolean).join("\n"));
        }
      });
    },
    "experimental.chat.system.transform": async (input, output) => {
      const guidance = context.get(input.sessionID);
      if (guidance) output.system.push(guidance);
    },
    event: async ({ event }) => {
      const props = event.properties;
      if (event.type === "session.deleted") {
        const id = props.info.id;
        await serial(() => { context.delete(id); sessions.delete(id); messageVersions.delete(id); });
        return;
      }
      if (!["message.part.updated", "session.error", "session.idle"].includes(event.type)) return;
      const session = props.part?.sessionID || props.sessionID;
      return safe(session, async route => {
        if (event.type === "message.part.updated") {
          const part = props.part;
          if (part.type !== "tool" || !["completed", "error"].includes(part.state.status)) return;
          const key = `${part.sessionID}:${part.callID}:${part.state.status}`;
          if (observed.has(key)) return;
          observed.add(key);
          if (observed.size > 2000) observed.delete(observed.values().next().value);
          await call("opencode", payload(route, "PostToolUse", {
            tool_name: part.tool, tool_input: part.state.input,
            tool_response: { isError: part.state.status === "error", exit_code: part.state.metadata?.exit },
          }), undefined, route);
        } else if (event.type === "session.error" && props.error?.name === "MessageAbortedError") {
          await call("opencode", payload(route, "Interrupt"), undefined, route);
        } else if (event.type === "session.idle") {
          const result = await call("opencode", payload(route, "Stop"), undefined, route);
          if (result.notice) {
            await client.tui.showToast({ body: { title: "agent-evolutionの起動確認", variant: "warning", duration: 30000,
              message: `${result.question}\n承認: evolution approve ${result.notice}\n見送り: evolution decline ${result.notice}` } });
          }
        }
      });
    },
  };
}

export default async function (ctx) { return createObserver(ctx); }
