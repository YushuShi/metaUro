(() => {
    'use strict';
    const header = document.querySelector('.site-header');
    if (!header) return;
    const sizeHeader = () => document.documentElement.style.setProperty('--site-header-height', `${header.getBoundingClientRect().height}px`);
    sizeHeader();
    if (window.ResizeObserver) new ResizeObserver(sizeHeader).observe(header);
    else window.addEventListener('resize', sizeHeader);

    // Keep population and language choices when moving between views.
    function syncLinks() {
        const current = new URL(location.href);
        const fields = {
            smoking_status: ['filter-smoking-status', 'summary-smoking-status', 'selection-smoking-status'],
            ethnicity: ['filter-ethnicity', 'summary-ethnicity', 'selection-ethnicity'],
            gender: ['filter-gender', 'summary-gender', 'selection-gender'],
            include_subexposures: ['include-subexposures'],
        };
        for (const link of header.querySelectorAll('a[data-site-link]')) {
            const url = new URL(link.href, location.href);
            for (const key of ['exposure', 'outcome']) {
                if (current.searchParams.has(key)) url.searchParams.set(key, current.searchParams.get(key));
            }
            for (const [key, ids] of Object.entries(fields)) {
                const control = ids.map(id => document.getElementById(id)).find(Boolean);
                if (control) url.searchParams.set(key, control.value);
                else if (current.searchParams.has(key)) url.searchParams.set(key, current.searchParams.get(key));
            }
            const exposure = document.getElementById('selection-exposure') || document.getElementById('exposure');
            if (exposure?.value) url.searchParams.set('exposure', exposure.dataset.canonicalExposure || exposure.value);
            const i18n = window.MetaVirI18n || window.MetaNeuroI18n || window.MetaPeeI18n;
            const language = i18n?.getLanguage?.() || document.documentElement.lang || current.searchParams.get('lang');
            if (language) url.searchParams.set('lang', language);
            link.href = url.pathname + url.search + url.hash;
        }
    }
    syncLinks();
    document.addEventListener('change', syncLinks);
    document.addEventListener('submit', event => {
        const form = event.target;
        if (form.method.toLowerCase() !== 'get') return;
        const language = form.querySelector('input[name="lang"]');
        if (language) language.value = document.documentElement.lang;
        const mode = form.querySelector('input[name="include_subexposures"]');
        if (mode) mode.value = document.getElementById('include-subexposures')?.value
            || window.includeSubexposuresMode?.()
            || new URL(location.href).searchParams.get('include_subexposures') || mode.value;
    });
    header.addEventListener('click', event => {
        if (event.target.closest('a[data-site-link]')) syncLinks();
    }, true);
})();
