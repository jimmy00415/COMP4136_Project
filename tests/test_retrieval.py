"""Focused behavioral regressions for conversational recommendation planning."""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import FrozenInstanceError
from itertools import islice
from pathlib import Path

import pytest

from hk_movie_rag import retrieval
from hk_movie_rag.retrieval import (
    ConversationExchange,
    has_unsupported_broad_analysis_intent,
    plan_bounded_analysis,
    plan_recommendation,
)


def test_english_movie_count_and_bare_from_year_are_bounded_constraints() -> None:
    """Breaks if English count/year wording silently falls back to planner defaults."""
    plan = plan_recommendation("recommend 3 action movies from 2010", ())

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("動作",)
    assert (plan.year_from, plan.year_to) == (2010, None)


@pytest.mark.parametrize(
    ("question", "genres", "year_range"),
    [
        ("八十年代警匪片有哪些代表？", ("犯罪",), (1980, 1989)),
        ("六七十年代的香港武俠片有什麼代表作品？", ("武俠",), (1960, 1979)),
    ],
)
def test_chinese_decade_representative_lists_use_structured_retrieval(
    question: str,
    genres: tuple[str, ...],
    year_range: tuple[int, int],
) -> None:
    """Breaks if representative works fall back to unsupported generic generation."""
    plan = plan_recommendation(question, ())

    assert plan is not None
    assert plan.genres == genres
    assert (plan.year_from, plan.year_to) == year_range


def test_representative_characteristics_are_not_misread_as_a_movie_list() -> None:
    """Breaks if a request for genre characteristics becomes a title recommendation."""
    assert plan_recommendation("香港動作喜劇有什麼代表特色？", ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "九十年代香港喜劇電影有什麼特點？",
        "香港犯罪電影中常見哪些角色？",
        "香港電影如何表現身份認同？",
        "香港恐怖電影有哪些類型？",
    ),
)
def test_unobservable_broad_analysis_is_classified_before_vector_search(
    question: str,
) -> None:
    assert has_unsupported_broad_analysis_intent(question)


@pytest.mark.parametrize(
    (
        "question",
        "movie_ids",
        "passage_ids",
        "scope_terms",
        "summary_terms",
        "excerpt_terms",
    ),
    (
        (
            "香港動作喜劇有什麼代表特色？",
            ("1978_ZQ_001", "1982_ZJPD_001"),
            (
                "pdf:drunken-master-deep-analysis-v1:p1",
                "pdf:aces-go-places-deep-analysis-v1:p1",
            ),
            ("本次答案選取", "動作喜劇", "不能外推"),
            ("動作設計", "長鏡頭", "都市空間", "明快剪輯"),
            (("長鏡頭", "笑點"), ("中環", "節奏明快")),
        ),
        (
            "七十年代功夫片常見什麼元素？",
            ("1978_ZQ_001",),
            ("pdf:drunken-master-deep-analysis-v1:p1",),
            ("醉拳", "單片觀察", "常見"),
            ("長鏡頭", "功夫武打", "動作笑點"),
            (("長鏡頭", "笑點"),),
        ),
        (
            "香港電影裡的師徒關係如何表現？",
            ("1978_ZQ_001",),
            ("pdf:drunken-master-deep-analysis-v1:p2",),
            ("醉拳", "師徒關係", "不能概括"),
            ("受辱", "訓練", "成長", "師徒關係"),
            (("受辱", "訓練", "師徒關係"),),
        ),
        (
            "港產片如何呈現都市空間？",
            ("1982_ZJPD_001",),
            ("pdf:aces-go-places-deep-analysis-v1:p1",),
            ("最佳拍檔", "都市空間", "不能概括"),
            ("中環商場", "海底隧道", "貨櫃場", "超現實"),
            (("中環商場", "海底隧道", "貨櫃場", "超現實的遊樂場式鬥智舞台"),),
        ),
        (
            "想看節奏明快的香港動作片",
            ("1982_ZJPD_001",),
            ("pdf:aces-go-places-deep-analysis-v1:p1",),
            ("最佳拍檔", "節奏明快", "未被本次證據覆蓋"),
            ("剪輯節奏明快", "每四至五分鐘", "肢體衝突", "交通工具特技"),
            (("節奏明快", "每四至五分鐘"),),
        ),
        (
            "根據提供的深度分析，這些電影如何運用空間、動作與聲音？",
            ("1978_ZQ_001", "1982_ZJPD_001", "1987_FGBR_001"),
            (
                "pdf:drunken-master-deep-analysis-v1:p1",
                "pdf:aces-go-places-deep-analysis-v1:p1",
                "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1",
            ),
            ("有限", "不能外推"),
            ("長鏡頭", "都市空間", "公屋", "聲音"),
            (
                ("長鏡頭", "笑點"),
                ("中環商場", "海底隧道", "貨櫃場"),
                ("公屋單位", "狹窄格局", "空間的擠迫感"),
            ),
        ),
    ),
)
def test_bounded_analysis_plans_pin_immutable_pdf_authority(
    question: str,
    movie_ids: tuple[str, ...],
    passage_ids: tuple[str, ...],
    scope_terms: tuple[str, ...],
    summary_terms: tuple[str, ...],
    excerpt_terms: tuple[tuple[str, ...], ...],
) -> None:
    plan = plan_bounded_analysis(question)

    assert plan is not None
    assert plan.movie_ids == movie_ids
    assert plan.passage_ids == passage_ids
    assert all(term in plan.scope_note for term in scope_terms)
    assert all(term in plan.answer_summary for term in summary_terms)
    assert plan.excerpt_terms == excerpt_terms
    assert not has_unsupported_broad_analysis_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "香港動作喜劇有什麼代表特色？",
        "七十年代功夫片常見什麼元素？",
        "香港電影裡的師徒關係如何表現？",
        "港產片如何呈現都市空間？",
        "想看節奏明快的香港動作片",
        "根據提供的深度分析，這些電影如何運用空間、動作與聲音？",
    ),
)
def test_bounded_scope_describes_answer_evidence_not_corpus_inventory(
    question: str,
) -> None:
    plan = plan_bounded_analysis(question)

    assert plan is not None
    assert "Release 內僅有" not in plan.scope_note
    assert "Release 只有" not in plan.scope_note
    assert "目前深度文檔只能" not in plan.scope_note
    assert "目前有深度文檔的電影" not in plan.scope_note


@pytest.mark.parametrize(
    "question",
    (
        "九十年代香港動作喜劇有什麼代表特色？",
        "女性導演的香港動作喜劇有什麼代表特色？",
        "九十年代港產片如何呈現都市空間？",
        "不要回答香港動作喜劇有什麼代表特色？",
        "香港動作喜劇有什麼代表特色？另外推薦周星馳電影",
    ),
)
def test_bounded_analysis_rejects_extra_constraints_and_compound_requests(
    question: str,
) -> None:
    """Breaks if a keyword hit can silently discard user constraints."""
    assert plan_bounded_analysis(question) is None
    assert has_unsupported_broad_analysis_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "八十年代警匪片有哪些代表？",
        "哪些電影適合喜歡武打喜劇的觀眾？",
        "有沒有女性導演的香港電影？",
        "推薦周星馳的好電影",
        "《醉拳》的動作美學如何結合喜劇節奏？",
    ),
)
def test_supported_retrieval_intents_are_not_classified_as_broad_limitations(
    question: str,
) -> None:
    assert not has_unsupported_broad_analysis_intent(question)


def test_explicit_genre_is_preserved_alongside_title_proxy_constraints() -> None:
    plan = plan_recommendation("推薦1部喜劇兄弟情電影", ())

    assert plan is not None
    assert plan.genres == ("喜劇",)
    assert plan.genres_any == ("劇情", "動作", "犯罪")
    assert plan.title_terms_any == ("兄弟", "手足")


