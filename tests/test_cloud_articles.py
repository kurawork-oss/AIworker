"""note articles and A8 affiliate articles, end to end, on both stores."""

from __future__ import annotations

import random

import pytest

from aiworker.cloud.articles import AffiliatePipeline, NotePipeline, link_html
from aiworker.cloud.hatena import Posted, PublishError, marker
from aiworker.core.errors import TransientError
from cloud_harness import NOW, harness

WORDS = ["時短", "手順", "失敗", "記録", "準備", "確認", "習慣", "道具", "計画", "改善",
         "比較", "目安", "注意", "結論", "理由", "具体例", "工夫", "仕組み", "続け方", "見直し"]
LINK = "https://px.a8.net/svt/ejp?a8mat=TEST"


def prose(seed: int, chars: int = 2200) -> str:
    """Varied like real writing: two different seeds share little wording."""
    rng = random.Random(seed)
    out = []
    while sum(map(len, out)) < chars:
        out.append("".join(chr(rng.randrange(0x4E00, 0x9FA0)) for _ in range(rng.randint(2, 4)))
                   + rng.choice(WORDS) + rng.choice("はがをにでと") + "。")
        if rng.random() < 0.05:
            out.append("\n\n## " + rng.choice(WORDS) + "\n\n")
    return "".join(out)


class FakeLLM:
    name = "fake-llm"

    def __init__(self, start=0, **override):
        self.start = start          # a different start writes different articles
        self.override = override
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        n = self.start + len(self.requests)
        out = {"title": f"記事タイトル {n}",
               "body": prose(n) + f"\n\n詳しくは https://made-up.example/{n} を参照。",
               "headline": "リード", "outline": ["a", "b"], "price_hint_jpy": "300",
               "categories": ["副業", "AI"], "description": "要約"}
        if request.context.get("channel") == "affiliate_article":
            out["body"] = prose(n) + "\n\n{{LINK}}\n\n" + prose(n + 100, 300) + "\n\n{{LINK}}\n\n{{LINK}}"
        out.update(self.override)
        return out


class FakeHatena:
    def __init__(self, *, lose_response=False, refuse=False):
        self.entries = []          # (title, body, url)
        self.lose_response = lose_response
        self.refuse = refuse

    def post(self, title, body, categories=None, *, draft=False):
        if self.refuse:
            raise PublishError("はてなブログ 400: invalid entry")
        url = f"https://example.hatenablog.com/entry/{len(self.entries) + 1}"
        self.entries.append((title, body, url))
        if self.lose_response:
            self.lose_response = False
            raise TransientError("timeout after the post went through")
        return Posted(url=url, entry_id=str(len(self.entries)))

    def find(self, uid, *, pages=2):
        for n, (_, body, url) in enumerate(self.entries, 1):
            if marker(uid) in body:
                return Posted(url=url, entry_id=str(n))
        return None


@pytest.fixture(params=["memory", "postgres"])
def h(request):
    yield from harness(request.param)


def note(h, llm=None, seed=7):
    return NotePipeline(h.store, llm or FakeLLM(), rng=random.Random(seed), now=NOW).run()


def affiliate(h, llm=None, hatena=None, seed=7, configured=True):
    pub = (lambda: hatena) if configured else (lambda: None)
    return AffiliatePipeline(h.store, llm or FakeLLM(), rng=random.Random(seed), now=NOW,
                             publisher=pub).run()


# --------------------------------------------------------------------------
# note
# --------------------------------------------------------------------------
def test_a_note_draft_waits_for_the_owner(h):
    report = note(h)
    assert report.created == 1 and report.to_review == 1, report.summary()
    [item] = h.all_items()
    assert item["kind"] == "note_article" and item["status"] == "pending_review"
    assert "made-up.example" not in item["body"]          # no invented links
    assert "AIの支援" in item["body"]                       # disclosure added
    assert item["meta"]["price_hint_jpy"] == 300


def test_an_approved_note_draft_is_ready_to_post(h):
    note(h)
    [item] = h.all_items()
    h.approve(item["id"])
    assert h.item(item["id"])["status"] == "awaiting_manual"


def test_a_near_copy_of_an_earlier_article_is_blocked(h):
    same = prose(1)
    note(h, llm=FakeLLM(body=same, title="一本目"))
    h.set_account(daily_limit=5)
    report = note(h, llm=FakeLLM(body=same, title="二本目"), seed=8)
    assert report.blocked == 1
    second = [i for i in h.all_items() if i["title"] == "二本目"][0]
    assert any("似すぎ" in x for x in second["quality_report"]["issues"])


def test_a_thin_article_is_blocked(h):
    report = note(h, llm=FakeLLM(body="短い本文です。"))
    assert report.blocked == 1


