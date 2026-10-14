"""
Light / Dark appearance for the Streamlit UI.

WHAT STREAMLIT SUPPORTS (1.38, the pinned version) — and what it does not:
  * The native theme is chosen by `.streamlit/config.toml` (`[theme] base=`) or
    by each viewer in Streamlit's own ⋮ → Settings menu. There is NO supported
    API to switch the theme for one session at runtime. `st._config.set_option`
    is private AND process-wide — it would flip the theme for every connected
    user — so it is deliberately not used.
  * Therefore: `config.toml` sets the native base to **dark** (the first-session
    default), and the in-app Light / Dark control applies a scoped stylesheet
    built from CSS custom properties. The stylesheet targets Streamlit's stable
    `data-testid` / `data-baseweb` attributes and the app's own `sdlc-*`
    classes — never generated `css-xxxx` / `st-emotion-cache-*` class names.

KNOWN LIMITS (documented in the README):
  * `st.dataframe` draws its cells on a <canvas> using the NATIVE theme, which
    CSS cannot recolour. In Light mode the grid canvas is colour-inverted
    (`filter: invert(1) hue-rotate(180deg)`) — readable and close, not exact.
  * Streamlit's own ⋮ → Settings theme picker still exists. If a viewer picks a
    different theme there, native-only parts (the grid canvas, some icons) follow
    that choice; everything else follows this app's switch.

Persistence: the choice lives in `st.session_state[THEME_KEY]` (the radio's
widget key), is mirrored to the `?theme=` URL query parameter (the app already
keeps `?project=` there), and survives reruns, tab changes, project switches
(the session wipe restores it from the URL) and a browser refresh.

Pure helpers (no Streamlit import) so they are unit-testable.
"""

from __future__ import annotations

from typing import Mapping, MutableMapping

THEME_KEY = "ui_theme"
QUERY_PARAM = "theme"
LIGHT = "Light"
DARK = "Dark"
OPTIONS: tuple[str, str] = (LIGHT, DARK)
DEFAULT_THEME = DARK


