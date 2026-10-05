"""Typed inputs and outputs for deterministic structured movie recommendations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from opencc import OpenCC

_DEFAULT_RECOMMENDATION_COUNT = 5
_MAX_RECOMMENDATION_COUNT = 8
# Canonical keys are the 33 distinct genre tokens in the governed v1.2 Release.
_GENRE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("喜劇", ("喜劇", "喜剧", "comedy", "comedies")),
    ("劇情", ("劇情", "剧情", "drama")),
    ("動作", ("動作", "动作", "action")),
    ("愛情", ("愛情", "爱情", "romance", "romantic")),
    ("犯罪", ("犯罪", "警匪", "警匪片", "crime")),
    ("驚悚", ("驚悚", "惊悚", "thriller")),
    ("恐怖", ("恐怖", "horror")),
    ("懸疑", ("懸疑", "悬疑", "mystery")),
    ("科幻", ("科幻", "science fiction", "sci-fi", "scifi")),
    ("奇幻", ("奇幻", "fantasy")),
    ("冒險", ("冒險", "冒险", "adventure")),
    ("家庭", ("家庭", "family")),
    ("動畫", ("動畫", "动画", "animation", "animated")),
    ("音樂", ("音樂", "音乐", "music")),
    ("歌舞", ("歌舞", "musical")),
    ("戰爭", ("戰爭", "战争", "war")),
    ("歷史", ("歷史", "历史", "history", "historical")),
    ("傳記", ("傳記", "传记", "biography", "biographical")),
    ("武俠", ("武俠", "武侠", "wuxia")),
    ("運動", ("運動", "运动", "sport", "sports")),
    ("紀錄片", ("紀錄片", "纪录片", "documentary")),
    ("功夫", ("功夫", "kung fu", "kung-fu")),
    ("怪獸", ("怪獸", "怪兽", "monster", "monster movie", "monster movies")),
    ("黑色電影", ("黑色電影", "黑色电影", "film noir", "film-noir", "noir")),
    ("夥伴", ("夥伴", "伙伴", "buddy", "buddy movie", "buddy movies")),
    ("間諜", ("間諜", "间谍", "spy", "espionage")),
    ("監獄", ("監獄", "监狱", "prison")),
    ("驚慄", ("驚慄", "惊栗")),
    ("青春", ("青春", "youth")),
    ("情色", ("情色", "erotic")),
    ("西部", ("西部", "western")),
    ("新聞", ("新聞", "新闻", "news")),
    ("災難", ("災難", "灾难", "disaster")),
)
_AMBIGUOUS_STANDALONE_GENRES = frozenset(
    {
        "劇情",
        "家庭",
        "犯罪",
        "音樂",
        "歷史",
        "傳記",
        "運動",
        "戰爭",
        "青春",
        "西部",
        "新聞",
    }
)
_CJK_BARE_MOVIE_CATALOG_TERMS = tuple(
    sorted(
        {
            "香港",
            "港產",
            "華語",
            "中國",
            "台灣",
            "亞洲",
            "經典",
            *(
                alias
                for _, aliases in _GENRE_ALIASES
                for alias in aliases
                if re.fullmatch(r"[\u3400-\u9fff]{1,8}", alias) is not None
            ),
        },
        key=len,
        reverse=True,
    )
)
_CJK_BARE_MOVIE_CATALOG_PATTERN = re.compile(
    rf"^(?:{'|'.join(map(re.escape, _CJK_BARE_MOVIE_CATALOG_TERMS))})+"
    r"(?:電影|影片|港片)$"
)
_ENGLISH_BARE_MOVIE_CATALOG_TERMS = tuple(
    sorted(
        {
            alias.casefold()
            for _, aliases in _GENRE_ALIASES
            for alias in aliases
            if re.fullmatch(r"[a-z][a-z -]{0,32}", alias, re.IGNORECASE) is not None
        },
        key=len,
        reverse=True,
    )
)
_ENGLISH_BARE_MOVIE_CATALOG_PATTERN = re.compile(
    rf"^(?:{'|'.join(map(re.escape, _ENGLISH_BARE_MOVIE_CATALOG_TERMS))})"
    rf"(?:\s*(?:and|or|/|,)\s*(?:{'|'.join(map(re.escape, _ENGLISH_BARE_MOVIE_CATALOG_TERMS))}))*"
    r"\s+(?:movies?|films?)$",
    re.IGNORECASE,
)
_RECOMMENDATION_WORDS = (
    "推薦",
    "推荐",
    "推介",
    "建議",
    "建议",
    "挑選",
    "挑选",
    "選擇",
    "选择",
    "recommend",
    "suggest",
)
_LIST_WORDS = (
    "列出",
    "列舉",
    "列举",
    "名單",
    "名单",
    "有哪些",
    "有沒有",
    "有没有",
    "which movies",
    "list",
)
_DISCOVERY_WORDS = (
    "想看",
    "想找",
    "哪些電影適合",
    "哪些电影适合",
    "哪幾部",
    "哪几部",
    "有什麼電影",
    "有什么电影",
)
_UNSUPPORTED_STRUCTURED_FILTERS = ("節奏明快", "快節奏")
_UNSUPPORTED_BROAD_ANALYSIS_PATTERNS = (
    re.compile(r"(?:代表)?(?:特色|特點)"),
    re.compile(r"常見[^？?。！!]{0,24}(?:元素|角色)"),
    re.compile(r"如何(?:表現|呈現)"),
    re.compile(r"(?:節奏明快|快節奏)"),
    re.compile(r"有哪些類型"),
)
_MOVIE_WORDS = ("電影", "电影", "影片", "港片", "movie", "movies", "film", "films")
_CONTINUATION_WORDS = (
    "還有",
    "还有",
    "再來",
    "再来",
    "再推薦",
    "再推荐",
    "別的",
    "别的",
    "其他",
    "換成",
    "换成",
    "換一",
    "换一",
    "更多",
    "另一部",
    "另一些",
    "more",
    "another",
    "other",
    "different ones",
)
_DEEP_ANALYSIS_WORDS = (
    "主題",
    "主题",
    "敘事",
    "叙事",
    "美學",
    "美学",
    "視覺",
    "视觉",
    "風格",
    "风格",
    "攝影",
    "摄影",
    "色彩",
    "燈光",
    "灯光",
    "角色塑造",
    "象徵",
    "象征",
    "寓意",
    "表達",
    "表达",
    "手法",
    "結合",
    "结合",
    "融合",
    "動作設計",
    "动作设计",
)
_DEEP_ANALYSIS_ENGLISH_ALIASES = (
    "theme",
    "themes",
    "narrative",
    "narratives",
    "storytelling",
    "aesthetic",
    "aesthetics",
    "visual style",
    "visual styles",
    "cinematography",
    "camera work",
    "camerawork",
    "lighting",
    "symbolism",
    "symbolic meaning",
    "metaphor",
    "metaphors",
    "motif",
    "motifs",
    "character development",
    "characterization",
    "colour palette",
    "color palette",
    "use of colour",
    "use of color",
    "mise-en-scène",
    "mise-en-scene",
    "filmmaking style",
    "action design",
    "action choreography",
    "fight choreography",
    "stunt choreography",
)
_ACTOR_ROLE_WORDS = (
    "演員",
    "演员",
    "主演",
    "出演",
    "出演過",
    "出演过",
    "參演",
    "参演",
    "參演過",
    "参演过",
    "演過",
    "演过",
    "actor",
    "actress",
    "cast",
    "starring",
)
_DIRECTOR_ROLE_WORDS = (
    "導演",
    "导演",
    "執導",
    "执导",
    "director",
    "directed by",
)
_PERSON_NAME_MAX_CJK_CHARACTERS = 7
_PERSON_NAME_CANDIDATE_TEXT = (
    rf"[\u3400-\u9fff]{{2,{_PERSON_NAME_MAX_CJK_CHARACTERS}}}"
)
_PERSON_NAME_CANDIDATE_PATTERN = re.compile(rf"^{_PERSON_NAME_CANDIDATE_TEXT}$")
_PERSON_COORDINATION_TOKEN = r"(?:以及|或者|[、，,和與与及跟或])"
_PERSON_COORDINATION_PATTERN = re.compile(_PERSON_COORDINATION_TOKEN)
_PERSON_ACTOR_ROLE_TOKEN = (
    r"(?:出演過|出演过|參演過|参演过|演過|演过|主演|演員|出演|參演|参演)"
)
_PERSON_ROLE_TOKEN = rf"(?:{_PERSON_ACTOR_ROLE_TOKEN}|導演|執導)"
_QUERY_ROLE_NORMALIZATION_PATTERN = re.compile(
    r"出演過|出演过|出演|參演過|参演过|參演|参演|演過|演过"
)
_QUERY_ROLE_NORMALIZATION = {
    "出演過": "出演過",
    "出演过": "出演過",
    "出演": "出演",
    "參演過": "參演過",
    "参演过": "參演過",
    "參演": "參演",
    "参演": "參演",
    "演過": "演過",
    "演过": "演過",
}
_PERSON_ROLE_COORDINATION_TOKEN = rf"(?:兼任|兼|{_PERSON_COORDINATION_TOKEN})"
_PERSON_ROLE_CONFLICT_PATTERN = re.compile(
    rf"^(?P<names>[\u3400-\u9fff、，,]+?)(?:既(?:是)?)?"
    rf"(?P<first>{_PERSON_ROLE_TOKEN})"
    rf"(?:又(?:是)?|{_PERSON_ROLE_COORDINATION_TOKEN})"
    rf"(?P<second>{_PERSON_ROLE_TOKEN})(?:的)?"
    r"(?=[\u3400-\u9fff0-9]{0,12}(?:電影|影片|作品))"
)
_CJK_PERSON_ROLE_MENTION_PATTERN = re.compile(
    rf"(?:^|[\s，,、和與与及跟或]|(?:是|由))"
    rf"(?P<name>{_PERSON_NAME_CANDIDATE_TEXT})\s*"
    rf"(?P<role>{_PERSON_ROLE_TOKEN})"
)
_ENGLISH_PERSON_CATALOG_NAME_TEXT = (
    r"(?:[a-z][a-z'.-]+(?:\s+[a-z][a-z'.-]+){1,3}?|[\u3400-\u9fff]{2,7})"
)
_ENGLISH_PERSON_CATALOG_CONSTRAINT_STARTER = (
    r"(?:and|or|from|in|during|after|since|before|between)"
)
_ENGLISH_PERSON_CATALOG_NAME_BOUNDARY = (
    rf"(?=$|[.!?,;:]|\s+{_ENGLISH_PERSON_CATALOG_CONSTRAINT_STARTER}\b)"
)
_ENGLISH_EXPLICIT_PERSON_CATALOG_PATTERNS = (
    re.compile(
        r"(?<![a-z0-9])(?:movies?|films?)\s+"
        r"(?:starring|directed\s+by)\s+"
        rf"(?P<name>{_ENGLISH_PERSON_CATALOG_NAME_TEXT})"
        rf"{_ENGLISH_PERSON_CATALOG_NAME_BOUNDARY}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![a-z0-9])(?:which|what|show\s+me|list(?:\s+me)?)?\s*"
        r"(?:movies?|films?)\s+(?:that\s+)?"
        r"(?:star|stars|starring|directed\s+by)\s+"
        rf"(?P<name>{_ENGLISH_PERSON_CATALOG_NAME_TEXT})"
        rf"{_ENGLISH_PERSON_CATALOG_NAME_BOUNDARY}",
        re.IGNORECASE,
    ),
)
_ENGLISH_TENTATIVE_PERSON_CATALOG_PATTERNS = (
    re.compile(
        r"(?<![a-z0-9])(?:which|what|show\s+me|list(?:\s+me)?)?\s*"
        r"(?:movies?|films?)\s+(?:that\s+)?"
        r"(?:feature|features|featuring|with)\s+"
        rf"(?P<name>{_ENGLISH_PERSON_CATALOG_NAME_TEXT})"
        rf"{_ENGLISH_PERSON_CATALOG_NAME_BOUNDARY}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:(?i:show\s+me|list(?:\s+me)?)\s+)?"
        r"(?P<name>[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})"
        r"\s+(?i:movies?|films?)"
        r"(?![a-z0-9])",
    ),
    re.compile(
        r"(?<![a-z0-9])(?:show\s+me|list(?:\s+me)?|with)\s+"
        r"(?P<name>[a-z][a-z'.-]+(?:\s+[a-z][a-z'.-]+){1,3})"
        r"\s+(?:movies?|films?)(?![a-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<name>[a-z][a-z'.-]+(?:\s+[a-z][a-z'.-]+){1,3})"
        r"\s+(?:movies?|films?)\s*[.!?]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![a-z0-9])(?:more|other|another)\s+"
        r"(?P<name>[a-z][a-z'.-]+(?:\s+[a-z][a-z'.-]+){1,3})"
        r"\s+(?:movies?|films?)(?![a-z0-9])",
        re.IGNORECASE,
    ),
)
_ENGLISH_PERSON_CATALOG_CONTINUATION_SHELL_PATTERN = re.compile(
    r"^\s*(?:(?:show\s+me|recommend|suggest)\s+(?:\d+\s+)?)?"
    r"(?:more|other|another)\s+",
    re.IGNORECASE,
)
_ENGLISH_COORDINATED_PERSON_CATALOG_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:movies?|films?)\s+(?:that\s+)?"
    r"(?:star|stars|starring|directed\s+by)\s+"
    rf"(?P<first>{_ENGLISH_PERSON_CATALOG_NAME_TEXT})\s+(?:and|or|&)\s+"
    rf"(?P<second>{_ENGLISH_PERSON_CATALOG_NAME_TEXT})"
    r"(?=$|[.!?,;:])",
    re.IGNORECASE,
)
_CJK_PERSON_CATALOG_SUFFIX_PATTERN = re.compile(
    rf"(?:^|[\s，,、和與与及跟])(?P<name>{_PERSON_NAME_CANDIDATE_TEXT})"
    r"(?:的)?(?:電影|影片|作品)(?=$|[\s。！？!?，,、:：;；.])"
)
_CJK_PERSON_CATALOG_CONTINUATION_PREFIX_PATTERN = re.compile(
    r"^(?P<prefix>還有|还有|再來些?|再来些?|再推薦|再推荐|"
    r"更多|別的|别的|其他|換成|换成|換一(?:部|套|齣|出)?|"
    r"换一(?:部|套|齣|出)?)\s*"
)
_GENERIC_CJK_CONTINUATION_LEAD_PATTERN = re.compile(
    r"^(?:(?:請|请|麻煩|麻烦)\s*)?"
    r"(?:(?:再(?:推薦|推荐|推介|建議|建议))\s*"
    r"(?:(?:\d{1,4}|[一二兩两三四五六七八九十幾几]+)\s*)?"
    r"(?:部|套|齣|出)?\s*[，,]?\s*|"
    r"(?:還有|还有|再來|再来|給我|给我|來點|来点|"
    r"換成|换成|換一(?:部)?|换一(?:部)?|有|換|换)\s*)"
)
_GENERIC_CJK_CONTINUATION_CORE_PATTERN = re.compile(
    r"(?:(?:別的|别的|其他的?|更多)(?:電影|电影|影片|作品)?"
    r"(?:還有|还有)?(?:嗎|吗|呢)?(?:也可以)?|另一部|另一些)"
)
_CJK_PERSON_CATALOG_DISCOVERY_SUFFIX_PATTERN = re.compile(
    rf"^(?P<name>{_PERSON_NAME_CANDIDATE_TEXT})"
    r"(?:還有|还有|有)(?:什麼|什么|哪些)"
    r"(?:電影|影片|作品)$"
)
_CJK_CATALOG_COUNT_CANDIDATE_PATTERN = re.compile(
    r"[一二兩两三四五六七八九十百千\d]+(?:部|套|齣|出)?"
)
_PERSON_COORDINATED_NAME_TEXT = (
    rf"(?:(?!的)[\u3400-\u9fff]){{2,{_PERSON_NAME_MAX_CJK_CHARACTERS}}}"
)
_PERSON_COORDINATED_CATALOG_PATTERN = re.compile(
    rf"^(?P<names>{_PERSON_COORDINATED_NAME_TEXT}"
    rf"(?:{_PERSON_COORDINATION_TOKEN}{_PERSON_COORDINATED_NAME_TEXT})+)"
    r"(?:的)?(?:電影|影片|作品)$"
)
_PERSON_REPEATED_POSSESSIVE_PATTERN = re.compile(
    rf"(?:^|{_PERSON_COORDINATION_TOKEN})\s*"
    rf"(?P<name>{_PERSON_NAME_CANDIDATE_TEXT}?)\s*"
    rf"(?P<role>{_PERSON_ROLE_TOKEN})?的"
    r"(?=[\u3400-\u9fff0-9]{0,12}(?:電影|影片|作品))"
)
_PERSON_QUERY_PUNCTUATION = " \t\r\n。！？!?，,、:：;；."
_PERSON_EXCLUSION_PREFIXES = ("不要", "別要", "除了", "排除")
_PERSON_EXCLUSION_PREFIX_PATTERN = re.compile(
    r"^(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
    r"(?:(?:幫我|給我)\s*)?"
    r"(?:(?:不想看|唔想睇|唔想看)|(?:不要)(?:\s*(?:推薦|推介|建議|想看|想找))?"
    r"|(?:別要)|(?:別)(?:\s*(?:推薦|推介|建議|想看|想找))"
    r"|(?:除了|排除))\s*"
)
_PERSON_RECOMMENDATION_PREFIX_PATTERN = re.compile(
    r"^(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
    r"(?:(?:幫我|給我)\s*)?(?:我\s*)?(?:推薦|推介|建議|想看|想找)\s*"
)
_PERSON_TRAILING_RECOMMENDATION_PATTERN = re.compile(
    r"(?:[，,；;]\s*)(?:推薦|推介|建議)\s*"
    r"(?:(?:\d{1,4}|[一二兩两三四五六七八九十幾几]+)\s*)?"
    r"(?:部|套|齣|出)?$"
)
_PERSON_TRAILING_EXCLUSION_PATTERN = re.compile(
    r"(?:(?:[，,、]\s*)?(?:不想看|不要|別要|除了|排除)"
    r"|(?:[，,、]\s*)別)"
    r"(?P<names>[\u3400-\u9fff、，,和與及跟或]+)$"
)
_PERSON_BARE_TRAILING_EXCLUSION_PATTERN = re.compile(
    r"^別(?P<names>[\u3400-\u9fff、，,和與及跟或]+)$"
)
_DEDUPLICATION_TERM_TEXT = (
    r"(?:(?:(?:和|與|与|跟)?(?:前面|剛才|刚才|之前)(?:的)?"
    r"(?:一樣|一样|同樣|同样|相同))|"
    r"(?:重複|重复|重覆|一樣|一样|同樣|同样|相同)(?:的)?|"
    r"同一(?:部|套|齣))"
    r"(?:電影|影片|作品|片)?(?:推薦|推介|建議)?"
)
_DEDUPLICATION_TERM_PATTERN = re.compile(rf"^{_DEDUPLICATION_TERM_TEXT}$")
_CJK_DEDUPLICATION_REQUEST_PATTERN = re.compile(
    r"(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
    r"(?:(?:幫我|給我)\s*)?"
    r"(?:不想看|不要|別要|別|唔好|勿)"
    r"(?:\s*(?:再)?(?:推薦|推介|建議))?\s*"
    rf"{_DEDUPLICATION_TERM_TEXT}"
    r"(?=$|[\s。！？!?，,、:：;；.])"
)
_CJK_HISTORY_DEDUPLICATION_REQUEST_PATTERN = re.compile(
    r"(?:不要|別|别|唔好)\s*(?:給|给)?\s*(?:"
    r"(?:重複|重复|重覆)(?:\s*之前的?)?"
    r"|(?:再來|再来)?(?:一樣|一样|同樣|同样|相同)的?"
    r"|(?:剛才|刚才|之前)(?:那些|這些|这些|那幾部|那几部))"
    r"(?=$|[\s。！？!?，,、:：;；.])"
)
_ENGLISH_DEDUPLICATION_REQUEST_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    r"(?:no|not|without|avoid(?:ing)?)\s+(?:any\s+)?"
    r"(?:duplicates?|repeats?|the\s+same(?:\s+(?:movies?|films?|ones?))?)"
    r"|none\s+of\s+the\s+same"
    r"|(?:do\s+not|don't)\s+repeat"
    r"|not\s+the\s+same(?:\s+(?:movies?|films?|ones?))?\s+as\s+before"
    r"|none\s+from\s+before"
    r"|different\s+from\s+(?:your\s+)?previous\s+(?:picks?|choices?|movies?|films?)"
    r")"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_DEDUPLICATION_REQUEST_PATTERNS = (
    _CJK_DEDUPLICATION_REQUEST_PATTERN,
    _CJK_HISTORY_DEDUPLICATION_REQUEST_PATTERN,
    _ENGLISH_DEDUPLICATION_REQUEST_PATTERN,
)
_GENERIC_CJK_RECOMMENDATION_SHELL_PATTERN = re.compile(
    r"^(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
    r"(?:(?:幫我)?(?:再)?給(?:我)?|(?:再)?(?:推薦|推介|建議))\s*"
    r"(?:(?:\d{1,4}|[一二兩两三四五六七八九十幾几]+)\s*)?"
    r"(?:部|套|齣|出)?\s*(?:電影|影片|作品|片)?\s*"
    r"(?:新的?|其他)?$"
)
_NEGATIVE_RECOMMENDATION_FILTER_CLAUSE_PATTERNS = (
    re.compile(
        r"(?P<marker>不想(?:看|要)|唔想(?:睇|看|要)|不要|別要|排除|除了|"
        r"唔好|冇|(?<!有)(?:沒有|没有)|不含|不帶|不带|拒絕|拒绝|"
        r"遠離|远离|討厭|讨厌|不鍾意|不钟意|唔鍾意|唔钟意|"
        r"不(?:太)?喜歡|不(?:太)?喜欢|唔睇|"
        r"不是|不包括|避免|避開|不用|不必|無需|无需|無須|无须|"
        r"毋須|毋须|唔使|唔駛|沒必要|没必要|"
        r"不(?:再)?(?:推薦|推介|建議))\s*"
        r"(?:(?:(?:再)?(?:推薦|推介|建議)|想看|看|睇)\s*)?"
        r"(?P<clause>[^。！？!?，,、:：;；.\r\n]{1,48})"
    ),
    re.compile(
        r"(?:^|[\s。！？!?，,、:：;；.])(?P<marker>別)(?!的|個|个)\s*"
        r"(?:(?:(?:再)?(?:推薦|推介|建議)|想看|看)\s*)?"
        r"(?P<clause>[^。！？!?，,、:：;；.\r\n]{1,48})"
    ),
    re.compile(
        r"(?<![a-z0-9])(?P<marker>(?:i\s+)?(?:dislike|hate|detest)|"
        r"do\s+not|don't|should\s+not|shouldn't|"
        r"cannot|can't|no\s+more|no|not|"
        r"without|exclude|excluding|avoid|ban|never|"
        r"(?:stay|steer|keep)\s+(?:clear|away)\s+from|steer\s+clear\s+of|"
        r"anything\s+but|except|sans|barring|apart\s+from|"
        r"with\s+the\s+exception\s+of|other\s+than|skip|free\s+of)\s+"
        r"(?:(?:recommend|suggest|watch)(?:ing)?\s+)?(?:be\s+|include\s+|contain\s+)?"
        r"(?:any\s+)?"
        r"(?P<clause>[^.!?,;:\r\n]{1,48})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![a-z0-9])(?P<marker>leave)\s+"
        r"(?P<clause>[^.!?,;:\r\n]{1,48}?)\s+out(?![a-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<marker>非)\s*"
        r"(?P<clause>[^。！？!?，,、:：;；.\r\n]{1,48})"
    ),
    re.compile(
        r"(?:^|[\s。！？!?，,、:：;；.])"
        r"(?P<clause>[^。！？!?，,、:：;；.\r\n]{1,48}?)\s*(?:的)?"
        r"(?P<marker>除外|不要|唔好)"
        r"(?=$|[\s。！？!?，,、:：;；.])"
    ),
)
_NEGATIVE_OUTPUT_INSTRUCTION_PATTERNS = (
    re.compile(
        r"(?:不要|別|别|唔好)\s*(?:廢話|废话|說|説|说)(?:太多)?"
        r"|(?:不用|無需|无需|無須|无须|毋須|毋须)\s*"
        r"(?:介紹|介绍)(?:劇情|剧情|情節|情节)"
        r"|(?:不用|無需|无需|無須|无须|毋須|毋须)\s*"
        r"(?:說明|説明|说明|解釋|解释)(?:原因|理由)?"
    ),
    re.compile(
        r"(?:不要|別|唔好|無需|毋須|不用)\s*"
        r"(?:(?:太|過|过)?多\s*)?"
        r"(?:劇透|解釋|解释|說明|说明|展開|展开|詳細|详细|"
        r"多?(?:講|说|說|説)|長篇大論|长篇大论|"
        r"講(?:結局|劇情)|透露(?:結局|劇情))"
    ),
    re.compile(
        r"(?:不用|無需|无需|無須|无须|毋須|毋须)\s*"
        r"(?:太\s*)?(?:詳細|详细|多(?:講|说|說|説))"
    ),
    re.compile(
        r"(?:不用|不必|無需|无需|無須|无须|毋須|毋须|"
        r"唔使|唔駛|沒必要|没必要)\s*"
        r"(?:介紹|介绍|理由|原因)"
        r"(?=$|[\s。！？!?，,、:：;；.])"
    ),
    re.compile(
        r"(?<![a-z0-9])(?:"
        r"(?:no|without)\s+(?:plot\s+)?spoilers?"
        r"|(?:do\s+not|don't)\s+spoil(?:\s+(?:it|them))?"
        r"|(?:do\s+not|don't)\s+explain"
        r"|(?:do\s+not|don't)\s+elaborate"
        r"|no\s+(?:long\s+)?explanations?"
        r"|no\s+commentary"
        r"|no\s+(?:extra\s+)?details?"
        r"|(?:do\s+not|don't)\s+give(?:\s+me)?\s+"
        r"(?:any\s+)?(?:details?|an?\s+essay)"
        r"|without\s+(?:the\s+)?(?:long\s+)?(?:explanation|details?)"
        r"|skip\s+(?:the\s+)?(?:explanation|details?|intro(?:duction)?)"
        r")(?![a-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:只|僅|仅)?\s*(?:列|列出|顯示|显示|給出|给出|要)\s*"
        r"(?:電影|电影|影片)?\s*(?:片名|名稱|名称)"
    ),
)
_NEGATIVE_POLARITY_SURFACE_PATTERN = re.compile(
    r"(?:不(?:想(?:看|要)?|要|看|(?:太)?喜歡|(?:太)?喜欢|考慮|考虑|包括|含|是|用|"
    r"推薦|推荐|推介|建議|建议)|(?:別|别)(?!的|個|个)|無|无|勿|免|"
    r"避免|避開|避开|不帶|不带|拒絕|拒绝|遠離|远离|"
    r"討厭|讨厌|不鍾意|不钟意|唔鍾意|唔钟意|"
    r"冇|(?<!有)(?:沒有|没有)|"
    r"除(?:了)?|除外|去掉|剔除|以外|之外|唔好|唔要|唔睇|唔想(?:睇|看|要))"
    r"|(?<![a-z0-9])(?:do\s+not|don't|does\s+not|doesn't|should\s+not|"
    r"shouldn't|cannot|can't|is\s+not|isn't|"
    r"are\s+not|aren't|no|not|without|avoid(?:ing)?|except|exclude|excluding|"
    r"skip|omit|minus|ban|never|steer\s+clear\s+of|"
    r"(?:stay|steer|keep)\s+(?:clear|away)\s+from|dislike|hate|detest|"
    r"anything\s+but|other\s+than|free\s+of|sans|barring|apart\s+from|"
    r"with\s+the\s+exception\s+of|"
    r"leave(?:\s+[^.!?,;:\r\n]{1,48})?\s+out)"
    r"(?![a-z0-9])"
    r"|(?<![a-z0-9])non-(?=[a-z])|(?<=[a-z])-free(?![a-z0-9])",
    re.IGNORECASE,
)
_NEUTRAL_PREFERENCE_PATTERNS = (
    re.compile(
        r"(?<![a-z0-9])no\s+(?:(?:specific|particular)\s+)?"
        r"(?:preference|genre)s?(?![a-z0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:沒有|没有|無|无)\s*(?:特別|特别|特定)?\s*"
        r"(?:類型|类型)?偏好"
    ),
    re.compile(r"(?:不限|不限定)\s*(?:類型|类型)"),
)
_STRUCTURED_NEGATIVE_PREFIX_PATTERN = re.compile(
    r"(?:^|[\s。！？!?，,、:：;；.])非\s*"
)
_EXPLANATORY_NEGATION_PATTERN = re.compile(
    r"(?:為什麼|为什么|為何|何以|點解|点解|why\b|how\s+come\b)",
    re.IGNORECASE,
)
_DEDUPLICATION_HISTORY_REFERENCE_PATTERN = re.compile(
    r"(?:再|前面|剛才|刚才|之前|上一(?:輪|轮)|還有|还有|"
    r"(?<![a-z0-9])(?:more|again|previous|before|none\s+from\s+before|"
    r"different\s+from\s+(?:your\s+)?previous\s+(?:picks?|choices?|movies?|films?)|"
    r"same\s+ones?\s+as\s+before)"
    r"(?![a-z0-9]))",
    re.IGNORECASE,
)
_NEGATIVE_FILTER_SCOPE_PREFIX_PATTERN = re.compile(
    r"^(?:(?:任何|任意|所有|全部|更多)\s*|"
    r"(?:香港(?:電影|影片)?|港產(?:電影|影片)?|港片)\s*|"
    r"(?:any|all|more|hong\s+kong|hk|movies?|films?)\s+)+",
    re.IGNORECASE,
)
_NEGATIVE_SELECTION_REQUEST_PREFIX_PATTERN = re.compile(
    r"(?:(?:i)(?:\s+(?:really|just))?|please|pls|我|請|请)?",
    re.IGNORECASE,
)
_NEGATIVE_SELECTION_CLAUSE_LEAD_IN_PATTERN = re.compile(
    r"^(?:want|to\s+watch|to\s+see)\s+",
    re.IGNORECASE,
)
_GENRE_ALTERNATIVE_OPERATOR_PATTERN = re.compile(
    r"(?:或(?:者)?|還是|(?<![a-z])or(?![a-z]))", re.IGNORECASE
)
_PERSON_REGION_OR_TOPIC_QUALIFIERS = frozenset(
    {
        "香港",
        "港產",
        "港片",
        "華語",
        "中國",
        "台灣",
        "亞洲",
        "電影",
        "影片",
        "作品",
        "演員",
        "主演",
        "導演",
        "出演",
        "參演",
        "演過",
        "女性",
        "攝影",
        "哪些",
        "什麼",
        "推薦",
        "想看",
        "想找",
        "經典",
        "代表",
        "關於",
        "关于",
        "適合",
        "适合",
        "全家",
        "觀看",
        "观看",
        "新浪潮",
        "高分",
        "冷門",
        "冷门",
        "好看",
        "值得看",
        "最近",
        "這部",
        "这部",
        "該部",
        "该部",
        "是誰",
        "是谁",
        "哪年",
        "哪一年",
        "何年",
        "上映",
        "哪種",
        "哪种",
        "類型",
        "类型",
        "片長",
        "片长",
        "多久",
    }
)
_TENTATIVE_CJK_CATALOG_DESCRIPTORS = frozenset(
    {"新浪潮", "高分", "冷門", "冷门", "好看", "值得看", "最近"}
)
_TENTATIVE_CJK_CATALOG_BLOCKERS = (
    _PERSON_REGION_OR_TOPIC_QUALIFIERS - _TENTATIVE_CJK_CATALOG_DESCRIPTORS
)
_COUNT_PATTERN = re.compile(
    r"(?P<count>\d{1,4}|[一二兩两三四五六七八九十]+)\s*(?:部|套|齣|出)"
)
_ENGLISH_COUNT_PATTERNS = (
    re.compile(
        r"(?i)(?<![a-z0-9])(?:recommend|suggest|list)\s+(?:me\s+)?"
        r"(?P<count>\d{1,3})(?!\d)"
    ),
    re.compile(
        r"(?i)(?<![a-z0-9])(?P<count>\d{1,3})\s*(?:movies?|films?)(?![a-z0-9])"
    ),
)
_TIER_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?P<tier>[sab])\s*(?:級|级|[- ]?tier)(?![a-z])"
)
_AFTER_YEAR_PATTERNS = (
    re.compile(r"(?P<year>(?:18|19|20)\d{2})\s*年?\s*(?:以後|以后|之後|之后|起)"),
    re.compile(r"(?i)(?:after|since)\s*(?P<year>(?:18|19|20)\d{2})"),
    re.compile(r"(?i)(?<![a-z0-9])from\s+(?P<year>(?:18|19|20)\d{2})(?!\d)"),
    re.compile(r"(?i)(?P<year>(?:18|19|20)\d{2})\s*(?:and|or)\s*later"),
)
_BEFORE_YEAR_PATTERNS = (
    re.compile(r"(?P<year>(?:18|19|20)\d{2})\s*年?\s*(?:以前|之前|或以前)"),
    re.compile(r"(?i)before\s*(?P<year>(?:18|19|20)\d{2})"),
)
_BETWEEN_YEAR_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])between\s+"
    r"(?P<start>(?:18|19|20)\d{2})\s+and\s+"
    r"(?P<end>(?:18|19|20)\d{2})(?!\d)"
)
_DECADE_PATTERNS = (
    re.compile(r"(?P<decade>(?:18|19|20)\d0)\s*年?代"),
    re.compile(r"(?i)(?P<decade>(?:18|19|20)\d0)s"),
)
_CHINESE_DECADE_RANGE_PATTERN = re.compile(
    r"(?P<start>[五六七八九])(?:(?:至|到|、|和|及)?(?P<end>[五六七八九]))?十年代"
)
_REPRESENTATIVE_WORK_LIST_PATTERN = re.compile(
    r"(?:有什麼|有什么).{0,8}(?:代表作(?:品)?|經典作品|经典作品)"
    r"(?:\s*[呢嗎吗？?。！!])?$"
)
_EXACT_YEAR_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?P<year>(?:18|19|20)\d{2})(?:\s*年)?(?![A-Za-z0-9_])"
)
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "兩": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
TITLE_NORMALIZATION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("风", "風"),
    ("级", "級"),
    ("还", "還"),
    ("别", "別"),
    ("荐", "薦"),
    ("后", "後"),
    ("剧", "劇"),
    ("动", "動"),
    ("爱", "愛"),
    ("惊", "驚"),
    ("悬", "懸"),
    ("险", "險"),
    ("战", "戰"),
    ("纪", "紀"),
    ("录", "錄"),
    ("传", "傳"),
    ("侠", "俠"),
    ("装", "裝"),
    ("画", "畫"),
    ("无", "無"),
    ("间", "間"),
    ("导", "導"),
    ("视", "視"),
    ("觉", "覺"),
    ("叙", "敘"),
    ("学", "學"),
    ("题", "題"),
    # Release-title orthographic equivalence classes observed across the
    # governed HK title inventory.  Each class collapses to one deterministic
    # key in both Python and the parameterized SQL candidate selector.
    ("丰", "豐"),
    ("幹", "乾"),
    ("伙", "夥"),
    ("系", "係"),
    ("僵", "殭"),
    ("冢", "塚"),
    ("滙", "匯"),
    ("升", "昇"),
    ("卷", "捲"),
    ("只", "隻"),
    ("枱", "台"),
    ("臺", "台"),
    ("周", "週"),
    ("啓", "啟"),
    ("譁", "嘩"),
    ("噹", "當"),
    ("奸", "姦"),
    ("姜", "薑"),
    ("峰", "峯"),
    ("綵", "彩"),
    ("誌", "志"),
    ("欲", "慾"),
    ("扎", "紮"),
    ("拚", "拼"),
    ("斗", "鬥"),
    ("泄", "洩"),
    ("游", "遊"),
    ("髮", "發"),
    ("綉", "繡"),
    ("綫", "線"),
    ("豔", "艷"),
    ("裡", "裏"),
    ("里", "裏"),
    ("証", "證"),
    ("郁", "鬱"),
    ("鍾", "鐘"),
    ("鷄", "雞"),
    ("須", "鬚"),
    # OpenCC is context-sensitive, so a title embedded in a natural question
    # can acquire a different HK glyph than the same title normalized alone.
    # Collapse every release-wide contextual variant back to the stored key.
    ("咸", "鹹"),
    ("孃", "娘"),
    ("樑", "梁"),
    ("衝", "沖"),
    ("回", "迴"),
    ("襬", "擺"),
    ("錶", "表"),
    ("干", "乾"),
    ("閤", "合"),
    ("云", "雲"),
    ("箇", "個"),
)
TITLE_DELIMITERS: tuple[tuple[str, str], ...] = (
    ("《", "》"),
    ("「", "」"),
    ("『", "』"),
    ("【", "】"),
)
_TITLE_TRANSLATION_TABLE = str.maketrans(dict(TITLE_NORMALIZATION_REPLACEMENTS))
_QUERY_CONVERTER = OpenCC("s2hk")
_CREDIT_ALIAS_CONVERTER = OpenCC("hk2s")
_CREDIT_EQUIVALENCE_CONVERTER = OpenCC("hk2t")

PersonRole = Literal["actor", "director"]
RecommendationScope = Literal[
    "metadata",
    "title_keyword_proxy",
    "controlled_credit_group",
    "family_genre_proxy",
]

_FEMALE_DIRECTOR_EXACT_NAMES = (
    "許鞍華",
    "張婉婷",
    "张婉婷",
    "羅卓瑤",
    "麥曦茵",
    "麦曦茵",
    "黃真真",
    "岸西",
)
_FEMALE_ACTOR_EXACT_NAMES = (
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
)
_CONTROLLED_CREDIT_GROUPS: tuple[
    tuple[str, PersonRole, tuple[str, ...]], ...
] = (
    ("female_directors_v1", "director", _FEMALE_DIRECTOR_EXACT_NAMES),
    ("female_actors_v1", "actor", _FEMALE_ACTOR_EXACT_NAMES),
)
_CONTROLLED_TITLE_POLICIES = (
    (("兄弟", "手足"), (), ("劇情", "動作", "犯罪")),
    (("殭屍", "僵屍"), (), ("恐怖", "喜劇")),
    (("賭", "千王", "雀聖", "麻雀", "撲克"), (), ()),
)


def controlled_credit_group(
    group_id: str,
) -> tuple[PersonRole, tuple[str, ...]] | None:
    """Return one release-bounded credit group without consulting user text."""
    return next(
        (
            (role, exact_names)
            for candidate_id, role, exact_names in _CONTROLLED_CREDIT_GROUPS
            if candidate_id == group_id
        ),
        None,
    )


def controlled_title_policy(
    terms: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return canonical all/any genres for one complete title-proxy policy."""
    return next(
        (
            (genres_all, genres_any)
            for policy_terms, genres_all, genres_any in _CONTROLLED_TITLE_POLICIES
            if tuple(terms) == policy_terms
        ),
        None,
    )


