/**
 * 电波推送 · DenpaPush Dashboard
 * bridge 通信 + ECG 心电图 + M3 动态主题 + 界面设置
 */

import {
  argbFromHex,
  hexFromArgb,
  themeFromSourceColor,
  sourceColorFromImage,
} from "./vendor/material-color-utilities.js";

const bridge = window.AstrBotPluginPage;

// ─── State ───
const state = {
  ctx: null,
  status: null,
  subscriptions: {},
  logs: [],
  uiConfig: {
    color_mode: "dynamic",
    brand_color: "#1d9bf0",
    background_mode: "theme",
    custom_background: "#F5F6F8",
    custom_background_dark: "#0C0E13",
    background_image: "",
    background_accent: "",
    corner_radius: 14,
    acrylic_enabled: true,
    material_opacity: 45,
    material_blur: 5,
    material_type: "acrylic",
    font_mode: "misans",
    glow_enabled: true,
    glow_intensity: 15,
    shadow_enabled: true,
    shadow_intensity: 60,
    bg_scrim: 40,
  },
};

const DEFAULT_SOURCE = "#1d9bf0";

// ─── Helpers ───
function currentIsDark() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}

function toast(msg, type = "info") {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

// ─── M3 Palette Application (照搬 denpa_echo 完整实现) ───
const paletteCache = {};
const STATUS_SOURCES = { success: "#1B9C5D", warning: "#C98A1B" };
const statusCache = {};
function statusColors(isDark) {
  const key = isDark ? "d" : "l";
  if (statusCache[key]) return statusCache[key];
  const mk = (src) => {
    const s = isDark
      ? themeFromSourceColor(argbFromHex(src)).schemes.dark
      : themeFromSourceColor(argbFromHex(src)).schemes.light;
    return { fg: hexFromArgb(s.primary), bg: hexFromArgb(s.primaryContainer) };
  };
  const v = { success: mk(STATUS_SOURCES.success), warning: mk(STATUS_SOURCES.warning) };
  statusCache[key] = v;
  return v;
}

function rgbStr(hex) {
  const h = hex.replace("#", "");
  return `${parseInt(h.slice(0, 2), 16)}, ${parseInt(h.slice(2, 4), 16)}, ${parseInt(h.slice(4, 6), 16)}`;
}

function alphaComposite(fg, bg, a) {
  const ph = fg.replace("#", ""), bh = bg.replace("#", "");
  const pr = parseInt(ph.slice(0, 2), 16), pg = parseInt(ph.slice(2, 4), 16), pb = parseInt(ph.slice(4, 6), 16);
  const br = parseInt(bh.slice(0, 2), 16), bg2 = parseInt(bh.slice(2, 4), 16), bb = parseInt(bh.slice(4, 6), 16);
  const r = Math.round(a * pr + (1 - a) * br);
  const g = Math.round(a * pg + (1 - a) * bg2);
  const b = Math.round(a * pb + (1 - a) * bb);
  const to = (x) => x.toString(16).padStart(2, "0");
  return `#${to(r)}${to(g)}${to(b)}`;
}

const SURFACE_TONES = {
  light: { appBg: 98, low: 96, mid: 94, high: 92, highest: 90 },
  dark: { appBg: 6, low: 10, mid: 12, high: 17, highest: 22 },
};
const SCRIM = { low: 0.05, mid: 0.08, high: 0.11, highest: 0.14 };

function derivePalette(sourceHex, isDark) {
  sourceHex = sourceHex || DEFAULT_SOURCE;
  const key = sourceHex.toLowerCase() + (isDark ? ":d" : ":l");
  if (paletteCache[key]) return paletteCache[key];
  const argb = argbFromHex(sourceHex);
  const theme = themeFromSourceColor(argb);
  const s = isDark ? theme.schemes.dark : theme.schemes.light;
  const tp = theme.palettes.primary;
  const neutral = theme.palettes.neutral;
  const nv = theme.palettes.neutralVariant;
  const st = SURFACE_TONES[isDark ? "dark" : "light"];
  const sc = (t) => hexFromArgb(neutral.tone(t));
  const primaryHex = hexFromArgb(s.primary);
  const cLow = sc(st.low), cMid = sc(st.mid), cHigh = sc(st.high), cHighest = sc(st.highest);
  const appBg = sc(st.appBg);
  const bg1 = alphaComposite(primaryHex, cLow, SCRIM.low);
  const bg2 = alphaComposite(primaryHex, cMid, SCRIM.mid);
  const bg3 = alphaComposite(primaryHex, cHigh, SCRIM.high);
  const bg4 = alphaComposite(primaryHex, cHighest, SCRIM.highest);
  const pal = {
    brand: hexFromArgb(s.primary),
    onBrand: hexFromArgb(s.onPrimary),
    surface: hexFromArgb(s.primaryContainer),
    onSurface: hexFromArgb(s.onPrimaryContainer),
    hover: hexFromArgb(tp.tone(isDark ? 76 : 44)),
    pressed: hexFromArgb(tp.tone(isDark ? 84 : 36)),
    tint: hexFromArgb(tp.tone(isDark ? 24 : 90)),
    weak: hexFromArgb(tp.tone(isDark ? 32 : 88)),
    line: hexFromArgb(tp.tone(isDark ? 48 : 60)),
    secondary: hexFromArgb(s.secondary),
    onSecondary: hexFromArgb(s.onSecondary),
    secondaryContainer: hexFromArgb(s.secondaryContainer),
    onSecondaryContainer: hexFromArgb(s.onSecondaryContainer),
    tertiary: hexFromArgb(s.tertiary),
    onTertiary: hexFromArgb(s.onTertiary),
    tertiaryContainer: hexFromArgb(s.tertiaryContainer),
    onTertiaryContainer: hexFromArgb(s.onTertiaryContainer),
    fg1: hexFromArgb(s.onSurface),
    fg2: hexFromArgb(s.onSurfaceVariant),
    fg3: hexFromArgb(nv.tone(isDark ? 66 : 40)),
    fg4: hexFromArgb(s.outlineVariant),
    fgInverted: hexFromArgb(s.inverseOnSurface),
    appBg, bg1, bg2, bg3, bg4,
    bgInv: hexFromArgb(s.inverseSurface),
    stroke1: hexFromArgb(s.outline),
    stroke2: hexFromArgb(s.outlineVariant),
    stroke3: isDark ? hexFromArgb(nv.tone(40)) : hexFromArgb(nv.tone(90)),
    fgTinted: hexFromArgb(tp.tone(isDark ? 80 : 36)),
    fgTinted2: hexFromArgb(tp.tone(isDark ? 70 : 44)),
    popupBg: isDark ? sc(st.high) : sc(st.highest),
    popupFg: hexFromArgb(s.onSurface),
    mica1: sc(isDark ? st.low : st.appBg),
    mica2: sc(isDark ? st.appBg : st.low),
    errorFg: hexFromArgb(s.error),
    errorBg: hexFromArgb(s.errorContainer),
  };
  paletteCache[key] = pal;
  return pal;
}

let _paletteCache = "";
function applyPalette(sourceHex, isDark) {
  const key = `${sourceHex}|${isDark}`;
  if (key === _paletteCache) return;
  _paletteCache = key;
  const p = derivePalette(sourceHex, isDark);
  const st = statusColors(isDark);
  const root = document.documentElement;
  const set = (k, v) => root.style.setProperty(k, v);
  set("--color-brand", p.brand);
  set("--color-brand-on", p.onBrand);
  set("--color-brand-surface", p.surface);
  set("--color-on-brand-surface", p.onSurface);
  set("--color-brand-hover", p.hover);
  set("--color-brand-pressed", p.pressed);
  set("--color-brand-tint", p.tint);
  set("--color-brand-weak", p.weak);
  set("--color-brand-line", p.line);
  set("--color-secondary", p.secondary);
  set("--color-on-secondary", p.onSecondary);
  set("--color-secondary-container", p.secondaryContainer);
  set("--color-on-secondary-container", p.onSecondaryContainer);
  set("--color-tertiary", p.tertiary);
  set("--color-on-tertiary", p.onTertiary);
  set("--color-tertiary-container", p.tertiaryContainer);
  set("--color-on-tertiary-container", p.onTertiaryContainer);
  set("--color-fg-1", p.fg1);
  set("--color-fg-2", p.fg2);
  set("--color-fg-3", p.fg3);
  set("--color-fg-4", p.fg4);
  set("--color-fg-inverted", p.fgInverted);
  set("--color-app-bg", p.appBg);
  set("--color-bg-1", p.bg1);
  set("--color-bg-2", p.bg2);
  set("--color-bg-3", p.bg3);
  set("--color-bg-4", p.bg4);
  set("--color-bg-inverted", p.bgInv);
  set("--color-stroke-1", p.stroke1);
  set("--color-stroke-2", p.stroke2);
  set("--color-stroke-3", p.stroke3);
  set("--color-fg-tinted", p.fgTinted);
  set("--color-fg-tinted-2", p.fgTinted2);
  set("--popup-bg", p.popupBg);
  set("--popup-fg", p.popupFg);
  set("--mica-tint-1", p.mica1);
  set("--mica-tint-2", p.mica2);
  set("--acrylic-rgb", rgbStr(p.bg2));
  set("--acrylic-rgb-low", rgbStr(p.bg1));
  set("--acrylic-rgb-high", rgbStr(p.bg4));
  set("--control-rgb", rgbStr(p.bg3));
  set("--color-success-fg", st.success.fg);
  set("--color-success-bg", st.success.bg);
  set("--color-warning-fg", st.warning.fg);
  set("--color-warning-bg", st.warning.bg);
  set("--color-error-fg", p.errorFg);
  set("--color-error-bg", p.errorBg);
}

// ─── Dynamic Accent from Background Image ───
// 照搬 denpa_echo: MCU sourceColorFromImage 需要 Image 元素，不能传 canvas
const dynamicSourceCache = {};
function applyDynamicAccent(imageSrc) {
  const isDark = currentIsDark();
  const cacheKey = imageSrc.substring(0, 64);
  if (dynamicSourceCache[cacheKey]) {
    applyPalette(dynamicSourceCache[cacheKey], isDark);
    return;
  }
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = async () => {
    try {
      // 缩小到 64x64 再取色，避免处理全分辨率大图卡顿
      const size = 64;
      const cvs = document.createElement("canvas");
      cvs.width = size; cvs.height = size;
      const c = cvs.getContext("2d");
      c.drawImage(img, 0, 0, size, size);
      const small = new Image();
      small.src = cvs.toDataURL("image/png");
      await new Promise((res) => { small.onload = res; });
      const srcArgb = await sourceColorFromImage(small);
      const hex = hexFromArgb(srcArgb);
      dynamicSourceCache[cacheKey] = hex;
      const ui = state.uiConfig;
      if (ui.color_mode === "dynamic" && ui.background_mode === "image") {
        applyPalette(hex, currentIsDark());
        if (ui.background_accent !== hex) {
          ui.background_accent = hex;
          bridge.apiPost("dashboard/ui_config", state.uiConfig).catch(() => {});
        }
      }
    } catch (e) {
      console.warn("[DenpaPush] dynamic accent extraction failed:", e);
    }
  };
  img.src = imageSrc;
}

// ─── UI Config ───
async function loadUiConfig() {
  try {
    const cfg = await bridge.apiGet("dashboard/ui_config");
    if (cfg && typeof cfg === "object") {
      state.uiConfig = { ...state.uiConfig, ...cfg };
    }
  } catch (_) {}
}

function applyUiConfig() {
  const root = document.documentElement;
  const ui = state.uiConfig;
  const isDark = currentIsDark();

  // Brand color
  if (ui.color_mode === "static" && ui.brand_color) {
    applyPalette(ui.brand_color, isDark);
  } else if (ui.color_mode === "dynamic") {
    if (ui.background_mode === "image" && ui.background_image) {
      // 已有提取结果 → 同步套用；否则异步取色
      if (ui.background_accent) {
        applyPalette(ui.background_accent, isDark);
      } else {
        applyDynamicAccent(ui.background_image.startsWith("data:") ? ui.background_image : `./bg?t=${Date.now()}`);
      }
    } else {
      applyPalette(DEFAULT_SOURCE, isDark);
    }
  }

  // Background
  const body = document.body;
  const bgLayer = document.getElementById("bg-layer");
  body.classList.remove("bg-mode-brand-gradient", "bg-mode-custom");
  if (bgLayer) bgLayer.style.backgroundImage = "";
  if (ui.background_mode === "brand_gradient") {
    body.classList.add("bg-mode-brand-gradient");
  } else if (ui.background_mode === "custom") {
    const bg = isDark ? ui.custom_background_dark || "#1a1a1a" : ui.custom_background || "#f5f5f5";
    root.style.setProperty("--color-app-bg", bg);
    body.classList.add("bg-mode-custom");
  } else if (ui.background_mode === "image" && ui.background_image) {
    const bgSrc = ui.background_image.startsWith("data:") ? ui.background_image : `./bg?t=${Date.now()}`;
    if (bgLayer) bgLayer.style.backgroundImage = `url('${bgSrc}')`;
  }

  // Radius
  const r = Math.max(0, Math.min(40, Number(ui.corner_radius ?? 14)));
  root.style.setProperty("--radius-large", `${r}px`);
  root.style.setProperty("--radius-xlarge", `${r}px`);
  root.style.setProperty("--radius-medium", `${Math.min(r, 12)}px`);
  root.style.setProperty("--radius-small", `${Math.min(r, 10)}px`);

  // Material
  root.style.setProperty("--material-opacity", ((ui.material_opacity ?? 45) / 100).toString());
  root.style.setProperty("--material-blur", `${ui.material_blur ?? 5}px`);
  root.style.setProperty("--bg-scrim", (ui.bg_scrim ?? 40) / 100);

  const appEl = document.getElementById("app");
  if (appEl) {
    appEl.classList.toggle("acrylic-off", ui.acrylic_enabled === false);
    appEl.classList.toggle("material-mica", ui.material_type === "mica");
    appEl.classList.toggle("glow-off", ui.glow_enabled === false);
    appEl.classList.toggle("shadow-off", ui.shadow_enabled === false);
    appEl.classList.toggle("font-builtin", ui.font_mode === "builtin");
    appEl.classList.toggle("font-misans", ui.font_mode !== "builtin");
    appEl.classList.toggle("bg-image-active", !!(ui.background_mode === "image" && ui.background_image));
    root.style.setProperty("--glow-strength", ((ui.glow_intensity ?? 15) / 100).toString());
    root.style.setProperty("--shadow-strength", ((ui.shadow_intensity ?? 60) / 100).toString());
  }

  syncSettingsInputs();
  updateBgPreview();
}

function syncSettingsInputs() {
  const ui = state.uiConfig;
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  const setChk = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };

  set("ui-color-mode", ui.color_mode);
  set("ui-bg-mode", ui.background_mode);
  set("ui-material-type", ui.material_type);
  set("ui-font", ui.font_mode);
  set("ui-radius", ui.corner_radius);
  set("ui-brand-color", ui.brand_color);
  set("ui-brand-color-picker", ui.brand_color);
  set("ui-custom-bg", ui.custom_background);
  set("ui-custom-bg-picker", ui.custom_background);
  set("ui-custom-bg-dark", ui.custom_background_dark);
  set("ui-custom-bg-dark-picker", ui.custom_background_dark);
  set("ui-material", ui.material_opacity);
  set("ui-blur", ui.material_blur);
  set("ui-glow", ui.glow_intensity);
  set("ui-shadow", ui.shadow_intensity);
  set("ui-scrim", ui.bg_scrim);
  setChk("ui-acrylic-on", ui.acrylic_enabled);
  setChk("ui-glow-on", ui.glow_enabled);
  setChk("ui-shadow-on", ui.shadow_enabled);

  // Slider labels
  const label = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  label("ui-radius-val", `${ui.corner_radius}px`);
  label("ui-material-val", `${ui.material_opacity}%`);
  label("ui-blur-val", `${ui.material_blur}px`);
  label("ui-glow-val", `${ui.glow_intensity}%`);
  label("ui-shadow-val", `${ui.shadow_intensity}%`);
  label("ui-scrim-val", `${ui.bg_scrim}%`);
}

