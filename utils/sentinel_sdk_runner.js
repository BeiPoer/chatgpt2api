"use strict";

const fs = require("fs");
const vm = require("vm");
const { performance } = require("perf_hooks");
const { webcrypto } = require("crypto");

function createEventTarget(target = {}) {
  const listeners = new Map();
  target.addEventListener = (type, listener) => {
    const items = listeners.get(type) || [];
    items.push(listener);
    listeners.set(type, items);
  };
  target.removeEventListener = (type, listener) => {
    listeners.set(type, (listeners.get(type) || []).filter((item) => item !== listener));
  };
  target.dispatchEvent = (event) => {
    for (const listener of listeners.get(event.type) || []) listener.call(target, event);
    return true;
  };
  target.__dispatch = (type, event = {}) => target.dispatchEvent({ type, target, ...event });
  return target;
}

function createStorage() {
  const values = new Map();
  const storage = {
    get length() {
      return values.size;
    },
    key(index) {
      return [...values.keys()][index] ?? null;
    },
    getItem(key) {
      return values.has(String(key)) ? values.get(String(key)) : null;
    },
    setItem(key, value) {
      values.set(String(key), String(value));
    },
    removeItem(key) {
      values.delete(String(key));
    },
    clear() {
      values.clear();
    },
    toString() {
      return "[object Storage]";
    },
  };
  return new Proxy(storage, {
    get(target, key, receiver) {
      if (Reflect.has(target, key)) return Reflect.get(target, key, receiver);
      return values.get(String(key));
    },
    set(target, key, value, receiver) {
      if (Reflect.has(target, key)) return Reflect.set(target, key, value, receiver);
      values.set(String(key), String(value));
      return true;
    },
    ownKeys() {
      return [...values.keys()];
    },
    getOwnPropertyDescriptor(target, key) {
      return Reflect.getOwnPropertyDescriptor(target, key) ||
        (values.has(String(key)) ? { configurable: true, enumerable: true, writable: true, value: values.get(String(key)) } : undefined);
    },
  });
}

function createElement(tagName, onIframeMessage) {
  const attributes = new Map();
  const element = createEventTarget({
    tagName: String(tagName || "div").toUpperCase(),
    nodeName: String(tagName || "div").toUpperCase(),
    nodeType: 1,
    style: {},
    children: [],
    childNodes: [],
    parentNode: null,
    ownerDocument: null,
    textContent: "",
    innerHTML: "",
    value: "",
    id: "",
    className: "",
  });
  element.appendChild = (child) => {
    if (!child) return child;
    child.parentNode = element;
    element.children.push(child);
    element.childNodes = element.children;
    if (child.tagName === "IFRAME") queueMicrotask(() => child.__dispatch("load"));
    return child;
  };
  element.removeChild = (child) => {
    const index = element.children.indexOf(child);
    if (index >= 0) element.children.splice(index, 1);
    if (child) child.parentNode = null;
    element.childNodes = element.children;
    return child;
  };
  element.remove = () => element.parentNode?.removeChild(element);
  element.setAttribute = (name, value) => {
    attributes.set(String(name), String(value));
    element[String(name)] = String(value);
  };
  element.getAttribute = (name) => attributes.get(String(name)) ?? element[String(name)] ?? null;
  element.hasAttribute = (name) => attributes.has(String(name));
  element.removeAttribute = (name) => attributes.delete(String(name));
  element.getBoundingClientRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 30, width: 100, height: 30, toJSON: () => ({}) });
  element.getClientRects = () => [element.getBoundingClientRect()];
  element.querySelector = () => null;
  element.querySelectorAll = () => [];
  element.matches = () => false;
  element.focus = () => {};
  element.blur = () => {};
  element.click = () => element.__dispatch("click");

  if (element.tagName === "IFRAME") {
    element.contentWindow = createEventTarget({
      postMessage(message) {
        onIframeMessage(element, message);
      },
    });
  }
  return element;
}

