/* Baujat PNG download and a note tied to the actual red leave-one-out cells. */
(() => {
    'use strict';
    function initialize() {
        const image = document.getElementById('baujat-plot');
        const download = document.getElementById('download-baujat-png');
        const table = document.getElementById('loo-tbody');
        const note = document.querySelector('.loo-significance-note');
        const slug = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
        function updateNote() {
            if (note) note.hidden = !table?.querySelector('[data-loo-significant="true"]');
        }
        function updateDownload() {
            if (!download) return;
            const src = image?.getAttribute('src');
            const available = Boolean(src && !image.classList.contains('hidden'));
            download.setAttribute('aria-disabled', available ? 'false' : 'true');
            if (available) download.href = src;
            else download.removeAttribute('href');
            const disease = document.getElementById('disease');
            const exposure = document.getElementById('exposure');
            download.download = `${slug(disease?.selectedOptions?.[0]?.textContent || disease?.value) || 'analysis'}-${slug(exposure?.value) || 'exposure'}-baujat.png`;
        }
        updateNote();
        updateDownload();
        if (table) new MutationObserver(updateNote).observe(table, {childList:true, subtree:true, attributes:true, attributeFilter:['data-loo-significant']});
        if (image) new MutationObserver(updateDownload).observe(image, {attributes:true, attributeFilter:['src', 'class']});
        download?.addEventListener('click', async event => {
            event.preventDefault();
            if (download.getAttribute('aria-disabled') === 'true') return;
            try {
                if (!image.complete || !image.naturalWidth) await image.decode();
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth;
                canvas.height = image.naturalHeight;
                const context = canvas.getContext('2d');
                context.fillStyle = '#ffffff';
                context.fillRect(0, 0, canvas.width, canvas.height);
                context.drawImage(image, 0, 0);
                const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
                if (!blob) throw Error('PNG encoding failed');
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = download.download;
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            } catch (error) {
                download.title = `PNG download unavailable: ${error.message}`;
            }
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
    else initialize();
})();