function updateBgPreview() {
  const ui = state.uiConfig;
  const wrap = document.getElementById("bg-preview-wrap");
  const img = document.getElementById("bg-preview");
  if (ui.background_image && wrap && img) {
    img.src = ui.background_image.startsWith("data:") ? ui.background_image : `./bg?t=${Date.now()}`;
    wrap.style.display = "";
  } else if (wrap) {
    wrap.style.display = "none";
  }
}

function collectUiConfig() {
  const get = (id) => document.getElementById(id)?.value;
  const getChk = (id) => document.getElementById(id)?.checked;
  return {
    color_mode: get("ui-color-mode"),
    background_mode: get("ui-bg-mode"),
    material_type: get("ui-material-type"),
    font_mode: get("ui-font"),
    corner_radius: Number(get("ui-radius")),
    brand_color: get("ui-brand-color"),
    custom_background: get("ui-custom-bg"),
    custom_background_dark: get("ui-custom-bg-dark"),
    material_opacity: Number(get("ui-material")),
    material_blur: Number(get("ui-blur")),
    glow_intensity: Number(get("ui-glow")),
    shadow_intensity: Number(get("ui-shadow")),
    bg_scrim: Number(get("ui-scrim")),
    acrylic_enabled: getChk("ui-acrylic-on"),
    glow_enabled: getChk("ui-glow-on"),
    shadow_enabled: getChk("ui-shadow-on"),
    background_image: state.uiConfig.background_image,
    background_accent: state.uiConfig.background_accent,
  };
}