def test_a_revised_note_comes_back_to_the_person_even_on_auto(h):
    note(h)
    [item] = h.all_items()
    h.ask_revision(item["id"], "もっと具体例を増やして")
    h.set_account(approval_policy="auto")
    llm = FakeLLM()
    report = note(h, llm=llm, seed=8)
    assert report.revised == 1
    assert "もっと具体例" in llm.requests[0].prompt
    assert h.item(item["id"])["status"] == "pending_review"


# --------------------------------------------------------------------------
# A8 -> Hatena Blog
# --------------------------------------------------------------------------
def test_without_programs_nothing_is_written(h):
    report = affiliate(h, hatena=FakeHatena())
    assert report.created == 0 and "提携案件が未登録" in report.notes


def test_an_affiliate_article_is_labelled_and_carries_only_our_link(h):
    h.add_program(name="テスト証券", link_url=LINK, facts="口座開設は無料。")
    llm = FakeLLM()
    affiliate(h, llm=llm, hatena=FakeHatena())
    [item] = h.all_items()
    body = item["body"]
    assert body.startswith("※本記事はプロモーション（広告）を含みます。")
    assert body.count(LINK) == 2                     # three slots, at most two links
    assert "{{LINK}}" not in body and "made-up.example" not in body
    assert 'rel="nofollow sponsored noopener"' in body
    assert marker(item["meta"]["uid"]) in body
    assert item["status"] == "pending_review"
    assert "口座開設は無料。" in llm.requests[0].prompt
    assert "これ以外の事実は書かない" in llm.requests[0].prompt


def test_a_program_whose_link_is_not_an_a8_link_is_ignored(h):
    h.add_program(name="怪しい案件", link_url="https://evil.example/track")
    report = affiliate(h, hatena=FakeHatena())
    assert report.created == 0


def test_an_approved_article_is_posted_once(h):
    h.add_program(name="案件", link_url=LINK)
    hatena = FakeHatena()
    affiliate(h, hatena=hatena)
    [item] = h.all_items()
    h.approve(item["id"])
    assert h.item(item["id"])["status"] == "approved"   # api: the pipeline posts it

    report = affiliate(h, hatena=hatena, seed=8)
    assert report.published == 1
    posted = h.item(item["id"])
    assert posted["status"] == "published"
    assert posted["external_url"].startswith("https://example.hatenablog.com/entry/")
    affiliate(h, hatena=hatena, seed=9)
    assert len(hatena.entries) == 1


def test_a_lost_response_does_not_post_twice(h):
    """The post went through but the reply never came: the next run finds the
    entry by its marker instead of posting it again (受入条件5)."""
    h.add_program(name="案件", link_url=LINK)
    hatena = FakeHatena(lose_response=True)
    affiliate(h, hatena=hatena)
    [item] = h.all_items()
    h.approve(item["id"])
    first = affiliate(h, hatena=hatena, seed=8)
    assert first.published == 0 and h.item(item["id"])["status"] == "scheduled"
    second = affiliate(h, hatena=hatena, seed=9)
    assert second.published == 1
    assert len(hatena.entries) == 1
    assert h.item(item["id"])["external_url"] == hatena.entries[0][2]


def test_the_daily_limit_caps_posting(h):
    h.add_program(name="案件", link_url=LINK)
    hatena = FakeHatena()
    affiliate(h, hatena=hatena)            # creates the account, and article 1
    h.set_account(daily_limit=2)
    affiliate(h, llm=FakeLLM(start=50), hatena=hatena, seed=11)  # article 2
    assert len(h.all_items()) == 2
    for i in h.all_items():
        h.approve(i["id"])
    h.set_account(daily_limit=1)
    report = affiliate(h, hatena=hatena, seed=8)
    assert report.published == 1 and "本日の投稿上限" in report.notes
    assert len(hatena.entries) == 1


def test_an_unconnected_blog_is_reported_not_failed(h):
    h.add_program(name="案件", link_url=LINK)
    affiliate(h, hatena=FakeHatena())
    [item] = h.all_items()
    h.approve(item["id"])
    report = affiliate(h, configured=False, seed=8)
    assert "はてなブログ未接続" in report.notes and not report.errors
    assert h.item(item["id"])["status"] == "approved"


def test_an_entry_the_blog_refuses_is_marked_failed(h):
    h.add_program(name="案件", link_url=LINK)
    affiliate(h, hatena=FakeHatena())
    [item] = h.all_items()
    h.approve(item["id"])
    report = affiliate(h, hatena=FakeHatena(refuse=True), seed=8)
    assert report.failed == 1 and h.item(item["id"])["status"] == "failed"


def test_the_link_markup_cannot_be_broken_by_a_program_name():
    html = link_html({"link_url": LINK, "name": '<script>x</script>"', "pixel_url": ""})
    assert "<script>" not in html and "&lt;script&gt;" in html
