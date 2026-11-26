"""Local lifecycle observations. This module never launches a model or an evaluation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

from .util import EvolutionError, canonical, digest, redact
from .state import PrivateState, StateError

PROJECT = Path(__file__).resolve().parents[2]
MARKER = "[agent-evolution-observer]"
APPROVED = "[agent-evolution-approved]"
AGENTS = ("codex", "claude", "opencode", "pi", "grok")
STATUSES = ("passed", "failed", "unverified", "cancelled")
TTL = 7 * 86400

MAX_BYTES = 1_000_000
ERRORS = {
    "E_PERMISSION": "保存先の所有者・権限・ACLが条件を満たさないか、アクセスが拒否されました。保存先を確認してください。",
    "E_STATE_MISSING": "この作業の開始記録がありません。新しいユーザー入力から監視を開始してください。",
    "E_TASK_CLOSED": "この作業は終了済みまたは監視対象外です。新しい作業の記録先を使ってください。",
    "E_TASK_EXPIRED": "この作業の記録期限が切れています。新しい作業として開始してください。",
    "E_INPUT": "監視入力が不正です。接続部分の入力形式を確認してください。",
    "E_INPUT_TOO_LARGE": "監視入力が上限を超えました。接続部分の入力サイズを確認してください。",
    "E_STATE_CORRUPT": "保存データの形式が不正です。対象の記録を確認してください。",
    "E_UNSAFE_PATH": "保存先にリンクまたは想定外のファイル種別があります。通常のディレクトリを指定してください。",
    "E_IO": "監視記録の入出力に失敗しました。保存先の状態を確認してください。",
    "E_NOTICE_DECIDED": "この通知への返答は記録済みです。二重に決定しないでください。",
    "E_NOTICE_EXPIRED": "この通知の有効期限が切れています。新しい作業結果を確認してください。",
    "E_RUNTIME_CONFIG": "監視用ランタイムの設定がないか不正です。配布物を確認してください。",
}


class ObserverError(EvolutionError):
    def __init__(self, code):
        self.code = code
        # Keep exception text fixed as well: callers must never expose input data.
        super().__init__(code + ": " + ERRORS[code])


def runtime_config():
    """Use only the configuration shipped alongside this observer release."""
    try:
        data = json.loads((PROJECT / "observer-runtime.json").read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or set(data) != {"version", "python", "skill"}
                or type(data["version"]) is not int or data["version"] != 1
                or any(not isinstance(data[key], str) or not data[key].strip()
                       or "\x00" in data[key] or not Path(data[key]).is_absolute()
                       for key in ("python", "skill"))):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ObserverError("E_RUNTIME_CONFIG") from None
    return data


RUNTIME = runtime_config()
SKILL = Path(RUNTIME["skill"])


def valid_identity(agent, cwd, session):
    if (agent not in AGENTS or not isinstance(cwd, str) or not cwd or "\x00" in cwd
            or not isinstance(session, str) or not session or len(session) > 1024):
        raise ObserverError("E_INPUT")


# Grok CLI injects these into every hook process:
# GROK_HOOK_EVENT, GROK_HOOK_NAME, GROK_SESSION_ID, GROK_WORKSPACE_ROOT.
# Source: https://docs.x.ai/build/features/hooks (confirmed 2026-11-26).
# Grok also loads ~/.claude/settings.json, so a Grok session would otherwise
# record both --agent claude and --agent grok unless non-grok calls are ignored.
GROK_RUNTIME_ENV = ("GROK_SESSION_ID", "GROK_HOOK_EVENT")
HOOK_FIELD_ALIASES = {
    "hookEventName": "hook_event_name",
    "sessionId": "session_id",
    "workspaceRoot": "cwd",
    "turnId": "turn_id",
    "promptId": "prompt_id",
    "toolName": "tool_name",
    "toolInput": "tool_input",
    "toolResult": "tool_response",
    "isSubagent": "is_subagent",
    "agentId": "agent_id",
}
# hookEventName and GROK_HOOK_EVENT use Grok's snake_case values.
GROK_HOOK_EVENTS = {
    "user_prompt_submit": "UserPromptSubmit",
    "post_tool_use": "PostToolUse",
    "post_tool_use_failure": "PostToolUseFailure",
    "stop": "Stop",
    "stop_cancelled": "StopCancelled",
    "interrupt": "Interrupt",
}


def grok_runtime():
    return any(os.environ.get(name) for name in GROK_RUNTIME_ENV)


def normalize_hook_payload(payload):
    """Accept Grok/Claude camelCase event fields as well as snake_case. Mutates in place."""
    if not isinstance(payload, dict):
        return payload
    for src, dst in HOOK_FIELD_ALIASES.items():
        # Grok sends camelCase and snake_case together; values such as tool_input
        # are dicts. A set membership test would raise TypeError (reported as E_INPUT).
        if src in payload and (dst not in payload or payload.get(dst) in (None, "")):
            payload[dst] = payload[src]
    event = payload.get("hook_event_name")
    if isinstance(event, str) and event in GROK_HOOK_EVENTS:
        payload["hook_event_name"] = GROK_HOOK_EVENTS[event]
    if payload.get("hook_event_name") in (None, ""):
        env_event = os.environ.get("GROK_HOOK_EVENT")
        if env_event in GROK_HOOK_EVENTS:
            payload["hook_event_name"] = GROK_HOOK_EVENTS[env_event]
    if payload.get("hook_event_name") == "StopCancelled":
        payload["hook_event_name"] = "Interrupt"
    return payload


def hook_context(agent, payload):
    """Validate and suppress before any state directory is created."""
    if not isinstance(payload, dict):
        raise ObserverError("E_INPUT")
    normalize_hook_payload(payload)
    if grok_runtime() and agent != "grok":
        return None
    cwd, session = payload.get("cwd"), payload.get("session_id")
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        raise ObserverError("E_INPUT")
    if (os.environ.get("AGENT_EVOLUTION_EVALUATION") == "1"
            or any(part.startswith(("agent-evolution-task-", "agent-evolution-reflect-")) for part in Path(cwd).parts)
            or payload.get("agent_id") or payload.get("is_subagent")):
        return None
    valid_identity(agent, cwd, session)
    if payload.get("hook_event_name") not in {"UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "Interrupt"}:
        raise ObserverError("E_INPUT")
    token = payload.get("turn_id") or payload.get("prompt_id")
    if token is not None and (not isinstance(token, str) or len(token) > 1024):
        raise ObserverError("E_INPUT")
    return cwd, session


@contextmanager
def state_operation():
    """Keep storage diagnostics fixed, without exposing paths or saved values."""
    try:
        yield
    except StateError as error:
        code = error.code if error.code in ERRORS else "E_IO"
        raise ObserverError(code) from None


def number(value):
    return type(value) in {int, float} and math.isfinite(value)


def validate_record(data, notice=False):
    try:
        good = (isinstance(data, dict) and re.fullmatch(r"[a-f0-9]{32}", data["id"])
                and data["agent"] in AGENTS and isinstance(data["cwd"], str) and data["cwd"]
                and re.fullmatch(r"[a-f0-9]{64}", data["session_hash"]) and number(data["created"])
                and isinstance(data["failures"], dict) and isinstance(data["skills"], list)
                and all(isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", x) for x in data["skills"])
                and all(type(data[k]) is bool for k in ("closed", "suppressed", "offered", "assessment_requested")))
        for failure in data["failures"].values():
            good = good and isinstance(failure, dict) and type(failure["resolved"]) is bool
        final = data["final"]
        if final is not None:
            good = (good and isinstance(final, dict) and final["status"] in STATUSES
                    and isinstance(final["summary"], str) and number(final["recorded"]))
        if notice:
            good = (good and final is not None and final["status"] == "failed"
                    and data["status"] in {"pending", "approved", "declined"} and number(data["expires"]))
        if not good:
            raise ObserverError("E_STATE_CORRUPT")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ObserverError("E_STATE_CORRUPT") from None
    return data


def clean(value, limit=500):
    return re.sub(r"[\x00-\x1f\x7f]", " ", redact(str(value)))[:limit]


def result_status(payload):
    """Only structured errors and anchored runtime exit records count as observations."""
    if payload.get("is_interrupt"):
        return "cancelled", None
    result = payload.get("tool_response", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", result)
            if match:
                code = int(match[1])
                return ("passed" if code == 0 else "failed"), code
            return "unknown", None
    if isinstance(result, dict):
        for key in ("exit_code", "exitCode"):
            code = result.get(key)
            if type(code) is int:
                return ("passed" if code == 0 else "failed"), code
        if result.get("isError") is True or result.get("is_error") is True:
            return "failed", None
        if result.get("isError") is False or result.get("is_error") is False:
            return "passed", None
        if result.get("interrupted") is True:
            return "cancelled", None
    if payload.get("hook_event_name") == "PostToolUseFailure":
        match = re.match(r"Exit code (-?\d+)(?:\n|$)", str(payload.get("error", "")))
        return "failed", int(match[1]) if match else None
    return "unknown", None


def skill_names(payload):
    inputs = payload.get("tool_input", {})
    if not isinstance(inputs, dict):
        return []
    paths = [inputs.get(k, "") for k in ("file_path", "filePath", "path")]
    command = inputs.get("command", "")
    # These are candidate associations, not proof that the skill caused a failure.
    if isinstance(command, str) and re.search(r"\b(cat|sed|head|tail)\b", command):
        paths.append(command)
    return sorted({m for value in paths if isinstance(value, str)
                   for m in re.findall(r"(?:skills|agent-skills)/([a-z0-9][a-z0-9-]{0,63})/SKILL\.md", value)
                   if redact(m) == m})


class Observer:
    def __init__(self, root, create=True):
        # Explicit state paths are not resolved: resolving first would hide links.
        if ".." in Path(root).parts:
            raise ObserverError("E_UNSAFE_PATH")
        self.root = Path(os.path.abspath(root))
        self.storage = None
        with state_operation():
            try:
                self.storage = PrivateState(self.root, create=create)
            except FileNotFoundError:
                if create:
                    raise
            if self.storage is not None and create:
                self.storage.ensure_ignore()

    @contextmanager
    def locked(self):
        # Each native hook is a separate process; serialize short local updates.
        with state_operation():
            if self.storage is None:
                raise ObserverError("E_STATE_MISSING")
            with self.storage.lock("observer.lock", blocking=True):
                yield

    def identity(self, agent, cwd, session):
        valid_identity(agent, cwd, session)
        return digest(canonical([agent, str(Path(cwd).resolve()), session]))

    def path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ObserverError("E_STATE_CORRUPT")
        return self.root / "sessions" / (identity + ".json")

    def read_json(self, path, notice=False):
        if self.storage is None:
            return None
        with state_operation():
            raw = self.storage.read_bytes(path.relative_to(self.root), missing_ok=True, max_bytes=MAX_BYTES)
        if raw is None:
            return None
        try:
            return validate_record(json.loads(raw), notice=notice)
        except (ValueError, UnicodeError):
            raise ObserverError("E_STATE_CORRUPT") from None

    def write_json(self, path, data):
        with state_operation():
            if self.storage is None:
                raise ObserverError("E_STATE_MISSING")
            self.storage.write_bytes(path.relative_to(self.root), canonical(data))

    def read(self, identity):
        state = self.read_json(self.path(identity))
        if state and state["session_hash"] != identity:
            raise ObserverError("E_STATE_CORRUPT")
        return state

    def write(self, identity, data):
        self.write_json(self.path(identity), data)

    def notices(self, agent):
        if self.storage is None:
            return []
        try:
            with state_operation(), self.storage.directory("notices") as parent:
                names = sorted(name for name in os.listdir(parent) if name.endswith(".json"))
        except FileNotFoundError:
            return []
        results = []
        for name in names:
            notice = self.read_json(self.root / "notices" / name, notice=True)
            if notice and notice["agent"] == agent:
                results.append({k: notice[k] for k in ("id", "agent", "cwd", "status", "final", "expires")})
        return results

    def command(self, agent, cwd, session):
        return shlex.join([RUNTIME["python"], "-I", "-S", "-B", str(PROJECT / "scripts/observe.py"),
                           "--state", str(self.root), "--agent", agent, "--cwd", str(cwd), "--session", session])

    def guidance(self, agent, cwd, session):
        command = self.command(agent, cwd, session)
        recording = (
            "終了判定はagent_evolution_outcomeツールでstatus・summary・必要ならskillを指定して記録してください。"
            "bashでobserve.pyを実行しないでください。回答本文に監視用のコメント・JSON・記録結果を付けないでください。"
            if agent == "pi" else
            "終了判定は次のローカルコマンドで記録し、回答本文に監視用のコメント・JSON・記録結果を付けないでください。"
            f"記録コマンド: {command} outcome --status STATUS --summary '確認結果' [--skill SKILL_NAME]。"
        )
        return (f"{MARKER} 作業終了時に失敗が残った場合だけ起動承認を求めます。"
                "途中のエラーはスキルの欠陥と断定しません。失敗を修正できたらpassed、"
                "未解決ならfailed、確認できなければunverified、中断ならcancelledで結果を記録してください。"
                f"{recording}"
                "記録できない場合も回答本文への埋め込みで代用せず、未記録として扱ってください。"
                "成功した作業や未確認だけの作業では起動を提案しません。"
                "失敗通知への承認は、その通知の調査・再現課題の準備だけに有効です。"
                "実モデル評価は対象・モデル・推論量・未消費の予算を確認してから。不足する条件だけ人に確認し、使い切った上限を再利用しません。")

    def hook(self, agent, payload):
        context = hook_context(agent, payload)
        if context is None:
            return {}
        cwd, session = context
        identity = self.identity(agent, cwd, session)
        event = payload.get("hook_event_name")
        with self.locked():
            state = self.read(identity)
            if event == "UserPromptSubmit":
                prompt = str(payload.get("prompt", ""))
                if prompt.startswith(MARKER):
                    return {}
                token = payload.get("turn_id") or payload.get("prompt_id")
                if not state or not token or state.get("turn") != token:
                    state = {"id": uuid.uuid4().hex, "agent": agent, "cwd": str(Path(cwd).resolve()),
                             "session_hash": identity, "turn": token, "created": time.time(),
                             "failures": {}, "skills": [], "final": None, "assessment_requested": False,
                             "offered": False, "closed": False, "suppressed": False}
                if prompt.strip().rstrip("。.!！").lower() in {"stop", "cancel", "停止", "中止", "止めて", "作業を止めて", "停止してください", "中止してください"}:
                    state["suppressed"] = True
                if prompt.startswith(APPROVED):
                    state["suppressed"] = True
                self.write(identity, state)
                if state["suppressed"]:
                    return {}
                return {"hookSpecificOutput": {"hookEventName": event,
                        "additionalContext": self.guidance(agent, cwd, session)}}
            if not state or time.time() - state["created"] > TTL:
                return {}
            if event == "Interrupt":
                state["suppressed"] = True
                self.write(identity, state)
                return {}
            if state["suppressed"] or state["closed"]:
                return {}
            if event in {"PostToolUse", "PostToolUseFailure"}:
                inputs = payload.get("tool_input", {})
                raw = canonical(inputs)
                # Observation commands must not invalidate their own final status.
                if ("scripts/observe.py" in raw or "agent-evolution-observer" in raw
                        or payload.get("tool_name") == "agent_evolution_outcome"):
                    return {}
                status, code = result_status(payload)
                if status == "unknown" and agent == "claude" and event == "PostToolUse":
                    status = "passed"  # Claude sends failures via PostToolUseFailure.
                tool = clean(payload.get("tool_name", "tool"), 80)
                key = digest(canonical([tool, inputs]))
                if status in {"passed", "failed"}:
                    state["final"] = None
                if status == "failed":
                    if len(state["failures"]) < 200 or key in state["failures"]:
                        state["failures"][key] = {"tool": tool, "exit_code": code, "resolved": False}
                elif status == "passed":
                    if key in state["failures"]:
                        state["failures"][key]["resolved"] = True
                    state["skills"] = sorted(set(state["skills"]) | set(skill_names(payload)))[:30]
                self.write(identity, state)
                return {}
            if event != "Stop":
                return {}
            final = state["final"]
            if final is None:
                if any(not v["resolved"] for v in state["failures"].values()) and not state["assessment_requested"]:
                    state["assessment_requested"] = True
                    self.write(identity, state)
                    return {"decision": "block", "reason": f"{MARKER} ツール失敗の記録があります。"
                            "終了時に失敗が残っているかを作業結果から確認し、次の手順で結果を記録してください。"
                            "この段階では人への起動確認も改善処理も開始しません。\n" + self.guidance(agent, cwd, session)}
                return {}
            if final["status"] != "failed":
                state["closed"] = True
                self.write(identity, state)
                return {}
            notice = {**state, "status": "pending", "expires": time.time() + TTL,
                      "consent_scope": "investigation-and-dataset-preparation-only"}
            self.write_json(self.notice_path(state["id"]), notice)
            state["offered"] = state["closed"] = True
            self.write(identity, state)
            command = self.command(agent, cwd, session)
            message = self.question(notice)
            return {"decision": "block", "reason": f"{MARKER} 以下の確認を人に1回だけ提示し、返答を待ってください。"
                    "承認前にagent-evolutionの調査・候補生成・評価を始めないでください。\n"
                    f"{message}\n通知ID: {notice['id']}\n"
                    f"明示的な返答の後だけ {command} decide --notice {notice['id']} --decision approve|decline を実行してください。"
                    "承認なら出力されたスキルの手順を開始し、辞退なら終了します。無回答は承認ではありません。",
                    "notice": notice["id"], "question": message}

    @staticmethod
    def question(notice):
        names = ", ".join(notice["skills"]) or "未特定（起動後に確認）"
        return (f"作業終了時に失敗が残っています: {notice['final']['summary']}\n"
                f"関連スキル候補: {names}。agent-evolutionを起動して、失敗原因と再現課題を整理しますか？"
                "［起動する／今回は見送る］実モデル評価はモデルと実行上限を確定してから開始します。")

    def outcome(self, agent, cwd, session, status, summary, skill=None):
        if status not in STATUSES or not summary.strip():
            raise ObserverError("E_INPUT")
        if skill and (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skill) or redact(skill) != skill):
            raise ObserverError("E_INPUT")
        identity = self.identity(agent, cwd, session)
        with self.locked():
            state = self.read(identity)
            if not state:
                raise ObserverError("E_STATE_MISSING")
            if state["closed"] or state["suppressed"]:
                raise ObserverError("E_TASK_CLOSED")
            if time.time() - state["created"] > TTL:
                raise ObserverError("E_TASK_EXPIRED")
            state["final"] = {"status": status, "summary": clean(summary), "recorded": time.time()}
            if skill:
                state["skills"] = sorted(set(state["skills"]) | {skill})
            self.write(identity, state)
        return {"recorded": status, "model_called": False}

    def notice_path(self, ident):
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ObserverError("E_INPUT")
        return self.root / "notices" / (ident + ".json")

    def decide(self, ident, decision, agent=None, cwd=None, session=None):
        if decision not in {"approve", "decline"}:
            raise ObserverError("E_INPUT")
        with self.locked():
            path = self.notice_path(ident)
            notice = self.read_json(path, notice=True)
            if not notice:
                raise ObserverError("E_STATE_MISSING")
            if notice["id"] != ident:
                raise ObserverError("E_STATE_CORRUPT")
            if agent is not None and notice["agent"] != agent:
                raise ObserverError("E_INPUT")
            if session and notice["session_hash"] != self.identity(agent, cwd, session):
                raise ObserverError("E_INPUT")
            if notice["status"] != "pending":
                raise ObserverError("E_NOTICE_DECIDED")
            if time.time() >= notice["expires"]:
                raise ObserverError("E_NOTICE_EXPIRED")
            notice.update(status="approved" if decision == "approve" else "declined", decided=time.time())
            self.write_json(path, notice)
            if decision == "approve":
                current = self.read(notice["session_hash"])
                if current:
                    current["suppressed"] = True
                    self.write(notice["session_hash"], current)
        result = {"notice": ident, "status": notice["status"], "model_called": False,
                  "evaluation_authorized": False, "promotion_authorized": False}
        if decision == "approve":
            result["instruction"] = (f"{APPROVED} {SKILL} を読み、通知 {path} の失敗について対象スキル・再現課題・期待結果を整理する。"
                                     "評価対象、モデル、推論設定、評価と生成の未消費の上限を確認し、不足する条件だけユーザーと確定してから実モデルを呼ぶ。")
        return result


def diagnostic(error):
    if isinstance(error, ObserverError):
        return error.code
    if isinstance(error, PermissionError) or isinstance(error, OSError) and error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return "E_PERMISSION"
    if isinstance(error, OSError) and error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
        return "E_UNSAFE_PATH"
    return "E_IO" if isinstance(error, OSError) else "E_INPUT"


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ObserverError("E_INPUT")


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--agent", choices=AGENTS, required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session", default="")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("hook")
    sub.add_parser("event")
    p = sub.add_parser("list")
    p.add_argument("--cwd", default=argparse.SUPPRESS)
    p = sub.add_parser("outcome")
    p.add_argument("--status", choices=STATUSES, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--skill")
    p = sub.add_parser("decide")
    p.add_argument("--notice", required=True)
    p.add_argument("--decision", choices=("approve", "decline"), required=True)
    args = None
    try:
        args = parser.parse_args(argv)
        payload = None
        if args.mode in {"hook", "event"}:
            raw = sys.stdin.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ObserverError("E_INPUT_TOO_LARGE")
            try:
                payload = json.loads(raw)
            except ValueError:
                raise ObserverError("E_INPUT") from None
            context = hook_context(args.agent, payload)
            if context is None:
                print("{}")
                return 0
            cwd = context[0]
        else:
            cwd = args.cwd
        if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
            raise ObserverError("E_INPUT")
        root = args.state if args.state is not None else Path(cwd) / ".evolution-observer"
        observer = Observer(root, create=args.mode != "list")
        if args.mode in {"hook", "event"}:
            result = observer.hook(args.agent, payload)
            if args.mode == "hook":
                result = {k: v for k, v in result.items() if k not in {"notice", "question"}}
        elif args.mode == "outcome":
            result = observer.outcome(args.agent, args.cwd, args.session, args.status, args.summary, args.skill)
        elif args.mode == "decide":
            result = observer.decide(args.notice, args.decision, args.agent, args.cwd, args.session)
        else:
            result = observer.notices(args.agent)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, EvolutionError) as error:
        code = diagnostic(error)
        message = f"agent-evolution: [{code}] 終了監視: {ERRORS[code]} 自動起動は行っていません。"
        if args is not None and args.mode in {"hook", "event"}:
            result = {"systemMessage": message}
            if args.mode == "event":
                result["error"] = {"code": code, "operation": args.mode}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        print(message + " no model was called", file=sys.stderr)
        return 2