// ─── Theme Toggle ───
const ICON_SUN = "M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0a.996.996 0 0 0 0-1.41l-1.06-1.06zm1.06-10.96a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36c.39-.39.39-1.03 0-1.41s-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z";
const ICON_MOON = "M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z";

function syncThemeIcon() {
  const pathEl = document.getElementById("icon-theme-path");
  const labelEl = document.getElementById("btn-theme-label");
  if (!pathEl) return;
  const dark = currentIsDark();
  pathEl.setAttribute("d", dark ? ICON_SUN : ICON_MOON);
  if (labelEl) labelEl.textContent = dark ? "亮色主题" : "暗色主题";
}

// ─── ECG Waveform Engine ───
class EcgWaveform {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx2d = canvas.getContext("2d");
    this.offset = 0;
    this.speed = 1.2;
    this.cycleLen = 220;
    this.active = false;
    this._resize();
    this._bindResize();
    this._loop();
  }

  _resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    if (!w || !h) return;
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
  }

  _bindResize() {
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(() => this._resize(), 150); });
  }

  setActive(v) { this.active = v; this.speed = v ? 1.6 : 0.8; }

  _ecgY(x) {
    const p = ((x % this.cycleLen) + this.cycleLen) % this.cycleLen;
    const t = p / this.cycleLen;
    let y = 0;
    if (t > 0.1 && t < 0.2) y = Math.sin((t - 0.1) / 0.1 * Math.PI) * 0.08;
    else if (t > 0.28 && t < 0.32) y = -0.12;
    else if (t > 0.32 && t < 0.36) y = 0.55 * Math.sin((t - 0.32) / 0.04 * Math.PI);
    else if (t > 0.36 && t < 0.40) y = -0.18;
    else if (t > 0.5 && t < 0.65) y = Math.sin((t - 0.5) / 0.15 * Math.PI) * 0.15;
    y += (Math.random() - 0.5) * 0.006;
    return y;
  }

  _loop() {
    this._draw();
    requestAnimationFrame(() => this._loop());
  }

  _draw() {
    const { ctx2d: ctx, w, h } = this;
    if (!w || !h) { this._resize(); return; }
    ctx.clearRect(0, 0, w, h);
    this.offset += this.speed;
    const mid = h * 0.55;
    const amp = h * 0.7;

    // Grid
    ctx.strokeStyle = "rgba(128,128,128,.06)";
    ctx.lineWidth = 1;
    for (let gx = 0; gx < w; gx += 36) { ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, h); ctx.stroke(); }
    for (let gy = 0; gy < h; gy += 36) { ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke(); }

    // Brand color from CSS var
    const brand = getComputedStyle(document.documentElement).getPropertyValue("--color-brand").trim() || "#1d9bf0";

    // Waveform
    const grad = ctx.createLinearGradient(0, 0, w, 0);
    grad.addColorStop(0, brand + "0d");
    grad.addColorStop(0.6, brand + "80");
    grad.addColorStop(1, brand);
    ctx.beginPath();
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    for (let x = 0; x <= w; x += 2) {
      const y = mid - this._ecgY(x + this.offset) * amp;
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Fill
    ctx.lineTo(w, mid);
    ctx.lineTo(0, mid);
    ctx.closePath();
    const fg = ctx.createLinearGradient(0, mid - amp * 0.5, 0, mid);
    fg.addColorStop(0, brand + "14");
    fg.addColorStop(1, brand + "00");
    ctx.fillStyle = fg;
    ctx.fill();

    // Head glow
    const headY = mid - this._ecgY(w + this.offset) * amp;
    ctx.beginPath();
    ctx.arc(w - 1, headY, 3, 0, Math.PI * 2);
    ctx.fillStyle = brand;
    ctx.shadowColor = brand;
    ctx.shadowBlur = 8;
    ctx.fill();
    ctx.shadowBlur = 0;
  }
}

