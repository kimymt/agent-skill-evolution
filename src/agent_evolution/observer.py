"""Local lifecycle observations. This module never launches a model or an evaluation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

from .util import EvolutionError, atomic_write, canonical, digest, redact

PROJECT = Path(__file__).resolve().parents[2]
SKILL = PROJECT.parent / "agent-skills/agent-evolution/SKILL.md"
MARKER = "[agent-evolution-observer]"
APPROVED = "[agent-evolution-approved]"
AGENTS = ("codex", "claude", "opencode", "pi")
STATUSES = ("passed", "failed", "unverified", "cancelled")
TTL = 7 * 86400


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
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self):
        # Each native hook is a separate process; serialize short local updates.
        with (self.root / "observer.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def identity(self, agent, cwd, session):
        if agent not in AGENTS or not isinstance(session, str) or not session or len(session) > 1024:
            raise EvolutionError("Invalid observer identity")
        return digest(canonical([agent, str(Path(cwd).resolve()), session]))

    def path(self, identity):
        return self.root / "sessions" / (identity + ".json")

    def read(self, identity):
        p = self.path(identity)
        return json.loads(p.read_text()) if p.exists() else None

    def write(self, identity, data):
        atomic_write(self.path(identity), canonical(data))

    def command(self, agent, cwd, session):
        return shlex.join([str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py"),
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
        if not isinstance(payload, dict):
            raise EvolutionError("Expected hook object")
        cwd, session = payload.get("cwd"), payload.get("session_id")
        if not isinstance(cwd, str) or not cwd:
            raise EvolutionError("Missing working directory")
        if os.environ.get("AGENT_EVOLUTION_EVALUATION") == "1" or any(
                part.startswith(("agent-evolution-task-", "agent-evolution-reflect-")) for part in Path(cwd).parts):
            return {}
        if payload.get("agent_id") or payload.get("is_subagent"):
            return {}
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
                if "scripts/observe.py" in raw or "agent-evolution-observer" in raw:
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
            atomic_write(self.notice_path(state["id"]), canonical(notice))
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
            raise EvolutionError("A final status and summary are required")
        if skill and (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skill) or redact(skill) != skill):
            raise EvolutionError("Invalid skill name")
        identity = self.identity(agent, cwd, session)
        with self.locked():
            state = self.read(identity)
            if not state or state["closed"] or state["suppressed"] or time.time() - state["created"] > TTL:
                raise EvolutionError("No active observation for this task")
            state["final"] = {"status": status, "summary": clean(summary), "recorded": time.time()}
            if skill:
                state["skills"] = sorted(set(state["skills"]) | {skill})
            self.write(identity, state)
        return {"recorded": status, "model_called": False}

    def notice_path(self, ident):
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise EvolutionError("Invalid notice ID")
        return self.root / "notices" / (ident + ".json")

    def decide(self, ident, decision):
        if decision not in {"approve", "decline"}:
            raise EvolutionError("Expected approve or decline")
        with self.locked():
            path = self.notice_path(ident)
            if not path.exists():
                raise EvolutionError("Unknown notice")
            notice = json.loads(path.read_text())
            if notice["status"] != "pending" or time.time() >= notice["expires"]:
                raise EvolutionError("Notice already decided or expired")
            notice.update(status="approved" if decision == "approve" else "declined", decided=time.time())
            atomic_write(path, canonical(notice))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=PROJECT / ".evolution-observer")
    parser.add_argument("--agent", choices=AGENTS, required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session", default="")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("hook")
    sub.add_parser("event")
    sub.add_parser("list")
    p = sub.add_parser("outcome")
    p.add_argument("--status", choices=STATUSES, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--skill")
    p = sub.add_parser("decide")
    p.add_argument("--notice", required=True)
    p.add_argument("--decision", choices=("approve", "decline"), required=True)
    args = parser.parse_args(argv)
    try:
        observer = Observer(args.state)
        if args.mode in {"hook", "event"}:
            raw = sys.stdin.read(1_000_001)
            if len(raw) > 1_000_000:
                raise EvolutionError("Hook payload too large")
            result = observer.hook(args.agent, json.loads(raw))
            # Only contract fields go to native hooks. JS integrations use the same reason.
            if args.mode == "hook":
                result = {k: v for k, v in result.items() if k not in {"notice", "question"}}
        elif args.mode == "outcome":
            result = observer.outcome(args.agent, args.cwd, args.session, args.status, args.summary, args.skill)
        elif args.mode == "decide":
            result = observer.decide(args.notice, args.decision)
        else:
            result = [{k: n[k] for k in ("id", "agent", "cwd", "status", "final", "expires")}
                      for p in sorted((observer.root / "notices").glob("*.json"))
                      if (n := json.loads(p.read_text()))["agent"] == args.agent]
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, EvolutionError):
        if args.mode in {"hook", "event"}:
            print(json.dumps({"systemMessage": "agent-evolution: 終了監視の記録に失敗しました。自動起動は行っていません。"}, ensure_ascii=False))
            return 0
        print("agent-evolution: observer operation failed; no model was called", file=sys.stderr)
        return 2
