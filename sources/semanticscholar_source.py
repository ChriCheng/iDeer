"""Semantic Scholar source — free alternative to Web of Science.

Searches across all academic venues (not just arXiv) via the Semantic Scholar
API, which covers 200M+ papers.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta

from sources.base import BaseSource
from core.config import LLMConfig, CommonConfig, PROJECT_ROOT
from core.cache_utils import safe_read_json, atomic_write_json
from fetchers.semanticscholar_fetcher import fetch_papers_for_queries
from email_utils.base_template import get_stars
from email_utils.semanticscholar_template import get_paper_block_html


class SemanticScholarSource(BaseSource):
    name = "semanticscholar"
    default_title = "Semantic Scholar Daily"

    def __init__(
        self, source_args: dict, llm_config: LLMConfig, common_config: CommonConfig
    ):
        super().__init__(source_args, llm_config, common_config)
        self.queries = source_args.get("queries", [])
        self.max_results = source_args.get("max_results", 60)
        self.max_papers = source_args.get("max_papers", 30)
        self.year_filter = source_args.get("year", "")
        self.fields_of_study = source_args.get("fields_of_study", [])
        self.api_key = source_args.get("api_key", "")
        self.seen_window_days = source_args.get("seen_window_days", 180)
        self.seen_path = os.path.join(
            str(PROJECT_ROOT),
            common_config.state_dir,
            "seen",
            "semanticscholar_seen.json",
        )
        self.seen_papers = self._load_seen_papers()
        # If no explicit queries, derive from the interest description
        if not self.queries:
            self.queries = self._derive_queries_from_description()

        import hashlib

        query_sig = hashlib.sha256("|".join(sorted(self.queries)).encode()).hexdigest()[
            :10
        ]
        cache_key = f"papers_{query_sig}_{self.max_results}"
        if self.year_filter:
            cache_key += f"_{self.year_filter}"
        cached = self._load_fetch_cache(cache_key)
        if cached is not None:
            self.raw_papers = cached
        else:
            self.raw_papers = fetch_papers_for_queries(
                self.queries,
                max_results_per_query=self.max_results,
                year=self.year_filter or None,
                fields_of_study=self.fields_of_study or None,
                api_key=self.api_key,
            )
            if self.raw_papers:
                self._save_fetch_cache(cache_key, self.raw_papers)

    def _paper_seen_key(self, item: dict) -> str:
        """
        Generate a stable key for a Semantic Scholar paper.
        Prefer paper_id; fall back to DOI/arXiv/url/title hash.
        """
        for key in ("paper_id", "doi", "arxiv_id", "url"):
            value = str(item.get(key, "")).strip().lower()
            if value:
                return value

        title = str(item.get("title", "")).strip().lower()
        authors = str(item.get("authors", "")).strip().lower()
        return hashlib.sha256(f"{title}|{authors}".encode("utf-8")).hexdigest()

    def _load_seen_papers(self) -> dict:
        """
        Load permanently seen/recommended Semantic Scholar papers.
        Once a paper is recorded here, it will not be recommended again.
        """
        data = safe_read_json(self.seen_path) or {}
        return data if isinstance(data, dict) else {}

    def _mark_seen(self, recommendations: list[dict]) -> None:
        """
        Mark final recommended papers as seen.
        Only final recommendations are recorded, not all fetched candidates.
        """
        if not recommendations:
            return

        data = dict(self.seen_papers)

        for item in recommendations:
            key = self._paper_seen_key(item)
            old = data.get(key, {})
            data[key] = {
                "paper_id": item.get("paper_id", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "year": item.get("year", ""),
                "venue": item.get("venue", ""),
                "first_seen": old.get("first_seen", self.run_date),
                "last_seen": self.run_date,
            }

        atomic_write_json(self.seen_path, data)
        self.seen_papers = data

    def _derive_queries_from_description(self) -> list[str]:
        """Extract up to 3 search queries from the user description."""
        desc = self.description.strip()
        if not desc:
            return ["artificial intelligence"]

        lines = [
            line.strip().lstrip("0123456789.-) ")
            for line in desc.split("\n")
            if line.strip()
        ]
        queries = []
        for line in lines:
            # Skip negative preference lines
            lower = line.lower()
            if any(
                neg in lower
                for neg in ("not interested", "不感兴趣", "don't", "exclude")
            ):
                continue
            # Clean up common prefixes
            for prefix in ("i'm interested in", "interested in", "关注", "研究"):
                if lower.startswith(prefix):
                    line = line[len(prefix) :].strip(" :：-")
            if line and len(line) > 2:
                queries.append(line[:120])
            if len(queries) >= 3:
                break

        return queries or ["artificial intelligence"]

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser):
        parser.add_argument(
            "--ss_queries",
            nargs="*",
            default=[],
            help="[SemanticScholar] Explicit search queries (derived from description if empty)",
        )
        parser.add_argument(
            "--ss_max_results",
            type=int,
            default=60,
            help="[SemanticScholar] Max results to fetch per query",
        )
        parser.add_argument(
            "--ss_max_papers",
            type=int,
            default=30,
            help="[SemanticScholar] Max papers to recommend after scoring",
        )
        parser.add_argument(
            "--ss_year",
            type=str,
            default="",
            help="[SemanticScholar] Year filter, e.g. '2024-' for papers from 2024 onward",
        )
        parser.add_argument(
            "--ss_fields_of_study",
            nargs="*",
            default=[],
            help="[SemanticScholar] Fields of study filter (e.g. 'Computer Science' 'Medicine')",
        )
        parser.add_argument(
            "--ss_api_key",
            type=str,
            default="",
            help="[SemanticScholar] Optional API key for higher rate limits",
        )

    @staticmethod
    def extract_args(args) -> dict:
        return {
            "queries": args.ss_queries,
            "max_results": args.ss_max_results,
            "max_papers": args.ss_max_papers,
            "year": args.ss_year,
            "fields_of_study": args.ss_fields_of_study,
            "api_key": args.ss_api_key,
        }

    def get_max_items(self) -> int:
        return self.max_papers

    def fetch_items(self) -> list[dict]:
        filtered = []
        skipped = 0

        for paper in self.raw_papers:
            key = self._paper_seen_key(paper)
            if key in self.seen_papers:
                skipped += 1
                continue
            filtered.append(paper)

        print(
            f"[{self.name}] {len(self.raw_papers)} total papers after query dedup; "
            f"{len(filtered)} new papers after seen-filter; "
            f"{skipped} skipped as already recommended"
        )

        return filtered

    def get_recommendations(self) -> list[dict]:
        recommendations = super().get_recommendations()
        self._mark_seen(recommendations)
        return recommendations

    def get_item_cache_id(self, item: dict) -> str:
        pid = item.get("paper_id", "unknown")
        return "ss_" + pid.replace("/", "_").replace(".", "_")[:80]

    def build_eval_prompt(self, item: dict) -> str:
        abstract = item.get("abstract", "") or "No abstract available."
        if len(abstract) > 600:
            abstract = abstract[:597] + "..."

        return f"""你是一个有帮助的学术研究助手，可以帮助我构建每日论文推荐系统。
