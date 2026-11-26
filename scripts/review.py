#!/usr/bin/env python3
"""PR の差分を Claude にレビューさせ、PR に 1 件のレビューコメントとして投稿する。

標準ライブラリだけで動く。GitHub Actions では review.yml から呼ばれる。
手元では --dry-run で、API を呼ばずにプロンプトだけを確かめられる。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MARKER = "<!-- pr-review -->"
ROOT = Path(__file__).resolve().parent.parent
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# レビューに渡さないファイル。トークンの節約と、読んでも意味のないものを外すため
EXCLUDE_PATTERNS = [
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "composer.lock",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "dist/*",
    "build/*",
    "vendor/*",
    "node_modules/*",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.zip",
]
REPO_DOCS = ["REVIEW.md", "AGENTS.md", "CLAUDE.md"]
DOC_LIMIT = 20_000
COMMENT_LIMIT = 4_000


# ---------- HTTP ----------


def request(method: str, url: str, headers: dict, body: dict | None = None, retries: int = 3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                raw = res.read().decode()
                ctype = res.headers.get("Content-Type", "")
                return json.loads(raw) if "json" in ctype else raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            # 混雑と一時的な障害だけ待って繰り返す
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                wait = int(e.headers.get("retry-after") or 0) or 10 * (attempt + 1)
                print(f"HTTP {e.code}。{wait}秒待って再試行する", file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit(f"{method} {url} が失敗した（HTTP {e.code}）：{detail}")
    raise SystemExit("到達しない")


class GitHub:
    def __init__(self, token: str, repo: str):
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review",
        }

    def get(self, path: str, accept: str | None = None):
        headers = dict(self.headers)
        if accept:
            headers["Accept"] = accept
        return request("GET", f"{self.base}/repos/{self.repo}{path}", headers)

    def get_all(self, path: str) -> list:
        items, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            chunk = self.get(f"{path}{sep}per_page=100&page={page}")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
            page += 1

    def post(self, path: str, body: dict):
        return request("POST", f"{self.base}/repos/{self.repo}{path}", self.headers, body, retries=1)


# ---------- 入力を集める ----------


def split_diff(diff: str) -> list[tuple[str, str]]:
    """unified diff をファイルごとに分け、(パス, 差分) の並びにする。"""
    files, current, path = [], [], None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                files.append((path or "", "".join(current)))
            current = [line]
            # "diff --git a/x b/x" の b/ 側を取る
            path = line.rstrip("\n").split(" b/", 1)[-1]
        else:
            current.append(line)
    if current:
        files.append((path or "", "".join(current)))
    return files


def excluded(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    for p in EXCLUDE_PATTERNS:
        if p.endswith("/*"):
            # "dist/*" は、どの階層の dist/ の下でも外す
            d = p[:-1]
            if path.startswith(d) or f"/{d}" in path:
                return True
        elif fnmatch.fnmatch(name, p):
            return True
    return False


def build_diff(diff: str, max_chars: int) -> tuple[str, list[str]]:
    """除外と切り詰めをした差分と、レビューしていない部分の一覧を返す。"""
    kept, skipped, total = [], [], 0
    for path, chunk in split_diff(diff):
        if excluded(path):
            skipped.append(f"{path}（除外の対象）")
            continue
        if total + len(chunk) > max_chars:
            room = max_chars - total
            if room > 2_000:
                kept.append(chunk[:room] + "\n[... 長すぎるため、ここから先を切った ...]\n")
                skipped.append(f"{path}（途中から）")
                total = max_chars
            else:
                skipped.append(f"{path}（差分の上限を超えたため）")
            continue
        kept.append(chunk)
        total += len(chunk)
    return "".join(kept), skipped


def read_repo_docs(target: Path) -> str:
    parts = []
    has_review = False
    for name in REPO_DOCS:
        for candidate in (target / name, target / ".github" / name):
            if candidate.is_file():
                text = candidate.read_text(errors="replace")[:DOC_LIMIT]
                parts.append(f'<repo_file path="{candidate.relative_to(target)}">\n{text}\n</repo_file>')
                has_review = has_review or name == "REVIEW.md"
                break
    if not has_review:
        default = (ROOT / "prompts" / "default-review.md").read_text()
        parts.insert(0, f'<default_review_rules>\n{default}\n</default_review_rules>')
    return "\n\n".join(parts)


def clip(text: str | None, limit: int = COMMENT_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n[... 以下省略 ...]"


def build_user_message(pr: dict, diff: str, skipped: list[str], previous: list[dict], discussion: list[dict]) -> str:
    lines = [
        "<pr>",
        f"題名：{pr.get('title', '')}",
        f"ベースのブランチ：{pr.get('base', {}).get('ref', '')}",
        "説明：",
        clip(pr.get("body")),
        "</pr>",
        "",
        f"<previous_reviews count=\"{len(previous)}\">",
    ]
    for i, r in enumerate(previous, 1):
        lines += [f"--- {i}回目（{r.get('submitted_at', '')}） ---", clip(r.get("body", "").replace(MARKER, ""))]
    lines += ["</previous_reviews>", "", "<discussion>"]
    for c in discussion:
        lines += [f"--- {c.get('user', {}).get('login', '?')}（{c.get('created_at', '')}） ---", clip(c.get("body"))]
    lines += ["</discussion>", "", "<diff>", diff or "（レビューできる差分がない）", "</diff>"]
    if skipped:
        lines += ["", "<not_reviewed>"] + [f"- {s}" for s in skipped] + ["</not_reviewed>"]
    lines += ["", f"このPRのレビューは、今回で{len(previous) + 1}回目である。出力の形に沿って、レビューコメントの本文だけを書く。"]
    return "\n".join(lines)


# ---------- Claude ----------


def call_claude(api_key: str, model: str, max_tokens: int, system: list[dict], user: str) -> dict:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    return request("POST", ANTHROPIC_URL, headers, body)


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


# ---------- 本体 ----------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default=os.environ.get("TARGET_DIR", "."), help="対象のリポジトリを取り出したディレクトリ")
    ap.add_argument("--model", default=os.environ.get("MODEL") or "claude-sonnet-5-5")
    ap.add_argument("--max-diff-chars", type=int, default=int(os.environ.get("MAX_DIFF_CHARS") or 120_000))
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("MAX_TOKENS") or 16_000))
    ap.add_argument("--dry-run", action="store_true", help="API を呼ばず、組み立てたプロンプトを表示する")
    ap.add_argument("--diff-file", help="--dry-run で使う差分のファイル。GitHub から取らない")
    args = ap.parse_args()

    target = Path(args.target)
    system_text = (ROOT / "prompts" / "system.md").read_text()
    repo_docs = read_repo_docs(target)
    # 固定の指示とリポジトリの文書は、同じ PR の再レビューで変わらないのでキャッシュする
    system = [
        {"type": "text", "text": system_text},
        {"type": "text", "text": f"<repository_docs>\n{repo_docs}\n</repository_docs>", "cache_control": {"type": "ephemeral"}},
    ]

    if args.diff_file:
        pr = {"title": "（dry-run）", "body": "", "base": {"ref": "main"}}
        raw_diff = Path(args.diff_file).read_text()
        previous, discussion, gh, number = [], [], None, None
    else:
        token, repo, number = os.environ["GITHUB_TOKEN"], os.environ["REPO"], os.environ["PR_NUMBER"]
        gh = GitHub(token, repo)
        pr = gh.get(f"/pulls/{number}")
        raw_diff = gh.get(f"/pulls/{number}", accept="application/vnd.github.diff")
        reviews = gh.get_all(f"/pulls/{number}/reviews")
        previous = [r for r in reviews if MARKER in (r.get("body") or "")]
        discussion = [c for c in gh.get_all(f"/issues/{number}/comments") if MARKER not in (c.get("body") or "")]
        discussion += [r for r in reviews if MARKER not in (r.get("body") or "") and r.get("body")]

    diff, skipped = build_diff(raw_diff, args.max_diff_chars)
    user = build_user_message(pr, diff, skipped, previous, discussion)

    if args.dry_run:
        print("===== system =====")
        print(system_text)
        print(system[1]["text"])
        print("===== user =====")
        print(user)
        print(f"\n（文字数：system {len(system_text) + len(system[1]['text'])}、user {len(user)}）", file=sys.stderr)
        return 0

    if not diff.strip():
        print("レビューできる差分がないため、投稿しない")
        write_summary("レビューできる差分がないため、投稿しなかった。")
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY が空である。対象のリポジトリの Settings > Secrets and variables > Actions の"
            "「Repository secrets」に登録し、呼び出し側の secrets: で渡す"
        )
    result = call_claude(api_key, args.model, args.max_tokens, system, user)
    text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text").strip()
    stop = result.get("stop_reason")
    if not text:
        # 思考（thinking）も max_tokens を使うため、大きな差分では本文を書く前に上限に達することがある
        hint = "max-tokens を上げる" if stop == "max_tokens" else "応答を確かめる"
        raise SystemExit(
            f"Claude の応答に本文がなかった（stop_reason={stop}、usage={json.dumps(result.get('usage', {}))}）。{hint}"
        )
    if stop == "max_tokens":
        text += "\n\n> 出力の上限に達したため、レビューが途中で切れている。"

    usage = result.get("usage", {})
    footer = (
        f"\n\n---\n<sub>pr-review · {result.get('model', args.model)} · "
        f"{len(previous) + 1}回目 · 入力 {usage.get('input_tokens', 0)} / "
        f"キャッシュ読み込み {usage.get('cache_read_input_tokens', 0)} / 出力 {usage.get('output_tokens', 0)} トークン</sub>"
    )
    body = f"{MARKER}\n## Claudeのレビュー\n\n{text}{footer}"
    gh.post(f"/pulls/{number}/reviews", {"event": "COMMENT", "body": body, "commit_id": pr["head"]["sha"]})

    write_summary(f"## pr-review\n\n- モデル：{args.model}\n- 使用量：`{json.dumps(usage)}`\n\n{text}")
    print("レビューを投稿した")
    return 0


if __name__ == "__main__":
    sys.exit(main())