def normalize(value) -> str | None:
    """'light' / 'Dark' / ['dark'] -> canonical label, or None if invalid."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    for option in OPTIONS:
        if lowered == option.lower():
            return option
    return None


def resolve_theme(session_state: Mapping, query_params: Mapping | None = None) -> str:
    """Effective theme: this session's explicit choice, else the URL's, else Dark."""
    chosen = normalize(session_state.get(THEME_KEY))
    if chosen:
        return chosen
    if query_params is not None:
        try:
            from_url = normalize(query_params.get(QUERY_PARAM))
        except Exception:
            from_url = None
        if from_url:
            return from_url
    return DEFAULT_THEME


def ensure_theme(session_state: MutableMapping, query_params: Mapping | None = None) -> str:
    """Make sure the session holds a valid theme BEFORE the radio is created
    (so the stylesheet emitted at the top of the page already matches it and
    there is no wrong-theme flash). Returns the effective theme."""
    theme = resolve_theme(session_state, query_params)
    if session_state.get(THEME_KEY) != theme:
        session_state[THEME_KEY] = theme
    return theme


def query_value(theme: str) -> str:
    return (normalize(theme) or DEFAULT_THEME).lower()


# --- design tokens -------------------------------------------------------------
# Mirrors Streamlit's own light/dark palettes (no redesign), with two
# accessibility adjustments: filled primary buttons use #d93636 (white text
# 4.6:1; Streamlit's #ff4b4b is 3.4:1) and Light-mode accent text uses #c62828
# (5.6:1 on white). Every text/background pair below is >= 4.5:1; input borders
# are >= 3:1 (WCAG 1.4.11).

_TOKENS: dict[str, dict[str, str]] = {
    DARK: {
        "color-scheme": "dark",
        "--sdlc-bg": "#0e1117",
        "--sdlc-bg-secondary": "#262730",
        "--sdlc-surface": "#1a1c24",
        "--sdlc-input-bg": "#262730",
        "--sdlc-text": "#fafafa",
        "--sdlc-text-muted": "#a3a8b8",
        "--sdlc-text-disabled": "rgba(250, 250, 250, 0.45)",
        "--sdlc-border": "rgba(250, 250, 250, 0.16)",
        "--sdlc-border-strong": "#6b7080",
        "--sdlc-accent": "#ff6b6b",
        "--sdlc-primary-bg": "#d93636",
        "--sdlc-primary-bg-hover": "#c22e2e",
        "--sdlc-on-primary": "#ffffff",
        "--sdlc-hover-bg": "rgba(250, 250, 250, 0.06)",
        "--sdlc-focus": "#79b8ff",
        "--sdlc-code-bg": "#262730",
        "--sdlc-code-text": "#7ee2a8",
        "--sdlc-info-bg": "#1a2b40", "--sdlc-info-text": "#c7e5ff",
        "--sdlc-success-bg": "#173324", "--sdlc-success-text": "#d6f5df",
        "--sdlc-warning-bg": "#3a3416", "--sdlc-warning-text": "#fff3b8",
        "--sdlc-error-bg": "#3d1e22", "--sdlc-error-text": "#ffd9d9",
        "--sdlc-rail-rgb": "250, 250, 250",
        "--sdlc-rail-done": "#4ade80",
        "--sdlc-rail-current": "#60a5fa",
        "--sdlc-rail-failed": "#f87171",
        "--sdlc-rail-todo-text": "rgba(250, 250, 250, 0.55)",
        "--sdlc-rail-readonly-text": "rgba(250, 250, 250, 0.62)",
        "--sdlc-rail-blocked-text": "rgba(250, 250, 250, 0.50)",
        "--sdlc-delta-up": "#4ade80",
        "--sdlc-delta-down": "#ff8a8a",
    },
    LIGHT: {
        "color-scheme": "light",
        "--sdlc-bg": "#ffffff",
        "--sdlc-bg-secondary": "#f0f2f6",
        "--sdlc-surface": "#f7f8fa",
        "--sdlc-input-bg": "#ffffff",
        "--sdlc-text": "#31333f",
        "--sdlc-text-muted": "#555867",
        "--sdlc-text-disabled": "rgba(49, 51, 63, 0.45)",
        "--sdlc-border": "rgba(49, 51, 63, 0.16)",
        "--sdlc-border-strong": "#8a8f9c",
        "--sdlc-accent": "#c62828",
        "--sdlc-primary-bg": "#d93636",
        "--sdlc-primary-bg-hover": "#bf2c2c",
        "--sdlc-on-primary": "#ffffff",
        "--sdlc-hover-bg": "rgba(49, 51, 63, 0.05)",
        "--sdlc-focus": "#1a5fd0",
        "--sdlc-code-bg": "#f0f2f6",
        "--sdlc-code-text": "#14632d",
        "--sdlc-info-bg": "#e8f2fc", "--sdlc-info-text": "#0b4f8a",
        "--sdlc-success-bg": "#e8f7ed", "--sdlc-success-text": "#14632d",
        "--sdlc-warning-bg": "#fff8d6", "--sdlc-warning-text": "#7a5a00",
        "--sdlc-error-bg": "#fdecec", "--sdlc-error-text": "#8a1f28",
        "--sdlc-rail-rgb": "30, 33, 45",
        "--sdlc-rail-done": "#15803d",
        "--sdlc-rail-current": "#1d4ed8",
        "--sdlc-rail-failed": "#b91c1c",
        "--sdlc-rail-todo-text": "#555867",
        "--sdlc-rail-readonly-text": "#555867",
        "--sdlc-rail-blocked-text": "#646876",
        "--sdlc-delta-up": "#15803d",
        "--sdlc-delta-down": "#b91c1c",
    },
}

# Component rules — identical for both themes; only the tokens differ, so the
# two modes can never drift apart structurally. Selectors use stable Streamlit
# `data-testid` / `data-baseweb` hooks (verified against the 1.38 DOM).
_RULES = """
.stApp, [data-testid="stHeader"] {
    background-color: var(--sdlc-bg);
    color: var(--sdlc-text);
}
[data-testid="stSidebar"], [data-testid="stSidebarContent"] {
    background-color: var(--sdlc-bg-secondary);
    color: var(--sdlc-text);
}
.stApp [data-testid="stMarkdownContainer"],
.stApp [data-testid="stMarkdownContainer"] :is(h1, h2, h3, h4, h5, h6),
.stApp [data-testid="stHeadingWithActionElements"],
.stApp [data-testid="stHeading"], .stApp [data-testid="stHeading"] * ,
.stApp [data-testid="stWidgetLabel"], .stApp [data-testid="stWidgetLabel"] p,
.stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricLabel"],
.stApp [data-testid="stMetricLabel"] p,
.stApp [data-testid="stExpander"] summary, .stApp [data-testid="stExpander"] summary p,
.stApp [data-testid="stSpinner"], .stApp [data-testid="stFileUploaderDropzoneInstructions"],
.stApp [data-testid="stToolbar"] button, .stApp [data-testid="stMainMenu"] svg {
    color: var(--sdlc-text);
}
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stCaptionContainer"] p,
.stApp [data-testid="InputInstructions"],
.stApp [data-testid="stFileUploaderDropzoneInstructions"] small {
    color: var(--sdlc-text-muted);
}
/* Streamlit sets the delta colour as an INLINE style -> !important is required */
.stApp [data-testid="stMetricDelta"] { color: var(--sdlc-delta-up) !important; }
.stApp [data-testid="stMetricDelta"]:has([data-testid="stMetricDeltaIcon-Down"]) {
    color: var(--sdlc-delta-down) !important;
}
.stApp [data-testid="stMetricDelta"] :is(div, svg) { color: inherit; }
.stApp hr { border-color: var(--sdlc-border); background-color: var(--sdlc-border); }
/* labels rendered INSIDE widgets inherit the widget's colour (buttons, tabs,
   radios), not the generic markdown colour above */
