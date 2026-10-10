import { randomUUID } from "node:crypto";
import { invoke, queue } from "../bridge.mjs";

export function createObserver(pi, call = invoke, outcomeParameters) {
  const serial = queue();
  const payload = (ctx, event, fields = {}) => ({
    cwd: ctx.cwd, session_id: ctx.sessionManager.getSessionId(), hook_event_name: event, ...fields,
  });
  const safely = (ctx, work) => serial(work).catch(() => {
    if (ctx.hasUI) ctx.ui.notify("agent-evolution: 終了監視に失敗しました。評価は起動していません。", "warning");
  });
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
      const operation = ["--cwd", ctx.cwd, "--session", ctx.sessionManager.getSessionId(),
        "outcome", "--status", params.status, "--summary", params.summary];
      if (params.skill) operation.push("--skill", params.skill);
      const result = await serial(() => call("pi", null, operation));
      if (result.recorded !== params.status) throw new Error("作業結果の記録を確認できませんでした。");
      return { content: [{ type: "text", text: "Recorded." }], details: undefined };
    },
  });
  pi.on("before_agent_start", async (event, ctx) => safely(ctx, async () => {
    const result = await call("pi", payload(ctx, "UserPromptSubmit", { prompt: event.prompt, turn_id: randomUUID() }));
    const content = result.hookSpecificOutput?.additionalContext;
    if (content) return { message: { customType: "agent-evolution-observer", content, display: false } };
  }));
  pi.on("tool_result", async (event, ctx) => safely(ctx, () => call("pi", payload(ctx, "PostToolUse", {
    tool_name: event.toolName, tool_input: event.input,
    tool_response: { isError: event.isError, exit_code: event.details?.exitCode },
    is_interrupt: ctx.signal?.aborted === true,
  }))));
  pi.on("agent_before_settle", async (event, ctx) => {
    if (event.outcome === "aborted") await safely(ctx, () => call("pi", payload(ctx, "Interrupt")));
  });
  pi.on("agent_settled", async (_event, ctx) => safely(ctx, async () => {
    const result = await call("pi", payload(ctx, "Stop"));
    if (!result.notice) {
      if (result.reason && ctx.hasUI) ctx.ui.notify("agent-evolution: 作業結果が未記録です。失敗確定まで起動確認は行いません。", "warning");
      return;
    }
    if (!ctx.hasUI) {
      pi.sendMessage({ customType: "agent-evolution-observer", content: result.reason, display: true }, { triggerTurn: false });
      return;
    }
    const approved = await ctx.ui.confirm("agent-evolutionの起動確認", result.question);
    const decision = await call("pi", null, ["decide", "--notice", result.notice, "--decision", approved ? "approve" : "decline"]);
    if (approved) pi.sendUserMessage(decision.instruction);
  }));
}

export default async function (pi) {
  const { Type } = await import("@earendil-works/pi-ai");
  createObserver(pi, invoke, Type.Object({
    status: Type.Union(["passed", "failed", "unverified", "cancelled"].map(value => Type.Literal(value))),
    summary: Type.String({ description: "確認結果の短い説明" }),
    skill: Type.Optional(Type.String()),
  }));
}