// ─── Render: Status ───
function renderStatus(data) {
  if (!data) return;
  const badge = document.getElementById("monitor-badge");
  const running = data.monitor_running;
  badge.className = `badge ${running ? "badge-success" : "badge-neutral"}`;
  badge.textContent = running ? "● 监控中" : "○ 离线";

  document.getElementById("stat-loop").textContent = running ? "运行中" : "已停止";
  document.getElementById("stat-pushes").textContent = data.total_pushes || 0;
  document.getElementById("stat-subs").textContent = data.total_tracked || 0;
  document.getElementById("stat-interval").textContent = `${data.poll_interval || 5} min`;


  if (ecg) ecg.setActive(running);

  // Dynamic theme: use brand_color from status if dynamic mode
  if (state.uiConfig.color_mode === "dynamic" && data.brand_color) {
    state.uiConfig.background_accent = data.brand_color;
    applyPalette(data.brand_color, currentIsDark());
  }
}

// ─── Render: Subscriptions (管理订阅 tab) ───
function renderSubs(data) {
  if (!data) return;
  const container = document.getElementById("subs-session-list");
  const subTabs = document.getElementById("sub-tabs-subs");
  const sessionSelect = document.getElementById("session-select");
  if (!container) return;
  container.innerHTML = "";
  if (subTabs) subTabs.innerHTML = "";

  const sessions = Object.keys(data);

  // Update session selector dropdown
  if (sessionSelect) {
    const curVal = sessionSelect.value;
    sessionSelect.innerHTML = '<option value="__all__">全部会话</option>';
    sessions.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s.length > 28 ? s.slice(0, 28) + "…" : s;
      sessionSelect.appendChild(opt);
    });
    if (sessions.includes(curVal) || curVal === "__all__") sessionSelect.value = curVal;
  }

  // Sub-tabs in sidebar (flat account list)
  if (subTabs) {
    for (const [session, users] of Object.entries(data)) {
      for (const name of Object.keys(users)) {
        const btn = document.createElement("button");
        btn.className = "sub-tab";
        btn.innerHTML = `<span class="dot" style="background:var(--color-success-fg)"></span>@${escapeHtml(name)}`;
        subTabs.appendChild(btn);
      }
    }
  }

  if (sessions.length === 0) {
    container.innerHTML = '<p style="color:var(--color-fg-3);font-size:13px;padding:12px 0">暂无订阅，使用下方输入框添加</p>';
    return;
  }

  const filterSession = sessionSelect ? sessionSelect.value : "__all__";

  for (const [session, users] of Object.entries(data)) {
    if (filterSession !== "__all__" && session !== filterSession) continue;
    const names = Object.keys(users);
    const isMonitored = state.status?.monitored_sessions?.includes(session);

    const group = document.createElement("div");
    group.style.marginBottom = "var(--space-l)";
    group.innerHTML = `
      <div class="flex" style="align-items:center;justify-content:space-between;margin-bottom:var(--space-s)">
        <span style="font-size:12px;font-family:var(--font-family-mono);color:var(--color-fg-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%">${escapeHtml(session)}</span>
        <label class="flex" style="align-items:center;gap:6px;font-size:12px;color:var(--color-fg-2);cursor:pointer">
          <input type="checkbox" class="monitor-toggle" data-session="${escapeHtml(session)}" ${isMonitored ? "checked" : ""} style="width:16px;height:16px;accent-color:var(--color-brand)" />
          监控
        </label>
      </div>
    `;

    names.forEach(name => {
      const info = users[name];
      const lastCheck = info.last_checked_at
        ? new Date(info.last_checked_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
        : "—";
      const el = document.createElement("div");
      el.className = "tweet-card";
      el.style.background = "color-mix(in srgb, var(--color-brand) 3%, transparent)";
      el.style.borderColor = "color-mix(in srgb, var(--color-brand) 15%, transparent)";
      el.style.marginBottom = "var(--space-s)";
      el.innerHTML = `
        <div class="t-header">
          <div class="t-av" style="background:var(--color-brand)">${name.charAt(0).toUpperCase()}</div>
          <div class="t-meta">
            <div class="t-name">@${escapeHtml(name)}</div>
            <div class="t-handle">最后检查 ${lastCheck}</div>
          </div>
          <button class="btn btn-subtle btn-sm btn-remove" data-name="${escapeHtml(name)}">
            <svg viewBox="0 0 24 24" style="width:14px;height:14px"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
          </button>
        </div>
      `;
      group.appendChild(el);
    });

    container.appendChild(group);
  }

  // Bind remove buttons
  container.querySelectorAll(".btn-remove").forEach(btn => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      try {
        await bridge.apiPost("dashboard/unsubscribe", { username: name });
        toast(`已取消追踪 @${name}`, "success");
        refresh();
      } catch (e) {
        toast(e?.message || "操作失败", "error");
      }
    });
  });

  // Bind monitor toggles
  container.querySelectorAll(".monitor-toggle").forEach(chk => {
    chk.addEventListener("change", async () => {
      const session = chk.dataset.session;
      try {
        await bridge.apiPost("dashboard/toggle_monitor", { session, enabled: chk.checked });
        toast(chk.checked ? "已开启监控" : "已关闭监控", "success");
        refresh();
      } catch (e) {
        toast(e?.message || "操作失败", "error");
        chk.checked = !chk.checked;
      }
    });
  });
}