function decodeRuntimeError(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  const candidates = [value.trim()];
  try {
    if (/^[A-Za-z0-9+/=_-]+$/.test(value.trim())) {
      candidates.push(Buffer.from(value.trim().replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
    }
  } catch {}
  return candidates.find((item) => /(?:TypeError|ReferenceError|SyntaxError|RangeError|EvalError|URIError):|Cannot (?:read|set) properties|is not a function|(?:turnstile|session_observer)_vm_timeout/i.test(item)) || "";
}

function parseToken(value, field) {
  if (!value) return {};
  if (typeof value === "object") return value;
  try {
    return JSON.parse(String(value));
  } catch {
    return { [field]: String(value) };
  }
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const requirements = input.requirements || {};
  const requirementsToken = String(input.requirements_token || "");
  const flow = String(input.flow || "");
  const deviceId = String(input.device_id || "");
  const sdkUrl = String(input.sdk_url || "https://sentinel.openai.com/sentinel/sdk.js");
  const userAgent = String(input.user_agent || "Mozilla/5.0");
  const location = new URL("https://chatgpt.com/");
  let window;

  const document = createEventTarget({});
  const respondToIframe = (iframe, message) => {
    const result = { cachedChatReq: requirements, cachedProof: requirementsToken };
    queueMicrotask(() => window.__dispatch("message", {
      source: iframe.contentWindow,
      origin: new URL(sdkUrl).origin,
      data: { type: "response", requestId: message?.requestId, result },
    }));
  };

  const documentElement = createElement("html", respondToIframe);
  const head = createElement("head", respondToIframe);
  const body = createElement("body", respondToIframe);
  documentElement.ownerDocument = document;
  head.ownerDocument = document;
  body.ownerDocument = document;
  documentElement.appendChild(head);
  documentElement.appendChild(body);

  const currentScript = createElement("script", respondToIframe);
  currentScript.src = sdkUrl;
  currentScript.setAttribute("src", sdkUrl);
  document.currentScript = currentScript;
  document.scripts = [currentScript];
  document.documentElement = documentElement;
  document.head = head;
  document.body = body;
  document.location = location;
  document.referrer = "https://chatgpt.com/";
  document.readyState = "complete";
  document.visibilityState = "visible";
  document.hidden = false;
  document.createElement = (tag) => {
    const element = createElement(tag, respondToIframe);
    element.ownerDocument = document;
    return element;
  };
  document.createTextNode = (text) => ({ nodeType: 3, textContent: String(text), parentNode: null });
  document.querySelector = () => null;
  document.querySelectorAll = () => [];
  document.getElementById = () => null;
  document.getElementsByTagName = (tag) => String(tag).toLowerCase() === "script" ? document.scripts : [];

  const cookies = new Map([["oai-did", deviceId]]);
  Object.defineProperty(document, "cookie", {
    get: () => [...cookies.entries()].map(([key, value]) => `${key}=${value}`).join("; "),
    set: (value) => {
      const [pair] = String(value).split(";", 1);
      const index = pair.indexOf("=");
      if (index > 0) cookies.set(pair.slice(0, index).trim(), pair.slice(index + 1).trim());
    },
  });

  class Navigator {
    constructor() {
      this.userAgent = userAgent;
      this.language = "en-US";
      this.languages = ["en-US", "en"];
      this.hardwareConcurrency = 16;
      this.deviceMemory = 8;
      this.platform = "Win32";
      this.vendor = "Google Inc.";
      this.product = "Gecko";
      this.cookieEnabled = true;
      this.onLine = true;
      this.webdriver = false;
      this.plugins = [];
      this.mimeTypes = [];
      this.maxTouchPoints = 0;
      this.userAgentData = { brands: [{ brand: "Chromium", version: "149" }, { brand: "Google Chrome", version: "149" }], mobile: false, platform: "Windows" };
    }
  }
  Object.assign(Navigator.prototype, {
    registerProtocolHandler() {},
    sendBeacon() { return true; },
    vibrate() { return true; },
  });

  const localStorage = createStorage();
  const sessionStorage = createStorage();
  localStorage.setItem("STATSIG_LOCAL_STORAGE_INTERNAL_STORE_V4", "{}");
  localStorage.setItem("STATSIG_LOCAL_STORAGE_STABLE_ID", deviceId);
  localStorage.setItem("client-correlated-secret", deviceId.replace(/-/g, ""));
  localStorage.setItem("oai/apps/capExpiresAt", String(Date.now() + 3600000));
  localStorage.setItem("oai-did", deviceId);
  localStorage.setItem("STATSIG_LOCAL_STORAGE_LOGGING_REQUEST", "{}");
  localStorage.setItem("UiState.isNavigationCollapsed.1", "false");
  const timerIds = new Set();
  const intervalIds = new Set();
  const trackedSetTimeout = (callback, delay = 0, ...args) => {
    const id = setTimeout(() => {
      timerIds.delete(id);
      callback(...args);
    }, delay);
    timerIds.add(id);
    return id;
  };
  const trackedClearTimeout = (id) => {
    timerIds.delete(id);
    clearTimeout(id);
  };
  const trackedSetInterval = (callback, delay = 0, ...args) => {
    const id = setInterval(callback, delay, ...args);
    intervalIds.add(id);
    return id;
  };
  const trackedClearInterval = (id) => {
    intervalIds.delete(id);
    clearInterval(id);
  };

  window = createEventTarget({
    document,
    location,
    navigator: new Navigator(),
    localStorage,
    sessionStorage,
    performance,
    crypto: webcrypto,
    screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24 },
    history: { length: 2, state: null, scrollRestoration: "auto", pushState() {}, replaceState() {}, back() {}, forward() {}, go() {} },
    innerWidth: 1920,
    innerHeight: 969,
    outerWidth: 1920,
    outerHeight: 1080,
    devicePixelRatio: 1,
    origin: location.origin,
    isSecureContext: true,
    setTimeout: trackedSetTimeout,
    clearTimeout: trackedClearTimeout,
    setInterval: trackedSetInterval,
    clearInterval: trackedClearInterval,
    queueMicrotask,
    requestAnimationFrame: (callback) => trackedSetTimeout(() => callback(performance.now()), 16),
    cancelAnimationFrame: trackedClearTimeout,
    requestIdleCallback: (callback) => trackedSetTimeout(() => callback({ didTimeout: false, timeRemaining: () => 50 }), 0),
    cancelIdleCallback: trackedClearTimeout,
    atob: (value) => Buffer.from(String(value), "base64").toString("binary"),
    btoa: (value) => Buffer.from(String(value), "binary").toString("base64"),
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    Blob: globalThis.Blob || class {},
    Headers: globalThis.Headers || class {},
    Request: globalThis.Request || class {},
    Response: globalThis.Response || class {},
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" }),
    getComputedStyle: (element) => element?.style || {},
    matchMedia: (query) => ({ matches: false, media: String(query), onchange: null, addEventListener() {}, removeEventListener() {} }),
    postMessage(message) {
      queueMicrotask(() => window.__dispatch("message", { source: window, origin: location.origin, data: message }));
    },
    MutationObserver: class { observe() {} disconnect() {} takeRecords() { return []; } },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } },
    Event: class { constructor(type) { this.type = type; } },
    CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
    HTMLElement: class {},
    Element: class {},
    Node: class {},
    console: { log() {}, info() {}, warn() {}, error() {}, debug() {} },
  });
  window.window = window;
  window.self = window;
  window.top = window;
  window.parent = window;
  window.globalThis = window;
  window.global = window;

  const context = vm.createContext(window);
  new vm.Script(String(input.sdk_source || ""), { filename: sdkUrl }).runInContext(context, { timeout: 5000 });
  if (!context.SentinelSDK?.token) throw new Error("SentinelSDK.token is unavailable");

  const tokenPayload = parseToken(await context.SentinelSDK.token(flow), "t");
  const soPayload = context.SentinelSDK.sessionObserverToken
    ? parseToken(await context.SentinelSDK.sessionObserverToken(flow), "so")
    : {};
  const turnstileError = decodeRuntimeError(tokenPayload.t);
  const soError = decodeRuntimeError(soPayload.so);
  const output = {
    mode: "node-sdk",
    proof_token: String(tokenPayload.p || ""),
    turnstile_token: turnstileError ? "" : String(tokenPayload.t || ""),
    challenge_token: String(tokenPayload.c || requirements.token || ""),
    so_token: soError ? "" : String(soPayload.so || ""),
    token_error: String(tokenPayload.e || ""),
    turnstile_error: turnstileError,
    so_error: String(soPayload.e || soError || ""),
  };
  for (const id of timerIds) clearTimeout(id);
  for (const id of intervalIds) clearInterval(id);
  process.stdout.write(JSON.stringify(output));
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ mode: "node-sdk", error: String(error?.stack || error) }));
  process.exitCode = 1;
});