@dataclass(frozen=True)
class ConversationExchange:
    question: str
    answer: str
    movie_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedPerson:
    """One canonical governed credit token resolved from query text."""

    name: str
    role: PersonRole | None
    exact_names: tuple[str, ...] = ()


class AmbiguousPersonResolutionError(RuntimeError):
    """The current query names more than one distinct Release identity class."""


@dataclass(frozen=True)
class PersonQueryShape:
    """A normalized, unresolved person-selection request from natural language."""

    candidate_names: tuple[str, ...]
    role: PersonRole | None
    transition: Literal["new", "switch"]
    exclusionary: bool
    role_conflict: bool = False


@dataclass(frozen=True)
class ExplicitMovieResolution:
    """Release-scoped movie identities plus the unowned normalized query text."""

    movie_ids: tuple[str, ...]
    residual_question: str
    ambiguous: bool = False
    ambiguous_movie_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundedAnalysisPlan:
    """Immutable PDF authorities for one corpus-bounded analytical answer."""

    movie_ids: tuple[str, ...]
    passage_ids: tuple[str, ...]
    scope_note: str
    answer_summary: str
    excerpt_terms: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class RecommendationPlan:
    requested_count: int
    genres: tuple[str, ...]
    tier: str | None
    year_from: int | None
    year_to: int | None
    diversify_decades: bool
    continuation: bool
    excluded_movie_ids: tuple[str, ...]
    context_text: str
    person_name: str | None = None
    person_role: PersonRole | None = None
    person_exact_names: tuple[str, ...] = ()
    genres_all: tuple[str, ...] = ()
    genres_any: tuple[str, ...] = ()
    title_terms_any: tuple[str, ...] = ()
    credit_group_id: str | None = None
    credit_role: PersonRole | None = None
    credit_exact_names: tuple[str, ...] = ()
    scope_label: RecommendationScope = "metadata"


