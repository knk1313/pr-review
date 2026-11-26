# pr-review

PRを開くたびに、差分をClaudeにレビューさせ、PRに1件のレビューコメントとして投稿するGitHub Actionsのワークフローである。各リポジトリからは、数行のワークフローで呼び出す。

## 特徴

- 公式のClaude Code GitHub Actionではなく、Messages APIを直接呼ぶ。Claude MaxやTeamのプランに付く毎月のAPIクレジットを、レビューに充てられる
- 対象のリポジトリの`REVIEW.md`、`AGENTS.md`、`CLAUDE.md`を、何を問題とみなすかの基準として読む。`REVIEW.md`は、[repo-kit](https://github.com/knk1313/repo-kit)の`setup`で置ける。`REVIEW.md`がないときは、[既定の基準](prompts/default-review.md)を使う
- 指摘は「重要」「細かい」「既存」に分け、`ファイル:行`を付けて、10件までにまとめる
- 前のレビューと、PRの会話にある人間のコメントも渡す。作者が理由を添えて閉じた指摘は繰り返さない
- 依存のないPythonのスクリプト1本で動く

## 使い方

### 1. APIキーを用意する

プランのAPIクレジットを使うなら、claude.aiの「設定 > 請求」でクレジットを申請し、紐づけたConsoleの組織でAPIキーを発行する。

### 2. シークレットを登録する

対象のリポジトリの「Settings > Secrets and variables > Actions」の「Repository secrets」に、`ANTHROPIC_API_KEY`という名前でAPIキーを登録する。「Environment secrets」は、ジョブに`environment:`を指定したときだけ渡るため、このワークフローでは使えない。

### 3. ワークフローを置く

[examples/caller.yml](examples/caller.yml)を、対象のリポジトリの`.github/workflows/pr-review.yml`に写し、`<SHA>`をpr-reviewのコミットのSHAに置き換える。

```yaml
jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
    uses: knk1313/pr-review/.github/workflows/review.yml@<SHA>
    with:
      pr-review-ref: <SHA>
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`uses:`のSHAで固定されるのはワークフローの定義だけである。ワークフローは、スクリプトを`pr-review-ref`の版で取り出す。両方に同じSHAを書かないと、スクリプトは`main`に追従する。

### 4. 確かめる

PRを開くと、数十秒でレビューが付く。初めて使うときは、Consoleの「Cost」で、クレジットから引かれていることを確かめる。

## 設定

`with:`で次の値を変えられる。

| 名前 | 既定 | 内容 |
| --- | --- | --- |
| `model` | `claude-sonnet-5-5` | レビューに使うモデル |
| `max-diff-chars` | `120000` | レビューに渡す差分の上限（文字数）。超えた分はレビューせず、その旨をコメントに書く |
| `max-tokens` | `4000` | レビューの出力の上限（トークン） |
| `skip-label` | `skip-review` | このラベルが付いたPRはレビューしない |
| `pr-review-ref` | `main` | 取り出すpr-reviewのスクリプトの版。`uses:`で固定したSHAと同じ値を渡す |

## レビューしないもの

- 下書きのPR。下書きを解除したときにレビューする
- フォークからのPRとDependabotのPR。シークレットが渡らないため
- `skip-label`のラベルが付いたPR
- ロックファイル、生成物、画像などのファイル。一覧は[scripts/review.py](scripts/review.py)の`EXCLUDE_PATTERNS`にある

## 安全のための決まり

- `REVIEW.md`などの基準は、PRのブランチではなく、ベースのブランチの版を読む。PRで基準を緩めても、そのPRのレビューには効かない
- PRの題名、説明、差分、コメントは、レビューする対象のデータとして渡す。そこに書かれた指示には従わないよう、モデルに伝えている
- ワークフローの権限は、コードを読むこととPRにレビューを書くことだけにする
- 呼び出し側は、ワークフローとスクリプトをコミットのSHAで固定する。pr-reviewの`main`が変わっても、APIキーと書き込みの権限を持つ処理が知らないうちに変わらない

## 手元で試す

APIを呼ばずに、組み立てたプロンプトを表示できる。

```bash
git diff main > /tmp/pr.diff
python3 scripts/review.py --dry-run --diff-file /tmp/pr.diff --target .
```

テストは次のとおり。

```bash
python3 -m unittest discover -s tests
```

## 構成

```text
.github/workflows/review.yml  再利用のワークフロー
scripts/review.py             差分を集め、APIを呼び、レビューを投稿する
prompts/system.md             レビュアーへの指示（見る範囲、重要度、出力の形）
prompts/default-review.md     REVIEW.mdがないときの基準
examples/caller.yml           対象のリポジトリに置くワークフローの例
```