// ─── Render: Push History ───
function renderHistory(data) {
  const items = data?.history || [];
  const containers = [
    document.getElementById("recent-pushes"),
    document.getElementById("tracking-history"),
  ].filter(Boolean);
  if (!containers.length) return;

  const emptyHtml = '<p style="color:var(--color-fg-3);font-size:13px;padding:12px 0">暂无推送记录</p>';
  if (items.length === 0) {
    containers.forEach(c => { c.innerHTML = emptyHtml; });
    return;
  }

  const fragment = document.createDocumentFragment();
  items.slice(0, 10).forEach(item => {
    const seed = item.seed_color || "var(--color-brand)";
    const pal = item.palette || {};
    const primary = pal.primary || seed;
    const time = item.time ? new Date(item.time).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";

    const el = document.createElement("div");
    el.className = "tweet-card";
    el.style.background = `color-mix(in srgb, ${seed} 5%, transparent)`;
    el.style.borderColor = `color-mix(in srgb, ${seed} 18%, transparent)`;
    el.innerHTML = `
      <div class="t-header">
        <div class="t-av" style="background:${seed}">${(item.screen_name || "?").charAt(0).toUpperCase()}</div>
        <div class="t-meta">
          <div class="t-name" style="color:${primary}">${escapeHtml(item.user_name || item.screen_name)}</div>
          <div class="t-handle">@${escapeHtml(item.screen_name)}</div>
        </div>
        <span class="t-time">${time}</span>
        <span class="t-tag" style="background:color-mix(in srgb, ${seed} 12%, transparent);color:${primary}">✓ 已推送</span>
      </div>
      <div class="t-body">${escapeHtml(item.text || "")}</div>
      ${item.translated_text ? `<div class="t-trans" style="background:color-mix(in srgb, ${seed} 5%, transparent);border-color:${seed}"><div class="label" style="color:${seed}">中文翻译</div>${escapeHtml(item.translated_text)}</div>` : ""}
      <div class="t-palette">
        ${Object.entries(pal).filter(([k, v]) => !k.endsWith("_rgb") && typeof v === "string" && v.startsWith("#")).slice(0, 4).map(([, c]) => `<span style="background:${c}"></span>`).join("")}
        <span class="pal-label">seed: ${seed}</span>
      </div>
    `;
    fragment.appendChild(el);
  });

  containers.forEach(c => {
    c.innerHTML = "";
    c.appendChild(fragment.cloneNode(true));
  });
}