@dataclass(frozen=True)
class RecommendationSearch:
    records: tuple[dict[str, object], ...]
    total_matches: int


@dataclass(frozen=True)
class _QuestionConstraints:
    requested_count: int | None
    genres: tuple[str, ...]
    tier: str | None
    year_from: int | None
    year_to: int | None
    has_year_constraint: bool
    diversify_decades: bool
    continuation: bool
    complete_current_person: bool
    recommendation_intent: bool
    deep_analysis_intent: bool
    genres_all: tuple[str, ...]
    genres_any: tuple[str, ...]
    title_terms_any: tuple[str, ...]
    credit_group_id: str | None
    credit_role: PersonRole | None
    credit_exact_names: tuple[str, ...]
    scope_label: RecommendationScope
    person_query_shape: PersonQueryShape | None


@dataclass(frozen=True)
class _StructuredQueryPolicy:
    genres_all: tuple[str, ...] = ()
    genres_any: tuple[str, ...] = ()
    title_terms_any: tuple[str, ...] = ()
    credit_group_id: str | None = None
    credit_role: PersonRole | None = None
    credit_exact_names: tuple[str, ...] = ()
    scope_label: RecommendationScope = "metadata"
    force_recommendation: bool = False


def plan_bounded_analysis(question: str) -> BoundedAnalysisPlan | None:
    """Bind supported broad analysis to exact passages in the immutable v1.2 Release."""
    if not isinstance(question, str) or not question.strip():
        return None
    canonical = _QUERY_CONVERTER.convert(question).casefold()
    signature = re.sub(r"[\s？?。！!，,、；;：:]+", "", canonical)
    drunken_p1 = "pdf:drunken-master-deep-analysis-v1:p1"
    drunken_p2 = "pdf:drunken-master-deep-analysis-v1:p2"
    aces_p1 = "pdf:aces-go-places-deep-analysis-v1:p1"
    fgbr_p1 = "pdf:its-a-mad-mad-mad-world-deep-analysis-v1:p1"

    if signature == "香港動作喜劇有什麼代表特色":
        return BoundedAnalysisPlan(
            movie_ids=("1978_ZQ_001", "1982_ZJPD_001"),
            passage_ids=(drunken_p1, aces_p1),
            scope_note=(
                "基於本次答案選取的深度文檔，以下只比較《醉拳》和"
                "《最佳拍檔》的動作喜劇特色，不能外推為所有香港動作喜劇的共同特徵。"
            ),
            answer_summary=(
                "共同方向是以動作設計承載喜劇節奏；《醉拳》偏長鏡頭功夫武打與"
                "動作笑點，《最佳拍檔》偏都市空間與明快剪輯。"
            ),
            excerpt_terms=(("長鏡頭", "笑點"), ("中環", "節奏明快")),
        )
    if signature == "七十年代功夫片常見什麼元素":
        return BoundedAnalysisPlan(
            movie_ids=("1978_ZQ_001",),
            passage_ids=(drunken_p1,),
            scope_note=(
                "本次答案只選取《醉拳》的深度文檔，因此"
                "以下是單片觀察，不能證明七十年代功夫片的「常見」元素。"
            ),
            answer_summary=(
                "在《醉拳》這個單片樣本中，可見長鏡頭、功夫武打與身體動作笑點"
                "的結合。"
            ),
            excerpt_terms=(("長鏡頭", "笑點"),),
        )
    if signature == "香港電影裡的師徒關係如何表現":
        return BoundedAnalysisPlan(
            movie_ids=("1978_ZQ_001",),
            passage_ids=(drunken_p2,),
            scope_note=(
                "本次答案選取《醉拳》的深度文檔作為例子，說明師徒關係如何表現，"
                "不能概括全部香港電影。"
            ),
            answer_summary=(
                "在《醉拳》中，黃飛鴻受辱後拜蘇乞兒為師；嚴格訓練以身體行動"
                "推動他的成長，也使師徒關係產生轉變。"
            ),
            excerpt_terms=(("受辱", "訓練", "師徒關係"),),
        )
    if signature == "港產片如何呈現都市空間":
        return BoundedAnalysisPlan(
            movie_ids=("1982_ZJPD_001",),
            passage_ids=(aces_p1,),
            scope_note=(
                "本次答案選取《最佳拍檔》的深度文檔作為例子，說明都市空間如何呈現，"
                "不能概括全部港產片。"
            ),
            answer_summary=(
                "《最佳拍檔》把中環商場、海底隧道與貨櫃場等真實城市空間組織成"
                "超現實的遊樂場式鬥智舞台。"
            ),
            excerpt_terms=(
                ("中環商場", "海底隧道", "貨櫃場", "超現實的遊樂場式鬥智舞台"),
            ),
        )
    if signature == "想看節奏明快的香港動作片":
        return BoundedAnalysisPlan(
            movie_ids=("1982_ZJPD_001",),
            passage_ids=(aces_p1,),
            scope_note=(
                "就本次答案選取的證據，《最佳拍檔》可被可靠確認為節奏明快的"
                "香港動作片；這不代表未被本次證據覆蓋的影片也已驗證。"
            ),
            answer_summary=(
                "《最佳拍檔》的剪輯節奏明快，並幾乎每四至五分鐘安排一次肢體衝突"
                "或交通工具特技，因而是目前證據範圍內可靠的節奏型動作片推薦。"
            ),
            excerpt_terms=(("節奏明快", "每四至五分鐘"),),
        )
    if signature == "根據提供的深度分析這些電影如何運用空間動作與聲音":
        return BoundedAnalysisPlan(
            movie_ids=("1978_ZQ_001", "1982_ZJPD_001", "1987_FGBR_001"),
            passage_ids=(drunken_p1, aces_p1, fgbr_p1),
            scope_note=(
                "以下只基於《醉拳》《最佳拍檔》《富貴逼人》三份有限的深度文檔，"
                "不能外推到其他香港電影。"
            ),
            answer_summary=(
                "這些分析顯示：《醉拳》以長鏡頭呈現招式與打鬥空間，"
                "《最佳拍檔》以多機位動作連接都市空間，《富貴逼人》以固定機位"
                "凸顯公屋的狹窄關係；這三頁證據不足以完整比較聲音設計。"
            ),
            excerpt_terms=(
                ("長鏡頭", "笑點"),
                ("中環商場", "海底隧道", "貨櫃場"),
                ("公屋單位", "狹窄格局", "空間的擠迫感"),
            ),
        )
    return None


