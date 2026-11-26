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

```sh
python3 -I -S -B scripts/observer_bundle.py build --output /path/to/observer-releases
# buildが返したbundleの絶対パスを指定する
python3 -I -S -B scripts/install_observer.py --runtime /absolute/path/to/candidate --dry-run
python3 -I -S -B scripts/install_observer.py --runtime /absolute/path/to/candidate
```

監視実行コードは `~/.local/share/agent-evolution/observer/releases/<SHA-256>/` に通常ファイルとして配布する。Documents内の開発コピーや `.venv` を変更しても登録済みの版は変わらない。配布済みの版は上書きせず、更新ごとに候補作成、内容検証、接続先の切替を行う。配布・保護範囲の変更は具体案への人の承認を得てから実施する。

`observer-runtime.json` はPython実体と正本スキルの絶対パスを指定する。Pythonは `-I -S -B` で起動し、設定欠落・不正時に開発コピーへ戻らない。SKILL.mdの正本と既存リンクはそのまま使う。Piの入力Schemaは配布物内にあり、外部パッケージを動的にimportしない。

インストーラーは既存設定を保持し、変更前後のハッシュ・対象定義・適用状況を `~/.local/share/agent-evolution/observer/changes/日時.json` に記録する。他の設定全体は複製しない。既知の管理対象コマンドと入口だけを更新し、独自に変更された入口や設定競合は上書きしない。途中で失敗した場合は記録を確認し、旧開発コピーへ自動復帰しない。再実行は同じ設定を重複登録しない。指定製品だけなら `--agents codex,claude` を使う。

保護の前提は、通常ツールの書込み許可範囲に配布先とPython実体が含まれないこと。同一ユーザーの制限なし実行、管理者、ホーム全体を許可したセッションからの改変は防がない。配布先の権限0700/0600だけを同一ユーザーに対する隔離とは扱わない。

- Codex: `~/.codex/hooks.json`（`~/.codex-work/hooks.json` がある場合はそちらにも登録する）
- Claude Code: `~/.claude/settings.json`
- Grok CLI: `~/.grok/hooks/agent-evolution.json`
- Pi: `~/.pi/agent/extensions/agent-evolution.js`
- OpenCode: `~/.config/opencode/plugins/agent-evolution.js`

**Codexは新しいセッションで `/hooks` を開き、追加されたobserve.pyの定義を信頼する操作が必要。** 信頼情報や承認ポリシーはインストーラーで書き換えない。Grok CLIでは、ホーム以外の作業フォルダで `/hooks` や `/hooks-trust` が必要になることがある。Claude Code・Pi・OpenCodeも次回起動で読み込ませる。

解除する場合は監視停止への承認を得た上で、Codex・Claude Codeの該当配布版 `scripts/observe.py` を実行する管理対象ハンドラーだけを削除する。Pi・OpenCodeは上記のagent-evolution.jsだけを削除する。既存のverify-changesなどのフックを残す。古いバックアップの丸ごと復元は、その後の設定変更を消すため通常の解除には使わない。

## 保存と実行範囲

新しい作業の既定保存先は `<セッション開始時の作業フォルダ>/.evolution-observer/`。開始・観測・結果記録・承認/辞退で同じ保存先を使用する。ツール単位の作業フォルダ変更では切り替えない。明示した `--state` を優先し、Pi・OpenCodeでは `AGENT_EVOLUTION_OBSERVER_STATE` もセッション開始時に固定する。

セッションと作業フォルダの識別ハッシュ、ツール入力のハッシュ、ツール名、終了コード、最終結果の短い要約、関連スキル候補、通知と承認・辞退を保存する。プロンプト全文、コマンド本文、ツール出力、会話履歴、モデルの思考過程は保存しない。既知の秘密文字列パターンは要約から除くが、自由文に秘密を記録しないことも必要。

新しい記録用フォルダは0700、ファイルは0600で作成し、保存先のリンクや想定外のファイル種別を拒否する。記録用フォルダ内の `.gitignore` に `*` を置き、通常のGit追加から除外する。強制追加やフォルダ全体のコピーは防がない。記録は各プロジェクトの移動・削除・共有に伴って移動・消失・共有され得る。中央索引や自動移行は行わない。

移行前の中央保存先（プロジェクト直下の `.evolution-observer`）は残す。既に案内した明示的な `--state` コマンドは引き続きその場所を使う。新規作業は新保存先へ記録する。読み取り専用の環境では記録不能を表示し、別の場所や高い権限へ自動切替しない。

承認は通知IDごとに1回だけ記録できる。通知期限を過ぎた承認は拒否する。ファイルは自動削除しない。不要になった観測記録は、この保存先を対象に手動で削除できる。

確認だけで使用できるコマンド:

```sh
python3 -I -S -B /absolute/path/to/release/scripts/observe.py --agent codex --cwd /path/to/project list
# 移行前の中央記録の閲覧
python3 -I -S -B /absolute/path/to/release/scripts/observe.py --agent codex --state .evolution-observer list
```

フック自身はモデルを呼ばない。Codex・Claude CodeのStop継続は、終了判定や質問を作る通常セッションのモデル応答を最大2回追加し得る。評価タスクやGEPAの呼び出しとは別である。Piの承認後は準備用の通常応答を開始する。実モデル評価・候補生成の回数上限は既存CLIで管理し、起動承認だけで以前の予算を再利用しない。

評価用の一時作業フォルダと `AGENT_EVOLUTION_EVALUATION=1` の処理は監視対象から除外し、改善評価が起動確認を再帰的に発生させないようにする。

記録失敗時は権限不足、状態なし、終了済み/抑止済み、期限切れ、入力不正/過大、保存データ不正などの固定エラーコードで原因を区別する。生の例外や入力本文は表示しない。フックは警告を返して終了コード0で通常作業を継続し、直接操作の失敗は終了コード2を返す。記録処理の失敗は実モデル評価の失敗とは別であり、記録処理自体はモデルを呼ばない。

## 検証

Pythonテストは正常終了、未解決失敗、解消済みエラー、未確認、中断、無回答、辞退、承認、通知の重複、同時実行、期限切れ、別セッション、設定保全を確認する。JavaScriptテストはPi・OpenCodeのイベントと承認分岐、実Pythonスクリプトとの接続を確認する。

```sh
.venv/bin/python -m pytest -q
node --test tests/observer-integrations.test.mjs
```

実施結果と実環境の未確認事項は [OBSERVER_VERIFICATION.md](OBSERVER_VERIFICATION.md) に記録する。

## 公式仕様

- [Codexのフックと信頼確認](https://learn.chatgpt.com/docs/hooks)
- [Claude Codeのフック](https://code.claude.com/docs/en/hooks)
- [Piの拡張](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
- [OpenCodeのプラグイン](https://opencode.ai/docs/plugins/)
