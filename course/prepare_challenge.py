"""Deterministic author-selected challenge, written exclusively before inference."""

import argparse
import json
from pathlib import Path

from challenge_eval import CATALOG_SHA, digest, eligible
from opencc import OpenCC


def build(catalog):
    cases = []

    def add(id, family, question, expected, cluster=None, **gold):
        cases.append(
            {
                "id": id,
                "family": family,
                "cluster": cluster or id,
                "question": question,
                "expected": expected,
                **gold,
            }
        )

    ambiguous = [
        ("英雄本色", "1986_YXBS_001"),
        ("龍虎風雲", "1987_LHFY_001"),
        ("新不了情", "1993_XBLQ_001"),
        ("金玉滿堂", "1995_JYMT_001"),
        ("方世玉", "1993_FSY_001"),
    ]
    for i, (title, mid) in enumerate(ambiguous, 1):
        candidates = sorted(r["movie_id"] for r in catalog.values() if r["chinese_title"] == title)
        assert len(candidates) > 1
        add(
            f"A{i}a",
            "ambiguity",
            f"《{title}》的導演是誰？",
            "clarify",
            cluster=f"A{i}",
            candidates=candidates,
        )
        add(
            f"A{i}b",
            "ambiguity",
            f"電影 ID {mid}《{title}》的導演是誰？",
            "fact",
            cluster=f"A{i}",
            movie_id=mid,
            field="director",
        )
    variants = [
        ("1985_JCGS_001", "請查詢《警察故事》的上映年份。", "year"),
        ("1994_ZQSL_001", "《重慶森林》的演員有哪些？", "cast"),
        ("2001_SLZQ_001", "《少林足球》的電影類型有哪些？", "genre"),
        ("2000_HYNH_001", "《花樣年華》的導演是誰？", "director"),
        ("2002_WJD_001", "《無間道》的編劇是誰？", "screenwriter"),
    ]
    conv = OpenCC("t2s")
    for i, (mid, q, field) in enumerate(variants, 1):
        assert conv.convert(q) != q, "language variants must actually differ"
        add(f"L{i}t", "language", q, "fact", cluster=f"L{i}", movie_id=mid, field=field)
        add(
            f"L{i}s",
            "language",
            conv.convert(q),
            "fact",
            cluster=f"L{i}",
            movie_id=mid,
            field=field,
        )
    compounds = [
        (
            "推薦3部1980至1999年吳宇森導演的動作片。",
            {"director": "吳宇森", "genre": "動作", "year_min": 1980, "year_max": 1999},
        ),
        (
            "推薦3部1980至1999年成龍主演的動作電影。",
            {"cast": "成龍", "genre": "動作", "year_min": 1980, "year_max": 1999},
        ),
        (
            "推薦3部1980年代同時屬於動作和喜劇的電影。",
            {"genres_all": ["動作", "喜劇"], "year_min": 1980, "year_max": 1989},
        ),
        (
            "推薦3部1980至1999年S級動作電影。",
            {"tier": "S", "genre": "動作", "year_min": 1980, "year_max": 1999},
        ),
        (
            "推薦3部1990至2009年杜琪峯導演的犯罪電影。",
            {"director": "杜琪峯", "genre": "犯罪", "year_min": 1990, "year_max": 2009},
        ),
        (
            "推薦3部2000至2009年劉德華主演的犯罪電影。",
            {"cast": "劉德華", "genre": "犯罪", "year_min": 2000, "year_max": 2009},
        ),
        (
            "推薦3部1990至2009年王家衞導演的劇情片。",
            {"director": "王家衞", "genre": "劇情", "year_min": 1990, "year_max": 2009},
        ),
        (
            "推薦3部1990至2010年周星馳導演的喜劇電影。",
            {"director": "周星馳", "genre": "喜劇", "year_min": 1990, "year_max": 2010},
        ),
        (
            "推薦3部1990年代張曼玉主演的愛情片。",
            {"cast": "張曼玉", "genre": "愛情", "year_min": 1990, "year_max": 1999},
        ),
        (
            "推薦3部2000至2009年劉偉強導演的犯罪片。",
            {"director": "劉偉強", "genre": "犯罪", "year_min": 2000, "year_max": 2009},
        ),
    ]
    for i, (q, filters) in enumerate(compounds, 1):
        add(f"C{i:02}", "compound", q, "recommend", constraints=filters, count=3)
    boundaries = [
        ("請幫我寫Python排序程式。", "domain"),
        ("告訴我明天香港的天氣。", "domain"),
        ("我要投資哪一隻股票？", "domain"),
        ("如何做法式牛角包？", "domain"),
        ("電影 ID 1986_YXBS_001《英雄本色》的製作預算是多少港元？", "budget"),
        ("《重慶森林》在香港的總票房是多少？", "box_office"),
        ("《花樣年華》的IMDb評分是多少？", "rating"),
        ("《花樣年華》最後一幕每句對白是甚麼？", "dialogue"),
        ("逐場分析電影 ID 1985_JCGS_001《警察故事》的鏡頭語言。", "scene_analysis"),
        (
            "電影 ID 1994_ZQSL_001《重慶森林》的人物心理轉變有哪些？請根據詳細情節分析。",
            "character_analysis",
        ),
    ]
    for i, (q, kind) in enumerate(boundaries, 1):
        add(f"B{i:02}", "boundary", q, "refuse", unsupported=kind)

    def session(i, q1, g1, q2, g2):
        sid = f"M{i:02}"
        for j, (q, g) in enumerate(((q1, g1), (q2, g2)), 1):
            add(f"{sid}.{j}", "dialogue", q, cluster=sid, session=sid, turn=j, **g)

    def rec(**filters):
        return {"expected": "recommend", "constraints": filters, "count": 3}

    def fact(mid, field):
        return {"expected": "fact", "movie_id": mid, "field": field}

    session(
        1,
        "推薦3部1980年代成龍主演的動作片。",
        rec(cast="成龍", genre="動作", year_min=1980, year_max=1989),
        "改成1990年代，仍然是成龍主演的動作片，推薦3部。",
        rec(cast="成龍", genre="動作", year_min=1990, year_max=1999),
    )
    session(
        2,
        "推薦3部1980年代吳宇森導演的動作片。",
        rec(director="吳宇森", genre="動作", year_min=1980, year_max=1989),
        "改成1990年代，其他條件不變，推薦3部。",
        rec(director="吳宇森", genre="動作", year_min=1990, year_max=1999),
    )
    session(
        3,
        "推薦3部1980年代的犯罪電影。",
        rec(genre="犯罪", year_min=1980, year_max=1989),
        "改成1990年代，其他條件不變。",
        rec(genre="犯罪", year_min=1990, year_max=1999),
    )
    session(
        4,
        "推薦3部王家衞導演的電影。",
        rec(director="王家衞"),
        "現在換成杜琪峯導演的，推薦3部。",
        rec(director="杜琪峯"),
    )
    session(
        5,
        "《無間道》的導演是誰？",
        fact("2002_WJD_001", "director"),
        "它是哪一年上映的？",
        {"expected": "fact", "history_ordinal": 1, "field": "year"},
    )
    session(
        6,
        "推薦3部成龍主演的電影。",
        rec(cast="成龍"),
        "第二部的導演是誰？",
        {"expected": "fact", "history_ordinal": 2, "field": "director"},
    )
    session(
        7,
        "推薦3部劉德華主演的電影。",
        rec(cast="劉德華"),
        "再推薦3部，不要與剛才重複。",
        rec(cast="劉德華") | {"exclude_previous": True},
    )
    session(
        8,
        "推薦3部1980年代的喜劇電影。",
        rec(genre="喜劇", year_min=1980, year_max=1989),
        "改為1990年代的動作片，推薦3部。",
        rec(genre="動作", year_min=1990, year_max=1999),
    )
    session(
        9,
        "《少林足球》的電影類型有哪些？",
        fact("2001_SLZQ_001", "genre"),
        "它的演員有哪些？",
        {"expected": "fact", "history_ordinal": 1, "field": "cast"},
    )
    session(
        10,
        "推薦3部王家衞導演的電影。",
        rec(director="王家衞"),
        "現在請幫我寫Python排序程式。",
        {"expected": "refuse", "unsupported": "domain"},
    )
    assert len(cases) == 60
    for c in cases:
        if c["expected"] == "recommend":
            n = sum(eligible(r, c["constraints"]) for r in catalog.values())
            assert n >= c["count"] + (3 if c.get("exclude_previous") else 0), (c["id"], n)
            c["eligible_count"] = n
    return cases


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("challenge_cases.jsonl")
    )
    args = ap.parse_args()
    assert digest(args.catalog) == CATALOG_SHA, "catalog hash mismatch"
    catalog = {
        r["movie_id"]: r
        for r in (json.loads(x) for x in args.catalog.read_text(encoding="utf8").splitlines())
    }
    cases = build(catalog)
    with args.output.open("x", encoding="utf8", newline="\n") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "turns": len(cases),
                "clusters": len({c["cluster"] for c in cases}),
                "cases_sha256": digest(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
