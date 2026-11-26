# 実行手順

この文書は、共通CLIの実際のサブコマンドと引数に合わせた実行手順です。グローバルオプション `--state PATH` はサブコマンドより前に置きます。省略時の保存先は作業ディレクトリの `.evolution/` です。同梱の `scripts/run.py` から起動した場合は、指定がなければリポジトリの `.evolution/` を使います。

通常の実行順は次のとおりです。

`start` → `baseline` → `evolve` → `evaluate` → `select` → `finalize` → `report` → `approve` → `promote`

`doctor` と `dataset validate` は `start` の前に使う確認用コマンドです。モデルは呼びません。手で書いた本文を評価する場合は、`evolve` の代わりに `import-candidate` を使います。

## 人が承認する地点

- 設定ファイルの対象スキル、課題、製品、モデル、推論設定、予算は、`start` の前に人が確定する。未選択の製品を勝手に追加しない。高性能モデルへ自動切替しない。
- `start` と `doctor` はモデルを呼ばない。終了監視の起動承認や、対象整理の承認だけで、実モデル評価や正本への適用まで許可されたとは扱わない。
- `baseline`、`evolve`、`evaluate`、`finalize` は実モデルを呼び得る。既存の利用許可と予算を引き継ぎ、未指定ならモデル呼び出し前に人が確定する。
- `select` では、人が評価結果を確認した候補ハッシュだけを指定する。
- 採用は `finalize` で適格と判定された候補だけが対象。承認は人が行う。人が `report` の差分・課題別結果・未選択製品・費用不明の項目と、候補ハッシュ・評価レポート・適用先を確認し、採用を指示した場合にだけ `approve` と `promote` を実行する。`approve`、`promote`、`rollback` は対話端末では実行内容（run、候補、レポート、反映先、rollback では採用ID）を表示して y/N で確認する。対話端末でなければ、人が明示した `--yes` がないと非0で終了する。エージェントは `--yes` を自分で付けてはいけない。改善案の生成依頼だけでは正本を置き換えない。対象スキルを使う作業を終えてから採用し、新しいセッションを開始する。
- `rollback` も人の指示が必要で、`approve` や `promote` と同じ確認手順を取る。正本が後から編集されていれば上書きしない。エージェントは `--yes` を自分で付けてはいけない。
- `cancel` は人の判断で実行する。

## 設定ファイルのひな形

`start --config PATH` と `doctor --config PATH` が読む JSON です。公式スキーマは配布リポジトリの `schemas/experiment.schema.json`、実例は `examples/codex-smoke.json` です。`skill` と `suite` は、設定ファイルがあるディレクトリからの相対パス、または絶対パスで書きます。必須項目は `skill`、`suite`、`targets` です。追加のキーは受け付けません。

