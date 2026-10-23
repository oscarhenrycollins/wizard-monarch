/* Appearance client v1: framework-neutral, no shell selectors or globals. */
(() => {
  'use strict';
  const root = document.documentElement;
  const names = ['canvas','surface','surface-raised','text','text-secondary','text-muted','border','accent','accent-soft','on-accent','reader-surface'];
  const fallback = {canvas:'#05030c',surface:'rgba(18,12,38,.58)','surface-raised':'rgba(28,19,58,.92)',text:'#f3efff','text-secondary':'#b9b0d8','text-muted':'#a59abf',border:'rgba(205,190,255,.24)',accent:'#b99bff','accent-soft':'rgba(143,99,255,.16)','on-accent':'#160b32','reader-surface':'#0e0919'};
  const trustedHost = location.origin; // Preview only. Installed app manifest supplies its exact trusted host origin.
  let current = null, connected = false;
  function valid(a) {
    if (!a || a.version !== 1 || !Number.isSafeInteger(a.revision) || a.revision < 0) return false;
    if (!['window','immersive','standalone'].includes(a.presentation)) return false;
    if (!['dark','light'].includes(a.colorScheme) || typeof a.skin !== 'string' || !/^[a-z0-9-]{1,40}$/.test(a.skin)) return false;
    if (!a.tokens || typeof a.tokens !== 'object') return false;
    return names.every(k => typeof a.tokens[k] === 'string' && a.tokens[k].length < 96 && CSS.supports('color',a.tokens[k]) && !/[;{}]/.test(a.tokens[k]));
  }
  function apply(a) {
    if (!valid(a)) return false;
    if (connected && current && a.revision < current.revision) return false;
    for (const k of names) root.style.setProperty('--wz-'+k,a.tokens[k]);
    root.style.setProperty('--wz-font-ui','ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif');
    root.style.setProperty('--wz-radius','14px');
    root.style.colorScheme = a.colorScheme;
    root.dataset.skin=a.skin; root.dataset.presentation=a.presentation; root.dataset.colorScheme=a.colorScheme;
    root.dataset.comparison=a.comparison==='original'?'original':'unified';
    current=Object.freeze({...a,tokens:Object.freeze({...a.tokens})});
    dispatchEvent(new CustomEvent('wizard:theme',{detail:current}));
    return true;
  }
  apply({version:1,revision:0,skin:'nebula',presentation:'standalone',colorScheme:'dark',comparison:'unified',tokens:fallback});
  addEventListener('message',e => {
    if (parent===window || e.source!==parent || e.origin!==trustedHost || e.data?.type!=='wizard:appearance') return;
    if (apply(e.data.appearance)) { connected=true; parent.postMessage({type:'wizard:appearance:ack',version:1,revision:current.revision},trustedHost); }
  });
  window.WizardTheme={get current(){return current},get connected(){return connected},version:1};
  if (parent!==window) parent.postMessage({type:'wizard:appearance:ready',version:1,app:root.dataset.app||'unknown'},trustedHost);
})();