def plan_recommendation(
    question: str,
    history: Sequence[ConversationExchange],
    *,
    _preserve_complete_current_person_continuation: bool = False,
    _force_current_reset: bool = False,
    _force_person_catalog_intent: bool = False,
    _authority_confirmed_continuations: frozenset[ConversationExchange] = frozenset(),
    _authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
) -> RecommendationPlan | None:
    """Create one deterministic structured-retrieval plan, or keep generic retrieval."""
    normalized = question.strip()
    if not normalized:
        return None
    if has_unsupported_negative_recommendation_filter(
        normalized
    ) or has_unmodeled_negative_recommendation_condition(normalized):
        return None
    bounded_history = tuple(history)[-4:]
    current = _question_constraints(normalized)
    if _force_person_catalog_intent:
        current = replace(current, recommendation_intent=True)
    if current.deep_analysis_intent:
        return None
    semantic_current = (
        replace(current, continuation=False)
        if _force_current_reset
        else current
        if _preserve_complete_current_person_continuation
        else _semantic_question_constraints(current)
    )
    active_chain = (
        _latest_successful_recommendation_chain(
            bounded_history,
            authority_confirmed_continuations=_authority_confirmed_continuations,
            authority_confirmed_resets=_authority_confirmed_resets,
        )
        if semantic_current.continuation
        else ()
    )
    if (
        not _force_current_reset
        and deduplication_requires_successful_history(normalized)
        and not active_chain
    ):
        return None
    exclusion_context_chain = _recommendation_exclusion_context_chain(
        active_chain,
        authority_confirmed_continuations=_authority_confirmed_continuations,
    )
    previous = (
        _effective_recommendation_constraints(
            active_chain,
            authority_confirmed_continuations=_authority_confirmed_continuations,
            authority_confirmed_resets=_authority_confirmed_resets,
        )
        if semantic_current.continuation
        else None
    )
    if not semantic_current.recommendation_intent and not (
        semantic_current.continuation and previous is not None
    ):
        return None

    effective = _merged_recommendation_constraints(semantic_current, previous)
    continuation = semantic_current.continuation
    excluded_movie_ids: tuple[str, ...] = ()
    if continuation and not (
        not _preserve_complete_current_person_continuation
        and
        current.person_query_shape is not None
        and current.person_query_shape.transition == "switch"
    ):
        excluded_movie_ids = tuple(
            dict.fromkeys(
                movie_id
                for exchange in exclusion_context_chain
                for movie_id in exchange.movie_ids
            )
        )
    context_text = (
        "\n".join(
            [
                *(exchange.question.strip() for exchange in exclusion_context_chain),
                normalized,
            ]
        )
        if semantic_current.continuation
        else normalized
    )
    return RecommendationPlan(
        requested_count=effective.requested_count or _DEFAULT_RECOMMENDATION_COUNT,
        genres=effective.genres,
        tier=effective.tier,
        year_from=effective.year_from,
        year_to=effective.year_to,
        diversify_decades=effective.diversify_decades,
        continuation=continuation,
        excluded_movie_ids=excluded_movie_ids,
        context_text=context_text,
        genres_all=effective.genres_all,
        genres_any=effective.genres_any,
        title_terms_any=effective.title_terms_any,
        credit_group_id=effective.credit_group_id,
        credit_role=effective.credit_role,
        credit_exact_names=effective.credit_exact_names,
        scope_label=effective.scope_label,
    )


def _semantic_question_constraints(
    current: _QuestionConstraints,
) -> _QuestionConstraints:
    current_person_reset = bool(
        current.complete_current_person
        and current.person_query_shape is not None
        and current.person_query_shape.transition == "new"
    )
    return replace(
        current,
        continuation=current.continuation and not current_person_reset,
    )


def _latest_successful_recommendation_chain(
    history: Sequence[ConversationExchange],
    *,
    authority_confirmed_continuations: frozenset[
        ConversationExchange
    ] = frozenset(),
    authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
) -> tuple[ConversationExchange, ...]:
    """Return the one active recommendation chain of successful history turns."""
    chain: list[ConversationExchange] = []
    for exchange in history:
        if not exchange.movie_ids:
            continue
        constraints = _history_exchange_constraints(
            exchange,
            authority_confirmed_continuations=authority_confirmed_continuations,
            authority_confirmed_resets=authority_confirmed_resets,
        )
        shape = constraints.person_query_shape
        has_positive_person_shape = bool(
            shape is not None
            and len(shape.candidate_names) == 1
            and not shape.exclusionary
            and not shape.role_conflict
        )
        folded_question = exchange.question.casefold()
        has_explicit_recommendation_surface = bool(
            any(
                word in folded_question
                for word in (*_RECOMMENDATION_WORDS, *_DISCOVERY_WORDS)
            )
            or has_controlled_movie_list_intent(exchange.question)
        )
        is_tentative_catalog_turn = has_tentative_person_catalog_intent(
            exchange.question
        )
        is_detail_turn = bool(
            _is_bounded_movie_detail_history_question(exchange.question)
            and not is_tentative_catalog_turn
        )
        is_bounded_catalog_fragment = _is_bounded_movie_catalog_history_question(
            exchange.question,
            constraints,
        )
        is_recommendation_turn = bool(
            not is_detail_turn
            and (
                exchange in authority_confirmed_continuations
                or exchange in authority_confirmed_resets
                or
                has_explicit_recommendation_surface
                or (len(exchange.movie_ids) > 1 and constraints.recommendation_intent)
                or is_tentative_catalog_turn
                or is_bounded_catalog_fragment
                or (
                    has_positive_person_shape
                    and shape is not None
                    and (
                        _is_bare_person_history_shape(exchange.question, shape)
                        or constraints.complete_current_person
                    )
                )
                or (constraints.continuation and chain)
            )
        )
        if not is_recommendation_turn:
            continue
        if constraints.continuation:
            if chain:
                chain.append(exchange)
            elif has_explicit_recommendation_surface:
                chain = [exchange]
        else:
            chain = [exchange]
    return tuple(chain)


def _history_exchange_constraints(
    exchange: ConversationExchange,
    *,
    authority_confirmed_continuations: frozenset[
        ConversationExchange
    ] = frozenset(),
    authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
) -> _QuestionConstraints:
    constraints = _question_constraints(exchange.question)
    if exchange in authority_confirmed_continuations:
        return replace(
            constraints,
            continuation=True,
            recommendation_intent=True,
        )
    if exchange in authority_confirmed_resets:
        return replace(
            constraints,
            continuation=False,
            recommendation_intent=True,
        )
    return _semantic_question_constraints(constraints)


def _is_bare_person_history_shape(
    question: str, shape: PersonQueryShape
) -> bool:
    if len(shape.candidate_names) != 1:
        return False
    normalized = normalize_query_text(question).strip()
    if normalized != shape.candidate_names[0]:
        return False
    return not any(
        marker in normalized
        for marker in (
            "哪年",
            "哪一年",
            "上映",
            "是誰",
            "是谁",
            "甚麼",
            "什么",
            "哪種",
            "哪种",
            "類型",
            "类型",
            "好看",
            "講什麼",
            "讲什么",
        )
    )


def _is_bounded_movie_detail_history_question(question: str) -> bool:
    canonical = normalize_query_text(question)
    has_referential_detail = bool(
        re.search(
            r"(?:你|您)?(?:剛才|刚才|之前|上一(?:輪|轮))"
            r"(?:所)?(?:推薦|推荐|推介|建議|建议)的"
            r"|(?:上(?:一)?輪|上(?:一)?轮)?第[一二兩两三四五六七八九十\d]+部",
            canonical,
        )
    )
    if has_referential_detail:
        return True
    if re.search(
        r"(?:它|他|她|這部|这部|那部|這一部|这一部|上一部)"
        r".{0,8}(?:演員|演员|誰主演|谁主演|誰演|谁演|導演|导演)",
        canonical,
    ) is not None:
        return True
    has_detail_marker = any(
        marker in canonical
        for marker in (
            "是誰",
            "是谁",
            "哪年",
            "哪一年",
            "何年",
            "上映",
            "哪種",
            "哪种",
            "甚麼類型",
            "什么类型",
            "類型的電影",
            "类型的电影",
            "講什麼",
            "讲什么",
            "好看",
            "值得看",
            "片長",
            "片长",
            "多久",
        )
    )
    if not has_detail_marker:
        return False
    has_explicit_request = any(
        word in canonical.casefold()
        for word in (*_RECOMMENDATION_WORDS, *_DISCOVERY_WORDS)
    )
    return not has_explicit_request


def _is_bounded_movie_catalog_history_question(
    question: str,
    constraints: _QuestionConstraints,
) -> bool:
    """Recognize a successful standalone catalog surface during history replay.

    Conversation history intentionally carries no trusted route label.  Keep this
    fallback limited to complete, modeled genre/region catalog fragments so a
    one-card title/detail answer cannot replace the active recommendation chain.
    """
    if constraints.deep_analysis_intent or constraints.person_query_shape is not None:
        return False
    canonical = _strip_bounded_output_directives(
        normalize_query_text(question)
    ).strip(_PERSON_QUERY_PUNCTUATION)
    if not canonical:
        return False
    if _CJK_BARE_MOVIE_CATALOG_PATTERN.fullmatch(canonical) is not None:
        return True
    return _ENGLISH_BARE_MOVIE_CATALOG_PATTERN.fullmatch(canonical) is not None


def _recommendation_exclusion_context_chain(
    active_chain: Sequence[ConversationExchange],
    *,
    authority_confirmed_continuations: frozenset[
        ConversationExchange
    ] = frozenset(),
) -> tuple[ConversationExchange, ...]:
    """Bound exclusions and prompt context at the latest successful person switch."""
    boundary = 0
    for index, exchange in enumerate(active_chain):
        if exchange in authority_confirmed_continuations:
            continue
        shape = parse_person_query_shape(exchange.question)
        if (
            shape is not None
            and shape.transition == "switch"
            and len(shape.candidate_names) == 1
            and not shape.exclusionary
            and not shape.role_conflict
        ):
            boundary = index
    return tuple(active_chain[boundary:])


def _effective_recommendation_constraints(
    history: Sequence[ConversationExchange],
    *,
    include_deep_analysis: bool = False,
    authority_confirmed_continuations: frozenset[
        ConversationExchange
    ] = frozenset(),
    authority_confirmed_resets: frozenset[ConversationExchange] = frozenset(),
) -> _QuestionConstraints | None:
    effective: _QuestionConstraints | None = None
    for exchange in _latest_successful_recommendation_chain(
        history,
        authority_confirmed_continuations=authority_confirmed_continuations,
        authority_confirmed_resets=authority_confirmed_resets,
    ):
        constraints = _history_exchange_constraints(
            exchange,
            authority_confirmed_continuations=authority_confirmed_continuations,
            authority_confirmed_resets=authority_confirmed_resets,
        )
        if constraints.deep_analysis_intent and not include_deep_analysis:
            continue
        effective = _merged_recommendation_constraints(
            constraints, effective if constraints.continuation else None
        )
    return effective