`targets` と `generator` の各プロファイルでは、`id`、`agent`、`effort`、`backend` が必須です。`model` は省略でき、省略時は各ハーネスの既定モデルを使います。`agent` は `claude`、`codex`、`opencode`、`pi`。`backend` は `codex-native`、`docker`、`simulation`。`codex-native` は `agent` が `codex` のときだけ使えます。`version` は省略するか `"auto"` にすると実環境から取得し、明示した場合は完全一致を要求します。`network` の既定は `"none"`。`auth_env` に書けるのは `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`OPENROUTER_API_KEY` だけです。値ではなく変数名だけを書き、秘密情報は設定ファイルに含めません。

`docker` は製品別の実効設定を読む診断が未実装のため、この配布では実評価を開始しません。`simulation` の結果は性能改善の証拠にも採用条件にも使いません。

`evolve` を使う場合は `generator` に実モデル用プロファイルを置き、`budget.max_generations` を 1 以上にします。手書き本文だけを評価する場合は `"generator": null` のままで構いません。

```json
{
  "skill": "./path/to/target-skill",
  "suite": "./path/to/suite.json",
  "targets": [
    {
      "id": "codex-default",
      "agent": "codex",
      "effort": "high",
      "version": "auto",
      "backend": "codex-native",
      "image": null,
      "network": "none",
      "auth_env": []
    }
  ],
  "budget": {
    "max_task_runs": 4,
    "max_candidates": 1,
    "max_generations": 0,
    "wall_seconds": 3600,
    "task_seconds": 240,
    "max_output_bytes": 2000000
  },
  "gates": {
    "repeats": 3,
    "min_holdout_groups": 5,
    "min_gain": 0.05,
    "max_time_growth": 0.2
  },
  "generator": null
}
```

`budget` と `gates` の既定値は、上のひな形ではなくスキーマ側の既定です（`max_task_runs` 200、`max_candidates` 4、`max_generations` 4、`task_seconds` 180 など）。値を固定したい項目だけを明示します。

課題ファイルは `dataset validate --suite PATH` で確認します。

## 各ステップ

準備（モデルなし）:

```sh
agent-evolution dataset validate --suite PATH
agent-evolution doctor --config PATH
```

1. `start` — 設定を読み、スキルと課題を固定し、実行環境を検査して run を作る。モデルは呼ばない。

```sh
agent-evolution start --config PATH
```

2. `baseline` — 現在版を指定した split で評価する。既定の `--split` は `train`。実モデルを呼び得る。`--split holdout` は使えない。

```sh
agent-evolution baseline --run RUN_ID
agent-evolution baseline --run RUN_ID --split train
```

3. `evolve` — GEPA で候補を探す。`--run RUN_ID` だけを取る。`generator` と 1 以上の `budget.max_generations` が必要。train と validation を使い、holdout は読まない。実モデルを呼び得る。最良候補は run 内で `select` 済みになることがある。手書き本文を入れる場合は、このステップの代わりに次を使う。

```sh
agent-evolution evolve --run RUN_ID
agent-evolution import-candidate --run RUN_ID --body PATH
```

`import-candidate` の `--body` は frontmatter を含めない本文だけにする。

4. `evaluate` — 指定した候補ハッシュを評価する。既定の `--split` は `validation`。実モデルを呼び得る。holdout は指定できない（`finalize` を使う）。同一 run で完了済みの同じ評価は再利用される。

```sh
agent-evolution evaluate --run RUN_ID --candidate-sha SHA
agent-evolution evaluate --run RUN_ID --candidate-sha SHA --split validation
```

5. `select` — holdout に進める候補を固定する。現在版（baseline）は選べない。`finalize` のあとで別の候補へ変更することはできない。

```sh
agent-evolution select --run RUN_ID --candidate-sha SHA
```

6. `finalize` — 候補を固定し、現在版と候補版を holdout で反復比較する。その前に train と validation の評価が完了している必要がある。実モデルを呼び得る。固定後の再探索はできない。

```sh
agent-evolution finalize --run RUN_ID
```

7. `report` — 差分と課題別結果を書き出す。正本は変更しない。出力の `report_sha` を採用時に使う。

```sh
agent-evolution report --run RUN_ID
```

8. `approve` — 適格な候補とレポートを、人が確認した記録として残す。候補は `select` と `finalize` で固定したものと一致し、レポートハッシュも一致している必要がある。正本はまだ変更しない。承認は候補ハッシュ・評価レポート・適用先に結び付く。対話端末では内容確認のあと y/N で進める。非対話では人が付けた `--yes` が必要。エージェントは `--yes` を自分で付けてはいけない。

```sh
agent-evolution approve --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
agent-evolution approve --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA --yes
```

9. `promote` — 承認済みの同じ候補・レポート・適用先に対して、正本の `SKILL.md` を置き換える。正本が評価後に編集されていれば拒否する。名前、説明、補助ファイルは変更できない。`approve` と同じ確認手順を取る。エージェントは `--yes` を自分で付けてはいけない。

```sh
agent-evolution promote --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA
agent-evolution promote --run RUN_ID --candidate-sha SHA --report-sha REPORT_SHA --yes
```

必要なら、返された採用IDを指定して戻す。`approve` や `promote` と同じ確認手順を取る。エージェントは `--yes` を自分で付けてはいけない。

```sh
agent-evolution rollback --promotion PROMOTION_ID
agent-evolution rollback --promotion PROMOTION_ID --yes
```

## cancel の使い方

実行中またはこの先の評価・生成を止めたいときに使います。`--run` が必須です。

```sh
agent-evolution cancel --run RUN_ID
agent-evolution --state PATH cancel --run RUN_ID
```

この操作は `runs/RUN_ID/cancel` に取消フラグを書き込みます。実行中のプロセスはフラグを見て中断し、以後の `baseline`、`evolve`、`evaluate`、`finalize` などは `Run cancelled; create a new run to continue` で拒否します。キャンセルした run は再開できません。続ける場合は、予算を含め新しい run を作ります。壁時計の期限は run 作成時から進み、再実行ではリセットされません。
