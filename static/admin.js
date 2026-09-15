document.documentElement.classList.add('js-enabled');
const navigation = document.querySelector('.admin-navigation');
const mobile = matchMedia('(max-width: 760px)');
function updateNavigation() { if (navigation) navigation.open = !mobile.matches; }
updateNavigation();
mobile.addEventListener('change', updateNavigation);

// Native details keeps row actions accessible without JS or overlay clipping.
document.querySelectorAll('.action-menu').forEach(menu => {
  menu.addEventListener('toggle', () => {
    if (menu.open) document.querySelectorAll('.action-menu').forEach(other => {
      if (other !== menu) other.open = false;
    });
  });
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('.action-menu[open]').forEach(menu => {
    menu.open = false;
    menu.querySelector('summary').focus();
  });
});
const tabs = [...document.querySelectorAll('.email-tabs [role=tab]')];
function selectTab(tab) {
  tabs.forEach(item => {
    const selected = item === tab;
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(item.getAttribute('aria-controls'));
    panel.hidden = !selected;
    panel.setAttribute('role', 'tabpanel');
  });
}
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined) { event.preventDefault(); selectTab(tabs[next]); tabs[next].focus(); }
  });
});
if (tabs.length) {
  const active = tabs.find(tab => tab.getAttribute('aria-controls') === location.hash.slice(1)) || tabs[0];
  selectTab(active);
  document.querySelectorAll('.email-panel form').forEach(form => {
    form.action = location.pathname + '#' + form.closest('.email-panel').id;
  });
}