以下是我最近研究领域的描述：
{self.description}

以下是来自 Semantic Scholar 的论文，我为你提供了标题和摘要：
标题: {item['title']}
作者: {item.get('authors', '')}
年份: {item.get('year', '')}
发表期刊/会议: {item.get('venue', '')}
引用数: {item.get('citation_count', 0)}
摘要: {abstract}

1. 总结这篇论文的主要内容。
2. 请评估这篇论文与我研究领域的相关性，并给出 0-10 的评分。其中 0 表示完全不相关，10 表示高度相关。

请按以下 JSON 格式给出你的回答：
{{
    "summary": "一段纯文本的中文总结（不要嵌套JSON/dict，直接写一段话）",
    "relevance": <你的评分>
}}
重要：summary 必须是一段纯文本字符串，不要返回嵌套的 JSON 对象或字典。
使用中文回答。
直接返回上述 JSON 格式，无需任何额外解释。"""

    def parse_eval_response(self, item: dict, response: str) -> dict:
        response = response.strip("```").strip("json")
        data = json.loads(response)
        return {
            "title": item["title"],
            "paper_id": item.get("paper_id", ""),
            "abstract": item.get("abstract", ""),
            "summary": self._ensure_str(data["summary"]),
            "score": float(data["relevance"]),
            "url": item.get("url", ""),
            "authors": item.get("authors", ""),
            "venue": item.get("venue", ""),
            "year": str(item.get("year", "")),
            "citation_count": item.get("citation_count", 0),
        }

    def render_item_html(self, item: dict) -> str:
        rate = get_stars(item.get("score", 0))
        return get_paper_block_html(
            item["title"],
            rate,
            item.get("authors", ""),
            item.get("venue", ""),
            str(item.get("year", "")),
            item.get("citation_count", 0),
            item["summary"],
            item.get("url", ""),
        )

    def get_theme_color(self) -> str:
        return "108,62,193"  # purple

    def get_section_header(self) -> str:
        query_hint = ", ".join(self.queries[:3])
        return f'<div class="section-title" style="border-bottom-color: #6c3ec1;">🔬 Semantic Scholar ({query_hint})</div>'

    def build_summary_overview(self, recommendations: list[dict]) -> str:
        lines = []
        for i, r in enumerate(recommendations):
            venue = r.get("venue", "")
            venue_str = f" [{venue}]" if venue else ""
            lines.append(
                f"{i + 1}. {r['title']}{venue_str} "
                f"(citations={r.get('citation_count', 0)}) "
                f"- Score: {r.get('score', 0)} - {r['summary']}"
            )
        return "\n".join(lines)

    def get_summary_prompt_template(self) -> str:
        return """
            请直接输出一段 HTML 片段，严格遵循以下结构，不要包含 JSON、Markdown 或多余说明：
            <div class="summary-wrapper">
              <div class="summary-section">
                <h2>今日学术动态</h2>
                <p>分析今天论文体现的研究趋势，解释其与我研究兴趣的联系...</p>
              </div>
              <div class="summary-section">
                <h2>重点推荐</h2>
                <ol class="summary-list">
                  <li class="summary-item">
                    <div class="summary-item__header"><span class="summary-item__title">论文标题</span><span class="summary-pill">相关性</span></div>
                    <p><strong>推荐理由：</strong>...</p>
                    <p><strong>关键贡献：</strong>...</p>
                  </li>
                </ol>
              </div>
              <div class="summary-section">
                <h2>补充观察</h2>
                <p>值得持续关注的方向或潜在研究机会...</p>
              </div>
            </div>

            这些论文来自 Semantic Scholar（覆盖 arXiv 之外的会议、期刊等），
            请特别指出那些发表在高影响力 venue 的论文。
            用中文撰写内容，重点推荐部分建议返回 3-5 篇论文。
        """
