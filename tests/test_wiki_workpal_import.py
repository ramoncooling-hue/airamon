from pathlib import Path

from scripts.import_wiki_workpal import _chapter_values, _wiki_links, is_graph_page, parse_page


def test_parse_page_reads_wiki_contract_and_links(tmp_path: Path):
    root = tmp_path / "wiki"
    page = root / "AgentPlatforms" / "AgentGuide.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
title: Agent Guide
type: guide
tags: [Agent, Workflow]
source: raw/提炼md/source.md
knowledge_status: pending_review
---
## 摘要
用一句话说明。

## 详细内容
正文 [[Ontology]] 和 [[AgentPlatforms/Other]]。
""",
        encoding="utf-8",
    )
    parsed = parse_page(page, root)
    assert parsed.title == "Agent Guide"
    assert parsed.page_type == "guide"
    assert parsed.tags == ("Agent", "Workflow")
    assert parsed.source_ref == "raw/提炼md/source.md"
    assert parsed.chapter == "AgentPlatforms"
    assert parsed.chapter_key == "wiki-agentplatforms"
    assert parsed.links == ("Ontology", "AgentPlatforms/Other")
    assert parsed.gist == "用一句话说明。"


def test_links_deduplicate_and_strip_aliases():
    assert _wiki_links("[[A|别名]] [[A]] [[B.md#section]] [[B.md]]") == ("A", "B")


def test_index_and_governance_pages_are_excluded(tmp_path: Path):
    root = tmp_path / "wiki"
    assert not is_graph_page(root / "INDEX.md", root)
    assert not is_graph_page(root / "AgentPlatforms" / "AgentPlatforms-INDEX.md", root)
    assert not is_graph_page(root / "_governance" / "rules.md", root)
    assert is_graph_page(root / "AgentPlatforms" / "AgentGuide.md", root)


def test_root_chapter_is_stable():
    assert _chapter_values("Page.md") == ("Wiki", "wiki-wiki")
