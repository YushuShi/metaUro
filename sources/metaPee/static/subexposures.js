(() => {
    let mode = ['no','false','0'].includes(String(new URL(location.href).searchParams.get('include_subexposures') || 'yes').toLowerCase()) ? 'no' : 'yes';
    let modeVersion = 0;
    window.includeSubexposuresMode = () => mode;
    window.subexposureModeVersion = () => modeVersion;
    const originalFetch = window.fetch.bind(window);
    window.fetch = (input, options = {}) => {
        const requestedVersion = modeVersion;
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        if (url.origin === location.origin) {
            url.searchParams.set('include_subexposures', mode);
            if (typeof options.body === 'string' && String(options.headers && options.headers['Content-Type'] || '').includes('application/json')) {
                options = { ...options, body: JSON.stringify({ ...JSON.parse(options.body), include_subexposures: mode }) };
            }
            return originalFetch(url.href, options).then(response => {
                if (requestedVersion !== modeVersion && ['/analyze','/reanalyze'].includes(url.pathname)) {
                    const error = new Error('A newer subexposure mode was selected.');error.name='AbortError';throw error;
                }
                return response;
            });
        }
        return originalFetch(input, options);
    };
    function updateLinks() {
        for (const el of document.querySelectorAll('a[href], object[data]')) {
            const attr = el.tagName === 'OBJECT' ? 'data' : 'href';
            const raw = el.getAttribute(attr);
            if (!raw || raw.startsWith('#')) continue;
            const url = new URL(raw, location.href);
            if (url.origin === location.origin && !url.pathname.startsWith('/static/')) {
                url.searchParams.set('include_subexposures', mode);el.setAttribute(attr,url.pathname+url.search+url.hash);
            }
        }
    }
    document.addEventListener('DOMContentLoaded', () => {
        const toggle = document.getElementById('include-subexposures');
        if (toggle) {toggle.value=mode;toggle.addEventListener('change',()=>{
            mode=toggle.value;modeVersion++;const url=new URL(location.href);url.searchParams.set('include_subexposures',mode);
            const analyze=document.getElementById('analyze-btn');
            if(analyze){history.replaceState(null,'',url);updateLinks();analyze.click();}
            else location.assign(url.href);
        });}
        updateLinks();
    });
})();
