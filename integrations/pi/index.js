import { randomUUID } from "node:crypto";
import { invoke, queue, sessionRoute, observerWarning, validatedDecision } from "../bridge.mjs";

export function createObserver(pi, call = invoke, outcomeParameters) {
  const serial = queue();
  const sessions = new Map();
  const routeFor = ctx => {
    const id = ctx.sessionManager.getSessionId();
    if (!sessions.has(id)) sessions.set(id, sessionRoute(ctx.cwd, id));
    return sessions.get(id);
  };
  const payload = (route, event, fields = {}) => ({
    cwd: route.cwd, session_id: route.session, hook_event_name: event, ...fields,
  });
  const safely = async (ctx, work) => {
    try {
      const route = routeFor(ctx);
      return await serial(() => work(route));
    } catch (error) {
      if (ctx.hasUI) ctx.ui.notify(observerWarning(error), "warning");
    }
  };
  pi.on("session_start", async (_event, ctx) => safely(ctx, () => {}));
  if (outcomeParameters) pi.registerTool({
    name: "agent_evolution_outcome",
    label: "作業結果の記録",
    description: "作業終了時の結果をローカルに記録する。通常のbashではなくこのツールを使用し、記録内容を回答に含めない。",
    exposure: "model-only",
    parameters: outcomeParameters,
    renderShell: "self",
    renderCall: (_args, _theme, context) => ({
      render: () => context.isError ? ["agent-evolution: 作業結果を記録できませんでした。"] : [],
      invalidate() {},
    }),
    renderResult: (result, _options, _theme, context) => ({
      render: () => context.isError
        ? result.content.filter(part => part.type === "text").flatMap(part => part.text.split("\n")) : [],
      invalidate() {},
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (signal?.aborted) throw new Error("記録前に処理が中断されました。");
      const route = routeFor(ctx);
      const operation = ["outcome", "--status", params.status, "--summary", params.summary];
      if (params.skill) operation.push("--skill", params.skill);
      const result = await serial(() => call("pi", null, operation, route));
      if (result.recorded !== params.status) throw new Error("作業結果の記録を確認できませんでした。");
      return { content: [{ type: "text", text: "Recorded." }], details: undefined };
    },
  });
  pi.on("before_agent_start", async (event, ctx) => safely(ctx, async route => {
    const result = await call("pi", payload(route, "UserPromptSubmit", { prompt: event.prompt, turn_id: randomUUID() }), undefined, route);
    const content = result.hookSpecificOutput?.additionalContext;
    if (content) return { message: { customType: "agent-evolution-observer", content, display: false } };
  }));
  pi.on("tool_result", async (event, ctx) => safely(ctx, route => call("pi", payload(route, "PostToolUse", {
    tool_name: event.toolName, tool_input: event.input,
    tool_response: { isError: event.isError, exit_code: event.details?.exitCode },
    is_interrupt: ctx.signal?.aborted === true,
  }), undefined, route)));
  pi.on("agent_before_settle", async (event, ctx) => {
    if (event.outcome === "aborted") await safely(ctx, route => call("pi", payload(route, "Interrupt"), undefined, route));
  });
  pi.on("agent_settled", async (_event, ctx) => safely(ctx, async route => {
    const result = await call("pi", payload(route, "Stop"), undefined, route);
    if (!result.notice) {
      if (result.reason && ctx.hasUI) ctx.ui.notify("agent-evolution: 作業結果が未記録です。失敗確定まで起動確認は行いません。", "warning");
      return;
    }
    if (!ctx.hasUI) {
      pi.sendMessage({ customType: "agent-evolution-observer", content: result.reason, display: true }, { triggerTurn: false });
      return;
    }
    const approved = await ctx.ui.confirm("agent-evolutionの起動確認", result.question) === true;
    const action = approved ? "approve" : "decline";
    const decision = validatedDecision(await call("pi", null,
      ["decide", "--notice", result.notice, "--decision", action], route), result.notice, action);
    if (approved && ctx.sessionManager.getSessionId() === route.session) {
      pi.sendUserMessage(decision.instruction);
    } else if (approved && ctx.hasUI) {
      ctx.ui.notify("agent-evolution: 元のセッションに承認を記録しました。セッションが切り替わったため、準備の開始は保留しました。", "warning");
    }
  }));
}

export default async function (pi) {
  createObserver(pi, invoke, {
    type: "object",
    properties: {
      status: { anyOf: ["passed", "failed", "unverified", "cancelled"].map(value => ({ const: value, type: "string" })) },
      summary: { type: "string", description: "確認結果の短い説明" },
      skill: { type: "string" },
    },
    required: ["status", "summary"],
  });
}