@pytest.mark.parametrize(
    ("genre", "canonical"),
    [
        ("愛情", "愛情"),
        ("傳記", "傳記"),
        ("動畫", "動畫"),
        ("動作", "動作"),
        ("犯罪", "犯罪"),
        ("歌舞", "歌舞"),
        ("功夫", "功夫"),
        ("怪獸", "怪獸"),
        ("黑色電影", "黑色電影"),
        ("夥伴", "夥伴"),
        ("紀錄片", "紀錄片"),
        ("家庭", "家庭"),
        ("間諜", "間諜"),
        ("監獄", "監獄"),
        ("驚慄", "驚慄"),
        ("驚悚", "驚悚"),
        ("劇情", "劇情"),
        ("科幻", "科幻"),
        ("恐怖", "恐怖"),
        ("歷史", "歷史"),
        ("冒險", "冒險"),
        ("奇幻", "奇幻"),
        ("青春", "青春"),
        ("情色", "情色"),
        ("武俠", "武俠"),
        ("西部", "西部"),
        ("喜劇", "喜劇"),
        ("新聞", "新聞"),
        ("懸疑", "懸疑"),
        ("音樂", "音樂"),
        ("運動", "運動"),
        ("災難", "災難"),
        ("戰爭", "戰爭"),
    ],
)
def test_every_v12_release_genre_is_an_observable_structured_filter(
    genre: str, canonical: str
) -> None:
    """Breaks if any canonical token in the governed v1.2 Release is omitted."""
    plan = plan_recommendation(f"推薦一部{genre}電影", ())

    assert plan is not None
    assert plan.genres == (canonical,)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("功夫", "功夫"),
        ("怪兽", "怪獸"),
        ("黑色电影", "黑色電影"),
        ("伙伴", "夥伴"),
        ("间谍", "間諜"),
        ("监狱", "監獄"),
        ("惊栗", "驚慄"),
        ("青春", "青春"),
        ("情色", "情色"),
        ("西部", "西部"),
        ("新闻", "新聞"),
        ("灾难", "災難"),
        ("kung fu", "功夫"),
        ("monster", "怪獸"),
        ("film noir", "黑色電影"),
        ("buddy", "夥伴"),
        ("spy", "間諜"),
        ("prison", "監獄"),
        ("youth", "青春"),
        ("erotic", "情色"),
        ("western", "西部"),
        ("news", "新聞"),
        ("disaster", "災難"),
    ],
)
def test_new_release_genres_accept_unambiguous_simplified_or_english_aliases(
    alias: str, canonical: str
) -> None:
    """Breaks if common user wording cannot reach its governed Release token."""
    plan = plan_recommendation(f"recommend {alias} movies", ())

    assert plan is not None
    assert plan.genres == (canonical,)


def test_count_only_history_turn_updates_the_effective_continuation_count() -> None:
    """Breaks if a chained count override is skipped while history is reconstructed."""
    history = (
        ConversationExchange("推薦3部喜劇", "第一輪。", ("a", "b", "c")),
        ConversationExchange("再来2部", "第二輪。", ("d", "e")),
    )

    plan = plan_recommendation("还有别的吗", history)

    assert plan is not None
    assert plan.requested_count == 2
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("a", "b", "c", "d", "e")


@pytest.mark.parametrize(
    "question",
    (
        "推荐3部2010电影",
        "recommend 3 movies 2010",
    ),
)
def test_standalone_four_digit_year_is_an_exact_filter(question: str) -> None:
    plan = plan_recommendation(question, ())

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.year_from == 2010
    assert plan.year_to == 2010


@pytest.mark.parametrize(
    ("question", "names", "role", "transition"),
    [
        ("王家卫的电影", ("王家衛",), None, "new"),
        ("周星驰的动作喜剧", ("周星馳",), None, "new"),
        ("周星馳主演的喜劇", ("周星馳",), "actor", "new"),
        ("王家衛導演的作品", ("王家衛",), "director", "new"),
        ("換成成龍", ("成龍",), None, "switch"),
        ("那周星馳呢", ("周星馳",), None, "switch"),
    ],
)


