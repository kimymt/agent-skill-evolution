# Agent Evolution

[English](README.en.md)

共有エージェントスキルの `SKILL.md` 本文を、固定した課題・モデル・推論設定で比較し、改善候補と評価結果を記録するローカルCLIです。手動で作った候補の比較と、[GEPA](https://github.com/gepa-ai/gepa)による候補生成を扱います。

変更対象はスキル本文です。frontmatter、補助ファイル、エージェント本体のコードは進化の対象に含みません。候補の適用には評価結果の確認と人の承認が必要です。

## 配布版の状態

この配布版は、2026年10月10日21:27に保存された修正前スナップショットを基にしています。以後の互換性改善・セキュリティ修正は含みません。

Codex、Claude Code、OpenCode、PiのCLI接続・イベント変換コードがあります。過去に実モデル接続を確認したのは、macOS上のCodexだけです。他製品とDockerの実接続は未確認です。Codex nativeの設定は `0.160.0` に限定されています。最新CLIとの互換性は保証しません。

修正前コードには、評価側の設定で秘密検査の結果が変わる構造があります。別パスのファイル読込を指定スキルの読込と認定する構造や、別セッションへ移った子プロセスを停止対象から漏らす構造もあります。監視インストーラーはコマンド中のパス文字列で管理対象を識別するため、別フックを除去する可能性があります。深いJSONの解析例外を通常の評価失敗として記録できない場合もあります。この配布版では、これらの問題は解消していません。

## セットアップ

Python 3.11以降を使用します。リポジトリのルートで次を実行します。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/agent-evolution --help
```

`requirements.lock` はバージョンを固定した依存一覧で、GEPAとテスト用pytestも含みます。配布ファイルのハッシュは記録していません。エージェントCLIやコンテナイメージのインストールは、この手順に含まれていません。

## モデルを呼ばない動作確認

```sh
.venv/bin/agent-evolution --state .evolution-demo demo
.venv/bin/agent-evolution dataset validate --suite fixtures/bug-investigation-v1.json
```

`demo` は候補作成、比較、レポート出力をsimulationで実行します。実モデルは呼ばず、結果は採用条件を通過しません。同梱データセットは9件の合成課題で、train 2件、validation 2件、holdout 5件です。

## 実モデルでの比較

実評価には選択したエージェントCLI、モデルへの接続手段、[Gitleaks](https://github.com/gitleaks/gitleaks)、利用する実行方式の準備が必要です。

`examples/codex-smoke.json` をコピーして設定します。`skill` と `suite` の相対パスは設定ファイルの場所を基準に解決されます。サンプルは、別途用意する `investigate-bug` スキルを指定しています。このリポジトリだけでは実評価を開始できません。環境固有のPython絶対パスを使うサンプルと課題は配布から除外しています。

製品、モデル、推論量、CLIバージョン、評価・生成回数、時間上限、外部モデルへ渡すデータを実行前に確認します。次の例はconfigをリポジトリのルートに置いた場合です。`RUN_ID` と `SHA` は各コマンドの出力に置き換えます。

```sh
.venv/bin/agent-evolution doctor --agents codex
.venv/bin/agent-evolution start --config config.json
.venv/bin/agent-evolution baseline --run RUN_ID --split train
.venv/bin/agent-evolution import-candidate --run RUN_ID --body candidate-body.md
.venv/bin/agent-evolution evaluate --run RUN_ID --candidate-sha SHA --split train
.venv/bin/agent-evolution report --run RUN_ID
```

`start` は計画と入力を保存し、モデルは呼びません。`baseline` と `evaluate` は実評価でモデルを呼びます。候補ファイルはfrontmatterを除いた本文だけにします。

サンプルでは、trainで現在版と候補版を比較するため、評価の上限を4タスクに設定しています。validation・holdout・反復比較を含む最終採用には不足します。上限を増やす場合は追加の実行回数と費用を確認します。

## GEPAによる候補生成

```sh
.venv/bin/agent-evolution evolve --run RUN_ID
```

設定には生成用の `generator` が必要です。`budget.max_generations` には0より大きい値を指定します。評価用と生成用のモデルを分けられます。trainの課題・結果から候補を生成し、validationで選択します。探索にはholdoutを渡しません。このスナップショットの過去の検証記録では、実モデルを使うGEPA候補生成の接続は未確認です。

## 最終比較と適用

```sh
.venv/bin/agent-evolution select --run RUN_ID --candidate-sha SHA
.venv/bin/agent-evolution finalize --run RUN_ID
.venv/bin/agent-evolution report --run RUN_ID
```

`finalize` は候補を固定し、holdoutで現在版と候補版を比較します。既定は最低5課題グループ・3反復で、成功率、グループ単位のbootstrap区間、必須チェック、時間増加を判定します。候補固定後の再探索や、同じholdoutの別runでの再利用は拒否します。

結果が採用条件を満たし、人が差分・適用先・レポートを確認して採用を指示した場合に、次のコマンドを実行します。

```sh
.venv/bin/agent-evolution approve --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
.venv/bin/agent-evolution promote --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
.venv/bin/agent-evolution rollback --promotion PROMOTION_ID
```

承認は候補・レポートのハッシュと適用先に紐付きます。正本が評価後に変わっていれば適用を拒否します。変更前の本文をSQLiteへ保存し、復元でも後からの編集との競合を検査します。

## 保存先・予算・中断

通常の保存先は作業ディレクトリの `.evolution/` です。グローバルオプション `--state PATH` はサブコマンドの前に指定します。SQLite、比較したスキル、操作記録、確認対象の成果物、レポート、差分を保存します。

```sh
.venv/bin/agent-evolution --state .evolution cancel --run RUN_ID
```

予算は評価・生成の開始数と経過時間を制限します。CLI内部のAPI往復数や課金額の厳密な上限ではありません。失敗した開始も消費に数え、取得できない費用は `unknown` と表示します。完了済みの同一評価は同じrun内で再利用します。キャンセル後や実装変更後のrunは再開できません。

## 操作用スキルと終了監視

操作用スキルは、エージェントからこのCLIを呼ぶための指示文です。終了監視は、終了時に失敗が残った作業について改善作業の起動を提案する任意の連携です。監視スクリプト自身は実モデル評価や候補生成を開始しません。起動の承認とスキル適用の承認は別です。

修正前の登録スクリプトとスキルラッパーは、CLIの隣に `agent-skills/agent-evolution/` が存在する配置を前提とします。同梱スキルは `skills/agent-evolution/` にあります。この配布版には、リポジトリ内の同梱位置からスキルを自動登録する機能はありません。CLIだけのセットアップでは監視設定やスキル登録は行いません。

監視インストーラーはユーザーのフック・拡張設定を変更します。このバージョンの既存フック識別の問題を含む変更内容を確認したうえで、導入方法を別途決める必要があります。

## ライセンス

MITライセンスで配布します。全文は [LICENSE](LICENSE) を参照してください。依存ライブラリには、それぞれのライセンスが適用されます。
