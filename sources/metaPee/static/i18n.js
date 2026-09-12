(function () {
    'use strict';

    const STORAGE_KEY = 'metapee.language';
    const DEFAULT_LANGUAGE = 'en';
    const localizedPlotSources = new WeakMap();
    let localizedPlotReloadSequence = 0;
    const SUPPORTED_LANGUAGES = new Set(['en', 'zh-CN', 'zh-TW', 'nl', 'ko']);
    const SKIP_SELECTOR = '[translate="no"], .notranslate, script, style, code, pre, object';
    const TRANSLATABLE_ATTRIBUTES = ['aria-label', 'alt', 'placeholder', 'title'];
    const textSources = new WeakMap();
    const attributeSources = new WeakMap();
    let translations = {};
    let translationTemplates = [];
    let currentLanguage = DEFAULT_LANGUAGE;
    let observer = null;

    function normalize(value) {
        return String(value || '').trim().replace(/\s+/g, ' ');
    }

    function storedLanguage() {
        try {
            const value = window.localStorage.getItem(STORAGE_KEY);
            return SUPPORTED_LANGUAGES.has(value) ? value : DEFAULT_LANGUAGE;
        } catch (error) {
            return DEFAULT_LANGUAGE;
        }
    }

    function interpolate(value, variables) {
        return String(value).replace(/\{([a-zA-Z0-9_]+)\}/g, function (match, key) {
            return Object.prototype.hasOwnProperty.call(variables || {}, key)
                ? String(variables[key])
                : match;
        });
    }

    function escapeRegExp(value) {
        return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function prepareTranslationTemplates() {
        translationTemplates = Object.keys(translations).filter(function (source) {
            return /\{[a-zA-Z0-9_]+\}/.test(source);
        }).map(function (source) {
            const names = [];
            const parts = [];
            let cursor = 0;
            source.replace(/\{([a-zA-Z0-9_]+)\}/g, function (match, name, offset) {
                parts.push(escapeRegExp(source.slice(cursor, offset)));
                parts.push('(.+?)');
                names.push(name);
                cursor = offset + match.length;
                return match;
            });
            parts.push(escapeRegExp(source.slice(cursor)));
            return { source, names, pattern: new RegExp('^' + parts.join('') + '$') };
        });
    }

    function findTranslation(source, locale) {
        const exact = translations[source];
        if (exact && exact[locale]) return { value: exact[locale], variables: {} };
        for (const template of translationTemplates) {
            const match = source.match(template.pattern);
            const entry = translations[template.source];
            if (!match || !entry || !entry[locale]) continue;
            const variables = {};
            template.names.forEach(function (name, index) {
                variables[name] = match[index + 1];
            });
            return { value: entry[locale], variables };
        }
        return null;
    }

    function translate(source, variables, language) {
        const locale = language || currentLanguage;
        const rawSource = String(source || '');
        const leading = (rawSource.match(/^\s*/) || [''])[0];
        const trailing = (rawSource.match(/\s*$/) || [''])[0];
        const normalizedSource = normalize(rawSource);
        if (!normalizedSource) return rawSource;
        if (locale === DEFAULT_LANGUAGE) {
            return leading + interpolate(normalizedSource, variables || {}) + trailing;
        }
        const match = findTranslation(normalizedSource, locale);
        const mergedVariables = Object.assign({}, match ? match.variables : {}, variables || {});
        return leading + interpolate(match ? match.value : normalizedSource, mergedVariables) + trailing;
    }

    function isProtected(node) {
        const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
        return !element || Boolean(element.closest(SKIP_SELECTOR));
    }

    function textSource(node) {
        if (textSources.has(node)) return textSources.get(node);
        const raw = node.nodeValue || '';
        const leading = (raw.match(/^\s*/) || [''])[0];
        const trailing = (raw.match(/\s*$/) || [''])[0];
        const source = normalize(raw);
        const stored = { leading, trailing, source };
        textSources.set(node, stored);
        return stored;
    }

    function translateTextNode(node) {
        if (!node || node.nodeType !== Node.TEXT_NODE || isProtected(node)) return;
        const stored = textSource(node);
        if (!stored.source) return;
        const value = translate(stored.source);
        const nextValue = stored.leading + value + stored.trailing;
        if (node.nodeValue !== nextValue) node.nodeValue = nextValue;
    }

    function translateAttributes(element) {
        if (!element || element.nodeType !== Node.ELEMENT_NODE || isProtected(element)) return;
        let sources = attributeSources.get(element);
        if (!sources) {
            sources = {};
            attributeSources.set(element, sources);
        }
        TRANSLATABLE_ATTRIBUTES.forEach(function (attribute) {
            if (!element.hasAttribute(attribute)) return;
            if (!Object.prototype.hasOwnProperty.call(sources, attribute)) {
                sources[attribute] = normalize(element.getAttribute(attribute));
            }
            const source = sources[attribute];
            if (!source) return;
            const value = translate(source);
            if (element.getAttribute(attribute) !== value) element.setAttribute(attribute, value);
        });
    }

    function translateSubtree(root) {
        if (!root) return;
        if (root.nodeType === Node.TEXT_NODE) {
            translateTextNode(root);
            return;
        }
        if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
        if (root.nodeType === Node.ELEMENT_NODE) translateAttributes(root);
        const walker = document.createTreeWalker(
            root,
            NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
            {
                acceptNode: function (node) {
                    return isProtected(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
                }
            }
        );
        let node = walker.nextNode();
        while (node) {
            if (node.nodeType === Node.TEXT_NODE) translateTextNode(node);
            else translateAttributes(node);
            node = walker.nextNode();
        }
    }


    function localizedPlotUrl(source, language, reloadSequence) {
        const url = new URL(source, window.location.href);
        url.searchParams.delete('lang');
        url.searchParams.delete('plot_reload');
        if (language !== DEFAULT_LANGUAGE) url.searchParams.set('lang', language);
        url.searchParams.set('plot_reload', language + '-' + reloadSequence);
        return url.pathname + url.search + url.hash;
    }

    function updateLocalizedPlotElement(element, language, reloadSequence) {
        const attribute = element.localName === 'object' ? 'data' : 'href';
        let source = localizedPlotSources.get(element);
        if (!source) {
            source = element.getAttribute(attribute);
            if (!source) return;
            localizedPlotSources.set(element, source);
        }
        element.setAttribute(attribute, localizedPlotUrl(source, language, reloadSequence));
    }

    function updateLocalizedPlotUrls(language) {
        localizedPlotReloadSequence += 1;
        const elements = Array.from(document.querySelectorAll('[data-localized-plot]'));
        elements.filter(function (element) {
            return element.localName !== 'object';
        }).forEach(function (element) {
            updateLocalizedPlotElement(element, language, localizedPlotReloadSequence);
        });
        elements.filter(function (element) {
            return element.localName === 'object';
        }).forEach(function (element) {
            let source = localizedPlotSources.get(element);
            if (!source) {
                source = element.getAttribute('data');
                if (!source) return;
            }
            const replacement = element.cloneNode(true);
            replacement.setAttribute(
                'data',
                localizedPlotUrl(source, language, localizedPlotReloadSequence)
            );
            localizedPlotSources.set(replacement, source);
            element.replaceWith(replacement);
        });
    }

    function updateMenuState() {
        document.querySelectorAll('[data-language]').forEach(function (button) {
            const selected = button.dataset.language === currentLanguage;
            button.setAttribute('aria-checked', String(selected));
            button.classList.toggle('is-selected', selected);
        });
    }

    function setLanguage(language, options) {
        const locale = SUPPORTED_LANGUAGES.has(language) ? language : DEFAULT_LANGUAGE;
        currentLanguage = locale;
        document.documentElement.lang = locale;
        if (!options || options.persist !== false) {
            try {
                window.localStorage.setItem(STORAGE_KEY, locale);
            } catch (error) {
                // Translation still works when browser storage is unavailable.
            }
        }
        translateSubtree(document.documentElement);
        updateMenuState();
        window.dispatchEvent(new CustomEvent('metapee:languagechange', { detail: { language: locale } }));
    }

    function closeMenus(returnFocus) {
        document.querySelectorAll('[data-language-picker]').forEach(function (picker) {
            const toggle = picker.querySelector('[data-language-toggle]');
            const menu = picker.querySelector('[data-language-menu]');
            if (!toggle || !menu || menu.hidden) return;
            menu.hidden = true;
            toggle.setAttribute('aria-expanded', 'false');
            if (returnFocus) toggle.focus();
        });
    }

    function initializeMenu(picker) {
        const toggle = picker.querySelector('[data-language-toggle]');
        const menu = picker.querySelector('[data-language-menu]');
        if (!toggle || !menu) return;
        toggle.addEventListener('click', function () {
            const willOpen = menu.hidden;
            closeMenus(false);
            menu.hidden = !willOpen;
            toggle.setAttribute('aria-expanded', String(willOpen));
            if (willOpen) {
                const selected = menu.querySelector('[aria-checked="true"]');
                (selected || menu.querySelector('[data-language]')).focus();
            }
        });
        menu.addEventListener('click', function (event) {
            const option = event.target.closest('[data-language]');
            if (!option) return;
            setLanguage(option.dataset.language);
            closeMenus(true);
        });
        menu.addEventListener('keydown', function (event) {
            const options = Array.from(menu.querySelectorAll('[data-language]'));
            const currentIndex = options.indexOf(document.activeElement);
            if (event.key === 'Escape') {
                event.preventDefault();
                closeMenus(true);
            } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                const direction = event.key === 'ArrowDown' ? 1 : -1;
                const nextIndex = (currentIndex + direction + options.length) % options.length;
                options[nextIndex].focus();
            }
        });
    }

    function observeDynamicContent() {
        observer = new MutationObserver(function (mutations) {
            mutations.forEach(function (mutation) {
                mutation.addedNodes.forEach(translateSubtree);
                if (mutation.type === 'characterData') translateTextNode(mutation.target);
            });
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    async function initialize() {
        try {
            const response = await fetch('/static/i18n-translations.json?v=1.4');
            if (!response.ok) throw new Error('Translation catalogue could not be loaded.');
            translations = await response.json();
            prepareTranslationTemplates();
        } catch (error) {
            console.error('MetaPee translations unavailable:', error);
        }
        document.querySelectorAll('[data-language-picker]').forEach(initializeMenu);
        document.addEventListener('click', function (event) {
            if (!event.target.closest('[data-language-picker]')) closeMenus(false);
        });
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') closeMenus(true);
        });
        setLanguage(storedLanguage(), { persist: false });
        observeDynamicContent();
    }

    window.MetaPeeI18n = {
        t: translate,
        setLanguage,
        getLanguage: function () { return currentLanguage; },
        supportedLanguages: Array.from(SUPPORTED_LANGUAGES)
    };

    window.addEventListener('metapee:languagechange', function (event) {
        updateLocalizedPlotUrls((event.detail && event.detail.language) || DEFAULT_LANGUAGE);
    });

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
}());