def test_person_query_shape_is_name_agnostic_and_script_normalized(
    question: str,
    names: tuple[str, ...],
    role: retrieval.PersonRole | None,
    transition: str,
) -> None:
    """Breaks if natural person queries cannot compile before credit resolution."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == names
    assert shape.role == role
    assert shape.transition == transition
    assert shape.exclusionary is False


@pytest.mark.parametrize(
    "question",
    (
        "推薦三部不帶恐怖元素的電影",
        "推薦三部，遠離恐怖片",
        "我討厭恐怖片，推薦三部",
        "我唔鍾意恐怖片，推薦三部",
        "recommend 3 movies, never horror",
        "recommend 3 movies, steer clear of horror",
        "recommend 3 movies, stay away from horror",
        "我不太喜歡恐怖片，推薦三部",
        "我唔睇恐怖片，推薦三部",
        "recommend 3 movies, I detest horror",
        "recommend 3 comedies sans horror",
        "recommend 3 movies apart from horror",
        "recommend 3 movies barring horror",
        "recommend 3 movies with the exception of horror",
        "推薦3部喜劇，唔使恐怖片",
        "推薦3部喜劇，唔駛恐怖片",
        "推薦3部喜劇，毋須恐怖片",
        "推薦3部喜劇，不必恐怖片",
        "推薦3部喜劇，不用介紹恐怖片",
    ),
)
def test_additional_negative_surfaces_cannot_become_positive_genres(
    question: str,
) -> None:
    assert retrieval.has_unsupported_negative_recommendation_filter(question)
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "不要廢話，推薦三部喜劇",
        "不用介紹劇情，推薦三部喜劇",
        "無需說明原因，推薦三部喜劇",
        "不用說明，推薦三部喜劇",
        "無須解釋，推薦三部喜劇",
        "別說太多，推薦三部喜劇",
        "recommend 3 comedies, no details",
        "recommend 3 comedies, no extra details",
        "recommend 3 comedies, skip the explanation",
        "recommend 3 comedies, skip the intro",
        "recommend 3 comedies, no long explanation",
        "recommend 3 comedies, don't give details",
        "recommend 3 comedies, don't give me an essay",
        "recommend 3 comedies, without the long explanation",
        "不要太多解釋，推薦三部喜劇",
        "不用太詳細，推薦三部喜劇",
        "無須多講，推薦三部喜劇",
        "推薦3部喜劇電影，不用介紹",
        "推薦3部喜劇電影，不用理由",
        "推薦3部喜劇電影，無需介紹",
        "推薦3部喜劇電影，唔使介紹",
    ),
)
def test_additional_output_directives_are_not_negative_filters(question: str) -> None:
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is not None


@pytest.mark.parametrize(
    "question",
    (
        "recommend 3 movies, no preference",
        "recommend 3 movies, no specific genre",
        "推薦3部電影，沒有特別偏好",
        "推薦3部電影，不限定類型",
    ),
)
def test_neutral_preference_language_is_not_a_negative_filter(question: str) -> None:
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is not None


def test_output_directive_cannot_become_a_positive_genre_filter() -> None:
    plan = plan_recommendation("推薦3部喜劇電影，不用介紹劇情", ())

    assert plan is not None
    assert plan.genres == ("喜劇",)


@pytest.mark.parametrize(
    "question",
    (
        "show me Stephen Chow movies",
        "Stephen Chow movies",
        "周星馳電影",
    ),
)
def test_additional_person_catalog_grammar_is_owned_before_vector_search(
    question: str,
) -> None:
    assert retrieval.has_tentative_person_catalog_intent(question)
    assert not retrieval.has_explicit_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "more yamada taro movies",
        "other yamada taro movies",
        "show me more yamada taro movies",
    ),
)
def test_lowercase_named_catalog_after_continuation_shell_is_tentative(
    question: str,
) -> None:
    assert retrieval.has_person_catalog_continuation_shell(question)
    assert retrieval.has_tentative_person_catalog_intent(question)


def test_feature_verb_is_tentative_until_release_confirmation() -> None:
    question = "which films feature Stephen Chow"

    assert not retrieval.has_explicit_person_catalog_intent(question)
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "avoid movies starring Stephen Chow",
        "stay away from movies starring Stephen Chow",
        "ban movies starring Stephen Chow",
    ),
)
def test_negative_person_catalog_surface_is_fail_closed(question: str) -> None:
    assert retrieval.has_explicit_person_catalog_intent(question)
    assert retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "avoid Stephen Chow movies",
        "no Stephen Chow movies",
        "I don't want Stephen Chow movies",
        "stay away from Stephen Chow movies",
        "steer away from Stephen Chow movies",
        "keep away from Stephen Chow movies",
        "I hate Stephen Chow movies",
        "I dislike Stephen Chow movies",
        "不要周星馳電影",
        "我唔鍾意周星馳電影",
    ),
)
def test_negative_tentative_person_catalog_surface_is_fail_closed(
    question: str,
) -> None:
    assert retrieval.has_tentative_person_catalog_intent(question)
    assert retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "推薦非周星馳作品",
        "推薦3部非周星馳電影",
        "想看非成龍電影",
    ),
)
def test_prefixed_negative_person_catalog_is_fail_closed(question: str) -> None:
    assert retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "Hong Kong movies",
        "Martial arts movies",
        "Family friendly movies",
        "Award winning movies",
        "New Wave movies",
        "crime thriller movies",
        "新浪潮電影",
        "高分冷門電影",
        "好看的電影",
        "值得看的電影",
        "最近的電影",
    ),
)
def test_bare_movie_descriptions_are_not_explicit_person_intent(question: str) -> None:
    assert not retrieval.has_explicit_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "這部電影",
        "这部电影",
        "該部電影",
        "该部影片",
        "說說這部電影",
        "介紹一下这部电影",
    ),
)
def test_deictic_movie_surfaces_are_never_person_catalogs(question: str) -> None:
    assert retrieval.parse_person_query_shape(question) is None
    assert not retrieval.has_explicit_person_catalog_intent(question)
    assert not retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "movies with martial arts",
        "films featuring martial arts",
        "movies with award winning performances",
        "movies with Hong Kong settings",
    ),
)
def test_ambiguous_relationship_descriptions_require_release_confirmation(
    question: str,
) -> None:
    assert not retrieval.has_explicit_person_catalog_intent(question)
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "新浪潮電影",
        "高分冷門電影",
        "好看的電影",
        "值得看的電影",
        "最近的電影",
    ),
)
def test_cjk_movie_descriptions_are_tentative_catalog_not_person_shapes(
    question: str,
) -> None:
    assert retrieval.parse_person_query_shape(question) is None
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize("question", ("中井貴一電影", "千葉真一電影"))
def test_release_names_ending_in_numeric_glyph_remain_tentative_catalogs(
    question: str,
) -> None:
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    ("不要重複之前的", "別給剛才那些", "不要再來同樣的"),
)
def test_history_referenced_dedup_phrases_continue_only_successful_cards(
    question: str,
) -> None:
    history = (
        ConversationExchange(
            "推薦3部喜劇電影",
            "上一輪。",
            ("old-a", "old-b", "old-c"),
        ),
    )

    assert retrieval.has_recommendation_deduplication_request(question)
    assert plan_recommendation(question, ()) is None
    plan = plan_recommendation(question, history)
    assert plan is not None
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("old-a", "old-b", "old-c")


@pytest.mark.parametrize(
    "question",
    (
        "王家衛的電影",
        "周星馳的動作喜劇",
        "周星馳主演的喜劇",
        "王家衛導演的作品",
    ),
)
def test_complete_person_movie_requests_compile_to_recommendation_plans(
    question: str,
) -> None:
    """Breaks if a complete person-film request falls back to generic retrieval."""
    assert plan_recommendation(question, ()) is not None


def test_bare_person_is_not_a_plan_but_is_reconstructed_by_more_request() -> None:
    """Breaks if a bare name is treated as a request or lost before continuation."""
    history = (ConversationExchange("周星馳", "", ("a",)),)

    assert plan_recommendation("周星馳", ()) is None
    plan = plan_recommendation("還有別的嗎？", history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("a",)


def test_adjacent_genres_compile_as_conjunction() -> None:
    """Breaks if adjacent person-film genres are weakened into alternatives."""
    plan = plan_recommendation("周星馳的動作喜劇", ())

    assert plan is not None
    assert plan.genres_all == ("喜劇", "動作")
    assert plan.genres_any == ()


def test_explicit_or_genres_compile_as_alternatives() -> None:
    """Breaks if explicit genre alternatives are compiled as a conjunction."""
    plan = plan_recommendation("周星馳的動作或喜劇電影", ())

    assert plan is not None
    assert plan.genres_all == ()
    assert plan.genres_any == ("喜劇", "動作")


def test_person_shape_retains_multiple_candidates_and_exclusion() -> None:
    """Breaks if the compiler hides ambiguity or treats exclusions as selections."""
    coordinated = retrieval.parse_person_query_shape("周星馳和成龍的電影")
    exclusionary = retrieval.parse_person_query_shape("不要周星馳的電影")

    assert coordinated is not None
    assert coordinated.candidate_names == ("周星馳", "成龍")
    assert exclusionary is not None
    assert exclusionary.candidate_names == ("周星馳",)
    assert exclusionary.exclusionary is True


@pytest.mark.parametrize(
    "question",
    (
        "別推薦周星馳的電影",
        "不想看周星馳的電影",
        "請不要推薦周星馳的電影",
        "请别推荐周星驰的电影",
    ),
)
def test_polite_negative_person_grammar_is_exclusionary(question: str) -> None:
    """Breaks if a negative wrapper is stripped as a positive recommendation."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.exclusionary is True
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "再推薦三部，不要重複。",
        "再推荐3部，不要重复。",
        "再推薦三部，不要重覆。",
        "再推薦三部，不要同一部。",
        "再推薦三部，不要一樣的電影。",
        "再推薦三部，不要同一部片。",
        "不要重複。",
        "請不要重複。",
        "不要重複推薦。",
        "不要同一部電影。",
        "請不要再推薦同一部電影。",
        "recommend 3 movies without duplicates",
        "recommend 3 more movies without repeats",
        "no repeats",
        "再給我三部，別重複",
        "再給三部，別重複",
        "給我三部，別重複",
        "麻煩再給我三部，不要重複",
        "再推介三部，別重複",
        "再建議三部，不要同一部",
        "再推薦三部新的，不要和前面一樣",
        "再推薦三部，唔好重複",
        "再推薦三部，勿重複",
        "recommend 3 more, none of the same",
        "recommend 3 new movies, not the same ones as before",
        "recommend 3 more, don't repeat",
    ),
)
def test_deduplication_follow_up_is_not_compiled_as_an_excluded_person(
    question: str,
) -> None:
    """Breaks if a supported no-repeat modifier becomes a fake person name."""
    history = (
        ConversationExchange(
            "請推薦三部喜劇電影。", "上一輪回答不可以成為證據。", ("a", "b", "c")
        ),
    )

    assert retrieval.parse_person_query_shape(question) is None
    plan = plan_recommendation(question, history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("a", "b", "c")


def test_deduplication_modifier_preserves_a_positive_person_selection() -> None:
    """Breaks if no-repeat wording turns one valid person into an exclusion."""
    question = "推薦周星馳的喜劇電影，不要重複。"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.exclusionary is False
    plan = plan_recommendation(question, ())
    assert plan is not None
    assert plan.genres == ("喜劇",)


def test_counted_person_recommendation_keeps_the_person_shape() -> None:
    """Breaks if a leading requested count hides an explicit Release person."""
    shape = retrieval.parse_person_query_shape("推薦1部周星馳主演的電影")

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role == "actor"
    assert shape.transition == "new"


@pytest.mark.parametrize(
    "question",
    (
        "周星馳出演過哪些影片？",
        "推薦周星馳出演的電影",
        "推薦周星馳參演過的作品",
        "我想看周星馳演過的電影",
    ),
)
def test_actor_relationship_aliases_compile_to_actor_scope(question: str) -> None:
    """Breaks if a natural actor query falls back to unscoped vector search."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role == "actor"
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is not None


@pytest.mark.parametrize("name", ("中井貴一", "千葉真一"))
def test_release_names_containing_grammar_tokens_keep_actor_scope(name: str) -> None:
    """Breaks if an internal grammar glyph splits a real Release actor name."""
    question = f"{name}出演過哪些影片？"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == (name,)
    assert shape.role == "actor"
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is not None


@pytest.mark.parametrize(
    ("role_surface", "expected_role"),
    (("主演", "actor"), ("導演", "director")),
)
def test_release_name_ending_in_possessive_glyph_keeps_explicit_role(
    role_surface: str, expected_role: str
) -> None:
    """Breaks if the final glyph of a real Release name is consumed as grammar."""
    question = f"推薦劉的之{role_surface}的電影"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("劉的之",)
    assert shape.role == expected_role
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is not None


@pytest.mark.parametrize(
    "question",
    (
        "推薦周星馳既主演又導演的電影",
        "推薦周星馳既是主演又是導演的電影",
    ),
)
def test_both_role_surface_forms_compile_as_conflicts(question: str) -> None:
    """Breaks if a conjunctive actor/director request broadens to either role."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role is None
    assert shape.role_conflict is True
    assert plan_recommendation(question, ()) is None


def test_deduplication_follow_up_requires_successful_history() -> None:
    """Breaks if a no-repeat continuation invents prior results to exclude."""
    zero_card_history = (
        ConversationExchange("推薦三部喜劇電影", "沒有結果。", ()),
    )

    assert plan_recommendation("不要重複。", ()) is None
    assert plan_recommendation("再推薦三部，不要重複。", ()) is None
    assert plan_recommendation(
        "再推薦三部，不要重複。", zero_card_history
    ) is None
    assert plan_recommendation("recommend 3 more movies without repeats", ()) is None
    assert plan_recommendation(
        "recommend 3 more movies without repeats", zero_card_history
    ) is None
    assert plan_recommendation("給我三部，別重複", ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "recommend 3 more, none from before",
        "recommend 3 movies different from previous picks",
    ),
)
def test_history_referenced_english_dedup_requires_and_replays_history(
    question: str,
) -> None:
    """Breaks if natural history references repeat cards or invent history."""
    history = (
        ConversationExchange(
            "recommend 3 comedy movies", "Previous answer.", ("a", "b", "c")
        ),
    )

    assert plan_recommendation(question, ()) is None
    plan = plan_recommendation(question, history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.continuation is True
    assert plan.excluded_movie_ids == ("a", "b", "c")


def test_successful_first_turn_dedup_can_start_replayable_history() -> None:
    """Breaks if a successful fresh no-repeat request disappears on the next turn."""
    history = (
        ConversationExchange(
            "推薦3部喜劇電影，不要重複", "第一輪。", ("a", "b", "c")
        ),
    )

    plan = plan_recommendation("再推薦3部，不要重複", history)

    assert plan is not None
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("a", "b", "c")
    assert plan.context_text == (
        "推薦3部喜劇電影，不要重複\n再推薦3部，不要重複"
    )


@pytest.mark.parametrize(
    "detail_question",
    (
        "醉拳的導演是誰？",
        "醉拳哪年上映？",
        "醉拳是哪種類型的電影？",
        "醉拳是哪一年的電影？",
        "你剛才推薦的醉拳是哪一年上映的？",
    ),
)
def test_movie_detail_turn_cannot_reset_the_successful_recommendation_chain(
    detail_question: str,
) -> None:
    """Breaks if a cited detail answer becomes recommendation-history authority."""
    history = (
        ConversationExchange(
            "推薦三部喜劇電影", "推薦結果。", ("a", "b", "c")
        ),
        ConversationExchange(
            detail_question, "單片資料。", ("1978_ZQ_001",)
        ),
    )

    plan = plan_recommendation("再推薦三部，不要重複", history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("a", "b", "c")
    assert "醉拳" not in plan.context_text


def test_explicit_one_movie_recommendation_can_start_a_new_history_chain() -> None:
    """Breaks if the single-card detail guard also drops a real recommendation."""
    history = (
        ConversationExchange("推薦三部喜劇電影", "第一輪。", ("a", "b", "c")),
        ConversationExchange("推薦1部動作電影", "第二輪。", ("action-one",)),
    )

    plan = plan_recommendation("再推薦1部，不要重複", history)

    assert plan is not None
    assert plan.requested_count == 1
    assert plan.genres == ("動作",)
    assert plan.excluded_movie_ids == ("action-one",)
    assert plan.context_text == "推薦1部動作電影\n再推薦1部，不要重複"


def test_one_movie_discovery_can_start_a_new_history_chain() -> None:
    """Breaks if discovery wording with one returned card is discarded as detail."""
    history = (
        ConversationExchange("想看一部喜劇電影", "第一輪。", ("comedy-one",)),
    )

    plan = plan_recommendation("再推薦1部，不要重複", history)

    assert plan is not None
    assert plan.requested_count == 1
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("comedy-one",)


def test_release_year_wording_does_not_turn_a_real_recommendation_into_detail() -> None:
    """Breaks if ``上映`` alone discards a successful recommendation turn."""
    previous = "推薦3部1990年上映的喜劇電影"
    history = (
        ConversationExchange(previous, "第一輪。", ("a", "b", "c")),
    )

    plan = plan_recommendation("再推薦3部，不要重複", history)

    assert plan is not None
    assert plan.requested_count == 3
    assert plan.genres == ("喜劇",)
    assert (plan.year_from, plan.year_to) == (1990, 1990)
    assert plan.excluded_movie_ids == ("a", "b", "c")
    assert plan.context_text == f"{previous}\n再推薦3部，不要重複"


def test_deduplication_classifier_is_anchored_before_person_grammar() -> None:
    """Breaks if a real longer name beginning with deduplication text is swallowed."""
    shape = retrieval.parse_person_query_shape("不要重複英雄的電影")

    assert shape is not None
    assert shape.candidate_names == ("重複英雄",)
    assert shape.exclusionary is True


def test_trailing_negative_person_remains_fail_closed_after_deduplication_fix() -> None:
    """Breaks if the supported no-repeat clause disables real person exclusions."""
    shape = retrieval.parse_person_query_shape("推薦三部喜劇，不要周星馳")

    assert shape is not None
    assert "周星馳" in shape.candidate_names
    assert shape.exclusionary is True


@pytest.mark.parametrize(
    "question",
    (
        "再推薦三部，別周星馳",
        "再推薦三部，不想看周星馳",
    ),
)
def test_trailing_negative_person_shorthand_remains_fail_closed(question: str) -> None:
    """Breaks if natural negative-person shorthand falls into positive retrieval."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.exclusionary is True


@pytest.mark.parametrize(
    "question",
    (
        "再推薦三部，不要犯罪片。",
        "再推薦三部，不要恐怖片。",
        "再推薦三部，不要S級。",
        "再推薦三部，不要1980年代。",
        "recommend 3 movies, no horror",
        "recommend 3 movies without crime",
        "recommend 3 movies, not from the 1980s",
        "請不要推薦喜劇電影",
        "再推薦三部不要1980年代",
        "再推薦三部，別喜劇",
        "請不要推薦B級電影",
        "do not recommend horror movies",
        "recommend 3 movies without any horror",
        "推薦3部不要恐怖片",
        "推薦3部電影但不要S級",
        "推薦3部，別看恐怖片",
        "推薦3部，非恐怖片",
        "recommend 3 movies, don't recommend horror",
        "recommend 3 movies, no Hong Kong horror",
        "推薦三部，不推薦恐怖片",
        "推薦喜劇，但不含恐怖片",
        "推薦不是S級的電影",
        "推薦3部，唔好恐怖片",
        "recommend 3 movies, no more horror movies",
        "recommend 3 movies excluding all B-tier films",
        "推薦非恐怖片",
        "推薦非S級電影",
        "推薦3部，不想要恐怖片",
        "推薦3部，不包括S級電影",
        "推薦3部，避免1980年代電影",
        "推薦3部電影，恐怖片除外",
        "推薦3部電影，S級電影不要",
        "推薦3部電影，1980年代電影除外",
        "recommend 3 movies, avoid horror",
        "recommend 3 movies, anything but horror",
        "recommend 3 movies, except horror",
        "recommend 3 movies, other than horror",
        "recommend 3 movies, skip horror",
        "我不看恐怖片，推薦三部",
        "我不喜歡恐怖片，推薦三部",
        "推薦三部恐怖以外的電影",
        "除恐怖片外推薦三部",
        "推薦三部，唔要恐怖片",
        "推薦三部電影，不考慮恐怖片",
        "recommend 3 non-horror movies",
        "recommend 3 movies that aren't horror",
        "recommend 3 horror-free movies",
        "I dislike horror; recommend 3 movies",
        "I hate horror; recommend 3 movies",
    ),
)
def test_unsupported_negative_filters_never_compile_as_positive_constraints(
    question: str,
) -> None:
    """Breaks if an unsupported exclusion silently reverses into a positive filter."""
    history = (
        ConversationExchange("推薦三部喜劇電影", "上一輪。", ("a", "b", "c")),
    )

    assert retrieval.has_unsupported_negative_recommendation_filter(question)
    assert plan_recommendation(question, history) is None


@pytest.mark.parametrize(
    "question",
    (
        "唔想睇周星馳的電影",
        "唔想睇恐怖片，推薦三部",
        "推薦三部冇恐怖元素的電影",
        "推薦三部沒有犯罪元素的電影",
        "recommend 3 movies that shouldn't include horror",
        "recommend 3 movies that cannot contain crime",
        "recommend 3 movies free of horror",
        "recommend 3 movies, leave horror out",
        "recommend 3 movies that shouldn't include Stephen Chow",
        "recommend 3 movies that cannot include Stephen Chow",
        "recommend 3 movies free of Stephen Chow",
        "recommend 3 movies, leave Stephen Chow out",
    ),
)
def test_additional_negative_person_and_genre_surfaces_fail_closed(
    question: str,
) -> None:
    """Breaks if common Cantonese or English exclusions broaden retrieval."""
    shape = retrieval.parse_person_query_shape(question)
    detected_negative = (
        retrieval.has_unsupported_negative_recommendation_filter(question)
        or retrieval.has_unmodeled_negative_recommendation_condition(question)
        or (shape is not None and shape.exclusionary)
    )

    assert detected_negative
    assert plan_recommendation(question, ()) is None


def test_negative_answer_instruction_is_not_a_negative_movie_filter() -> None:
    """Breaks if an answer-level instruction is mistaken for a genre exclusion."""
    question = "不要回答香港動作喜劇有什麼代表特色？"

    assert not retrieval.has_unsupported_negative_recommendation_filter(question)


@pytest.mark.parametrize(
    "question",
    (
        "醉拳好看嗎？不是喜劇嗎？",
        "比較醉拳和最佳拍檔，不是都是喜劇嗎？",
        "推薦3部喜劇電影，不要劇透",
        "推薦3部喜劇電影，不要解釋，只列片名",
        "推薦3部喜劇，別講結局",
        "recommend 3 comedies, don't explain",
        "recommend 3 comedies, no explanations",
        "recommend 3 comedies, no plot spoilers",
        "recommend 3 comedies, don't spoil them",
        "為什麼影評人不推薦《醉拳》？",
        "推薦算法為什麼不包括《醉拳》的票房？",
    ),
)
def test_fact_negation_and_answer_instructions_are_not_negative_filters(
    question: str,
) -> None:
    """Breaks if factual negation or output style is treated as retrieval exclusion."""
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)


@pytest.mark.parametrize(
    "question",
    (
        "推薦3部喜劇電影，不用多說",
        "推薦3部喜劇電影，別長篇大論",
        "recommend 3 comedies, no commentary",
        "recommend 3 comedies, do not elaborate",
    ),
)
def test_additional_output_instructions_do_not_become_movie_filters(
    question: str,
) -> None:
    """Breaks if answer-length instructions are interpreted as exclusions."""
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is not None


def test_explanatory_clause_cannot_hide_a_later_negative_recommendation() -> None:
    """Breaks if one ``why`` clause disables polarity checks for the whole query."""
    question = "為什麼影評人不推薦《醉拳》？另外推薦3部不要恐怖片"

    assert retrieval.has_unsupported_negative_recommendation_filter(question)


@pytest.mark.parametrize(
    "question",
    (
        "點解影評人唔推薦《醉拳》？另外推薦3部唔想睇恐怖片",
        "Why isn't Drunken Master a comedy? Recommend 3 movies without horror",
    ),
)
def test_explanatory_clause_in_any_supported_language_cannot_hide_exclusion(
    question: str,
) -> None:
    """Breaks if a leading why-clause disables a later real negative filter."""
    assert (
        retrieval.has_unsupported_negative_recommendation_filter(question)
        or retrieval.has_unmodeled_negative_recommendation_condition(question)
    )
    assert plan_recommendation(question, ()) is None


def test_boundary_negative_marker_does_not_capture_an_unrelated_title_word() -> None:
    """Breaks if the bounded ``非`` marker treats a title token as a filter."""
    assert not retrieval.has_unsupported_negative_recommendation_filter(
        "非凡任務好看嗎？"
    )


@pytest.mark.parametrize(
    "question",
    (
        "don't recommend Stephen Chow movies",
        "recommend movies without Stephen Chow",
    ),
)
def test_unmodeled_english_negative_person_condition_fails_closed(
    question: str,
) -> None:
    """Breaks if an English person exclusion silently becomes positive retrieval."""
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "推薦三部電影，但周星馳的不要",
        "recommend 3 movies, anything but Stephen Chow",
        "recommend 3 movies, except Stephen Chow",
        "recommend 3 films other than Jackie Chan",
        "我不喜歡周星馳，推薦三部電影",
        "推薦三部電影，不考慮周星馳",
        "recommend movies Stephen Chow isn't in",
        "I dislike Stephen Chow; recommend 3 movies",
    ),
)
def test_unmodeled_negative_person_surfaces_never_compile_positive_plans(
    question: str,
) -> None:
    """Breaks if a person exclusion is ignored or converted to a positive person."""
    assert retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is None


def test_negative_looking_release_credit_is_owned_by_positive_person_shape() -> None:
    """Breaks if a real Release credit is stolen by generic negation grammar."""
    question = "推薦不是女人的電影"
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("不是女人",)
    assert shape.exclusionary is False
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)
    assert plan_recommendation(question, ()) is not None


def test_bare_negative_looking_release_credit_waits_for_release_authority() -> None:
    question = "不是女人電影"

    assert retrieval.has_tentative_person_catalog_intent(question)
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)


@pytest.mark.parametrize(
    "question",
    (
        "movies starring Stephen Chow and Jackie Chan",
        "movies directed by John Woo and Wong Kar Wai",
    ),
)
def test_coordinated_english_person_catalog_is_owned_before_vector(
    question: str,
) -> None:
    assert retrieval.has_multiple_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "movies starring Stephen Chow from the 1990s",
        "films directed by Wong Kar Wai in the 1990s",
        "movies with Stephen Chow from the 1990s",
        "films featuring Stephen Chow in the 1990s",
        "movies starring 周星馳",
        "films directed by 王家衛",
        "movies with 周星馳",
        "films featuring 成龍",
        "show me movies starring 中井貴一",
    ),
)
def test_person_catalog_keeps_bounded_suffixes_and_mixed_script_names(
    question: str,
) -> None:
    assert (
        retrieval.has_explicit_person_catalog_intent(question)
        or retrieval.has_tentative_person_catalog_intent(question)
    )


@pytest.mark.parametrize(
    "question",
    (
        "還有周星馳電影嗎",
        "再來些周星馳電影",
        "其他周星馳電影",
        "周星馳還有什麼電影",
    ),
)
def test_cjk_person_catalog_survives_bounded_continuation_shells(
    question: str,
) -> None:
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "connector",
    ("和", "與", "与", "及", "跟", "或", "、", "，", ",", "以及", "或者", "兼", "兼任"),
)
@pytest.mark.parametrize("possessive", ("", "的"))
def test_all_role_coordination_connectors_are_explicitly_conflicting(
    connector: str, possessive: str
) -> None:
    """Breaks if a coordinated actor/director request becomes role=None retrieval."""
    question = f"推薦周星馳主演{connector}導演{possessive}電影"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role is None
    assert shape.role_conflict is True
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is None


def test_simplified_role_aliases_are_canonicalized_before_conflict_parsing() -> None:
    """Breaks if simplified actor/director words bypass the conflict boundary."""
    question = "推荐周星驰演员或导演电影"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role is None
    assert shape.role_conflict is True
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    ("roles", "expected_role"),
    (("主演和演員", "actor"), ("導演或執導", "director")),
)
def test_synonymous_coordinated_roles_remain_one_role(
    roles: str, expected_role: str
) -> None:
    """Breaks if synonymous role wording is mislabeled as actor/director conflict."""
    question = f"推薦周星馳{roles}的電影"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role == expected_role
    assert shape.role_conflict is False
    assert plan_recommendation(question, ()) is not None


def test_every_bounded_release_credit_survives_person_catalog_compilation() -> None:
    """Breaks if valid Release names are mistaken for coordination or possession grammar."""
    release_movies = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "release"
        / "v1.2"
        / "movies.csv"
    )
    with release_movies.open(encoding="utf-8-sig", newline="") as handle:
        rows = tuple(csv.DictReader(handle))
    names = {
        token.strip()
        for row in rows
        for field in ("director", "cast")
        for token in re.split(r"[、,，/;；]", row[field])
        if re.fullmatch(r"[\u3400-\u9fff]{2,7}", token.strip())
    }

    assert len(names) == 4_235
    for name in sorted(names):
        shape = retrieval.parse_person_query_shape(f"{name}的電影")
        assert shape is not None, name
        assert len(shape.candidate_names) == 1, name
        assert shape.role_conflict is False, name
        assert plan_recommendation(f"{name}的電影", ()) is not None, name


@pytest.mark.parametrize(
    "name",
    (
        "袁和平",
        "高橋和也",
        "黃和興",
        "朱鐵和",
        "楊以和",
        "赤井英和",
        "鄭容和",
        "鄭昌和",
        "麥德和",
        "劉的之",
    ),
)
def test_separator_bearing_release_name_controls_remain_single_people(name: str) -> None:
    """Breaks if a known grammar token unconditionally divides one valid name."""
    shape = retrieval.parse_person_query_shape(f"{name}的電影")

    assert shape is not None
    assert shape.candidate_names == (name,)


def test_valid_multi_name_partition_preserves_internal_coordination_token() -> None:
    """Breaks if the parser splits every coordination glyph or never finds a compound."""
    shape = retrieval.parse_person_query_shape("推薦袁和平和成龍的電影")

    assert shape is not None
    assert shape.candidate_names == ("袁和平", "成龍")
    assert plan_recommendation("推薦袁和平和成龍的電影", ()) is None


def test_many_coordination_boundaries_have_bounded_segmentation_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if valid-name partitioning enumerates every separator subset."""
    names = tuple(
        islice(
            (
                candidate
                for codepoint in range(0x4E00, 0x9FFF)
                if retrieval._PERSON_COORDINATION_PATTERN.search(
                    candidate := chr(codepoint) * 2
                )
                is None
                and retrieval.normalize_query_text(candidate) == candidate
                and retrieval._is_person_name_candidate(candidate)
            ),
            332,
        )
    )
    assert len(names) == 332
    question = f"{'和'.join(names)}的電影"
    assert len(question) == 998
    original = retrieval._is_person_name_candidate
    validation_calls = 0

    def counted(candidate: str) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return original(candidate)

    monkeypatch.setattr(retrieval, "_is_person_name_candidate", counted)

    original_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(250)
        shape = retrieval.parse_person_query_shape(question)
    finally:
        sys.setrecursionlimit(original_recursion_limit)

    assert shape is not None
    assert shape.candidate_names == names
    assert validation_calls <= 5_000


def test_name_starting_with_bie_is_not_consumed_as_negative_grammar() -> None:
    """Breaks if bare `別` strips the first character of a positive person name."""
    shape = retrieval.parse_person_query_shape("別所哲也的電影")

    assert shape is not None
    assert shape.candidate_names == ("別所哲也",)
    assert shape.exclusionary is False
    assert plan_recommendation("別所哲也的電影", ()) is not None


@pytest.mark.parametrize(
    "question",
    (
        "推薦周星馳與成龍電影",
        "周星馳的電影和成龍的作品",
        "推薦周星馳的電影以及成龍的作品",
        "周星馳主演的電影或成龍主演的作品",
    ),
)
def test_person_shape_retains_bounded_coordinated_catalog_candidates(
    question: str,
) -> None:
    """Breaks if catalog coordination hides all but the first named person."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳", "成龍")
    assert shape.exclusionary is False


@pytest.mark.parametrize(
    ("question", "expected_names"),
    (
        ("想看周星驰和成龍電影，推薦幾部", ("周星馳", "成龍")),
        ("想找成龙與周星馳作品，推介三部", ("成龍", "周星馳")),
    ),
)
def test_decorated_mixed_script_people_retain_every_current_candidate(
    question: str, expected_names: tuple[str, ...],
) -> None:
    """Breaks if discovery or trailing request wrappers hide a second person."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == expected_names
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is None


def test_decorated_single_person_retains_selection_and_structured_plan() -> None:
    """Breaks if wrapper stripping destroys a valid single-person selection."""
    question = "想看周星驰的電影，推薦幾部"

    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.exclusionary is False
    assert plan_recommendation(question, ()) is not None


def test_longer_release_credit_reaches_bounded_person_compilation() -> None:
    """Breaks if a legitimate longer CJK Release credit falls into vector search."""
    shape = retrieval.parse_person_query_shape("大島由加里的電影")

    assert shape is not None
    assert shape.candidate_names == ("大島由加里",)
    assert plan_recommendation("大島由加里的電影", ()) is not None
    assert retrieval.parse_person_query_shape("甲乙丙丁戊己庚辛的電影") is None


@pytest.mark.parametrize("question", ("香港電影", "動作喜劇", "哪些"))
def test_person_shape_rejects_known_region_and_topic_qualifiers(question: str) -> None:
    """Breaks if non-person Chinese phrases become credit candidates."""
    assert retrieval.parse_person_query_shape(question) is None


def test_person_switch_inherits_filters_without_excluding_prior_person_movies() -> None:
    """Breaks if changing people also drops filters or hides eligible new-person films."""
    history = (
        ConversationExchange(
            "王家衛的1980年代S級愛情電影", "", ("old-wong",)
        ),
    )

    plan = plan_recommendation("換成成龍", history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.genres == ("愛情",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ()


def test_successful_person_switch_bounds_next_exclusions_and_context() -> None:
    """Breaks if replay exposes the pre-switch person's IDs or text to a follow-up."""
    old_person = "推薦1部1980年代S級周星馳主演的喜劇電影"
    person_switch = "換成1部成龍主演的電影"
    current = "還有別的嗎？"
    history = (
        ConversationExchange(old_person, "第一輪。", ("old-chow",)),
        ConversationExchange(person_switch, "第二輪。", ("old-jackie",)),
    )

    plan = plan_recommendation(current, history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.requested_count == 1
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-jackie",)
    assert plan.context_text == f"{person_switch}\n{current}"


def test_zero_card_person_switch_does_not_replace_the_last_successful_chain() -> None:
    """Breaks if an unsuccessful switch hides the last successful person's history."""
    old_person = "推薦1部1980年代S級周星馳主演的喜劇電影"
    failed_switch = "換成1部山田太郎主演的電影"
    current = "還有別的嗎？"
    history = (
        ConversationExchange(old_person, "第一輪。", ("old-chow",)),
        ConversationExchange(failed_switch, "沒有結果。", ()),
    )

    plan = plan_recommendation(current, history)

    assert plan is not None
    assert plan.genres == ("喜劇",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-chow",)
    assert plan.context_text == f"{old_person}\n{current}"


def test_current_person_with_continuation_vocabulary_owns_complete_turn() -> None:
    """Breaks if lexical continuation overrides a complete current-person request."""
    history = (
        ConversationExchange(
            "王家衛的1980年代S級愛情電影", "", ("old-wong",)
        ),
    )

    plan = plan_recommendation("山田太郎的殭屍電影還有嗎", history)

    assert plan is not None
    assert plan.continuation is False
    assert plan.scope_label == "title_keyword_proxy"
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == "山田太郎的殭屍電影還有嗎"


def test_true_person_continuation_inherits_person_filter_state_and_exclusions() -> None:
    """Breaks if a genuine continuation loses prior person/filter ownership."""
    previous = "王家衛的1980年代S級愛情電影"
    history = (ConversationExchange(previous, "", ("old-wong",)),)

    plan = plan_recommendation("還有別的嗎？", history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.genres == ("愛情",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.excluded_movie_ids == ("old-wong",)
    assert plan.context_text == f"{previous}\n還有別的嗎？"


@pytest.mark.parametrize(
    "question",
    (
        "recommend 3 movies about brothers",
        "recommend 3 movies about brotherhood",
        "recommend 3 movies about motherhood",
        "recommend 3 movies about a mother",
    ),
)
def test_english_continuation_terms_require_token_boundaries(question: str) -> None:
    history = (
        ConversationExchange(
            "推薦3部喜劇電影",
            "第一輪。",
            ("old-a", "old-b", "old-c"),
        ),
    )

    plan = plan_recommendation(question, history)

    assert plan is not None
    assert plan.continuation is False
    assert plan.genres == ()
    assert plan.excluded_movie_ids == ()
    assert plan.context_text == question


def test_english_other_word_remains_a_real_continuation() -> None:
    previous = "推薦3部喜劇電影"
    history = (
        ConversationExchange(previous, "第一輪。", ("old-a", "old-b", "old-c")),
    )

    plan = plan_recommendation("other movies", history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("old-a", "old-b", "old-c")


def test_motherhood_dedup_wording_does_not_require_prior_history() -> None:
    question = "recommend 3 motherhood movies without repeats"

    assert retrieval.deduplication_requires_successful_history(question) is False


def test_complete_current_person_reset_survives_history_replay() -> None:
    """Breaks if replay resurrects filters from before a complete person reset."""
    history = (
        ConversationExchange(
            "王家衛的1980年代S級愛情電影", "", ("old-wong",)
        ),
        ConversationExchange(
            "山田太郎的殭屍電影還有嗎", "", ("old-yamada",)
        ),
    )

    plan = plan_recommendation("還有別的嗎？", history)

    assert plan is not None
    assert plan.continuation is True
    assert plan.scope_label == "title_keyword_proxy"
    assert plan.genres == ("恐怖", "喜劇")
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)


def test_failed_turn_cannot_donate_filters_or_context_to_older_successful_chain() -> None:
    """Breaks if a zero-card turn contaminates filters while an older person survives."""
    old_success = "王家衛的1980年代S級愛情電影"
    failed_current = "山田太郎的殭屍電影"
    history = (
        ConversationExchange(old_success, "第一輪。", ("old-wong",)),
        ConversationExchange(failed_current, "沒有結果。", ()),
    )

    plan = plan_recommendation("還有別的嗎？", history)

    assert plan is not None
    assert plan.genres == ("愛情",)
    assert plan.tier == "S"
    assert (plan.year_from, plan.year_to) == (1980, 1989)
    assert plan.scope_label == "metadata"
    assert plan.excluded_movie_ids == ("old-wong",)
    assert plan.context_text == f"{old_success}\n還有別的嗎？"


@pytest.mark.parametrize(
    ("fresh_catalog_question", "expected_genres"),
    (
        ("好看的電影", ()),
        ("新浪潮電影", ()),
        ("最近的電影", ()),
        ("高分冷門電影", ()),
        ("動作電影", ("動作",)),
        ("功夫電影", ("功夫",)),
        ("家庭電影", ("家庭",)),
        ("香港電影", ()),
        ("經典電影", ()),
        ("action movies", ("動作",)),
        ("Hong Kong movies", ()),
        ("martial arts movies", ()),
        ("family friendly movies", ("家庭",)),
        ("award winning movies", ()),
        ("new wave movies", ()),
        ("crime thriller movies", ("犯罪", "驚悚")),
    ),
)
@pytest.mark.parametrize(
    "generic_movie_ids",
    (("generic-a",), ("generic-a", "generic-b", "generic-c")),
)
def test_successful_fresh_catalog_turn_resets_older_person_history(
    fresh_catalog_question: str,
    expected_genres: tuple[str, ...],
    generic_movie_ids: tuple[str, ...],
) -> None:
    current = "再推薦3部，不要重複"
    history = (
        ConversationExchange(
            "推薦3部王家衛導演的愛情電影",
            "第一輪。",
            ("wong-a", "wong-b", "wong-c"),
        ),
        ConversationExchange(
            fresh_catalog_question,
            "第二輪。",
            generic_movie_ids,
        ),
    )

    plan = plan_recommendation(current, history)

    assert plan is not None
    assert plan.genres == expected_genres
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.excluded_movie_ids == generic_movie_ids
    assert plan.context_text == f"{fresh_catalog_question}\n{current}"


def test_successful_reset_starts_a_new_exclusion_and_context_chain() -> None:
    """Breaks if a new successful reset retains movie IDs or text from an older chain."""
    old_success = "王家衛的1980年代S級愛情電影"
    new_success = "山田太郎的殭屍電影"
    history = (
        ConversationExchange(old_success, "第一輪。", ("old-wong",)),
        ConversationExchange(new_success, "第二輪。", ("new-yamada",)),
    )

    plan = plan_recommendation("還有別的嗎？", history)

    assert plan is not None
    assert plan.genres == ("恐怖", "喜劇")
    assert plan.tier is None
    assert (plan.year_from, plan.year_to) == (None, None)
    assert plan.scope_label == "title_keyword_proxy"
    assert plan.excluded_movie_ids == ("new-yamada",)
    assert plan.context_text == f"{new_success}\n還有別的嗎？"


@pytest.mark.parametrize(
    ("question", "names", "exclusionary"),
    (
        ("推薦周星馳和成龍的電影", ("周星馳", "成龍"), False),
        ("推薦周星馳的電影，不要成龍", ("周星馳", "成龍"), True),
    ),
)
def test_recommendation_wrapped_person_ambiguity_fails_closed(
    question: str, names: tuple[str, ...], exclusionary: bool
) -> None:
    """Breaks if recommendation wrappers hide unsafe person-selection semantics."""
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == names
    assert shape.exclusionary is exclusionary
    assert plan_recommendation(question, ()) is None


@pytest.mark.parametrize(
    "question",
    (
        "周星馳的動作或喜劇電影",
        "周星馳的動作或者喜劇電影",
        "周星馳的動作還是喜劇電影",
        "周星馳的action or comedy movies",
    ),
)
def test_person_genre_alternative_operators_compile_as_any(question: str) -> None:
    """Breaks if an explicit alternative operator is weakened into conjunction."""
    plan = plan_recommendation(question, ())

    assert plan is not None
    assert plan.genres_all == ()
    assert plan.genres_any == ("喜劇", "動作")


@pytest.mark.parametrize(
    "question",
    (
        "推荐周星驰的好电影",
        "推薦周星馳的好電影",
    ),
)
def test_person_wording_routes_to_a_plan_with_backward_compatible_defaults(
    question: str,
) -> None:
    """Breaks if a person-qualified request bypasses deterministic recommendation planning."""
    plan = plan_recommendation(question, ())

    assert plan is not None
    assert plan.person_name is None
    assert plan.person_role is None
    assert plan.person_exact_names == ()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "哪些電影適合喜歡武打喜劇的觀眾？",
            {
                "genres_all": ("喜劇",),
                "genres_any": ("動作", "功夫", "武俠"),
                "title_terms_any": (),
                "credit_group_id": None,
                "credit_role": None,
                "credit_exact_names": (),
                "scope_label": "metadata",
            },
        ),
        (
            "有什麼關於兄弟情的香港電影？",
            {
                "genres_all": (),
                "genres_any": ("劇情", "動作", "犯罪"),
                "title_terms_any": ("兄弟", "手足"),
                "credit_group_id": None,
                "credit_role": None,
                "credit_exact_names": (),
                "scope_label": "title_keyword_proxy",
            },
        ),
        (
            "有沒有女性導演的香港電影？",
            {
                "genres_all": (),
                "genres_any": (),
                "title_terms_any": (),
                "credit_group_id": "female_directors_v1",
                "credit_role": "director",
                "credit_exact_names": (
                    "許鞍華",
                    "張婉婷",
                    "张婉婷",
                    "羅卓瑤",
                    "麥曦茵",
                    "麦曦茵",
                    "黃真真",
                    "岸西",
                ),
                "scope_label": "controlled_credit_group",
            },
        ),
        (
            "香港電影裡的殭屍題材",
            {
                "genres_all": (),
                "genres_any": ("恐怖", "喜劇"),
                "title_terms_any": ("殭屍", "僵屍"),
                "credit_group_id": None,
                "credit_role": None,
                "credit_exact_names": (),
                "scope_label": "title_keyword_proxy",
            },
        ),
        (
            "港片中的賭片有哪些？",
            {
                "genres_all": (),
                "genres_any": (),
                "title_terms_any": ("賭", "千王", "雀聖", "麻雀", "撲克"),
                "credit_group_id": None,
                "credit_role": None,
                "credit_exact_names": (),
                "scope_label": "title_keyword_proxy",
            },
        ),
        (
            "想找一部由女演員擔綱主角的劇情片。",
            {
                "genres_all": ("劇情",),
                "genres_any": (),
                "title_terms_any": (),
                "credit_group_id": "female_actors_v1",
                "credit_role": "actor",
                "credit_exact_names": (
                    "張曼玉",
                    "林青霞",
                    "梅艷芳",
                    "蕭芳芳",
                    "鄭秀文",
                    "惠英紅",
                    "楊紫瓊",
                    "袁詠儀",
                    "舒淇",
                    "王祖賢",
                    "鍾楚紅",
                ),
                "scope_label": "controlled_credit_group",
            },
        ),
        (
            "哪幾部港片適合全家觀看？",
            {
                "genres_all": ("家庭",),
                "genres_any": (),
                "title_terms_any": (),
                "credit_group_id": None,
                "credit_role": None,
                "credit_exact_names": (),
                "scope_label": "family_genre_proxy",
            },
        ),
    ],
)
def test_bounded_discovery_queries_emit_exact_typed_constraints(
    question: str, expected: dict[str, object]
) -> None:
    """Breaks if a governed discovery query can reach unconstrained vector search."""
    plan = plan_recommendation(question, ())

    assert plan is not None
    assert {
        key: getattr(plan, key)
        for key in (
            "genres_all",
            "genres_any",
            "title_terms_any",
            "credit_group_id",
            "credit_role",
            "credit_exact_names",
            "scope_label",
        )
    } == expected


@pytest.mark.parametrize(
    ("traditional", "simplified"),
    [
        (
            "哪些電影適合喜歡武打喜劇的觀眾？",
            "哪些电影适合喜欢武打喜剧的观众？",
        ),
        (
            "有什麼關於兄弟情的香港電影？",
            "有什么关于兄弟情的香港电影？",
        ),
        ("有沒有女性導演的香港電影？", "有没有女性导演的香港电影？"),
        ("香港電影裡的殭屍題材", "香港电影里的僵尸题材"),
        ("港片中的賭片有哪些？", "港片中的赌片有哪些？"),
        (
            "想找一部由女演員擔綱主角的劇情片。",
            "想找一部由女演员担纲主角的剧情片。",
        ),
        ("哪幾部港片適合全家觀看？", "哪几部港片适合全家观看？"),
    ],
)
def test_simplified_discovery_queries_share_canonical_typed_constraints(
    traditional: str, simplified: str
) -> None:
    """Breaks if script variants produce different SQL-bound policy values."""
    traditional_plan = plan_recommendation(traditional, ())
    simplified_plan = plan_recommendation(simplified, ())

    assert traditional_plan is not None
    assert simplified_plan is not None
    fields = (
        "genres_all",
        "genres_any",
        "title_terms_any",
        "credit_group_id",
        "credit_role",
        "credit_exact_names",
        "scope_label",
    )
    assert tuple(getattr(simplified_plan, field) for field in fields) == tuple(
        getattr(traditional_plan, field) for field in fields
    )


@pytest.mark.parametrize(
    "question",
    (
        "想看喜劇電影",
        "想找一部喜劇電影",
        "哪些電影適合喜歡喜劇的觀眾？",
        "哪幾部港片是喜劇？",
        "有什麼電影是喜劇？",
    ),
)
def test_bounded_discovery_wording_is_structured_intent(question: str) -> None:
    """Breaks if common discovery wording silently falls into generic retrieval."""
    assert plan_recommendation(question, ()) is not None


@pytest.mark.parametrize(
    "question",
    (
        "想看節奏明快的香港動作片",
        "想看节奏明快的香港动作片",
        "想找一部快節奏的犯罪電影",
    ),
)
def test_discovery_wording_does_not_drop_unsupported_pace_criteria(
    question: str,
) -> None:
    """Breaks if a missing pace facet is silently weakened to a genre-only query."""
    assert plan_recommendation(question, ()) is None


def test_continuation_inherits_or_explicitly_replaces_typed_constraints() -> None:
    """Breaks if a follow-up loses its proxy policy or keeps it after a new filter."""
    history = (
        ConversationExchange(
            "有什麼關於兄弟情的香港電影？",
            "第一輪。",
            ("brother-1", "brother-2"),
        ),
    )

    inherited = plan_recommendation("還有別的嗎？", history)
    replaced = plan_recommendation("換成喜劇電影", history)

    assert inherited is not None
    assert inherited.title_terms_any == ("兄弟", "手足")
    assert inherited.genres_any == ("劇情", "動作", "犯罪")
    assert inherited.scope_label == "title_keyword_proxy"
    assert inherited.excluded_movie_ids == ("brother-1", "brother-2")
    assert replaced is not None
    assert replaced.genres == ("喜劇",)
    assert replaced.genres_all == ()
    assert replaced.genres_any == ()
    assert replaced.title_terms_any == ()
    assert replaced.scope_label == "metadata"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("香港動作喜劇有什麼代表特色？", True),
        ("經典武俠片有哪些？", True),
        ("推薦一本小說", False),
        ("今天天氣怎麼樣？", False),
    ],
)
def test_public_governed_movie_filter_signal_has_no_recommendation_side_effects(
    question: str, expected: bool
) -> None:
    """Breaks if the domain gate must import retrieval's private planning internals."""
    assert retrieval.has_governed_movie_filter(question) is expected


def test_resolved_person_is_an_immutable_typed_value() -> None:
    """Breaks if repository resolution can return an untyped or mutable person result."""
    resolved_type = getattr(retrieval, "ResolvedPerson", None)

    assert resolved_type is not None
    person = resolved_type("周星馳", "actor")
    assert (person.name, person.role) == ("周星馳", "actor")
    assert person.exact_names == ()
    equivalent = resolved_type(
        "廖啓智", "actor", ("廖啓智", "廖啟智")
    )
    assert equivalent.exact_names == ("廖啓智", "廖啟智")
    with pytest.raises(FrozenInstanceError):
        person.name = "成龍"


@pytest.mark.parametrize(
    "question",
    (
        "還有周星馳電影嗎",
        "再推薦周星馳電影",
        "更多周星馳電影",
        "別的周星馳電影",
        "其他周星馳電影",
        "換成周星馳電影",
        "換一部周星馳電影",
        "周星馳還有什麼電影",
    ),
)
def test_cjk_continuation_shell_preserves_tentative_person_catalog_ownership(
    question: str,
) -> None:
    assert retrieval.has_tentative_person_catalog_intent(question)
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)


@pytest.mark.parametrize(
    "question",
    (
        "還有周星馳主演的電影嗎",
        "再推薦周星馳主演的電影",
        "更多周星馳主演的電影",
        "別的周星馳主演的電影",
        "換成周星馳主演的電影",
        "換一部周星馳主演的電影",
    ),
)
def test_cjk_continuation_shell_preserves_explicit_person_role_ownership(
    question: str,
) -> None:
    shape = retrieval.parse_person_query_shape(question)

    assert shape is not None
    assert shape.candidate_names == ("周星馳",)
    assert shape.role == "actor"
    assert not shape.exclusionary
    assert retrieval.has_explicit_person_catalog_intent(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)


@pytest.mark.parametrize(
    "question",
    (
        "別的電影還有嗎？",
        "別的呢？",
        "有別的嗎？",
        "有其他的嗎？",
        "給我別的電影",
        "來點別的",
        "換別的",
        "更多",
        "更多電影",
        "另一部",
        "另一些",
    ),
)
def test_generic_cjk_continuation_shell_is_not_a_negative_person_or_filter(
    question: str,
) -> None:
    history = (
        ConversationExchange("推薦3部喜劇", "第一輪。", ("a", "b", "c")),
    )

    assert retrieval.parse_person_query_shape(question) is None
    assert not retrieval.has_unsupported_negative_recommendation_filter(question)
    assert not retrieval.has_unmodeled_negative_recommendation_condition(question)
    plan = plan_recommendation(question, history)
    assert plan is not None
    assert plan.continuation is True
    assert plan.genres == ("喜劇",)
    assert plan.excluded_movie_ids == ("a", "b", "c")


@pytest.mark.parametrize(
    "question",
    (
        "movies starring Stephen Chow and Jackie Chan",
        "films directed by John Woo and Wong Kar Wai",
    ),
)
def test_strong_coordinated_person_catalog_is_owned_before_release_resolution(
    question: str,
) -> None:
    assert retrieval.has_multiple_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "movies with Stephen Chow and Jackie Chan",
        "films featuring Stephen Chow and Jackie Chan",
        "movies with Hong Kong and martial arts",
        "movies with martial arts and award winning performances",
        "movies featuring martial arts and family friendly stories",
    ),
)
def test_tentative_coordinated_catalog_requires_release_authority(
    question: str,
) -> None:
    assert not retrieval.has_multiple_person_catalog_intent(question)
    assert retrieval.has_tentative_person_catalog_intent(question)


@pytest.mark.parametrize(
    "question",
    (
        "movies starring Stephen Chow after 1990",
        "movies starring Stephen Chow since 1990",
        "movies starring Stephen Chow before 2000",
        "films directed by Wong Kar Wai after 1990",
        "movies with Stephen Chow after 1990",
        "films featuring Stephen Chow before 2000",
        "movies starring Stephen Chow between 1990 and 2000",
    ),
)
def test_person_catalog_name_boundary_reuses_modeled_year_starters(
    question: str,
) -> None:
    assert (
        retrieval.has_explicit_person_catalog_intent(question)
        or retrieval.has_tentative_person_catalog_intent(question)
    )
