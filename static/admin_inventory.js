'use strict';

// Keep the placement chooser bounded, searchable and usable with keyboard input.
const pickerSearch = document.getElementById('plan-table-search');
const placementFilter = document.getElementById('plan-placement-filter');
const pickerButtons = Array.from(document.querySelectorAll('.picker-btn'));
function filterPicker() {
  const query = pickerSearch.value.trim();
  let visible = 0;
  pickerButtons.forEach(button => {
    const placed = button.classList.contains('placed');
    button.hidden = !button.dataset.number.includes(query)
      || (placementFilter.value === 'placed' && !placed)
      || (placementFilter.value === 'unplaced' && placed);
    if (!button.hidden) visible++;
  });
  document.getElementById('plan-picker-empty').hidden = visible > 0;
}
if (pickerSearch && placementFilter) {
  document.querySelector('.plan-picker-tools').hidden = false;
  pickerSearch.addEventListener('input', filterPicker);
  placementFilter.addEventListener('change', filterPicker);
  pickerButtons.forEach(button => button.addEventListener('click', () => {
    pickerButtons.forEach(other => other.setAttribute('aria-pressed', String(other === button)));
  }));
  const picker = document.querySelector('.table-picker');
  new MutationObserver(filterPicker).observe(picker, {subtree: true, attributes: true, attributeFilter: ['class']});
  const plan = document.getElementById('plan-inner');
  function selectMarker(event) {
    const marker = event.target.closest('.plan-marker');
    if (!marker || (event.type === 'keydown' && !['Enter', ' '].includes(event.key))) return;
    event.preventDefault();
    const button = pickerButtons.find(item => item.dataset.number === marker.dataset.number);
    if (button) button.click();
  }
  function labelMarkers() {
    plan.querySelectorAll('.plan-marker').forEach(marker => {
      marker.tabIndex = 0;
      marker.setAttribute('role', 'button');
      marker.setAttribute('aria-label', `Tisch ${marker.dataset.number} verschieben`);
    });
  }
  labelMarkers();
  new MutationObserver(labelMarkers).observe(plan, {childList: true});
  plan.addEventListener('click', selectMarker);
  plan.addEventListener('keydown', selectMarker);
}
