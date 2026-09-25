/* Navigation and settings integration for the preserved source interfaces. */
(() => {
  'use strict';
  const config = window.MetaUro;
  if (!config) return;
  function selectedScope() {
    const select = document.getElementById('disease') || document.getElementById('summary-scope') || document.getElementById('summary-disease');
    const option = select?.selectedOptions?.[0];
    return config.scopes.find(item => (item.source === (option?.dataset.source || config.source)) && [item.id,item.label].includes(option?.dataset.scopeKey || option?.value)) || config.current;
  }
  function exposureMode() {
    if (config.page === 'selection') return 'no';
    const value = document.getElementById('include-subexposures')?.value || new URL(location.href).searchParams.get('include_subexposures') || 'yes';
    return ['no', 'false', '0'].includes(value.toLowerCase()) ? 'no' : 'yes';
  }
  function navigation() {
    const item = selectedScope();
    const mode = exposureMode();
    document.querySelectorAll('[data-uro-nav]').forEach(link => {
      const page = link.dataset.uroNav;
      const productPage = {settings: '/settings', selection: '/study-selection'}[page];
      const url = new URL(productPage || `${item.prefix}/${page === 'summary' ? 'summary' : config.track === 'chatgpt' && item.group_id === 'prostate' ? 'chatgpt' : ''}`, location.origin);
      url.searchParams.set('scope',item.id);
      url.searchParams.set('include_subexposures',mode);
      const exposure = (document.getElementById('exposure') || document.getElementById('selection-exposure'))?.value;
      if(exposure && (page === 'selection' || (config.page === 'selection' && page === 'evidence'))) {
        url.searchParams.set('exposure', exposure);
      }
      if(config.track === 'chatgpt' && item.group_id === 'prostate') url.searchParams.set('track','chatgpt');
      link.href = url.pathname + url.search;
    });
  }
  // Capture before source handlers so a foreign scope never reaches the wrong engine.
  document.addEventListener('change', event => {
    if (['disease','summary-scope','summary-disease'].includes(event.target.id)) {
      const item = selectedScope();
      if (item.source !== config.source) {
        event.stopImmediatePropagation();
        const url = new URL(config.page === 'settings' ? '/settings' : `${item.prefix}/${config.page === 'summary' ? 'summary' : ''}`,location.origin);
        url.searchParams.set('scope',item.id);
        url.searchParams.set('include_subexposures',exposureMode());
        const exposure = document.getElementById('exposure')?.value;
        if(exposure)url.searchParams.set('exposure',exposure);
        location.assign(url.pathname+url.search);return;
      }
      const url = new URL(location.href);url.searchParams.set('scope',item.id);history.replaceState(null,'',url);
      navigation();
    }
    if(event.target.id === 'include-subexposures') {
      navigation();
      if(config.page === 'settings') {
        const url = new URL(location.href);url.searchParams.set('include_subexposures',event.target.value);history.replaceState(null,'',url);
      }
    }
    if(['exposure','selection-exposure'].includes(event.target.id)) navigation();
  },true);
  document.addEventListener('DOMContentLoaded', () => {
    const exposure = new URL(location.href).searchParams.get('exposure');
    if(exposure && document.getElementById('exposure')) document.getElementById('exposure').value=exposure;
    navigation();
    document.getElementById('reset-settings')?.addEventListener('click',()=>{
      const exposure = document.getElementById('include-subexposures');
      if (exposure) {
        exposure.value = 'yes';
        exposure.dispatchEvent(new Event('change', {bubbles: true}));
      }
      window.AnalysisSettings?.reset();
      const status=document.getElementById('settings-save-status');
      if(status)status.textContent=`Defaults restored for ${selectedScope().group.toLowerCase()} preferences.`;
    });
    // Persistent review mutations are disabled; temporary study selection remains available.
    const style=document.createElement('style');
    style.textContent='.verify-btn,.exclude-btn,.report-error-btn,button[onclick*="verifyStudy"],button[onclick*="excludeStudy"]{display:none!important}';document.head.append(style);
  });
  window.addEventListener('analysissettingschange',()=>{
    const status=document.getElementById('settings-save-status');
    if(status) status.textContent='Settings saved in this browser. Saved evidence is unchanged.';
  });
})();