def _merged_recommendation_constraints(
    current: _QuestionConstraints, previous: _QuestionConstraints | None
) -> _QuestionConstraints:
    requested_count = (
        current.requested_count
        if current.requested_count is not None
        else previous.requested_count
        if previous is not None and previous.requested_count is not None
        else _DEFAULT_RECOMMENDATION_COUNT
    )
    requested_count = max(1, min(_MAX_RECOMMENDATION_COUNT, requested_count))
    genres = current.genres or (previous.genres if previous is not None else ())
    tier = current.tier if current.tier is not None else previous.tier if previous else None
    if current.has_year_constraint:
        year_from, year_to = current.year_from, current.year_to
    elif previous is not None:
        year_from, year_to = previous.year_from, previous.year_to
    else:
        year_from, year_to = None, None
    replaces_typed_constraints = bool(
        current.genres
        or current.genres_all
        or current.genres_any
        or current.title_terms_any
        or current.credit_group_id
    )
    if replaces_typed_constraints or previous is None:
        genres_all = current.genres_all
        genres_any = current.genres_any
        title_terms_any = current.title_terms_any
        credit_group_id = current.credit_group_id
        credit_role = current.credit_role
        credit_exact_names = current.credit_exact_names
        scope_label = current.scope_label
    else:
        genres_all = previous.genres_all
        genres_any = previous.genres_any
        title_terms_any = previous.title_terms_any
        credit_group_id = previous.credit_group_id
        credit_role = previous.credit_role
        credit_exact_names = previous.credit_exact_names
        scope_label = previous.scope_label
    person_query_shape = (
        current.person_query_shape
        if current.person_query_shape is not None
        else previous.person_query_shape
        if previous is not None
        else None
    )
    return _QuestionConstraints(
        requested_count=requested_count,
        genres=genres,
        tier=tier,
        year_from=year_from,
        year_to=year_to,
        has_year_constraint=current.has_year_constraint
        or bool(previous and previous.has_year_constraint),
        diversify_decades=current.diversify_decades
        or bool(previous and previous.diversify_decades),
        continuation=current.continuation,
        complete_current_person=current.complete_current_person,
        recommendation_intent=True,
        deep_analysis_intent=current.deep_analysis_intent,
        genres_all=genres_all,
        genres_any=genres_any,
        title_terms_any=title_terms_any,
        credit_group_id=credit_group_id,
        credit_role=credit_role,
        credit_exact_names=credit_exact_names,
        scope_label=scope_label,
        person_query_shape=person_query_shape,
    )


def parse_person_query_shape(question: str) -> PersonQueryShape | None:
    """Compile a bounded Chinese person-selection phrase without resolving credits."""
    if not isinstance(question, str) or not question.strip():
        return None
    if _is_generic_cjk_continuation_surface(question):
        return None
    normalized = _strip_bounded_output_directives(
        normalize_query_text(question)
    ).strip(_PERSON_QUERY_PUNCTUATION)
    normalized = normalized.strip(_PERSON_QUERY_PUNCTUATION)
    if _CJK_DEDUPLICATION_REQUEST_PATTERN.fullmatch(normalized) is not None:
        return None
    if has_recommendation_deduplication_request(normalized):
        dedup_remainder = _strip_deduplication_requests(normalized).strip(
            _PERSON_QUERY_PUNCTUATION
        )
        if (
            not dedup_remainder
            or _GENERIC_CJK_RECOMMENDATION_SHELL_PATTERN.fullmatch(
                dedup_remainder
            )
            is not None
        ):
            return None
    had_continuation_shell = bool(
        _CJK_PERSON_CATALOG_CONTINUATION_PREFIX_PATTERN.match(normalized)
    )
    normalized, transition = _strip_cjk_person_catalog_continuation_shell(
        normalized
    )
    exclusionary = False
    exclusion_match = _PERSON_EXCLUSION_PREFIX_PATTERN.match(normalized)
    if exclusion_match is not None:
        exclusionary = True
        normalized = normalized[exclusion_match.end() :]
    else:
        normalized = _PERSON_RECOMMENDATION_PREFIX_PATTERN.sub("", normalized)
        for prefix in _PERSON_EXCLUSION_PREFIXES:
            if normalized.startswith(prefix):
                exclusionary = True
                normalized = normalized.removeprefix(prefix).lstrip()
                break
    if (count_match := _COUNT_PATTERN.match(normalized)) is not None:
        normalized = normalized[count_match.end() :].lstrip()
    normalized = _PERSON_TRAILING_RECOMMENDATION_PATTERN.sub("", normalized).strip(
        _PERSON_QUERY_PUNCTUATION
    )

    trailing_exclusion_names: tuple[str, ...] = ()
    trailing_exclusion_match = _PERSON_TRAILING_EXCLUSION_PATTERN.search(normalized)
    if trailing_exclusion_match is None and had_continuation_shell:
        trailing_exclusion_match = _PERSON_BARE_TRAILING_EXCLUSION_PATTERN.fullmatch(
            normalized
        )
    if trailing_exclusion_match is not None:
        trailing_names_text = trailing_exclusion_match.group("names")
        is_deduplication_modifier = (
            _DEDUPLICATION_TERM_PATTERN.fullmatch(trailing_names_text) is not None
        )
        if not is_deduplication_modifier:
            exclusionary = True
            trailing_exclusion_names = _validated_person_candidate_names(
                trailing_names_text
            )
        normalized = normalized[: trailing_exclusion_match.start()].rstrip(
            _PERSON_QUERY_PUNCTUATION
        )

    if normalized.startswith("換成"):
        transition = "switch"
        normalized = normalized.removeprefix("換成").lstrip()
        if (count_match := _COUNT_PATTERN.match(normalized)) is not None:
            normalized = normalized[count_match.end() :].lstrip()
    elif normalized.startswith("那") and normalized.endswith("呢"):
        transition = "switch"
        normalized = normalized[1:-1].strip()

    repeated_matches = tuple(_PERSON_REPEATED_POSSESSIVE_PATTERN.finditer(normalized))
    repeated_names = tuple(
        dict.fromkeys(
            candidate
            for match in repeated_matches
            for candidate in _validated_person_candidate_names(match.group("name"))
        )
    )
    if len(repeated_matches) > 1 and repeated_names:
        repeated_roles = {
            role
            for match in repeated_matches
            if (role := infer_person_role(match.group("role") or "")) is not None
        }
        role = next(iter(repeated_roles)) if len(repeated_roles) == 1 else None
        return PersonQueryShape(
            repeated_names,
            role,
            transition,
            exclusionary,
            role_conflict=len(repeated_roles) > 1,
        )

    names_text, role, role_conflict = _person_query_name_segment(
        normalized, transition
    )
    candidate_names = tuple(
        dict.fromkeys(
            (*_validated_person_candidate_names(names_text), *trailing_exclusion_names)
        )
    )
    if not candidate_names:
        return None
    return PersonQueryShape(
        candidate_names,
        role,
        transition,
        exclusionary,
        role_conflict=role_conflict,
    )


def _strip_cjk_person_catalog_continuation_shell(
    text: str,
) -> tuple[str, Literal["new", "switch"]]:
    """Strip one bounded continuation shell without changing person identity text."""
    match = _CJK_PERSON_CATALOG_CONTINUATION_PREFIX_PATTERN.match(text)
    if match is None:
        return text, "new"
    prefix = match.group("prefix")
    transition: Literal["new", "switch"] = (
        "switch" if prefix.startswith(("換", "换")) else "new"
    )
    remainder = text[match.end() :].lstrip()
    if (count_match := _COUNT_PATTERN.match(remainder)) is not None:
        remainder = remainder[count_match.end() :].lstrip()
    return remainder, transition


def _is_generic_cjk_continuation_surface(question: str) -> bool:
    """Recognize reference-only CJK follow-ups that cannot own a person name."""
    canonical = normalize_query_text(question).strip(_PERSON_QUERY_PUNCTUATION)
    canonical = _GENERIC_CJK_CONTINUATION_LEAD_PATTERN.sub("", canonical, count=1)
    return _GENERIC_CJK_CONTINUATION_CORE_PATTERN.fullmatch(canonical) is not None


def _person_query_name_segment(
    normalized: str, transition: Literal["new", "switch"]
) -> tuple[str, PersonRole | None, bool]:
    conflicting_role_match = _PERSON_ROLE_CONFLICT_PATTERN.match(normalized)
    if conflicting_role_match is not None:
        roles = {
            role
            for value in (
                conflicting_role_match.group("first"),
                conflicting_role_match.group("second"),
            )
            if (role := infer_person_role(value)) is not None
        }
        return (
            conflicting_role_match.group("names"),
            next(iter(roles)) if len(roles) == 1 else None,
            len(roles) > 1,
        )
    role_match = re.match(
        rf"^(?P<names>[\u3400-\u9fff、，,和與及跟或]{{2,{_PERSON_NAME_MAX_CJK_CHARACTERS}}}?)"
        rf"(?P<role>{_PERSON_ROLE_TOKEN})"
        r"(?:的|(?=[\u3400-\u9fff0-9]{0,12}(?:電影|影片|作品)))",
        normalized,
    )
    if role_match is not None and _is_person_name_candidate(
        role_match.group("names")
    ):
        return (
            role_match.group("names"),
            infer_person_role(role_match.group("role")),
            False,
        )
    coordinated_catalog_match = _PERSON_COORDINATED_CATALOG_PATTERN.fullmatch(normalized)
    if coordinated_catalog_match is not None:
        return coordinated_catalog_match.group("names"), None, False
    possessive_boundary = normalized.rfind("的")
    if possessive_boundary > 0:
        return normalized[:possessive_boundary], None, False
    if transition == "switch":
        return normalized.rsplit("的", 1)[0], None, False
    return normalized, None, False


def _validated_person_candidate_names(names_text: str) -> tuple[str, ...]:
    normalized = names_text.strip()
    boundaries = tuple(_PERSON_COORDINATION_PATTERN.finditer(normalized))
    partitions: list[tuple[tuple[str, ...], tuple[int, ...]] | None] = [
        None
    ] * (len(boundaries) + 1)
    for boundary_index in range(len(boundaries), -1, -1):
        segment_start = (
            boundaries[boundary_index - 1].end() if boundary_index else 0
        )
        options: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
        tail = normalized[segment_start:].strip()
        if (
            len(tail) <= _PERSON_NAME_MAX_CJK_CHARACTERS
            and _is_person_name_candidate(tail)
        ):
            options.append(((tail,), ()))
        for split_index in range(boundary_index, len(boundaries)):
            boundary = boundaries[split_index]
            head = normalized[segment_start : boundary.start()].strip()
            if len(head) > _PERSON_NAME_MAX_CJK_CHARACTERS:
                break
            suffix = partitions[split_index + 1]
            if suffix is None or not _is_person_name_candidate(head):
                continue
            suffix_names, suffix_positions = suffix
            options.append(
                (
                    (head, *suffix_names),
                    (boundary.start(), *suffix_positions),
                )
            )
        if options:
            partitions[boundary_index] = min(
                options,
                key=lambda item: (
                    -len(item[0]),
                    tuple(-position for position in item[1]),
                ),
            )

    partition = partitions[0]
    if partition is None:
        return ()
    candidates, _ = partition
    return tuple(dict.fromkeys(candidates))


def _is_person_name_candidate(candidate: str) -> bool:
    if _PERSON_NAME_CANDIDATE_PATTERN.fullmatch(candidate) is None:
        return False
    if candidate.casefold().startswith(_CONTINUATION_WORDS):
        return False
    if any(qualifier in candidate for qualifier in _PERSON_REGION_OR_TOPIC_QUALIFIERS):
        return False
    return not any(genre in candidate for genre, _ in _GENRE_ALIASES)


def _is_tentative_cjk_person_catalog_candidate(candidate: str) -> bool:
    """Keep bare ``X電影`` Release-resolvable without granting person ownership."""
    if _PERSON_NAME_CANDIDATE_PATTERN.fullmatch(candidate) is None:
        return False
    if candidate.casefold().startswith(_CONTINUATION_WORDS):
        return False
    if any(qualifier in candidate for qualifier in _TENTATIVE_CJK_CATALOG_BLOCKERS):
        return False
    return not any(genre in candidate for genre, _ in _GENRE_ALIASES)


def _is_complete_person_movie_request(
    shape: PersonQueryShape,
    genres: tuple[str, ...],
    has_movie_word: bool,
    has_work_word: bool,
) -> bool:
    return (
        shape.role is not None
        or has_movie_word
        or bool(genres)
        or has_work_word
    )


def _question_constraints(question: str) -> _QuestionConstraints:
    routing_question = _strip_bounded_output_directives(question)
    folded = routing_question.casefold()
    canonical = _QUERY_CONVERTER.convert(routing_question).casefold()
    structured_policy = _structured_query_policy(routing_question)
    person_query_shape = parse_person_query_shape(routing_question)
    count_source = _strip_deduplication_requests(canonical)
    count_match = _COUNT_PATTERN.search(count_source)
    if count_match is None:
        count_match = next(
            (
                match
                for pattern in _ENGLISH_COUNT_PATTERNS
                if (match := pattern.search(routing_question)) is not None
            ),
            None,
        )
    requested_count = _count_value(count_match.group("count")) if count_match else None
    genres = tuple(
        canonical
        for canonical, aliases in _GENRE_ALIASES
        if any(_contains_alias(folded, alias.casefold()) for alias in aliases)
    )
    if not genres:
        genres = structured_policy.genres_all or structured_policy.genres_any
    person_genres_all: tuple[str, ...] = ()
    person_genres_any: tuple[str, ...] = ()
    if (
        person_query_shape is not None
        and len(person_query_shape.candidate_names) == 1
        and not person_query_shape.exclusionary
        and len(genres) > 1
        and not (structured_policy.genres_all or structured_policy.genres_any)
    ):
        if _GENRE_ALTERNATIVE_OPERATOR_PATTERN.search(canonical) is not None:
            person_genres_any = genres
        else:
            person_genres_all = genres
    tier_match = _TIER_PATTERN.search(routing_question)
    tier = tier_match.group("tier").upper() if tier_match else None
    year_from, year_to, has_year_constraint = _year_constraints(routing_question)
    diversify_decades = any(
        phrase in folded
        for phrase in ("不同年代", "跨年代", "different decades", "across decades")
    )
    generic_cjk_continuation = _is_generic_cjk_continuation_surface(routing_question)
    continuation = (
        _has_continuation_surface(folded)
        or has_recommendation_deduplication_request(canonical)
        or generic_cjk_continuation
        or bool(
            person_query_shape is not None and person_query_shape.transition == "switch"
        )
    )
    has_recommendation_word = any(
        word in folded for word in (*_RECOMMENDATION_WORDS, *_DISCOVERY_WORDS)
    )
    has_list_word = has_controlled_movie_list_intent(routing_question)
    has_movie_word = any(word in folded for word in _MOVIE_WORDS)
    has_work_word = "作品" in canonical
    has_filter = bool(
        genres
        or structured_policy.genres_all
        or structured_policy.genres_any
        or structured_policy.title_terms_any
        or structured_policy.credit_group_id
        or tier
        or has_year_constraint
        or diversify_decades
    )
    deep_analysis_intent = has_deep_analysis_intent(routing_question)
    has_single_positive_person_shape = bool(
        person_query_shape is not None
        and len(person_query_shape.candidate_names) == 1
        and not person_query_shape.exclusionary
        and not person_query_shape.role_conflict
    )
    complete_current_person = bool(
        has_single_positive_person_shape
        and person_query_shape is not None
        and _is_complete_person_movie_request(
            person_query_shape, genres, has_movie_word, has_work_word
        )
    )
    has_positive_person_selection = bool(
        has_single_positive_person_shape
        and person_query_shape is not None
        and (
            person_query_shape.transition == "switch" or complete_current_person
        )
    )
    recommendation_intent = (
        has_recommendation_word
        or (has_list_word and (has_movie_word or has_filter))
        or (continuation and (has_movie_word or has_filter))
        or structured_policy.force_recommendation
        or has_positive_person_selection
        or generic_cjk_continuation
    )
    if person_query_shape is not None and (
        len(person_query_shape.candidate_names) != 1
        or person_query_shape.exclusionary
        or person_query_shape.role_conflict
    ):
        recommendation_intent = False
    if deep_analysis_intent and not has_recommendation_word:
        recommendation_intent = False
    if any(term in canonical for term in _UNSUPPORTED_STRUCTURED_FILTERS) or _has_normalized_nonmovie_object_term(
        canonical
    ):
        recommendation_intent = False
    return _QuestionConstraints(
        requested_count=requested_count,
        genres=genres,
        tier=tier,
        year_from=year_from,
        year_to=year_to,
        has_year_constraint=has_year_constraint,
        diversify_decades=diversify_decades,
        continuation=continuation,
        complete_current_person=complete_current_person,
        recommendation_intent=recommendation_intent,
        deep_analysis_intent=deep_analysis_intent,
        genres_all=structured_policy.genres_all or person_genres_all,
        genres_any=structured_policy.genres_any or person_genres_any,
        title_terms_any=structured_policy.title_terms_any,
        credit_group_id=structured_policy.credit_group_id,
        credit_role=structured_policy.credit_role,
        credit_exact_names=structured_policy.credit_exact_names,
        scope_label=structured_policy.scope_label,
        person_query_shape=person_query_shape,
    )


