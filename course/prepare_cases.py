"""Reproduce the 30 author-selected cases from metadata; no network or model calls."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from run_simple_eval import eligible

HERE = Path(__file__).resolve().parent
movies = [json.loads(s) for s in (HERE / "data/catalog.jsonl").read_text(encoding="utf-8").splitlines()]
catalog = {m["movie_id"]: m for m in movies}
cases = []
fact_specs = [
    ("1985_JCGS_001", "director", "《警察故事》的導演是誰？"),
    ("1986_YXBS_001", "director", "《英雄本色》的導演是誰？"),
    ("2002_WJD_001", "director", "《無間道》的導演是誰？"),
    ("1990_AFZC_001", "director", "《阿飛正傳》的導演是誰？"),
    ("2000_HYNH_001", "release_date", "《花樣年華》是哪一年上映的？"),
    ("1994_ZQSL_001", "release_date", "《重慶森林》是哪一年上映的？"),
    ("1999_XJZW_001", "director", "《喜劇之王》的導演是誰？"),
    ("2001_SLZQ_001", "genre", "《少林足球》的電影類型有哪些？"),
    ("1978_ZQ_001", "director", "《醉拳》的導演是誰？"),
    ("1985_JCGS_001", "release_date", "《警察故事》是哪一年上映的？"),
]
for mid, field, question in fact_specs:
    value = catalog[mid][field]
    terms = [value[:4]] if field == "release_date" else re.split(r"[、,，]", value)
    cases.append({"case_id": f"F{len(cases)+1:02}", "kind": "fact", "question": question,
                  "movie_id": mid, "field": field, "expected_terms": terms,
                  "gold_source": catalog[mid]["passage_id"]})

recommendations = [
    ("請推薦3部吳宇森導演的香港電影。", {"director": "吳宇森"}),
    ("請推薦3部王家衞導演的香港電影。", {"director": "王家衞"}),
    ("請推薦3部成龍導演的香港電影。", {"director": "成龍"}),
    ("請推薦3部杜琪峯導演的香港電影。", {"director": "杜琪峯"}),
    ("請推薦3部周星馳導演的香港電影。", {"director": "周星馳"}),
    ("請推薦3部成龍主演的香港電影。", {"cast": "成龍"}),
    ("請推薦3部劉德華主演的香港電影。", {"cast": "劉德華"}),
    ("請推薦3部張曼玉主演的香港電影。", {"cast": "張曼玉"}),
    ("請推薦3部1990至1999年的香港喜劇電影。", {"genre": "喜劇", "year_min": 1990, "year_max": 1999}),
    ("請推薦3部1980至1989年的香港動作電影。", {"genre": "動作", "year_min": 1980, "year_max": 1989}),
]
for i, (question, constraints) in enumerate(recommendations, 1):
    gold = sorted(mid for mid, movie in catalog.items() if eligible(movie, constraints))
    assert len(gold) >= 3, (question, len(gold))
    cases.append({"case_id": f"R{i:02}", "kind": "recommendation", "question": question,
                  "count": 3, "constraints": constraints, "eligible_movie_ids": gold,
                  "gold_source": "catalog.jsonl; all matching movie IDs accepted"})

out_of_domain = ["明天香港天氣會下雨嗎？", "怎樣修理家中的WiFi路由器？",
                 "請給我一份番茄炒蛋食譜。", "如何使用Python排序整數列表？",
                 "太陽系中最大的行星是哪一個？", "巴黎有哪些旅遊景點？"]
for i, question in enumerate(out_of_domain, 1):
    cases.append({"case_id": f"A{i:02}", "kind": "refusal", "subtype": "out_of_domain",
                  "question": question, "gold_source": "author expectation: outside movie domain"})
unsupported = [
    ("1986_YXBS_001", "《英雄本色》的製作預算是多少港元？", "production_budget"),
    ("1990_AFZC_001", "《阿飛正傳》使用哪一個型號的攝影機拍攝？", "camera_model"),
    ("2000_HYNH_001", "《花樣年華》導演的私人電話號碼是多少？", "private_phone"),
    ("1999_XJZW_001", "《喜劇之王》在2027年的全球票房是多少？", "future_box_office"),
]
for i, (mid, question, missing) in enumerate(unsupported, 7):
    assert missing not in catalog[mid]
    cases.append({"case_id": f"A{i:02}", "kind": "refusal", "subtype": "insufficient_metadata",
                  "question": question, "movie_id": mid, "unavailable_field": missing,
                  "gold_source": "author expectation: catalog does not support requested fact"})

assert Counter(c["kind"] for c in cases) == {"fact": 10, "recommendation": 10, "refusal": 10}
target = HERE / "data/cases.jsonl"
content = "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases)
with target.open("x", encoding="utf-8", newline="\n") as stream:
    stream.write(content)
hashes = {name: hashlib.sha256((HERE / "data" / name).read_bytes()).hexdigest()
          for name in ("catalog.jsonl", "cases.jsonl")}
pins = HERE / "preparation/input-pins.json"
with pins.open("x", encoding="utf-8") as stream:
    json.dump({"input_sha256": hashes, "cases": 30, "formal_evaluation_started": False}, stream, indent=2)
print(json.dumps({"cases": len(cases), "groups": dict(Counter(c["kind"] for c in cases)), "input_sha256": hashes}))
