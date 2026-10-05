"use strict";

const newChatButton = document.querySelector("#new-chat-button");
const questionForm = document.querySelector("#question-form");
const questionInput = document.querySelector("#question");
const askButton = document.querySelector("#ask-button");
const chatStatus = document.querySelector("#chat-status");
const chatError = document.querySelector("#chat-error");
const conversation = document.querySelector("#conversation");
const releaseSummary = document.querySelector("#release-summary");
const scopeMovies = document.querySelector("#scope-movies");
const scopeDocuments = document.querySelector("#scope-documents");
const scopePages = document.querySelector("#scope-pages");
const POSTER_RETRY_DELAYS_MS = [200, 500];
let successfulHistory = [];

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const error = new Error(`request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

async function loadConfig() {
  const config = await requestJson("/api/config");
  const counts = config.counts;
  releaseSummary.textContent = `${counts.movies.toLocaleString()}部電影｜1970‑2026元數據集 · ${counts.movie_documents}份深度文檔`;
  scopeMovies.textContent = counts.movies.toLocaleString();
  scopeDocuments.textContent = counts.movie_documents.toLocaleString();
  scopePages.textContent = counts.pdf_passages.toLocaleString();
  return config;
}

newChatButton.addEventListener("click", () => {
  chatError.textContent = "";
  questionInput.value = "";
  resetConversation();
  questionInput.focus();
});

document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => {
    questionInput.value = button.dataset.question || "";
    questionInput.focus();
  });
});

questionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) {
    chatError.textContent = "請先輸入問題。";
    questionInput.focus();
    return;
  }
  setLoading(true);
  chatError.textContent = "";
  try {
    const answer = await requestJson("/api/chat", {
      method: "POST",
      body: JSON.stringify({ question, history: successfulHistory }),
    });
    appendAnswer(question, answer);
    successfulHistory = [
      ...successfulHistory,
      {
        question,
        answer: String(answer.answer_markdown || ""),
        movie_ids: movieIds(answer.movies),
      },
    ].slice(-4);
    questionInput.value = "";
  } catch (error) {
    if (error.status === 422) {
      chatError.textContent = "問題格式不正確，請縮短後再試。";
    } else {
      chatError.textContent = "目前無法產生受引用支持的答案，請稍後再試。";
    }
  } finally {
    setLoading(false);
  }
});

function setLoading(loading) {
  askButton.disabled = loading;
  questionInput.disabled = loading;
  chatStatus.textContent = loading ? "正在檢索受控資料並驗證引用…" : "";
}

function resetConversation() {
  successfulHistory = [];
  conversation.replaceChildren(paragraph("答案會在這裡顯示，並附上資料庫引用。", "empty-state"));
}

function appendAnswer(question, answer) {
  if (successfulHistory.length === 0) conversation.replaceChildren();
  const turn = document.createElement("section");
  turn.className = "conversation-turn";

  const questionCard = document.createElement("section");
  questionCard.className = "question-card";
  questionCard.append(paragraph(question));
  turn.append(questionCard);

  const answerCard = document.createElement("article");
  answerCard.className = "answer-card";
  const answerHeading = document.createElement("h3");
  answerHeading.textContent = "回答";
  answerCard.append(answerHeading);
  String(answer.answer_markdown || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => answerCard.append(paragraph(line)));
  turn.append(answerCard);

  const movies = Array.isArray(answer.movies) ? answer.movies : [];
  movies.forEach((movie) => turn.append(renderMovie(movie)));

  const citations = Array.isArray(answer.citations) ? answer.citations : [];
  const citationSection = document.createElement("details");
  citationSection.className = "citation-section";
  const citationHeading = document.createElement("summary");
  citationHeading.textContent = `引用（${citations.length}）`;
  citationSection.append(citationHeading);
  citations.forEach((citation) => citationSection.append(renderCitation(citation)));
  turn.append(citationSection);

  conversation.append(turn);
}

function renderMovie(movie) {
  const card = document.createElement("article");
  card.className = "movie-card";
  if (isSameOriginPosterPath(movie.poster_url)) {
    const posterPath = movie.poster_url;
    const image = document.createElement("img");
    image.src = posterPath;
    image.alt = `${movie.chinese_title || movie.movie_id} 海報（技術測試展示）`;
    image.loading = "lazy";
    let retryCount = 0;
    let retryScheduled = false;
    let finalized = false;
    let loaded = false;
    image.addEventListener("load", () => {
      loaded = true;
      retryScheduled = false;
    });
    image.addEventListener("error", () => {
      if (loaded || finalized || retryScheduled) return;
      if (retryCount < POSTER_RETRY_DELAYS_MS.length) {
        const delay = POSTER_RETRY_DELAYS_MS[retryCount];
        retryCount += 1;
        retryScheduled = true;
        setTimeout(() => {
          retryScheduled = false;
          if (loaded || finalized) return;
          image.src = posterPath;
        }, delay);
        return;
      }
      finalized = true;
      const unavailable = document.createElement("div");
      const title = movie.chinese_title || movie.english_title || movie.movie_id;
      unavailable.className = "poster-unavailable";
      unavailable.setAttribute("role", "status");
      unavailable.setAttribute("aria-label", `${title}海報暫時無法載入`);
      unavailable.textContent = "海報暫時無法載入";
      image.replaceWith(unavailable);
    });
    card.append(image);
  } else {
    card.append(posterState("暂无海报", "暂无海报"));
  }
  const details = document.createElement("div");
  const heading = document.createElement("h3");
  heading.textContent = movie.chinese_title || movie.english_title || movie.movie_id;
  details.append(heading);
  if (movie.english_title) details.append(paragraph(movie.english_title, "english-title"));
  const facets = document.createElement("p");
  facets.className = "movie-facets";
  facets.textContent = `${movie.tier || "?"} 級${movie.pilot_movie === true ? " · 試點片" : ""}`;
  details.append(facets);
  const facts = [
    ["上映", movie.release_date],
    ["導演", movie.director],
    ["演員", movie.cast],
    ["類型", movie.genre],
  ];
  const list = document.createElement("dl");
  facts.filter(([, value]) => value).forEach(([label, value]) => {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    row.append(term, description);
    list.append(row);
  });
  details.append(list);
  card.append(details);
  return card;
}

function posterState(text, label) {
  const unavailable = document.createElement("div");
  unavailable.className = "poster-unavailable";
  unavailable.setAttribute("role", "status");
  unavailable.setAttribute("aria-label", label);
  unavailable.textContent = text;
  return unavailable;
}

function movieIds(movies) {
  if (!Array.isArray(movies)) return [];
  return movies
    .map((movie) => movie && movie.movie_id)
    .filter((movieId) => typeof movieId === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(movieId))
    .slice(0, 8);
}

function renderCitation(citation) {
  const card = document.createElement("article");
  card.className = "citation-card";
  const source = document.createElement("p");
  source.className = "citation-source";
  const page = citation.page_number ? ` · 第 ${citation.page_number} 頁` : " · 結構化資料";
  source.textContent = `${citation.movie_title || citation.movie_id}${page}`;
  const id = document.createElement("code");
  id.textContent = citation.citation_id;
  card.append(source, paragraph(citation.excerpt || ""), id);
  return card;
}

function isSameOriginPosterPath(value) {
  return typeof value === "string" && /^\/api\/posters\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value);
}

function paragraph(text, className = "") {
  const element = document.createElement("p");
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

(async () => {
  try {
    await loadConfig();
  } catch (error) {
    chatError.textContent = "目前無法讀取資料庫範圍，請稍後再試。";
  }
})();