// Keep the full edit page as a no-JavaScript fallback. The dialog reuses its forms.
const detailPanel = document.getElementById('booking-detail-panel');
if (detailPanel && typeof detailPanel.showModal === 'function') {
  const panelBody = document.getElementById('detail-panel-body');
  const panelStatus = document.getElementById('detail-panel-status');
  let detailUrl = null;
  let opener = null;
  let dirty = false;
  let saving = false;
  let requestNumber = 0;
  let savedScroll = 0;

  function status(message, error = false) {
    panelStatus.textContent = message;
    panelStatus.setAttribute('role', error ? 'alert' : 'status');
  }
  function renderDetails(doc, notices = []) {
    const details = doc.querySelector('#booking-details');
    if (!details) throw new Error('Buchungsdetails sind nicht verfügbar. Bitte öffne die vollständige Seite oder melde dich erneut an.');
    panelBody.replaceChildren(...notices.map(node => document.importNode(node, true)), document.importNode(details, true));
    panelBody.querySelectorAll('form').forEach(form => {
      if (!form.hasAttribute('action')) form.action = detailUrl;
    });
    dirty = false;
  }
  async function getDocument(url, options = {}) {
    let response;
    try { response = await fetch(url, {cache: 'no-store', ...options}); }
    catch (_) { throw new Error('Verbindung unterbrochen. Bitte prüfe den Buchungsstand vor einer erneuten Änderung.'); }
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    return {response, doc};
  }
  async function refreshOverview() {
    const {response, doc} = await getDocument(location.href);
    const card = doc.querySelector('.booking-card');
    if (!response.ok || !card) throw new Error('Die Übersicht konnte nicht aktualisiert werden. Bitte lade sie nach dem Schließen neu.');
    const oldCard = document.querySelector('.booking-card');
    const horizontalScroll = oldCard.querySelector('.table-scroll').scrollLeft;
    oldCard.replaceWith(document.importNode(card, true));
    document.querySelector('.booking-card .table-scroll').scrollLeft = horizontalScroll;
    const oldStats = document.querySelector('.stat-cards');
    const newStats = doc.querySelector('.stat-cards');
    if (oldStats && newStats) oldStats.replaceWith(document.importNode(newStats, true));
  }
  document.addEventListener('click', async event => {
    const link = event.target.closest('a[data-booking-detail]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    opener = link;
    detailUrl = link.href;
    document.getElementById('detail-panel-full-page').href = detailUrl;
    dirty = false;
    const request = ++requestNumber;
    panelBody.replaceChildren();
    status('Buchungsdetails werden geladen …');
    savedScroll = window.scrollY;
    document.body.classList.add('detail-panel-open');
    detailPanel.showModal();
    try {
      const {response, doc} = await getDocument(detailUrl);
      if (request !== requestNumber || !detailPanel.open) return;
      if (!response.ok) throw new Error('Die Buchungsdetails konnten nicht geladen werden.');
      renderDetails(doc);
      status('');
    } catch (error) {
      if (request !== requestNumber || !detailPanel.open) return;
      status(error.message || 'Die Verbindung wurde unterbrochen.', true);
      const fallback = document.createElement('a');
      fallback.href = detailUrl;
      fallback.className = 'btn secondary';
      fallback.textContent = 'Vollständige Seite öffnen';
      panelBody.replaceChildren(fallback);
    }
  });
  function closePanel() {
    if (saving) return;
    if (dirty && !confirm('Ungespeicherte Änderungen verwerfen?')) return;
    detailPanel.close();
  }
  document.getElementById('close-detail-panel').addEventListener('click', closePanel);
  detailPanel.addEventListener('cancel', event => {event.preventDefault(); closePanel();});
  detailPanel.addEventListener('close', () => {
    ++requestNumber;
    document.body.classList.remove('detail-panel-open');
    window.scrollTo({top: savedScroll, behavior: 'instant'});
    const target = opener?.isConnected ? opener : [...document.querySelectorAll('[data-booking-detail]')].find(link => link.href === detailUrl);
    (target || document.querySelector('.filter-bar input[type=search]'))?.focus({preventScroll: true});
  });
  function markDirty(event) {
    const form = event.target.closest('form');
    if (form) form.dataset.dirty = 'true';
    dirty = true;
  }
  panelBody.addEventListener('input', markDirty);
  panelBody.addEventListener('change', markDirty);
  panelBody.addEventListener('reset', event => {
    delete event.target.dataset.dirty;
    dirty = Boolean(panelBody.querySelector('form[data-dirty]'));
  });
  panelBody.addEventListener('submit', async event => {
    // Respect the existing cancellation confirmation before intercepting the form.
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (saving) return;
    const form = event.target;
    const otherChanges = [...panelBody.querySelectorAll('form[data-dirty]')].some(other => other !== form);
    if (otherChanges && !confirm('Ungespeicherte Eingaben in den anderen Formularen verwerfen und diese Aktion ausführen?')) return;
    const data = new FormData(form);
    saving = true;
    status('Änderung wird gespeichert …');
    panelBody.inert = true;
    document.getElementById('close-detail-panel').disabled = true;
    try {
      let {response, doc} = await getDocument(form.action, {method: 'POST', body: data});
      const notices = [...doc.querySelectorAll('[data-flash]')];
      if (!doc.querySelector('#booking-details')) {
        if (!response.ok) throw new Error('Die Änderung wurde nicht bestätigt. Bitte öffne die vollständige Seite und prüfe den Buchungsstand.');
        // Cancellation redirects to the overview; reload the current booking in the panel.
        const result = await getDocument(detailUrl);
        if (!result.response.ok) throw new Error('Buchung konnte nach dem Speichern nicht geladen werden. Bitte öffne die vollständige Seite.');
        doc = result.doc;
      }
      renderDetails(doc, notices);
      if (!response.ok) {
        const action = data.get('action');
        const failedForm = [...panelBody.querySelectorAll('form')].find(candidate => candidate.elements.namedItem('action')?.value === action);
        if (failedForm) {
          failedForm.dataset.dirty = 'true';
          for (const name of ['name', 'email', 'phone', 'hours', 'table']) {
            const field = failedForm.elements.namedItem(name);
            if (field && data.has(name)) field.value = data.get(name);
          }
        }
        dirty = true;
        status('Bitte prüfe die Fehlermeldung und deine Eingaben.', true);
      } else {
        await refreshOverview();
        status('Buchungsdetails aktualisiert.');
      }
    } catch (error) {
      status(error.message || 'Speicherstatus unklar. Bitte prüfe die Buchung vor einer erneuten Änderung.', true);
    } finally {
      saving = false;
      panelBody.inert = false;
      document.getElementById('close-detail-panel').disabled = false;
      panelStatus.setAttribute('tabindex', '-1');
      panelStatus.focus({preventScroll: true});
    }
  });
}
