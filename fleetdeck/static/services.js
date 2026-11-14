"use strict";

// The Services tab. Read-only catalogue from /api/services; the only write is hide/unhide.
const SERVICE_CATEGORY_ORDER = [
  "Media",
  "Home automation",
  "Network",
  "Storage & backup",
  "Monitoring",
  "Security & identity",
  "Development",
  "Productivity",
  "Tools",
  "Virtualization",
  "Other",
  "Support",
];

function servicesEnabled() {
  return !!(state.services && state.services.enabled);
}

let serviceSearchReady = false;

function serviceMatches(service) {
  if (service.hidden && !state.showHiddenServices) return false;
  if (!service.ui && !service.hidden && !state.showSupportServices) return false;
  const query = (state.serviceQuery || "").trim().toLowerCase();
  if (!query) return true;
  return [service.name, service.machine, service.stack, service.image, service.category]
    .some((value) => (value || "").toLowerCase().includes(query));
}

function serviceCard(service) {
  const links = service.links || [];
  const primary = links[0];
  const chips = links.slice(1).map((link) => `<a class="chip service-link" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer" title="${esc(link.url)}">${esc(link.label || link.kind)}</a>`).join("");
  const open = primary
    ? `<a class="button small primary service-open" href="${esc(primary.url)}" target="_blank" rel="noopener noreferrer" title="${esc(primary.url)}">Open</a>`
    : `<span class="muted service-no-link">No link found</span>`;
  const hint = primary ? "" : `<p class="muted service-hint">Add a <code>fleetdeck.url</code> label or a <code>services.links</code> entry.</p>`;
  const action = service.hidden
    ? `<button class="small" data-unhide-service="${esc(service.id)}">Unhide</button>`
    : `<button class="small" data-hide-service="${esc(service.id)}">Hide</button>`;
  const badge = (service.name || "?").slice(0, 1).toUpperCase();
  return `<article class="service-card${service.hidden ? " is-hidden-service" : ""}" data-service-id="${esc(service.id)}">
    <div class="service-head"><span class="service-badge" aria-hidden="true">${esc(badge)}</span>
      <div class="service-title"><h3>${esc(service.name)}</h3><p class="muted">${esc([service.machine, service.stack].filter(Boolean).join(" · "))}</p></div>
      <span class="state" title="${esc(service.state)}"><span class="dot ${DOT[service.state] || ""}"></span></span>
    </div>
    <p class="service-image muted" title="${esc(service.image)}">${esc(service.image || "manual link")}</p>
    <div class="service-links">${open}${chips}</div>
    ${hint}
    <div class="service-actions">${action}</div>
  </article>`;
}

function renderServices() {
  const el = $("#services");
  if (!servicesEnabled()) {
    el.hidden = true;
    return;
  }
  const all = state.services.services || [];
  const shown = all.filter(serviceMatches);
  const hidden = all.filter((service) => service.hidden).length;
  const support = all.filter((service) => !service.ui && !service.hidden).length;
  const query = (state.serviceQuery || "").trim();
  const groups = {};
  for (const service of shown) (groups[service.category] ||= []).push(service);
  const order = SERVICE_CATEGORY_ORDER.concat(Object.keys(groups).filter((name) => !SERVICE_CATEGORY_ORDER.includes(name)));
  const sections = order.filter((name) => groups[name]?.length).map((name) => `<section class="service-category">
      <h2>${esc(name)} <span class="muted">${groups[name].length}</span></h2>
      <div class="service-grid">${groups[name].map(serviceCard).join("")}</div>
    </section>`).join("");
  const empty = query
    ? `<div class="empty"><h3>No services match “${esc(query)}”</h3><p class="muted">Clear the search to show all services.</p><button class="small" data-clear-service-search>Clear search</button></div>`
    : support
      ? `<div class="empty"><h3>No services with a UI found</h3><p class="muted">${support} support container${support === 1 ? " is" : "s are"} hidden. Show them if one has a UI, or add a <code>fleetdeck.url</code> label.</p><button class="small" data-toggle-support-services>Show support containers (${support})</button></div>`
      : `<div class="empty"><h3>No services yet</h3><p class="muted">Run a container check to discover services, or add <code>services.links</code> in the config.</p></div>`;
  const checked = state.services.checked_at ? `Last scan ${ago(state.services.checked_at)}` : "No Docker scan yet";
  const markup = `<div class="services-toolbar">
      <input id="service-search" name="fleetdeck-service-filter" type="search" placeholder="Search services" value="${esc(state.serviceQuery || "")}" autocomplete="off" autocapitalize="off" spellcheck="false" data-1p-ignore="true" data-lpignore="true" aria-label="Search services">
      ${query ? `<button class="small" data-clear-service-search>Clear search</button>` : ""}
      <span class="muted">${esc(checked)}</span>
      <button class="small" data-refresh-services title="Read the last Docker scan again; run Check container updates to refresh it">Reload</button>
      ${hidden ? `<button class="small" data-toggle-hidden-services aria-pressed="${state.showHiddenServices}">${state.showHiddenServices ? "Hide hidden" : `Show hidden (${hidden})`}</button>` : ""}
      ${support ? `<button class="small" data-toggle-support-services aria-pressed="${state.showSupportServices}">${state.showSupportServices ? "Hide support containers" : `Show support containers (${support})`}</button>` : ""}
    </div>${sections || empty}`;
  if (el.innerHTML !== markup) el.innerHTML = markup;
  if (!serviceSearchReady) {
    serviceSearchReady = true;
    state.serviceQuery = "";
    const input = $("#service-search");
    if (input) input.value = "";
  }
}

async function setServiceHidden(id, hidden) {
  try {
    await api("/api/services/hidden", { method: "POST", body: JSON.stringify({ service_ids: [id], hidden }) });
    await load();
  } catch (error) {
    state.errors = { services: error.message };
    render();
  }
}

document.addEventListener("input", (event) => {
  if (event.target.id !== "service-search") return;
  state.serviceQuery = event.target.value;
  renderServices();
  const input = $("#service-search");
  if (input) {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
});

document.addEventListener("click", (event) => {
  const el = event.target.closest("[data-hide-service],[data-unhide-service],[data-toggle-hidden-services],[data-toggle-support-services],[data-refresh-services],[data-clear-service-search]");
  if (!el) return;
  if (el.dataset.clearServiceSearch !== undefined) {
    state.serviceQuery = "";
    render();
    $("#service-search")?.focus();
    return;
  }
  if (el.dataset.hideService) return setServiceHidden(el.dataset.hideService, true);
  if (el.dataset.unhideService) return setServiceHidden(el.dataset.unhideService, false);
  if (el.dataset.toggleHiddenServices !== undefined) {
    state.showHiddenServices = !state.showHiddenServices;
    remember.set("showHiddenServices", state.showHiddenServices);
    return render();
  }
  if (el.dataset.toggleSupportServices !== undefined) {
    state.showSupportServices = !state.showSupportServices;
    remember.set("showSupportServices", state.showSupportServices);
    return render();
  }
  if (el.dataset.refreshServices !== undefined) load(true);
});