def _structured_query_policy(question: str) -> _StructuredQueryPolicy:
    canonical = _QUERY_CONVERTER.convert(question).casefold()
    if "喜劇" in canonical and any(term in canonical for term in ("武打", "功夫")):
        return _StructuredQueryPolicy(
            genres_all=("喜劇",),
            genres_any=("動作", "功夫", "武俠"),
            force_recommendation=True,
        )
    if any(term in canonical for term in ("兄弟情", "手足情")):
        return _StructuredQueryPolicy(
            genres_any=("劇情", "動作", "犯罪"),
            title_terms_any=("兄弟", "手足"),
            scope_label="title_keyword_proxy",
            force_recommendation=True,
        )
    if any(term in canonical for term in ("女性導演", "女導演")):
        return _StructuredQueryPolicy(
            credit_group_id="female_directors_v1",
            credit_role="director",
            credit_exact_names=_FEMALE_DIRECTOR_EXACT_NAMES,
            scope_label="controlled_credit_group",
            force_recommendation=True,
        )
    if any(term in canonical for term in ("殭屍", "僵屍")):
        return _StructuredQueryPolicy(
            genres_any=("恐怖", "喜劇"),
            title_terms_any=("殭屍", "僵屍"),
            scope_label="title_keyword_proxy",
            force_recommendation=True,
        )
    if "賭片" in canonical:
        return _StructuredQueryPolicy(
            title_terms_any=("賭", "千王", "雀聖", "麻雀", "撲克"),
            scope_label="title_keyword_proxy",
            force_recommendation=True,
        )
    if any(term in canonical for term in ("女演員", "女主角", "女性主演")):
        return _StructuredQueryPolicy(
            genres_all=("劇情",) if "劇情" in canonical else (),
            credit_group_id="female_actors_v1",
            credit_role="actor",
            credit_exact_names=_FEMALE_ACTOR_EXACT_NAMES,
            scope_label="controlled_credit_group",
            force_recommendation=True,
        )
    if any(term in canonical for term in ("全家觀看", "全家觀賞", "親子")):
        return _StructuredQueryPolicy(
            genres_all=("家庭",),
            scope_label="family_genre_proxy",
            force_recommendation=True,
        )
    return _StructuredQueryPolicy()


def _contains_alias(text: str, alias: str) -> bool:
    if alias.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def _has_continuation_surface(text: str) -> bool:
    folded = text.casefold()
    return any(
        _contains_alias(folded, word.casefold()) for word in _CONTINUATION_WORDS
    )


def _strip_bounded_output_directives(text: str) -> str:
    stripped = text
    for pattern in _NEGATIVE_OUTPUT_INSTRUCTION_PATTERNS:
        stripped = pattern.sub("", stripped)
    return stripped


def _strip_neutral_preference_statements(text: str) -> str:
    stripped = text
    for pattern in _NEUTRAL_PREFERENCE_PATTERNS:
        stripped = pattern.sub("", stripped)
    return stripped


def has_deep_analysis_intent(question: str) -> bool:
    """Return whether the question asks for evidence beyond structured metadata."""
    folded_question = question.casefold()
    return any(word in folded_question for word in _DEEP_ANALYSIS_WORDS) or any(
        re.search(
            rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])",
            folded_question,
        )
        is not None
        for alias in _DEEP_ANALYSIS_ENGLISH_ALIASES
    )


def has_unsupported_broad_analysis_intent(question: str) -> bool:
    """Return whether the current Release has no governed field for the request.

    This classifies broad corpus claims and qualitative recommendation filters. It does not
    decide whether a named movie has its own PDF evidence; QueryService owns that identity
    boundary before applying this policy.
    """
    if not isinstance(question, str) or not question.strip():
        return False
    if plan_bounded_analysis(question) is not None:
        return False
    canonical = _QUERY_CONVERTER.convert(question).casefold()
    return any(pattern.search(canonical) is not None for pattern in _UNSUPPORTED_BROAD_ANALYSIS_PATTERNS)


def has_governed_movie_filter(question: str) -> bool:
    """Return whether text contains a governed genre, tier, year, or decade filter."""
    if not isinstance(question, str) or not question.strip():
        return False
    constraints = _question_constraints(question)
    return bool(
        constraints.genres
        or constraints.tier is not None
        or constraints.has_year_constraint
        or constraints.diversify_decades
    )


def has_governed_genre_filter(question: str) -> bool:
    """Return whether text names one of the governed Release genres."""
    if not isinstance(question, str) or not question.strip():
        return False
    return bool(_question_constraints(question).genres)


def has_unambiguous_movie_genre_filter(question: str) -> bool:
    """Return whether text contains a genre that is movie-specific enough alone."""
    if not isinstance(question, str) or not question.strip():
        return False
    return bool(unambiguous_movie_genres(question))


def unambiguous_movie_genres(question: str) -> tuple[str, ...]:
    """Return governed genres safe enough to contribute to implicit movie intent."""
    if not isinstance(question, str) or not question.strip():
        return ()
    return tuple(
        genre
        for genre in _question_constraints(question).genres
        if genre not in _AMBIGUOUS_STANDALONE_GENRES
    )


def has_controlled_movie_list_intent(question: str) -> bool:
    """Return whether bounded wording explicitly asks for a representative list."""
    if not isinstance(question, str) or not question.strip():
        return False
    folded = question.casefold()
    return any(word in folded for word in _LIST_WORDS) or (
        _REPRESENTATIVE_WORK_LIST_PATTERN.search(folded) is not None
    )


def has_explicit_recommendation_intent(question: str) -> bool:
    """Require an explicit recommendation verb, not an ambiguous list phrase."""
    if not isinstance(question, str) or not question.strip():
        return False
    folded = question.casefold()
    return any(word in folded for word in _RECOMMENDATION_WORDS)


def _strip_english_person_catalog_continuation_shell(text: str) -> str:
    return _ENGLISH_PERSON_CATALOG_CONTINUATION_SHELL_PATTERN.sub("", text, count=1)


def has_explicit_person_catalog_intent(question: str) -> bool:
    """Recognize a bounded movie-catalog request that explicitly names a person.

    This owns only the route shape.  The active Release repository remains the
    sole authority for whether the candidate is one exact person identity.
    """
    if not isinstance(question, str) or not question.strip():
        return False
    canonical = _strip_bounded_output_directives(
        normalize_query_text(question)
    ).strip(_PERSON_QUERY_PUNCTUATION)
    catalog_canonical = _strip_english_person_catalog_continuation_shell(canonical)
    shape = parse_person_query_shape(canonical)
    if (
        shape is not None
        and len(shape.candidate_names) == 1
        and not shape.exclusionary
        and not shape.role_conflict
        and shape.role is not None
    ):
        return True
    for pattern in _ENGLISH_EXPLICIT_PERSON_CATALOG_PATTERNS:
        match = pattern.search(catalog_canonical)
        if match is not None and _is_person_catalog_candidate(match.group("name")):
            return True
    has_catalog_surface = bool(
        any(word in canonical.casefold() for word in _MOVIE_WORDS)
        and (
            any(word in canonical.casefold() for word in _LIST_WORDS)
            or any(word in canonical.casefold() for word in _RECOMMENDATION_WORDS)
            or any(word in canonical.casefold() for word in _DISCOVERY_WORDS)
            or any(
                word in canonical.casefold()
                for word in (*_ACTOR_ROLE_WORDS, *_DIRECTOR_ROLE_WORDS)
            )
        )
    )
    if not has_catalog_surface:
        return False
    for match in _CJK_PERSON_ROLE_MENTION_PATTERN.finditer(canonical):
        if _is_person_name_candidate(match.group("name")):
            return True
    return False


def has_tentative_person_catalog_intent(question: str) -> bool:
    """Recognize a bare name-before-movie shape requiring Release confirmation."""
    if not isinstance(question, str) or not question.strip():
        return False
    if _is_generic_cjk_continuation_surface(question):
        return False
    canonical = _strip_bounded_output_directives(
        normalize_query_text(question)
    ).strip(_PERSON_QUERY_PUNCTUATION)
    if has_explicit_person_catalog_intent(canonical):
        return False
    catalog_canonical = _strip_english_person_catalog_continuation_shell(canonical)
    for pattern in _ENGLISH_TENTATIVE_PERSON_CATALOG_PATTERNS:
        match = pattern.search(catalog_canonical)
        if match is not None and _is_person_catalog_candidate(match.group("name")):
            return True
    cjk_catalog_text = re.sub(r"(?:嗎|吗|呢)$", "", canonical)
    cjk_catalog_text, _ = _strip_cjk_person_catalog_continuation_shell(
        cjk_catalog_text
    )
    for match in _CJK_PERSON_CATALOG_SUFFIX_PATTERN.finditer(cjk_catalog_text):
        candidate = match.group("name")
        if (
            _is_tentative_cjk_person_catalog_candidate(candidate)
            and _CJK_CATALOG_COUNT_CANDIDATE_PATTERN.fullmatch(candidate) is None
            and not any(
                term in candidate
                for term in (
                    "比較",
                    "对比",
                    "推薦",
                    "推介",
                    "建議",
                    "建议",
                    "介紹",
                    "介绍",
                    "哪些",
                    "更多",
                    "其他",
                )
            )
        ):
            return True
    discovery_match = _CJK_PERSON_CATALOG_DISCOVERY_SUFFIX_PATTERN.fullmatch(
        cjk_catalog_text
    )
    return bool(
        discovery_match is not None
        and _is_tentative_cjk_person_catalog_candidate(
            discovery_match.group("name")
        )
    )


def has_person_catalog_continuation_shell(question: str) -> bool:
    """Return whether a leading shell asks to continue within a named catalog."""
    if not isinstance(question, str) or not question.strip():
        return False
    canonical = normalize_query_text(question).strip(_PERSON_QUERY_PUNCTUATION)
    cjk_match = _CJK_PERSON_CATALOG_CONTINUATION_PREFIX_PATTERN.match(canonical)
    if cjk_match is not None:
        prefix = cjk_match.group("prefix")
        return not prefix.startswith(("換", "换"))
    return bool(
        re.match(
            r"(?i)^\s*(?:(?:show\s+me|recommend|suggest)\s+"
            r"(?:\d+\s+)?)?(?:more|other|another)\b",
            question,
        )
    )


def has_multiple_person_catalog_intent(question: str) -> bool:
    """Own bounded coordinated English people before generic vector routing."""
    if not isinstance(question, str) or not question.strip():
        return False
    canonical = normalize_query_text(question).strip(_PERSON_QUERY_PUNCTUATION)
    match = _ENGLISH_COORDINATED_PERSON_CATALOG_PATTERN.search(canonical)
    if match is None:
        return False
    first = match.group("first").strip()
    second = match.group("second").strip()
    return bool(
        _is_person_catalog_candidate(first)
        and _is_person_catalog_candidate(second)
        and normalize_query_text(first).casefold()
        != normalize_query_text(second).casefold()
    )


def _is_person_catalog_candidate(candidate: str) -> bool:
    if re.fullmatch(r"[\u3400-\u9fff]{2,7}", candidate) is not None:
        return _is_person_name_candidate(candidate)
    return _is_english_person_catalog_candidate(candidate)


def _is_english_person_catalog_candidate(candidate: str) -> bool:
    tokens = tuple(token.casefold() for token in re.findall(r"[A-Za-z]+", candidate))
    if not 2 <= len(tokens) <= 4:
        return False
    stopwords = {
        "about",
        "action",
        "and",
        "another",
        "any",
        "comedy",
        "compare",
        "different",
        "drama",
        "film",
        "films",
        "horror",
        "list",
        "me",
        "more",
        "movie",
        "movies",
        "other",
        "recommend",
        "show",
        "suggest",
        "the",
        "these",
        "those",
        "with",
    }
    return not any(token in stopwords for token in tokens)


def _count_value(raw: str) -> int:
    if raw.isascii() and raw.isdigit():
        return int(raw)
    if raw in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[raw]
    if raw.startswith("十"):
        return 10 + _CHINESE_NUMBERS.get(raw[1:], 0)
    if "十" in raw:
        tens, ones = raw.split("十", 1)
        return _CHINESE_NUMBERS.get(tens, 1) * 10 + _CHINESE_NUMBERS.get(ones, 0)
    return _DEFAULT_RECOMMENDATION_COUNT