.stApp :is([data-testid^="stBaseButton-"], [data-baseweb="tab"],
           [data-testid="stAlertContainer"]) [data-testid="stMarkdownContainer"],
.stApp :is([data-testid^="stBaseButton-"], [data-baseweb="tab"])
       [data-testid="stMarkdownContainer"] p {
    color: inherit;
}
.stApp [data-testid="stTooltipIcon"], .stApp [data-testid="stTooltipHoverTarget"] svg {
    color: var(--sdlc-text-muted);
}
.stApp a { color: var(--sdlc-accent); }

/* inline code (markdown + captions) / fenced code in rendered artifacts */
.stApp code {
    background-color: var(--sdlc-code-bg);
    color: var(--sdlc-code-text);
}
.stApp [data-testid="stMarkdownContainer"] pre {
    background-color: var(--sdlc-code-bg);
    border: 1px solid var(--sdlc-border);
}
.stApp [data-testid="stMarkdownContainer"] pre code { color: var(--sdlc-text); }
.stApp [data-testid="stMarkdownContainer"] :is(table, th, td) {
    border-color: var(--sdlc-border);
    color: var(--sdlc-text);
}

/* text inputs, text areas, selects */
.stApp [data-baseweb="input"], .stApp [data-baseweb="textarea"],
.stApp [data-baseweb="base-input"], .stApp [data-baseweb="select"] > div {
    background-color: var(--sdlc-input-bg);
    border-color: var(--sdlc-border-strong);
    color: var(--sdlc-text);
}
.stApp :is(input, textarea) {
    color: var(--sdlc-text);
    -webkit-text-fill-color: var(--sdlc-text);
    caret-color: var(--sdlc-text);
    background-color: transparent;
}
.stApp :is(input, textarea)::placeholder {
    color: var(--sdlc-text-muted);
    -webkit-text-fill-color: var(--sdlc-text-muted);
    opacity: 1;
}
.stApp [data-baseweb="select"] div, .stApp [data-baseweb="select"] svg {
    color: var(--sdlc-text);
}
.stApp :is(input, textarea):disabled {
    color: var(--sdlc-text-disabled);
    -webkit-text-fill-color: var(--sdlc-text-disabled);
}