// ─── Render: Logs ───
function renderLogs(data) {
  if (!data) return;
  const list = document.getElementById("log-list");
  const logs = data.logs || data;
  if (!Array.isArray(logs) || logs.length === 0) {
    list.innerHTML = '<p style="color:var(--color-fg-3);font-size:13px;padding:12px 0">等待信号捕获…</p>';
    return;
  }
  list.innerHTML = "";
  logs.slice(0, 40).forEach(log => {
    const time = new Date(log.time).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    const el = document.createElement("div");
    el.className = "log-item";
    el.innerHTML = `
      <span class="log-time">${time}</span>
      <span class="log-badge ${log.type}">${log.type === "push" ? "推送" : log.type === "error" ? "异常" : "信息"}</span>
      <span class="log-text">${escapeHtml(log.message)}</span>
    `;
    list.appendChild(el);
  });
}

// ─── Plugin Config Load ───
async function loadPluginConfig() {
  try {
    const cfg = await bridge.apiGet("dashboard/config");
    if (!cfg) return;
    const set = (id, val) => { const el = document.getElementById(id); if (el && val != null) el.value = val; };
    set("cfg-twitter_auth_token", cfg.twitter_auth_token || "");
    set("cfg-twitter_ct0", cfg.twitter_ct0 || "");
    set("cfg-poll_interval", cfg.poll_interval || 5);
    set("cfg-proxy", cfg.proxy || "");
    set("cfg-text_translate_provider", cfg.text_translate_provider || "");
    set("cfg-image_translate_provider", cfg.image_translate_provider || "");
    set("cfg-image_translate_mode", cfg.image_translate_mode || "multimodal");
    set("cfg-translation_language", cfg.translation_language || "中文");
    set("cfg-color_source", cfg.color_source || "avatar");
    set("cfg-gif_encoder", cfg.gif_encoder || "auto");
    set("cfg-text_translate_prompt", cfg.text_translate_prompt || "");
    set("cfg-image_translate_prompt", cfg.image_translate_prompt || "");
  } catch (e) {
    console.warn("[DenpaPush] load config error:", e);
  }
}

