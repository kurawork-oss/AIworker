"""Per-channel generation specs: system prompt, user prompt, response schema.

Kept in one table rather than one module per channel, because the channels
differ only in their prompt and their output keys -- the surrounding machinery
(diversity, guardrails, storage) is identical. Adding a channel is adding a
dict entry.

Every system prompt carries the same standing constraints. They are repeated
per channel on purpose: a guardrail the model never sees is a guardrail that
only ever fires after the tokens are paid for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import Channel

COMMON_RULES = """\
守るべき制約:
- 実在の人物・団体・商標・既存作品を模倣しない、名指ししない。
- 「絶対に稼げる」「必ず治る」のような断定的な効果・収益保証を書かない。
- 出典のない統計や数字を作らない。断定できないことは断定しない。
- テンプレートの使い回しをしない。指定された切り口(angle)と語り口(tone)を必ず反映する。
- 誇張した煽り表現・過度なハッシュタグの羅列をしない。
- 出力はJSONオブジェクト1つのみ。前置き・後書き・コードフェンスを付けない。
"""


@dataclass
class ChannelSpec:
    channel: str
    system: str
    prompt_template: str
    schema: dict[str, str]
    meta_keys: tuple[str, ...] = ()
    title_key: str = "title"
    body_key: str = "body"
    max_tokens: int = 2000
    temperature: float = 1.0
    notes: str = ""
    default_platforms: tuple[str, ...] = field(default_factory=tuple)


SPECS: dict[str, ChannelSpec] = {
    Channel.SOCIAL_POST.value: ChannelSpec(
        channel=Channel.SOCIAL_POST.value,
        system="あなたは日本語で発信する実務者です。読者は副業・業務効率化に関心のある社会人。\n" + COMMON_RULES,
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "X / Threads 向けの単発投稿を1本書いてください。\n"
            "- 140〜260文字。冒頭1行で結論または問いを出す。\n"
            "- ハッシュタグは付けても2個まで。\n"
            "- 過去の投稿と語り出しが似ないようにする。"
        ),
        schema={"title": "string (社内管理用の短い見出し)", "body": "string (投稿本文)"},
        default_platforms=("x", "threads"),
    ),
    Channel.SOCIAL_THREAD.value: ChannelSpec(
        channel=Channel.SOCIAL_THREAD.value,
        system="あなたは日本語で発信する実務者です。\n" + COMMON_RULES,
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "4〜6投稿のスレッドを書いてください。\n"
            "- 1投稿目で全体の結論を出す。\n"
            "- 各投稿は240文字以内、単体でも意味が通るようにする。\n"
            "- 最後の投稿で次の行動を1つだけ提示する。"
        ),
        schema={
            "title": "string",
            "parts": "string[] (各投稿の本文)",
            "body": "string (partsを'---'で連結したもの)",
        },
        meta_keys=("parts",),
        default_platforms=("x", "threads"),
    ),
    Channel.NOTE_ARTICLE.value: ChannelSpec(
        channel=Channel.NOTE_ARTICLE.value,
        system="あなたは日本語で有料級の実践記事を書く編集者です。\n" + COMMON_RULES,
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "note 公開用の記事下書きを書いてください。\n"
            "- 2000〜4000文字。見出し(##)を4〜6個。\n"
            "- 冒頭に「この記事で分かること」を3点。\n"
            "- 手順は再現可能な粒度で書く。抽象論だけで終わらせない。\n"
            "- 末尾にAI支援で作成した旨と、人間が確認している旨を1行入れる。\n"
            "- アフィリエイトリンクを載せる場合は本文中に「PR」表記を入れる。"
        ),
        schema={
            "title": "string",
            "headline": "string (リード文1〜2行)",
            "outline": "string[] (見出しの一覧)",
            "body": "string (本文markdown)",
            "price_hint_jpy": "number (0なら無料公開の想定)",
        },
        meta_keys=("headline", "outline", "price_hint_jpy"),
        max_tokens=6000,
        default_platforms=("note",),
    ),
    Channel.SHORTS_SCRIPT.value: ChannelSpec(
        channel=Channel.SHORTS_SCRIPT.value,
        system="あなたは日本語のショート動画構成作家です。\n" + COMMON_RULES,
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "YouTube Shorts (30〜50秒) の台本一式を書いてください。\n"
            "- 台本は [秒数] 形式のタイムコード付き。\n"
            "- 最初の2秒で離脱を止める一言を置く。\n"
            "- video_title は32文字以内、煽りすぎない。\n"
            "- description には AI生成素材を含む旨の一文と #shorts を入れる。"
        ),
        schema={
            "title": "string (管理用)",
            "video_title": "string (動画タイトル)",
            "body": "string (台本本文)",
            "description": "string (概要欄)",
            "tags": "string[] (5個以内)",
        },
        meta_keys=("video_title", "description", "tags"),
        max_tokens=3000,
        default_platforms=("youtube",),
    ),
    Channel.STOCK_ASSET.value: ChannelSpec(
        channel=Channel.STOCK_ASSET.value,
        system=(
            "あなたはストックフォト/ストック動画向けの素材企画担当です。"
            "審査に通ることと権利上の安全を最優先します。\n" + COMMON_RULES
        ),
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "ストックサイト投稿用の生成プロンプトとメタデータを作ってください。\n"
            "- prompt には実在人物・ブランド・ロゴ・文字を含めない。\n"
            "- negative_prompt に text, watermark, logo, brand, real person を必ず含める。\n"
            "- keywords は英語で10〜20個、重複と過剰な一般語を避ける。\n"
            "- description は英語で1〜2文。"
        ),
        schema={
            "title": "string (英語の素材タイトル)",
            "body": "string (日本語の素材メモ)",
            "prompt": "string (英語の生成プロンプト)",
            "negative_prompt": "string",
            "keywords": "string[]",
            "description": "string (英語)",
            "ai_generated": "boolean (常にtrue)",
        },
        meta_keys=("prompt", "negative_prompt", "keywords", "description", "ai_generated"),
        default_platforms=("adobe_stock",),
    ),
    Channel.DIGITAL_PRODUCT.value: ChannelSpec(
        channel=Channel.DIGITAL_PRODUCT.value,
        system="あなたはデジタル商品(プロンプト集・テンプレート)の企画者です。\n" + COMMON_RULES,
        prompt_template=(
            "テーマ「{theme}」について、切り口「{angle}」・語り口「{tone}」で "
            "デジタル商品の企画案と販売ページ下書きを書いてください。\n"
            "- 収録内容を箇条書きで具体的に。\n"
            "- 想定価格と、その価格である理由を書く。\n"
            "- 効果保証の表現を使わない。"
        ),
        schema={
            "title": "string (商品名)",
            "body": "string (販売ページ下書き)",
            "contents": "string[] (収録内容)",
            "price_jpy": "number",
        },
        meta_keys=("contents", "price_jpy"),
        max_tokens=4000,
        default_platforms=("note",),
    ),
}


def spec_for(channel: str) -> ChannelSpec:
    try:
        return SPECS[channel]
    except KeyError:
        raise ValueError(
            f"unknown channel '{channel}'. known: {sorted(SPECS)}"
        ) from None