/* select dropdown + tooltips render in a portal outside .stApp */
[data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"],
[data-baseweb="menu"], [data-baseweb="tooltip"] > div {
    background-color: var(--sdlc-bg-secondary);
    color: var(--sdlc-text);
}
[data-baseweb="popover"] li, [data-baseweb="popover"] [role="option"] {
    color: var(--sdlc-text);
}
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] [aria-selected="true"] {
    background-color: var(--sdlc-hover-bg);
}

/* file uploader */
.stApp [data-testid="stFileUploaderDropzone"] {
    background-color: var(--sdlc-bg-secondary);
    color: var(--sdlc-text);
    border: 1px dashed var(--sdlc-border-strong);
}
.stApp [data-testid="stFileUploaderDropzone"] svg { color: var(--sdlc-text-muted); }

/* buttons */
.stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-minimal"]) {
    background-color: var(--sdlc-bg);
    color: var(--sdlc-text);
    border-color: var(--sdlc-border-strong);
}
.stApp [data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
    background-color: var(--sdlc-bg-secondary);
}
.stApp :is([data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-minimal"]):hover:not(:disabled) {
    border-color: var(--sdlc-accent);
    color: var(--sdlc-accent);
}
.stApp [data-testid="stBaseButton-primary"] {
    background-color: var(--sdlc-primary-bg);
    border-color: var(--sdlc-primary-bg);
    color: var(--sdlc-on-primary);
}
.stApp [data-testid="stBaseButton-primary"]:hover:not(:disabled) {
    background-color: var(--sdlc-primary-bg-hover);
    border-color: var(--sdlc-primary-bg-hover);
    color: var(--sdlc-on-primary);
}
.stApp :is([data-testid^="stBaseButton-"]):disabled {
    background-color: transparent;
    color: var(--sdlc-text-disabled);
    border-color: var(--sdlc-border);
    cursor: not-allowed;
}
/* hover toolbar on dataframes (download / search / fullscreen) */
.stApp [data-testid="stElementToolbar"] > div {
    background-color: var(--sdlc-bg-secondary);
    border: 1px solid var(--sdlc-border);
}
.stApp [data-testid="stBaseButton-elementToolbar"] {
    color: var(--sdlc-text);
    background-color: transparent;
}
.stApp [data-testid="stBaseButton-header"],
.stApp [data-testid="stBaseButton-headerNoPadding"] {
    color: var(--sdlc-text);
    background-color: transparent;
}

/* tabs */
.stApp [data-baseweb="tab-list"] { background-color: transparent; }
.stApp [data-baseweb="tab"] { color: var(--sdlc-text); background-color: transparent; }
.stApp [data-baseweb="tab"] p { color: inherit; }
.stApp [data-baseweb="tab"][aria-selected="true"] { color: var(--sdlc-accent); }
.stApp [data-baseweb="tab-highlight"] { background-color: var(--sdlc-accent); }
.stApp [data-baseweb="tab-border"] { background-color: var(--sdlc-border); }
/* the "more tabs" fade at the right edge of an overflowing tab bar */
.stApp [data-testid="stTabs"]::after {
    background-image: linear-gradient(to right, transparent, var(--sdlc-bg));
}

/* cards, expanders, st.status, bordered containers */
.stApp [data-testid="stExpander"] details {
    border-color: var(--sdlc-border);
    background-color: var(--sdlc-surface);
}
.stApp [data-testid="stExpander"] summary:hover,
.stApp [data-testid="stExpander"] summary:hover p { color: var(--sdlc-accent); }
.stApp [data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--sdlc-border); }

