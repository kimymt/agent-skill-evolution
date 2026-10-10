# 作業終了時に起動の承認を求める

ユーザー指定の条件は「終了時に失敗が残った場合だけ」。通常作業の途中でエラーが発生しても、最後に修正できていれば提案しない。

## 動作

1. 作業開始時に、終了判定の記録方法を担当エージェントへ渡す。
2. ツール結果から失敗の有無をローカルで記録する。同じツール入力が後に成功した場合は解消済みにする。
3. 担当エージェントが作業全体を `passed / failed / unverified / cancelled` で判定する。終了コード0でも成果物が期待を満たさなければfailed、環境不足で確認不能ならunverifiedとする。
4. 終了時にfailedが記録されていた場合だけ、失敗の要約を付けて起動するかを尋ねる。
5. 人の承認後にagent-evolutionを開始し、対象スキル・原因・再現課題を整理する。実モデル評価の条件と予算が未確定なら、その条件を確認する。

確認文の例:

> 作業終了時に失敗が残っています: 保存処理のテストが失敗。関連スキル候補: investigate-bug。agent-evolutionを起動して、失敗原因と再現課題を整理しますか？［起動する／今回は見送る］

同じ作業の通知は1回。辞退・無回答で実行は始まらない。新しい作業で新しい失敗が残った場合は、別の通知を作る。通知の有効期間は7日。

## 製品ごとの接続

| 製品 | 開始・観測・終了 | 承認の提示 |
| --- | --- | --- |
| Codex | UserPromptSubmit、PostToolUse、Stop、Interrupt | Stopが同じエージェントに確認の質問を要求する |
| Claude Code | UserPromptSubmit、PostToolUse、PostToolUseFailure、Stop | Stopが同じエージェントに確認の質問を要求する |
| Pi | before_agent_start、tool_result、agent_settled | 作業が完全に終了してからUI確認。承認時に準備の依頼を送る |
| OpenCode | chat.message、message.part.updated、session.idle | TUI通知。`evolution approve 通知ID` / `evolution decline 通知ID` と返信 |

Codex・Claude Codeでは失敗の観測だけがあり終了判定が未記録の場合、1回だけ結果の確認・記録をエージェントに要求する。失敗の最終判定が付くまで人への起動確認は行わない。起動確認を提示する継続要求も1回まで。

終了結果はフックが提示する `scripts/observe.py ... outcome --status STATUS --summary ...` でローカルに記録する。Piでは専用の `agent_evolution_outcome` ツールを使い、通常のbashコマンドは実行させない。Piの記録ツールは待機・成功時の表示を0行とし、記録失敗時の表示を残す。回答本文には監視用コメント・JSON・記録結果を付けない。回答本文から終了判定を読み取る処理は廃止した。記録先への書き込みが許可されない場合も、本文への埋め込みで代用せず未記録として扱う。

担当エージェントが終了判定を記録しない場合や、フックが無効・未信頼の場合は起動確認を保証できない。意味的な失敗や確認手順の網羅性をスクリプトだけで証明する機能ではない。

## 設定

以下のインストーラーはユーザーのフック・拡張設定を書き換える。既存フック識別の問題と変更案を確認し、具体的な変更への人の承認を得てから実行する。配布用フォルダの作成では実行していない。

```sh
cd agent-evolution
.venv/bin/python scripts/install_observer.py --dry-run
.venv/bin/python scripts/install_observer.py
```

変更前のJSONを同じフォルダの `*.before-agent-evolution-日時` に保存する。この修正前版はコマンド中のパス文字列で管理対象を識別します。別フックの引数に同じパスが含まれると、そのフックを除去する可能性があります。指定製品だけなら `--agents codex,claude` を使う。

- Codex: `~/.codex/hooks.json`
- Claude Code: `~/.claude/settings.json`
- Pi: `~/.pi/agent/extensions/agent-evolution.js`
- OpenCode: `~/.config/opencode/plugins/agent-evolution.js`

**Codexは新しいセッションで `/hooks` を開き、追加されたobserve.pyの定義を信頼する操作が必要。** 信頼情報や承認ポリシーはインストーラーで書き換えない。Claude Code・Pi・OpenCodeも次回起動で読み込ませる。

解除する場合、Codex・Claude Codeは `scripts/observe.py` を指すハンドラーだけを削除する。Pi・OpenCodeは上記のagent-evolution.jsだけを削除する。解除対象を確認し、既存のverify-changesなどのフックを残す。古いバックアップの丸ごと復元は、その後の設定変更を消すため通常の解除には使わない。

## 保存と実行範囲

保存先はこのプロジェクトの `.evolution-observer/`。セッションと作業フォルダの識別ハッシュ、ツール入力のハッシュ、ツール名、終了コード、最終結果の短い要約、関連スキル候補、通知と承認・辞退を保存する。プロンプト全文、コマンド本文、ツール出力、会話履歴、モデルの思考過程は保存しない。既知の秘密文字列パターンは要約から除くが、自由文に秘密を記録しないことも必要。

承認は通知IDごとに1回だけ記録できる。通知期限を過ぎた承認は拒否する。ファイルは自動削除しない。不要になった観測記録は、この保存先を対象に手動で削除できる。

確認だけで使用できるコマンド:

```sh
.venv/bin/python scripts/observe.py --agent codex list
```

フック自身はモデルを呼ばない。Codex・Claude CodeのStop継続は、終了判定や質問を作る通常セッションのモデル応答を最大2回追加し得る。評価タスクやGEPAの呼び出しとは別である。Piの承認後は準備用の通常応答を開始する。実モデル評価・候補生成の回数上限は既存CLIで管理し、起動承認だけで以前の予算を再利用しない。

評価用の一時作業フォルダと `AGENT_EVOLUTION_EVALUATION=1` の処理は監視対象から除外し、改善評価が起動確認を再帰的に発生させないようにする。

## 検証

Pythonテストは正常終了、未解決失敗、解消済みエラー、未確認、中断、無回答、辞退、承認、通知の重複、同時実行、期限切れ、別セッション、設定保全を確認する。JavaScriptテストはPi・OpenCodeのイベントと承認分岐、実Pythonスクリプトとの接続を確認する。

```sh
.venv/bin/python -m pytest -q
node --test tests/observer-integrations.test.mjs
```

ローカル検証記録は配布から除外している。
