/* Local released recovery status only; this never retrieves source evidence. */
(() => {
    const start=async()=>{
        const target=document.getElementById('settings-preprint-status');if(!target)return;
        try{
            const response=await fetch('/api/preprint-recovery-status');const data=await response.json();
            target.textContent=response.ok && data.available ? `${data.contexts} reviewed recovery contexts (${data.records} records) are available in this release.` : 'No reviewed preprint recoveries are available in this release.';
        }catch(_){target.textContent='Preprint recovery availability could not be verified. No live screening is started.';}
    };
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