/* alerts: st.info / st.success / st.warning / st.error */
.stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
    background-color: var(--sdlc-info-bg); color: var(--sdlc-info-text);
}
.stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {
    background-color: var(--sdlc-success-bg); color: var(--sdlc-success-text);
}
.stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {
    background-color: var(--sdlc-warning-bg); color: var(--sdlc-warning-text);
}
.stApp [data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {
    background-color: var(--sdlc-error-bg); color: var(--sdlc-error-text);
}
.stApp [data-testid="stAlertContainer"] [data-testid="stMarkdownContainer"],
.stApp [data-testid="stAlertContainer"] [data-testid="stMarkdownContainer"] *,
.stApp [data-testid="stAlertContainer"] svg { color: inherit; }

/* radio (the theme switch itself) */
.stApp [data-testid="stRadio"] label, .stApp [data-testid="stRadio"] p,
.stApp [data-baseweb="radio"] [data-testid="stMarkdownContainer"] p { color: var(--sdlc-text); }
.stApp [data-baseweb="radio"] > div:first-child {
    background-color: var(--sdlc-input-bg);
    box-shadow: inset 0 0 0 1px var(--sdlc-border-strong);
}
.stApp [data-baseweb="radio"] > div:first-child > div { background-color: var(--sdlc-input-bg); }
.stApp [data-baseweb="radio"]:has(input:checked) > div:first-child {
    background-color: var(--sdlc-primary-bg);
    box-shadow: none;
}
.stApp [data-baseweb="radio"]:has(input:checked) > div:first-child > div {
    background-color: var(--sdlc-on-primary);
}

/* visible keyboard focus everywhere (WCAG 2.4.7) */
.stApp :is(button, a, summary, [role="tab"], [role="option"]):focus-visible,
[data-baseweb="popover"] :is([role="option"], li):focus-visible {
    outline: 2px solid var(--sdlc-focus) !important;
    outline-offset: 2px;
}
.stApp :is([data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div):focus-within {
    outline: 2px solid var(--sdlc-focus);
    outline-offset: 1px;
}
.stApp [data-testid="stRadio"] label:has(input:focus-visible),
.stApp [data-testid="stFileUploaderDropzone"]:focus-within {
    outline: 2px solid var(--sdlc-focus);
    outline-offset: 2px;
    border-radius: 6px;
}

/* respect OS / browser accessibility preferences (never overrides the chosen theme) */
@media (prefers-reduced-motion: reduce) {
    .stApp *, .stApp *::before, .stApp *::after {
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
    }
}
@media (prefers-contrast: more) {
    :root {
        --sdlc-border: var(--sdlc-border-strong);
        --sdlc-text-muted: var(--sdlc-text);
    }
}
@media (forced-colors: active) {
    .stApp :focus-visible { outline-color: Highlight !important; }
}
"""

# Light only: the dataframe grid is a <canvas> painted with the NATIVE (dark)
# theme; inverting it is the closest CSS-only approximation (see module notes).
_LIGHT_ONLY_RULES = """
.stApp [data-testid="stDataFrame"] canvas,
.stApp [data-testid="stDataFrameResizable"] canvas {
    filter: invert(1) hue-rotate(180deg);
}
"""


def theme_css(theme: str) -> str:
    """The complete <style> block for `theme` (falls back to Dark)."""
    theme = normalize(theme) or DEFAULT_THEME
    tokens = _TOKENS[theme]
    declarations = "\n".join(f"    {name}: {value};" for name, value in tokens.items())
    extra = _LIGHT_ONLY_RULES if theme == LIGHT else ""
    return (
        f'<style data-sdlc-theme="{theme.lower()}">\n'
        f":root {{\n{declarations}\n}}\n"
        f"{_RULES}{extra}</style>"
    )


def tokens(theme: str) -> dict[str, str]:
    """A copy of the design tokens for `theme` (used by tests / docs)."""
    return dict(_TOKENS[normalize(theme) or DEFAULT_THEME])
