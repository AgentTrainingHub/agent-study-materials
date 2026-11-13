"""固定 BM25 原语，记录词项贡献；来源过滤由本地工程负责。"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_NUMBER = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class RankConfig:
    name: str = "bm25"
    k1: float = 1.5
    b: float = 0.75
    idf: str = "lucene_bm25_log1p"


@dataclass(frozen=True)
class TermContribution:
    term: str
    tf: int
    idf: float
    score: float


@dataclass(frozen=True)
class ScoredDocument:
    doc_id: str
    score: float
    terms: tuple[TermContribution, ...]


def algorithm_record(config: RankConfig | None = None) -> dict[str, str | float]:
    return asdict(config or RankConfig())


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    covered: list[tuple[int, int]] = []
    for match in _IDENT.finditer(text):
        tokens.append(match.group(0).lower())
        covered.append(match.span())
    for match in _NUMBER.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        tokens.append(match.group(0))
    for match in _CJK.finditer(text):
        chars = match.group(0)
        if len(chars) == 1:
            tokens.append(chars)
        else:
            tokens.extend(chars[index : index + 2] for index in range(len(chars) - 1))
    return tokens


def _idf(documents: int, df: int) -> float:
    if df <= 0 or documents <= 0:
        return 0.0
    return math.log(1 + (documents - df + 0.5) / (df + 0.5))


def rank(
    query: str,
    documents: tuple[tuple[str, str], ...],
    *,
    limit: int,
    config: RankConfig | None = None,
) -> tuple[ScoredDocument, ...]:
    """Return documents with a positive score, highest first. Ties use document identity."""
    settings = config or RankConfig()
    if limit <= 0 or not documents:
        return ()
    query_terms = list(dict.fromkeys(tokenize(query)))
    if not query_terms:
        return ()
    counts = []
    lengths = []
    for _, text in documents:
        bag: dict[str, int] = {}
        for term in tokenize(text):
            bag[term] = bag.get(term, 0) + 1
        counts.append(bag)
        lengths.append(sum(bag.values()))
    average = sum(lengths) / len(documents)
    if average <= 0:
        return ()
    df = {term: sum(1 for bag in counts if term in bag) for term in query_terms}
    scored: list[ScoredDocument] = []
    for index, (doc_id, _) in enumerate(documents):
        total = 0.0
        parts: list[TermContribution] = []
        for term in query_terms:
            tf = counts[index].get(term, 0)
            if tf <= 0:
                continue
            weight = _idf(len(documents), df[term])
            norm = settings.k1 * (1 - settings.b + settings.b * lengths[index] / average)
            contribution = weight * (tf * (settings.k1 + 1)) / (tf + norm)
            total += contribution
            parts.append(TermContribution(term, tf, weight, contribution))
        if total > 0:
            scored.append(ScoredDocument(doc_id, total, tuple(parts)))
    scored.sort(key=lambda item: (-item.score, item.doc_id))
    return tuple(scored[:limit])
