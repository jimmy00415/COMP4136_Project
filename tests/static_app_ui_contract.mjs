import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const scriptSource = await readFile(
  new URL("../src/hk_movie_rag/static/app.js", import.meta.url),
  "utf8",
);

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.replacement = null;
    this._src = "";
    this.srcAssignments = [];
    this.focusCount = 0;
  }

  get src() {
    return this._src;
  }

  set src(value) {
    this._src = value;
    this.srcAssignments.push(value);
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  replaceWith(replacement) {
    this.replacement = replacement;
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  focus() {
    this.focusCount += 1;
  }

  async dispatch(type) {
    const listener = this.listeners.get(type);
    assert.equal(typeof listener, "function", `missing ${type} listener`);
    return listener({ preventDefault() {} });
  }
}

function response(status, payload = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return payload;
    },
  };
}

async function boot(initialResponder = async () => response(401)) {
  const selectors = [
    "#chat-panel",
    "#new-chat-button",
    "#question-form",
    "#question",
    "#ask-button",
    "#chat-status",
    "#chat-error",
    "#conversation",
    "#release-summary",
    "#scope-movies",
    "#scope-documents",
    "#scope-pages",
  ];
  const elements = Object.fromEntries(selectors.map((selector) => [selector, new FakeElement()]));
  const timers = [];
  const timerDelays = [];
  let responder = initialResponder;
  const context = vm.createContext({
    console,
    setTimeout(callback, delay) {
      assert.equal(typeof callback, "function");
      assert.equal(Number.isFinite(delay) && delay > 0, true);
      timers.push(callback);
      timerDelays.push(delay);
      return timers.length;
    },
    document: {
      querySelector(selector) {
        return elements[selector];
      },
      querySelectorAll() {
        return [];
      },
      createElement(tagName) {
        return new FakeElement(tagName);
      },
      createDocumentFragment() {
        return new FakeElement("fragment");
      },
    },
    fetch(path, options) {
      return responder(path, options);
    },
  });
  vm.runInContext(scriptSource, context, { filename: "app.js" });
  await new Promise((resolve) => setImmediate(resolve));
  return {
    context,
    elements,
    respondWith(nextResponder) {
      responder = nextResponder;
    },
    runNextTimer() {
      const callback = timers.shift();
      assert.equal(typeof callback, "function", "missing scheduled timer");
      callback();
    },
    pendingTimers() {
      return timers.length;
    },
    timerDelays,
  };
}

test("public boot does not steal focus and scroll past the mobile header", async () => {
  const app = await boot(async (path) => {
    assert.equal(path, "/api/config");
    return response(200, {
      counts: { movies: 4658, movie_documents: 2, pdf_passages: 6 },
    });
  });

  assert.equal(app.elements["#question"].focusCount, 0);
  assert.equal(app.elements["#chat-error"].textContent, "");
});

test("public chat failure keeps the workspace visible with a controlled error", async () => {
  const app = await boot();
  app.elements["#question"].value = "醉拳的導演是誰？";
  app.respondWith(async (path) => {
    assert.equal(path, "/api/chat");
    return response(401);
  });

  await app.elements["#question-form"].dispatch("submit");

  assert.equal(app.elements["#chat-panel"].hidden, false);
  assert.equal(
    app.elements["#chat-error"].textContent,
    "目前無法產生受引用支持的答案，請稍後再試。",
  );
});

test("public config failure keeps the chat workspace available", async () => {
  const app = await boot();

  assert.equal(app.elements["#chat-panel"].hidden, false);
  assert.equal(
    app.elements["#chat-error"].textContent,
    "目前無法讀取資料庫範圍，請稍後再試。",
  );
});

test("poster load retries the exact governed path and can then succeed", async () => {
  const app = await boot();
  const card = vm.runInContext(
    `renderMovie({
      movie_id: "1978_ZQ_001",
      chinese_title: "醉拳",
      english_title: "Drunken Master",
      release_date: "1978",
      director: "袁和平",
      cast: "成龍",
      genre: "動作",
      tier: "S",
      pilot_movie: true,
      poster_url: "/api/posters/1978_ZQ_001"
    })`,
    app.context,
  );
  const image = card.children[0];
  assert.deepEqual(image.srcAssignments, ["/api/posters/1978_ZQ_001"]);

  await image.dispatch("error");
  assert.equal(image.replacement, null);
  assert.equal(app.pendingTimers(), 1);
  app.runNextTimer();

  assert.equal(image.src, "/api/posters/1978_ZQ_001");
  assert.deepEqual(image.srcAssignments, [
    "/api/posters/1978_ZQ_001",
    "/api/posters/1978_ZQ_001",
  ]);
  await image.dispatch("load");
  await image.dispatch("error");
  assert.equal(app.pendingTimers(), 0);
  assert.equal(image.replacement, null);
  assert.deepEqual(app.timerDelays, [200]);
});