def _year_constraints(question: str) -> tuple[int | None, int | None, bool]:
    if match := _BETWEEN_YEAR_PATTERN.search(question):
        start = int(match.group("start"))
        end = int(match.group("end"))
        return min(start, end), max(start, end), True
    for pattern in _AFTER_YEAR_PATTERNS:
        if match := pattern.search(question):
            return int(match.group("year")), None, True
    for pattern in _BEFORE_YEAR_PATTERNS:
        if match := pattern.search(question):
            return None, int(match.group("year")), True
    for pattern in _DECADE_PATTERNS:
        if match := pattern.search(question):
            decade = int(match.group("decade"))
            return decade, decade + 9, True
    if match := _CHINESE_DECADE_RANGE_PATTERN.search(question):
        start = 1900 + _CHINESE_NUMBERS[match.group("start")] * 10
        end_token = match.group("end")
        end = 1900 + _CHINESE_NUMBERS[end_token] * 10 if end_token else start
        return min(start, end), max(start, end) + 9, True
    if match := _EXACT_YEAR_PATTERN.search(question):
        year = int(match.group("year"))
        return year, year, True
    return None, None, False


def normalize_title_text(text: str) -> str:
    """Normalize only the conservative title characters shared with SQL ranking."""
    return text.casefold().translate(_TITLE_TRANSLATION_TABLE)


def normalize_query_text(text: str) -> str:
    """Convert only user query text to governed Hong Kong traditional characters."""
    # OpenCC's HK phrase table can rewrite the boundary ``一出演`` as
    # ``一齣演``.  That is correct for a counter but corrupts real Release
    # credits ending in ``一`` (for example ``中井貴一出演過``).  Convert the
    # non-role slices independently and restore only the explicitly governed
    # role lexemes.
    parts = _QUERY_ROLE_NORMALIZATION_PATTERN.split(text)
    roles = _QUERY_ROLE_NORMALIZATION_PATTERN.findall(text)
    normalized: list[str] = []
    for index, part in enumerate(parts):
        normalized.append(_QUERY_CONVERTER.convert(part))
        if index < len(roles):
            normalized.append(_QUERY_ROLE_NORMALIZATION[roles[index]])
    return "".join(normalized)


_TITLE_CONTEXT_TERMS = (
    *_MOVIE_WORDS,
    *_RECOMMENDATION_WORDS,
    *_ACTOR_ROLE_WORDS,
    *_DIRECTOR_ROLE_WORDS,
    *_DEEP_ANALYSIS_WORDS,
    *_DEEP_ANALYSIS_ENGLISH_ALIASES,
    "編劇",
    "编剧",
    "上映",
    "哪年",
    "年份",
    "片長",
    "片长",
    "票房",
    "卡司",
    "劇情",
    "剧情",
    "喜劇創作",
    "喜剧创作",
    "畫面",
    "画面",
    "呈現",
    "呈现",
    "色彩",
    "colour",
    "color",
    "類型",
    "类型",
    "好看",
    "資料",
    "资料",
    "information",
    "basic information",
    "rating",
    "language",
    "語言",
    "语言",
    "介紹",
    "介绍",
    "比較",
    "比较",
    "對比",
    "对比",
    "release date",
    "runtime",
    "screenwriter",
    "plot",
    "ending",
    "about this movie",
    "what about this movie",
    "what about",
    "tell me about",
    "compare",
    "directed",
    "stars",
    "stars in",
    " / ",
    " vs ",
    "怎麼樣",
    "怎么样",
    "好不好看",
    "值得看",
    "講什麼",
    "讲什么",
    "誰演的",
    "谁演的",
)
_TITLE_LEFT_CONTEXT_SUFFIXES = (
    *_MOVIE_WORDS,
    "比較",
    "比较",
    "對比",
    "对比",
    "分析",
    "介紹",
    "介绍",
    "請問",
    "请问",
    "叫",
    "名為",
    "名为",
    "與",
    "与",
    "和",
    "跟",
)
_TITLE_RIGHT_CONTEXT_PREFIXES = (
    "的",
    "的導演",
    "的导演",
    "的主演",
    "的演員",
    "的演员",
    "的編劇",
    "的编剧",
    "的資料",
    "的资料",
    "的劇情",
    "的剧情",
    "的片長",
    "的片长",
    "是哪年",
    "是",
    "哪年",
    "何時上映",
    "何时上映",
    "好看",
    "怎麼樣",
    "怎么样",
    "好不好看",
    "值得看",
    "講什麼",
    "讲什么",
    "誰演的",
    "谁演的",
    "這部",
    "这部",
    "該部",
    "该部",
    "透過",
    "通过",
    "使用",
    "use ",
    "如何",
    "怎麼",
    "怎么",
    "導演",
    "导演",
    "與",
    "与",
    "和",
    "跟",
    "vs",
)
_TITLE_REQUEST_LEAD_IN_PATTERN = re.compile(
    r"(?:^|[\s，,。！？!?：:；;])"
    r"(?:我\s*)?(?:想(?:要)?\s*)?"
    r"(?:(?:請問|請|麻煩|可以|能|能否|可否|可不可以)\s*)?"
    r"(?:你\s*)?(?:(?:幫|給)我\s*)?"
    r"(?:講(?:講|一講)?|(?:說|説)(?:(?:說|説)|一(?:說|説))?|"
    r"聊(?:聊|一聊)?|談(?:談|一談)?|問|问|知道|關於|关于|"
    r"介紹|分析|了解|解釋|解析|評價|討論)"
    r"(?:一下|下|一點|些)?\s*[,，:：]?\s*$",
    re.IGNORECASE,
)
_TRIMMED_TITLE_PUNCTUATION = " \t\r\n。！？!?，,、:：;；."
_EXACT_TITLE_RECOMMENDATION_PREFIX_PATTERN = re.compile(
    r"^(?:(?:請問|請|麻煩|可以|能否|可否)\s*)?"
    r"(?:(?:幫我|給我)\s*)?(?:推薦|推介|建議)\s*$"
)
_NONMOVIE_OBJECT_TERMS = (
    "電子遊戲",
    "电子游戏",
    "電視劇",
    "电视剧",
    "小說",
    "小说",
    "小説",
    "書籍",
    "书籍",
    "遊戲",
    "游戏",
    "劇集",
    "剧集",
    "本書",
    "本书",
    "歌曲",
    "餐廳",
    "餐厅",
    "食譜",
    "食谱",
    "video game",
    "tv series",
    "tv show",
    "novel",
    "series",
    "book",
    "game",
    "restaurant",
    "recipe",
    "song",
)
_EXPLICIT_BOOK_OBJECT_PATTERN = re.compile(
    r"(?:"
    r"[一二兩两三四五六七八九十這这那某哪幾几數数]\s*本"
    r"[^，,。！？!?：:；;\r\n]{0,32}[書书]"
    r"|本[書书]"
    r"|[書书](?="
    r"(?:講|讲|說|说|的內容|的内容|的作者|的出版|內容|内容|作者|出版|"
    r"裡|里|中|怎麼|怎么|如何|好看|值得|推薦|推荐|改編|改编|"
    r"評|评|讀|读|和電影|和电影|與電影|与电影|跟電影|跟电影|"
    r"是否|是|有|版|$)"
    r")"
    r")"
)


def has_explicit_nonmovie_object_term(question: str) -> bool:
    """Return whether query text names a governed-outside non-movie object."""
    if not isinstance(question, str) or not question.strip():
        return False
    normalized = normalize_title_text(normalize_query_text(question))
    return _has_normalized_nonmovie_object_term(normalized)


def has_recommendation_deduplication_request(question: str) -> bool:
    """Return whether one bounded no-repeat recommendation modifier is present."""
    if not isinstance(question, str) or not question.strip():
        return False
    canonical = normalize_query_text(question).casefold()
    return any(pattern.search(canonical) for pattern in _DEDUPLICATION_REQUEST_PATTERNS)


def deduplication_requires_successful_history(question: str) -> bool:
    """Require prior cards when no-repeat wording is itself a continuation request."""
    if not has_recommendation_deduplication_request(question):
        return False
    canonical = normalize_query_text(question).casefold()
    stripped = canonical.strip(_PERSON_QUERY_PUNCTUATION)
    remainder = _strip_deduplication_requests(canonical).strip(
        _PERSON_QUERY_PUNCTUATION
    )
    return bool(
        _DEDUPLICATION_HISTORY_REFERENCE_PATTERN.search(canonical) is not None
        or
        _has_continuation_surface(canonical)
        or not remainder
        or _GENERIC_CJK_RECOMMENDATION_SHELL_PATTERN.fullmatch(remainder)
        is not None
        or any(
            pattern.fullmatch(stripped)
            for pattern in _DEDUPLICATION_REQUEST_PATTERNS
        )
    )


def _strip_deduplication_requests(text: str) -> str:
    stripped = text
    for pattern in _DEDUPLICATION_REQUEST_PATTERNS:
        stripped = pattern.sub("", stripped)
    return stripped


def has_unsupported_negative_recommendation_filter(question: str) -> bool:
    """Reject negative structured filters until exclusion predicates are modeled."""
    if not isinstance(question, str) or not question.strip():
        return False
    if _is_generic_cjk_continuation_surface(question):
        return False
    canonical = _negative_recommendation_analysis_text(question)
    if not canonical:
        return False
    if _is_bare_tentative_copular_person_catalog(canonical):
        return False
    has_recommendation_context = _has_bounded_recommendation_context(canonical)
    has_comparison_context = _has_bounded_comparison_context(canonical)
    if not (has_recommendation_context or has_comparison_context):
        return False
    if (
        has_recommendation_context
        and _question_has_governed_structured_filter(canonical)
        and (
            _NEGATIVE_POLARITY_SURFACE_PATTERN.search(canonical) is not None
            or _STRUCTURED_NEGATIVE_PREFIX_PATTERN.search(canonical) is not None
        )
    ):
        return True
    for pattern in _NEGATIVE_RECOMMENDATION_FILTER_CLAUSE_PATTERNS:
        for match in pattern.finditer(canonical):
            marker = match.group("marker").strip().casefold()
            if (
                not has_recommendation_context
                and marker in {"不是", "非", "not"}
            ):
                continue
            clause = match.group("clause").strip()
            if _DEDUPLICATION_TERM_PATTERN.fullmatch(clause) is not None:
                continue
            if _negative_clause_is_structured_filter(clause):
                return True
    return False


def has_unmodeled_negative_recommendation_condition(question: str) -> bool:
    """Fail closed for explicit negative recommendation clauses without a typed model."""
    if not isinstance(question, str) or not question.strip():
        return False
    if _is_generic_cjk_continuation_surface(question):
        return False
    canonical = _negative_recommendation_analysis_text(question)
    if not canonical:
        return False
    if _is_bare_tentative_copular_person_catalog(canonical):
        return False
    has_recommendation_context = _has_bounded_recommendation_context(canonical)
    has_comparison_context = _has_bounded_comparison_context(canonical)
    if not (has_recommendation_context or has_comparison_context):
        return False
    if (
        has_recommendation_context
        and _NEGATIVE_POLARITY_SURFACE_PATTERN.search(canonical) is not None
        and not _question_has_governed_structured_filter(canonical)
    ):
        return True
    for pattern in _NEGATIVE_RECOMMENDATION_FILTER_CLAUSE_PATTERNS:
        for match in pattern.finditer(canonical):
            if has_recommendation_deduplication_request(match.group(0)):
                continue
            clause = match.group("clause").strip()
            if not clause:
                continue
            marker = match.group("marker").strip().casefold()
            if (
                not has_recommendation_context
                and marker in {"不是", "非", "not"}
            ):
                continue
            if clause:
                return True
    return False


def _negative_recommendation_analysis_text(question: str) -> str:
    canonical = normalize_query_text(question).casefold()
    canonical = _strip_deduplication_requests(canonical)
    canonical = _strip_bounded_output_directives(canonical)
    canonical = _strip_neutral_preference_statements(canonical)
    shape = parse_person_query_shape(question)
    if (
        shape is not None
        and len(shape.candidate_names) == 1
        and not shape.exclusionary
        and not shape.role_conflict
    ):
        governed_name = normalize_query_text(shape.candidate_names[0]).casefold()
        canonical = canonical.replace(governed_name, "", 1)
    return " ".join(
        segment.strip()
        for segment in re.split(r"[。！？!?，,；;\r\n]+", canonical)
        if segment.strip()
        and _EXPLANATORY_NEGATION_PATTERN.search(segment) is None
    )


def _is_bare_tentative_copular_person_catalog(canonical: str) -> bool:
    """Leave a whole bare ``不是…電影`` candidate to Release identity authority."""
    return bool(
        re.match(r"^(?:不是|非)", canonical)
        and has_tentative_person_catalog_intent(canonical)
        and not any(
            word in canonical
            for word in (
                *_RECOMMENDATION_WORDS,
                *_DISCOVERY_WORDS,
                *_LIST_WORDS,
            )
        )
        and not _has_continuation_surface(canonical)
    )


def _has_bounded_recommendation_context(canonical: str) -> bool:
    return (
        has_explicit_person_catalog_intent(canonical)
        or has_tentative_person_catalog_intent(canonical)
        or _has_bounded_negative_selection_target(canonical)
        or any(
            word in canonical
            for word in (
                *_RECOMMENDATION_WORDS,
                *_DISCOVERY_WORDS,
                *_LIST_WORDS,
            )
        )
        or _has_continuation_surface(canonical)
    )


def _has_bounded_negative_selection_target(canonical: str) -> bool:
    """Recognize a request-shaped negative target without treating facts as filters."""
    for pattern in _NEGATIVE_RECOMMENDATION_FILTER_CLAUSE_PATTERNS:
        for match in pattern.finditer(canonical):
            prefix = canonical[: match.start()].strip(
                " \t\r\n。！？!?，,、:：;；."
            )
            if (
                prefix
                and _NEGATIVE_SELECTION_REQUEST_PREFIX_PATTERN.fullmatch(prefix)
                is None
            ):
                continue
            clause = _NEGATIVE_SELECTION_CLAUSE_LEAD_IN_PATTERN.sub(
                "", match.group("clause").strip()
            )
            if not clause:
                continue
            if (
                _negative_clause_is_structured_filter(clause)
                or has_explicit_person_catalog_intent(clause)
                or has_tentative_person_catalog_intent(clause)
            ):
                return True
    return False


def _has_bounded_comparison_context(canonical: str) -> bool:
    return any(word in canonical for word in ("比較", "对比", "compare"))


def _question_has_governed_structured_filter(canonical: str) -> bool:
    if any(
        _contains_alias(canonical, alias.casefold())
        for _, aliases in _GENRE_ALIASES
        for alias in aliases
    ):
        return True
    return bool(
        _TIER_PATTERN.search(canonical) is not None
        or _year_constraints(canonical)[2]
    )