// ─── Refresh ───
async function refresh() {
  try {
    const [status, subs, logs, history] = await Promise.all([
      bridge.apiGet("dashboard/status"),
      bridge.apiGet("dashboard/subscriptions"),
      bridge.apiGet("dashboard/logs"),
      bridge.apiGet("dashboard/history"),
    ]);
    if (status) { state.status = status; renderStatus(status); }
    if (subs) { state.subscriptions = subs; renderSubs(subs); }
    if (logs) { state.logs = logs; renderLogs(logs); }
    if (history) { renderHistory(history); }
  } catch (e) {
    console.warn("[DenpaPush] refresh error:", e);
  }
}

// ─── Tab Switching ───
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  const tabBtn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (tabBtn) tabBtn.classList.add("active");
  document.querySelectorAll(".tab-content").forEach(p => p.classList.remove("active"));
  const panel = document.getElementById(`tab-${name}`);
  if (panel) panel.classList.add("active");
  // Sub-tabs visibility
  const sub = document.getElementById("sub-tabs-subs");
  if (sub) sub.classList.toggle("show", name === "subs");
}

// ─── Init ───
let ecg;

async function init() {
  state.ctx = await bridge.ready();
  await loadUiConfig();
  applyUiConfig();
  syncThemeIcon();

  ecg = new EcgWaveform(document.getElementById("ecg-canvas"));

  // Tab clicks
  document.querySelectorAll(".tab[data-tab]").forEach(tab => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  // Sidebar toggle
  document.getElementById("sidebar-toggle").addEventListener("click", () => {
    document.getElementById("sidebar").classList.toggle("collapsed");
  });

  // Theme toggle
  const themeBtn = document.getElementById("btn-theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", (e) => {
      const html = document.documentElement;
      const switchTheme = () => {
        html.dataset.theme = html.dataset.theme === "dark" ? "light" : "dark";
        syncThemeIcon();
        applyUiConfig();
      };
      const x = e.clientX || window.innerWidth / 2;
      const y = e.clientY || window.innerHeight / 2;
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (document.startViewTransition && !reduce) {
        const r = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
        html.style.setProperty("--vt-x", x + "px");
        html.style.setProperty("--vt-y", y + "px");
        html.style.setProperty("--vt-r", r + "px");
        document.startViewTransition(switchTheme);
      } else {
        switchTheme();
      }
    });
  }

  // Refresh button
  document.getElementById("btn-refresh-all").addEventListener("click", refresh);

  // Add subscription
  document.getElementById("btn-add-sub").addEventListener("click", async () => {
    const input = document.getElementById("add-username");
    const name = input.value.trim().replace(/^@/, "");
    if (!name) return;
    try {
      await bridge.apiPost("dashboard/subscribe", { username: name });
      toast(`已开始追踪 @${name}`, "success");
      input.value = "";
      refresh();
    } catch (e) {
      toast(e?.message || "添加失败", "error");
    }
  });
  document.getElementById("add-username").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("btn-add-sub").click();
  });

  // Session filter change → re-render subscription list
  document.getElementById("session-select")?.addEventListener("change", () => {
    renderSubs(state.subscriptions);
  });

  // ─── Plugin config (schema) load/save ───
  loadPluginConfig();
  document.getElementById("btn-save-config").addEventListener("click", async () => {
    const keys = [
      "twitter_auth_token", "twitter_ct0", "poll_interval",
      "text_translate_provider", "image_translate_provider",
      "image_translate_mode", "translation_language",
      "text_translate_prompt", "image_translate_prompt",
      "color_source", "gif_encoder", "proxy",
    ];
    const payload = {};
    keys.forEach(k => {
      const el = document.getElementById(`cfg-${k}`);
      if (el) payload[k] = el.value;
    });
    // poll_interval → int
    payload.poll_interval = Number(payload.poll_interval) || 5;
    try {
      await bridge.apiPost("dashboard/config", payload);
      toast("配置已保存", "success");
      refresh();
    } catch (e) {
      toast(e?.message || "保存失败", "error");
    }
  });

  // ─── Settings panel bindings ───
  bindSettingsEvents();

  // Initial load + polling
  refresh();
  setInterval(refresh, 30000);

  document.getElementById("app").classList.add("ready");
}

