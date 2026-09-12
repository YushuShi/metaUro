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
  function navigation() {
    const item = selectedScope();
    const mode = document.getElementById('include-subexposures')?.value || new URL(location.href).searchParams.get('include_subexposures') || 'yes';
    document.querySelectorAll('[data-uro-nav]').forEach(link => {
      const page = link.dataset.uroNav;
      const url = new URL(page === 'settings' ? '/settings' : `${item.prefix}/${page === 'summary' ? 'summary' : config.track === 'chatgpt' && item.group_id === 'prostate' ? 'chatgpt' : ''}`, location.origin);
      url.searchParams.set('scope',item.id);
      url.searchParams.set('include_subexposures',mode);
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
        const url = new URL(config.page === 'settings' ? '/settings' : `${item.prefix}/`,location.origin);
        url.searchParams.set('scope',item.id);
        url.searchParams.set('include_subexposures',document.getElementById('include-subexposures')?.value || 'yes');
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
  },true);
  document.addEventListener('DOMContentLoaded', () => {
    const exposure = new URL(location.href).searchParams.get('exposure');
    if(exposure && document.getElementById('exposure')) document.getElementById('exposure').value=exposure;
    navigation();
    document.getElementById('reset-settings')?.addEventListener('click',()=>{
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