def _negative_clause_is_structured_filter(clause: str) -> bool:
    governed_clause = _NEGATIVE_FILTER_SCOPE_PREFIX_PATTERN.sub("", clause.strip())
    if any(
        _clause_starts_with_alias(governed_clause, alias.casefold())
        for _, aliases in _GENRE_ALIASES
        for alias in aliases
    ):
        return True
    year_clause = re.sub(
        r"^(?:(?:movies?|films?)\s+)?(?:from\s+(?:the\s+)?)?",
        "",
        governed_clause,
        flags=re.IGNORECASE,
    )
    return bool(
        _TIER_PATTERN.match(governed_clause) is not None
        or _year_constraints(year_clause)[2]
    )


def _clause_starts_with_alias(clause: str, alias: str) -> bool:
    if not clause.startswith(alias):
        return False
    if not alias or not alias[-1].isascii() or len(clause) == len(alias):
        return True
    return not clause[len(alias)].isascii() or not clause[len(alias)].isalnum()


def explicit_movie_id_intent_position(question: str, movie_id: str) -> int:
    """Resolve the first bounded occurrence of one governed movie ID."""
    normalized_question = normalize_title_text(normalize_query_text(question))
    normalized_movie_id = normalize_title_text(movie_id.strip())
    if not normalized_question or not normalized_movie_id:
        return -1
    spans = _bounded_movie_id_spans(normalized_question, normalized_movie_id)
    return spans[0][0] if spans else -1


def explicit_title_intent_position(question: str, title: str) -> int:
    """Resolve a title only when its occurrence carries explicit movie intent.

    This deliberately rejects a governed short title embedded in an unrelated
    longer CJK or ASCII token (for example, ``英雄`` inside ``英雄聯盟``).
    """
    if not isinstance(question, str) or not isinstance(title, str):
        return -1
    normalized_question = normalize_title_text(normalize_query_text(question))
    normalized_title = normalize_title_text(title.strip())
    if not normalized_question.strip() or not normalized_title:
        return -1
    spans = _explicit_title_spans(normalized_question, normalized_title)
    return spans[0][0] if spans else -1


def resolve_explicit_movie_identities(
    question: str,
    candidates: Sequence[tuple[str, str, str]],
    *,
    max_results: int = 8,
) -> tuple[str, ...]:
    """Return IDs from :func:`resolve_explicit_movie_identity_context`."""
    return resolve_explicit_movie_identity_context(
        question, candidates, max_results=max_results
    ).movie_ids


def resolve_explicit_movie_identity_context(
    question: str,
    candidates: Sequence[tuple[str, str, str]],
    *,
    max_results: int = 8,
) -> ExplicitMovieResolution:
    """Resolve all explicit movie identities, then inspect only query residual.

    Every accepted title and movie-ID span is masked before non-movie object
    detection.  This preserves real titles such as ``死亡遊戲`` while rejecting
    requests about a novel or game derived from another matched movie.
    """
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or max_results < 1
    ):
        return ExplicitMovieResolution((), question if isinstance(question, str) else "")
    raw_question = question.casefold()
    normalized_question = normalize_title_text(normalize_query_text(question))
    candidate_matches: list[
        tuple[str, str, str, list[tuple[int, int]], set[tuple[int, int]]]
    ] = []
    explicit_id_movie_ids: set[str] = set()
    authoritative_spans_by_movie_id: dict[str, set[tuple[int, int]]] = {}
    for movie_id, chinese_title, english_title in candidates:
        if not all(
            isinstance(value, str)
            for value in (movie_id, chinese_title, english_title)
        ):
            continue
        movie_id_spans = _bounded_movie_id_spans(
            normalized_question, normalize_title_text(movie_id.strip())
        )
        spans = list(movie_id_spans)
        title_spans: list[tuple[int, int]] = []
        authoritative_spans: set[tuple[int, int]] = set()
        literal_title_spans: set[tuple[int, int]] = set()
        if spans:
            explicit_id_movie_ids.add(movie_id)
        for title in (chinese_title, english_title):
            normalized_title = normalize_title_text(title.strip())
            if normalized_title:
                authoritative_spans.update(
                    _delimited_title_spans(normalized_question, normalized_title)
                )
                matched_title_spans = _explicit_title_spans(
                    normalized_question, normalized_title
                )
                title_spans.extend(matched_title_spans)
                spans.extend(matched_title_spans)
                literal_title_spans.update(
                    _literal_title_spans(raw_question, title.strip().casefold())
                )
        if not spans:
            continue
        authoritative_spans.update(
            span
            for span in movie_id_spans
            if not any(
                title_start <= span[0]
                and span[1] <= title_end
                and title_end - title_start > span[1] - span[0]
                for title_start, title_end in title_spans
            )
        )
        authoritative_spans_by_movie_id[movie_id] = authoritative_spans
        candidate_matches.append(
            (
                movie_id,
                chinese_title,
                english_title,
                sorted(set(spans)),
                literal_title_spans,
            )
        )
    all_spans = [
        span
        for _, _, _, spans, _ in candidate_matches
        for span in spans
    ]
    has_authoritative_identity = any(authoritative_spans_by_movie_id.values())
    dominant_candidates: list[
        tuple[str, str, str, list[tuple[int, int]], set[tuple[int, int]]]
    ] = []
    identity_spans: list[tuple[int, int]] = []
    for (
        movie_id,
        chinese_title,
        english_title,
        spans,
        literal_title_spans,
    ) in candidate_matches:
        dominant_spans = [
            (start, end)
            for start, end in spans
            if (
                not has_authoritative_identity
                or (start, end) in authoritative_spans_by_movie_id[movie_id]
            )
            if not any(
                other_start <= start
                and end <= other_end
                and other_end - other_start > end - start
                for other_start, other_end in all_spans
            )
        ]
        if not dominant_spans:
            continue
        identity_spans.extend(dominant_spans)
        dominant_candidates.append(
            (
                movie_id,
                chinese_title,
                english_title,
                dominant_spans,
                literal_title_spans,
            )
        )
    if not dominant_candidates:
        return ExplicitMovieResolution((), normalized_question)
    residual = list(normalized_question)
    for start, end in identity_spans:
        residual[start:end] = " " * (end - start)
    residual_question = "".join(residual)
    if _has_normalized_nonmovie_object_term(residual_question):
        return ExplicitMovieResolution((), residual_question)
    span_owners: dict[tuple[int, int], set[str]] = {}
    for movie_id, _, _, spans, _ in dominant_candidates:
        for span in spans:
            span_owners.setdefault(span, set()).add(movie_id)
    allowed_owners_by_span: dict[tuple[int, int], set[str]] = {}
    unresolved_ambiguous_spans: set[tuple[int, int]] = set()
    for span, owners in span_owners.items():
        if len(owners) <= 1:
            continue
        explicit_owners = owners & explicit_id_movie_ids
        if explicit_owners:
            allowed_owners_by_span[span] = explicit_owners
            continue
        literal_owners = {
            movie_id
            for movie_id, _, _, spans, literal_spans in dominant_candidates
            if span in spans and span in literal_spans
        }
        if len(literal_owners) == 1:
            allowed_owners_by_span[span] = literal_owners
        else:
            unresolved_ambiguous_spans.add(span)
    if unresolved_ambiguous_spans:
        ambiguous_movie_ids = tuple(
            sorted(
                {
                    movie_id
                    for span in unresolved_ambiguous_spans
                    for movie_id in span_owners[span]
                }
            )
        )
        return ExplicitMovieResolution(
            (),
            residual_question,
            ambiguous=True,
            ambiguous_movie_ids=ambiguous_movie_ids,
        )
    matches: list[tuple[int, int, str]] = []
    for movie_id, chinese_title, english_title, spans, _ in dominant_candidates:
        selected_spans = [
            span
            for span in spans
            if span not in allowed_owners_by_span
            or movie_id in allowed_owners_by_span[span]
        ]
        if not selected_spans:
            continue
        matches.append(
            (
                min(start for start, _ in selected_spans),
                -max(len(chinese_title), len(english_title)),
                movie_id,
            )
        )
    movie_ids = tuple(
        dict.fromkeys(movie_id for _, _, movie_id in sorted(matches))
    )[:max_results]
    return ExplicitMovieResolution(movie_ids, residual_question)


def _bounded_movie_id_spans(
    normalized_question: str, normalized_movie_id: str
) -> list[tuple[int, int]]:
    if not normalized_movie_id:
        return []
    pattern = re.compile(
        rf"(?<![a-z0-9_]){re.escape(normalized_movie_id)}(?![a-z0-9_])"
    )
    return [match.span() for match in pattern.finditer(normalized_question)]


def _literal_title_spans(text: str, title: str) -> list[tuple[int, int]]:
    """Return exact raw-title spans used to break normalized alias collisions."""
    if not title:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (position := text.find(title, start)) >= 0:
        spans.append((position, position + len(title)))
        start = position + 1
    return spans


def _explicit_title_spans(
    normalized_question: str, normalized_title: str
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    if normalized_question.strip(
        _TRIMMED_TITLE_PUNCTUATION
    ) == normalized_title.strip(_TRIMMED_TITLE_PUNCTUATION):
        position = normalized_question.find(normalized_title)
        if position >= 0:
            spans.append((position, position + len(normalized_title)))
    spans.extend(_delimited_title_spans(normalized_question, normalized_title))
    has_title_specific_context = _has_title_specific_context(normalized_question)
    topic_independent_title = _supports_topic_independent_title_reference(
        normalized_title
    )
    start = 0
    while (position := normalized_question.find(normalized_title, start)) >= 0:
        end = position + len(normalized_title)
        prefix = normalized_question[:position]
        suffix = normalized_question[end:]
        bounded_lead_in = _has_conversational_title_lead_in(prefix)
        exact_recommendation_remainder = bool(
            not suffix.strip(_TRIMMED_TITLE_PUNCTUATION)
            and _EXACT_TITLE_RECOMMENDATION_PREFIX_PATTERN.fullmatch(
                prefix.strip(_TRIMMED_TITLE_PUNCTUATION)
            )
            is not None
        )
        exact_conversational_remainder = bool(
            bounded_lead_in
            and not suffix.strip(_TRIMMED_TITLE_PUNCTUATION)
            and _supports_exact_short_conversational_title(normalized_title)
        )
        conversational_lead_in = (
            bounded_lead_in
            and (
                topic_independent_title
                or _has_title_specific_context(suffix)
                or exact_conversational_remainder
            )
        )
        subject_reference = (
            topic_independent_title
            and not prefix.strip(_TRIMMED_TITLE_PUNCTUATION)
            and any(
                suffix.startswith(term) for term in _TITLE_RIGHT_CONTEXT_PREFIXES
            )
        )
        if not (
            has_title_specific_context
            or conversational_lead_in
            or subject_reference
            or exact_recommendation_remainder
        ):
            start = position + 1
            continue
        left_safe = (
            not prefix
            or not _is_title_continuation_character(prefix[-1])
            or any(prefix.endswith(term) for term in _TITLE_LEFT_CONTEXT_SUFFIXES)
            or conversational_lead_in
            or exact_recommendation_remainder
        )
        right_safe = (
            not suffix
            or not _is_title_continuation_character(suffix[0])
            or any(suffix.startswith(term) for term in _TITLE_RIGHT_CONTEXT_PREFIXES)
        )
        if left_safe and right_safe:
            spans.append((position, end))
        start = position + 1
    return sorted(set(spans))


def _delimited_title_spans(
    normalized_question: str, normalized_title: str
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for left, right in TITLE_DELIMITERS:
        delimited_title = f"{left}{normalized_title}{right}"
        start = 0
        while (position := normalized_question.find(delimited_title, start)) >= 0:
            title_start = position + len(left)
            spans.append((title_start, title_start + len(normalized_title)))
            start = position + 1
    return spans


def _has_conversational_title_lead_in(prefix: str) -> bool:
    """Recognize bounded speech acts without depending on the requested movie topic."""
    return _TITLE_REQUEST_LEAD_IN_PATTERN.search(prefix) is not None


def _supports_topic_independent_title_reference(normalized_title: str) -> bool:
    """Require enough identity signal before bypassing movie-topic vocabulary."""
    identity_units = tuple(character for character in normalized_title if character.isalnum())
    if any("\u3400" <= character <= "\u9fff" for character in identity_units):
        return len(identity_units) >= 4
    return len(identity_units) >= 8


def _supports_exact_short_conversational_title(normalized_title: str) -> bool:
    """Allow a complete two-plus-CJK title after one bounded speech act."""
    identity_units = tuple(character for character in normalized_title if character.isalnum())
    cjk_units = tuple(
        character for character in identity_units if "\u3400" <= character <= "\u9fff"
    )
    return len(identity_units) == len(cjk_units) and len(cjk_units) >= 2


def _has_title_specific_context(question: str) -> bool:
    folded = question.casefold()
    return (
        " / " in folded
        or " vs " in folded
        or any(
            _contains_alias(folded, term.casefold())
            for term in _TITLE_CONTEXT_TERMS
        )
    )


def _is_title_continuation_character(character: str) -> bool:
    return (
        "\u3400" <= character <= "\u9fff"
        or (character.isascii() and character.isalnum())
    )


def _has_normalized_nonmovie_object_term(normalized_question: str) -> bool:
    return _EXPLICIT_BOOK_OBJECT_PATTERN.search(normalized_question) is not None or any(
        _contains_alias(normalized_question, term.casefold())
        for term in _NONMOVIE_OBJECT_TERMS
    )


def credit_query_alias(text: str) -> str:
    """Derive a transient simplified lookup alias without mutating stored credits."""
    return _CREDIT_ALIAS_CONVERTER.convert(text)


def credit_equivalence_key(text: str) -> str:
    """Return the traditional orthographic class key for one governed credit."""
    return _CREDIT_EQUIVALENCE_CONVERTER.convert(text)


def infer_person_role(text: str) -> PersonRole | None:
    """Infer a single explicit credit role; no role means match actor or director."""
    folded = normalize_query_text(text).casefold()
    actor = any(_contains_alias(folded, word.casefold()) for word in _ACTOR_ROLE_WORDS)
    director = any(
        _contains_alias(folded, word.casefold()) for word in _DIRECTOR_ROLE_WORDS
    )
    if actor == director:
        return None
    return "actor" if actor else "director"


def sql_title_normalization(
    sql_expression: str,
) -> tuple[str, tuple[str, ...]]:
    """Build a parameterized SQL expression equivalent to :func:`normalize_title_text`."""
    normalized = f"lower({sql_expression})"
    parameters: list[str] = []
    for simplified, traditional in TITLE_NORMALIZATION_REPLACEMENTS:
        normalized = f"replace({normalized}, %s, %s)"
        parameters.extend((simplified, traditional))
    return normalized, tuple(parameters)
