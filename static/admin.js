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
