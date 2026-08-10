from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_theme_bootstrap_toggle_and_accessibility_contract():
    html = ROOT.joinpath("gui/index.html").read_text(encoding="utf-8")
    app = ROOT.joinpath("gui/app.js").read_text(encoding="utf-8")
    styles = ROOT.joinpath("gui/styles.css").read_text(encoding="utf-8")

    assert html.index('document.documentElement.dataset.theme = theme;') < html.index('/gui/styles.css?v=')
    assert 'id="theme-toggle"' in html
    assert 'type="button"' in html
    assert 'aria-pressed="false"' in html
    assert '<div class="rail-health-row">' in html
    assert html.index('id="api-chip"') < html.index('id="theme-toggle-label"')
    assert '<span id="theme-toggle-label" hidden>' in html
    assert 'const THEME_STORAGE_KEY = "fxalpha.theme";' in app
    assert 'document.documentElement.dataset.theme = normalized;' in app
    assert 'window.localStorage?.setItem(THEME_STORAGE_KEY, normalized);' in app
    assert 'document.getElementById("theme-toggle")?.addEventListener("click"' in app
    assert 'html[data-theme="light"]' in styles
    assert '--fx-neutral-surface-rgb: 255, 255, 255;' in styles
    assert '--fx-blue-text: #1d4ed8;' in styles
    assert '.rail-health-row {' in styles
    assert 'min-width: 58px;' in styles
    assert 'min-height: 34px;' in styles
    assert 'html[data-theme="light"] body .research-flow-tracker' in styles
    assert 'html[data-theme="light"] body :is(.research-flow-tracker.is-blocked, .research-flow-current.is-blocked)' in styles
    assert 'html[data-theme="light"] body .compact-system-strip span' in styles
    assert 'html[data-theme="light"] body #panel-research .progress-cockpit.is-blocked' in styles
    assert 'linear-gradient(100deg, rgba(180, 35, 53, 0.045), transparent 28%)' in styles
    assert '/* Light theme v3: platform-wide clean canvas' in styles
    assert '--fx-structural-surface: #ffffff;' in styles
    assert 'html[data-theme="light"] body .panel :is(div, article, section, aside, form, header, nav):is(' in styles
    assert '[class*="-card"]' in styles
    assert '[class*="-panel"]' in styles
    assert '[class*="-hero"]' in styles
    assert 'background: linear-gradient(90deg, var(--fx-danger-soft), transparent 22%), #ffffff;' in styles
    assert 'background: linear-gradient(90deg, var(--fx-warning-soft), transparent 22%), #ffffff;' in styles
    assert 'background: linear-gradient(90deg, var(--fx-success-soft), transparent 22%), #ffffff;' in styles
    assert 'background: linear-gradient(90deg, var(--fx-info-soft), transparent 22%), #ffffff;' in styles
    for panel_id in (
        '#panel-overview',
        '#panel-data-foundation',
        '#panel-research',
        '#panel-library',
        '#panel-model-research',
        '#panel-trading',
    ):
        assert panel_id in styles
    assert 'background: rgba(2, 6, 23,' not in styles
    assert 'background: rgba(15, 23, 42,' not in styles
    assert ':focus-visible' in styles
    assert '@media (prefers-reduced-motion: reduce)' in styles
    assert styles.rfind('html[data-theme="light"] body #panel-trading') > styles.rfind('--console-fill: rgba(var(--fx-neutral-deep-rgb), 0.62);')


def test_gui_read_path_uses_compact_projection_and_visibility_aware_polling():
    app = ROOT.joinpath("gui/app.js").read_text(encoding="utf-8")
    api = ROOT.joinpath("api_server.py").read_text(encoding="utf-8")

    for marker in (
        '/factor/library/audit/status${activePanel === "library" ? "" : "?compact=true"}',
        '${MODEL_API_PREFIX}/registry?status=${wantsPaperModelCatalog ? "production" : "library"}&compact=true',
        '/trade/status?compact=true',
        '/paper/status${wantsFullPaperFleet ? "" : "?compact=true"}',
    ):
        assert marker in app
    assert 'if (document.hidden) return;' in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert 'setPanelBusy(activePanel, true);' in app
    assert 'setPanelBusy(activePanel, false);' in app
    assert 'gzip.compress(body, compresslevel=5)' in api
    assert 'public, max-age=31536000, immutable' in api


def test_design_guide_defines_both_themes_as_one_component_contract():
    guide = ROOT.joinpath("gui/GUI_DESIGN_GUIDE.md").read_text(encoding="utf-8")

    assert '### 3.2 深浅主题契约' in guide
    assert '不得改变业务状态、DOM 数据契约、权限或操作流程' in guide
    assert '深色与浅色主题' in guide