test("poster retry exhaustion stops after two retries with accessible final text", async () => {
  const app = await boot();
  const card = vm.runInContext(
    `renderMovie({
      movie_id: "1978_ZQ_001",
      chinese_title: "醉拳",
      english_title: "Drunken Master",
      release_date: "1978",
      director: "袁和平",
      cast: "成龍",
      genre: "動作",
      tier: "S",
      pilot_movie: true,
      poster_url: "/api/posters/1978_ZQ_001"
    })`,
    app.context,
  );
  const image = card.children[0];

  await image.dispatch("error");
  app.runNextTimer();
  await image.dispatch("error");
  app.runNextTimer();
  await image.dispatch("error");

  assert.equal(app.pendingTimers(), 0);
  assert.deepEqual(app.timerDelays, [200, 500]);
  assert.deepEqual(image.srcAssignments, [
    "/api/posters/1978_ZQ_001",
    "/api/posters/1978_ZQ_001",
    "/api/posters/1978_ZQ_001",
  ]);
  assert.equal(image.replacement.textContent, "海報暫時無法載入");
  assert.equal(image.replacement.attributes.get("role"), "status");
  assert.equal(image.replacement.attributes.get("aria-label"), "醉拳海報暫時無法載入");
});

test("successful turns append bounded history while a failed turn preserves the transcript", async () => {
  const app = await boot();
  const requests = [];
  let calls = 0;
  app.respondWith(async (path, options) => {
    assert.equal(path, "/api/chat");
    requests.push(JSON.parse(options.body));
    calls += 1;
    if (calls === 3) return response(503);
    return response(200, {
      answer_markdown: `答案 ${calls}`,
      citations: [],
      movies: [{
        movie_id: `movie_${calls}`,
        chinese_title: `電影 ${calls}`,
        english_title: "",
        release_date: "2000",
        director: "導演",
        cast: "演員",
        genre: "類型",
        tier: "S",
        pilot_movie: false,
        poster_url: null,
      }],
    });
  });

  app.elements["#question"].value = "第一問";
  await app.elements["#question-form"].dispatch("submit");
  app.elements["#question"].value = "第二問";
  await app.elements["#question-form"].dispatch("submit");
  const successfulTurns = app.elements["#conversation"].children.slice();
  app.elements["#question"].value = "失敗問題";
  await app.elements["#question-form"].dispatch("submit");

  assert.deepEqual(requests[0], { question: "第一問", history: [] });
  assert.deepEqual(requests[1], {
    question: "第二問",
    history: [{ question: "第一問", answer: "答案 1", movie_ids: ["movie_1"] }],
  });
  assert.equal(successfulTurns.length, 2);
  assert.equal(successfulTurns[0].children[1].className, "answer-card");
  assert.equal(successfulTurns[0].children[2].className, "movie-card");
  assert.equal(app.elements["#conversation"].children.length, 2);
  assert.equal(app.elements["#chat-error"].textContent, "目前無法產生受引用支持的答案，請稍後再試。");
});

test("null poster reserves a placeholder without creating an image and new chat clears memory", async () => {
  const app = await boot();
  const card = vm.runInContext(
    `renderMovie({
      movie_id: "no_poster",
      chinese_title: "無海報電影",
      english_title: "",
      release_date: "2000",
      director: "導演",
      cast: "演員",
      genre: "類型",
      tier: "A",
      pilot_movie: false,
      poster_url: null
    })`,
    app.context,
  );
  assert.equal(card.children[0].tagName, "div");
  assert.equal(card.children[0].textContent, "暂无海报");

  app.elements["#conversation"].replaceChildren(new FakeElement("article"));
  await app.elements["#new-chat-button"].dispatch("click");

  assert.equal(app.elements["#conversation"].children.length, 1);
  assert.equal(app.elements["#conversation"].children[0].textContent, "答案會在這裡顯示，並附上資料庫引用。");
});