function bindSettingsEvents() {
  // Sliders live update
  const sliders = [
    ["ui-material", "ui-material-val", "%"],
    ["ui-blur", "ui-blur-val", "px"],
    ["ui-glow", "ui-glow-val", "%"],
    ["ui-shadow", "ui-shadow-val", "%"],
    ["ui-scrim", "ui-scrim-val", "%"],
  ];
  sliders.forEach(([id, labelId, suffix]) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", () => {
      document.getElementById(labelId).textContent = el.value + suffix;
      liveApplySettings();
    });
  });

  // Radius
  const radiusEl = document.getElementById("ui-radius");
  if (radiusEl) radiusEl.addEventListener("input", () => {
    document.getElementById("ui-radius-val").textContent = radiusEl.value + "px";
    liveApplySettings();
  });

  // Selects / checkboxes → live apply
  ["ui-color-mode", "ui-bg-mode", "ui-material-type", "ui-font", "ui-acrylic-on", "ui-glow-on", "ui-shadow-on"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", liveApplySettings);
  });

  // Color pickers sync
  const colorPairs = [
    ["ui-brand-color-picker", "ui-brand-color"],
    ["ui-custom-bg-picker", "ui-custom-bg"],
    ["ui-custom-bg-dark-picker", "ui-custom-bg-dark"],
  ];
  colorPairs.forEach(([pickerId, textId]) => {
    const picker = document.getElementById(pickerId);
    const text = document.getElementById(textId);
    if (picker && text) {
      picker.addEventListener("input", () => { text.value = picker.value; liveApplySettings(); });
      text.addEventListener("change", () => { picker.value = text.value; liveApplySettings(); });
    }
  });

  // Save
  document.getElementById("btn-save-ui").addEventListener("click", async () => {
    const cfg = collectUiConfig();
    state.uiConfig = { ...state.uiConfig, ...cfg };
    try {
      await bridge.apiPost("dashboard/ui_config", cfg);
      toast("设置已保存", "success");
    } catch (e) {
      toast("保存失败", "error");
    }
  });

  // Reset
  document.getElementById("btn-reset-ui").addEventListener("click", () => {
    state.uiConfig = {
      color_mode: "dynamic", brand_color: "#1d9bf0", background_mode: "theme",
      custom_background: "#F5F6F8", custom_background_dark: "#0C0E13",
      background_image: "", background_accent: "", corner_radius: 14,
      acrylic_enabled: true, material_opacity: 45, material_blur: 5,
      material_type: "acrylic", font_mode: "misans",
      glow_enabled: true, glow_intensity: 15, shadow_enabled: true,
      shadow_intensity: 60, bg_scrim: 40,
    };
    applyUiConfig();
    toast("已恢复默认");
  });

  // Background image upload
  const bgFile = document.getElementById("bg-image-file");
  if (bgFile) {
    bgFile.addEventListener("change", () => {
      const file = bgFile.files[0];
      if (file) {
        document.getElementById("bg-file-name").textContent = file.name;
      }
    });
  }
  document.getElementById("btn-bg-upload")?.addEventListener("click", async () => {
    const file = bgFile?.files[0];
    if (!file) { toast("请先选择图片", "error"); return; }
    const btn = document.getElementById("btn-bg-upload");
    btn.disabled = true;
    btn.textContent = "上传中...";
    try {
      const resp = await bridge.upload("bg/upload", file);
      // bridge.upload 返回格式不固定，兼容多种结构
      const dataUri = resp.data || (resp.body && resp.body.data) || (typeof resp === "string" && resp.startsWith("data:") ? resp : "");
      if (!dataUri) throw new Error("上传响应中未找到背景图数据");
      state.uiConfig.background_image = dataUri;
      state.uiConfig.background_accent = "";
      state.uiConfig.background_mode = "image";
      document.getElementById("bg-file-name").textContent = (resp && resp.filename) || file.name;
      document.getElementById("ui-bg-mode").value = "image";
      updateBgPreview();
      // 直接应用背景
      const body = document.body;
      body.classList.remove("bg-mode-brand-gradient", "bg-mode-custom");
      const bgLayer = document.getElementById("bg-layer");
      if (bgLayer) bgLayer.style.backgroundImage = `url('${dataUri}')`;
      // 立即触发动态取色
      if (state.uiConfig.color_mode === "dynamic") {
        await applyDynamicAccent(dataUri);
      }
      applyUiConfig();
      // 持久化
      bridge.apiPost("dashboard/ui_config", state.uiConfig).catch(() => {});
      toast("背景图上传成功", "success");
    } catch (e) {
      toast(`上传失败: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "上传";
    }
  });
  document.getElementById("btn-bg-remove")?.addEventListener("click", async () => {
    try {
      await bridge.apiPost("bg/remove", {});
      state.uiConfig.background_image = "";
      state.uiConfig.background_accent = "";
      applyUiConfig();
      toast("背景图已移除");
    } catch (_) {}
  });
}

function liveApplySettings() {
  const cfg = collectUiConfig();
  state.uiConfig = { ...state.uiConfig, ...cfg };
  applyUiConfig();
}

init();
