---
name: agent-evolution
description: Evaluate and improve a shared SKILL.md using fixed tasks, selected coding agents, and recorded model settings. Use when asked to compare skill variants, run skill evolution, review evaluation evidence, or apply or roll back an evaluated skill version.
---

# スキルの比較・改善

共通CLIの導入と操作は、配布リポジトリの `README.md` を読む。同梱の `scripts/run.py` は、CLIフォルダの隣に `agent-skills/agent-evolution/` がある配置を前提とする。リポジトリ内の同梱位置からそのまま起動できるラッパーではない。

## 作業終了時の起動確認

終了監視から呼ばれた場合は、[承認後の開始手順](references/completion-observer.md)を読む。終了時に失敗が残った作業だけ起動を提案し、人の明示的な承認を待つ。途中のエラーが解消した作業、未確認だけの作業、中断した作業は起動対象にしない。

起動の承認を得たら、対象スキル・失敗原因・再現課題を整理する。通知の承認だけで実モデル評価や正本への適用まで許可されたとは扱わない。対象、モデル、推論設定、未消費の評価・生成予算が既に明示されていれば引き継ぎ、不足する条件だけを確認する。

## 評価する

- 対象スキル、課題、使う製品・モデル・推論設定、実行上限を設定ファイルで固定する。1製品だけで開始できる。未選択の製品を勝手に追加しない。
- `doctor` と `dataset validate` で前提を確認する。`start` はスナップショットを作るだけ。`baseline`、`evaluate`、`evolve`、`finalize` は実モデルを呼び得る。既存の利用許可と予算を引き継ぎ、未指定ならモデル呼び出し前に確定する。
- 現在版と候補版は同じモデル・推論設定で比較する。候補生成用のモデルは別設定にできる。高性能モデルへ自動切替しない。
- 最初は `demo` で一巡動作を確認できる。simulationの結果は性能改善の証拠にしない。
- `evolve` はGEPAで候補を探す。手で提案した本文を評価する場合は `import-candidate` を使う。探索中にholdoutを読まない。

## 結果と採用

- `report` の差分・課題別結果・未選択製品・費用不明の項目を確認する。評価した候補ハッシュと正本を区別する。
- 採用は `finalize` で適格と判定された候補のみ。人が候補・適用先・レポートを確認し、採用を指示した場合に `approve` と `promote` を実行する。改善案の生成依頼だけでは正本を置き換えない。
- `rollback` は採用IDを指定する。競合時に正本を強制上書きしない。
- 隔離や秘密検出の失敗を回避するために、ホスト通常実行へ切り替えたり、採用条件を緩めたりしない。
- 終了監視は個別に設定したフック・拡張で行う。定期実行・全履歴の収集・他のプラグイン設定の変更はこのスキルの通常操作に含めない。

参照: [GEPA](https://github.com/gepa-ai/gepa)、配布リポジトリの `README.md`。
