/* Shared container presentation. Provider actions remain separate and server-enforced. */
function containerFacts(row) {
  const update = row.image_update === true || (row.image_update == null && row.status === "update");
  const review = !!(row.advisory || row.newer_version || row.pinned_digest || row.deferred_hours || ["advisory", "deferred"].includes(row.status));
  const failure = row.status === "failed" || (!!row.check_issue && row.check_issue !== "missing_result");
  const coverage = ["local", "excluded", "unknown"].includes(row.status) || row.check_issue === "missing_result";
  const current = !failure && row.status !== "unknown" && !row.check_issue && (row.status === "current" || (row.image_update === false && !row.pinned_digest && !row.deferred_hours && ["fleetdeck_button", "fleetdeck_schedule", "dockhand_schedule"].includes(row.source)));
  return { update, review, failure, coverage, current };
}

function containerView(v) {
  if (v.dockhand) {
    const dh = v.dockhand;
    return { provider: "dockhand", rows: dh.containers || [], checking: !!dh.checking,
      stale: !!dh.stale || !!dh.error || ["stopped", "offline"].includes(v.target.status), error: dh.error, checkError: dh.check_error,
      historyError: dh.history_error, syncedAt: dh.synced_at, checkedAt: dh.last_check_at,
      scheduledAt: dh.scheduled_check_at, scheduledStatus: dh.scheduled_status,
      scheduledErrors: dh.scheduled_errors, scheduledChecked: dh.scheduled_checked };
  }
  if (!dockerEnabled(v)) return { provider: dockerSettings(v).mode, rows: [], checking: false, stale: false };
  const r = v.stacks, rows = [];
  for (const stack of r?.stacks || []) {
    for (const service of stack.services || []) {
      const running = ["running", "restarting"].includes(service.status);
      const status = stack.error ? "failed" : !running ? "excluded" : service.outdated ? "update" : service.registry === "unknown" ? "unknown" : "current";
      rows.push({ container_id: `${stack.name}:${service.name}`, name: service.name, stack: stack.name,
        image: service.image, state: service.status, status, checked_at: r.checked_at,
        image_update: running && service.outdated ? true : status === "current" ? false : null,
        reported_at: running && service.outdated ? r.checked_at : null, check_issue: stack.error ? "stack" : null,
        source: "fleetdeck_native", nativeStack: stack, image_id: service.image_id, tag_id: service.tag_id, coverage_reason: !running ? "Stopped service · not checked" : status === "unknown" ? service.registry_note : null });
    }
    if (stack.error && !stack.services?.length) rows.push({ name: stack.name, stack: stack.name, image: "",
      status: "failed", state: "Stack check unavailable", check_issue: "stack", checked_at: r.checked_at, source: "fleetdeck_native", nativeStack: stack });
  }
  return { provider: "native", rows, checking: ["stacks", "stack-update"].includes(v.active_job?.action),
    stale: !!r?.error || (!v.target.updatable && !!r), error: r?.error, checkedAt: r?.checked_at, syncedAt: r?.checked_at,
    emptyStacks: r?.docker === "yes" && Array.isArray(r.stacks) && !r.stacks.length && !r.error && Number.isFinite(Date.parse(r.checked_at)) };
}

function containerSummary(v) {
  const view = containerView(v), rows = view.rows, facts = rows.map(containerFacts);
  const count = key => facts.filter(f => f[key]).length;
  const useful = view.emptyStacks || facts.some((f, i) => f.update || f.current || (f.review && rows[i].image_update === false && !f.failure && rows[i].status !== "unknown"));
  return { ...view, updates: count("update"), reviews: count("review"), coverage: count("coverage"), failures: count("failure"),
    current: count("current"), usable: useful, unknown: !useful, value: useful ? count("update") : "—" };
}

function containerStatus(v) {
  const policy = dockerSettings(v), s = containerSummary(v);
  let symbol = "help", label = "Not checked", tone = "muted";
  if (policy.mode === "disabled") label = "Off";
  else if (!v.dockhand && !dockerEnabled(v)) { symbol = "external"; label = "Monitored externally"; }
  else if (v.active_job?.action === "dockhand-update") return '<span class="status-inline muted"><span class="spin"></span>Updating containers…</span>';
  else if (s.checking) return '<span class="status-inline muted"><span class="spin"></span>Checking containers…</span>';
  else if (v.target.status === "stopped") { symbol = "power"; label = "Not running"; tone = "unavailable"; }
  else if (v.target.status === "offline") { symbol = "power"; label = "Offline"; tone = "unavailable"; }
  else if (s.error || s.checkError) { symbol = "alert"; label = "Check unavailable"; tone = "bad"; }
  else if (s.stale && (s.rows.length || s.emptyStacks)) { symbol = "clock"; label = "Last-known results"; }
  else if (s.failures) { symbol = "alert"; label = s.usable ? "Partial results" : "Check failed"; tone = s.usable ? "warn" : "bad"; }
  else if (s.historyError || s.scheduledStatus === "failed") { symbol = "alert"; label = "History warning"; tone = "warn"; }
  else if (s.emptyStacks) { symbol = "layersOff"; label = "No stacks found"; }
  else if (s.updates) { symbol = "container"; label = "Updates available"; tone = "container-tone"; }
  else if (s.reviews) { symbol = "tag"; label = "Review needed"; tone = "container-review-tone"; }
  else if (s.current || s.coverage) { symbol = "check"; label = "Up to date"; tone = "ok"; }
  return `<span class="status-inline ${tone}">${symbol ? icon(symbol) : ""}<span>${label}</span></span>`;
}

function containerCheckTime(v, withLabel = false) {
  const policy = dockerSettings(v), s = containerSummary(v);
  if (policy.mode === "disabled") return '<span class="muted">Off</span>';
  if (!v.dockhand && !dockerEnabled(v)) return "";
  const lastKnown = s.stale ? '<span class="cell-detail">Last known</span>' : "";
  const relevant = s.rows.filter(r => !["local", "excluded"].includes(r.status));
  const dated = relevant.filter(r => r.checked_at && Number.isFinite(Date.parse(r.checked_at)));
  const times = dated.map(r => Date.parse(r.checked_at));
  const updated = relevant.filter(r => !r.checked_at && r.source === "fleetdeck_update" && r.status === "current" && Number.isFinite(Date.parse(r.updated_at)));
  if (!times.length) {
    if (s.emptyStacks) return `<span title="Last completed native scan found no Compose stacks; standalone containers are not covered">${withLabel ? "Checked " : ""}${ago(s.checkedAt)}${lastKnown}</span>`;
    if (updated.length) return `<span title="Confirmed Dockhand update time; not a registry-check timestamp">Updated ${ago(new Date(Math.max(...updated.map(r => Date.parse(r.updated_at)))).toISOString())}${lastKnown}</span>`;
    if (s.provider === "native" && s.checkedAt && s.error) return `<span title="Latest native container check attempt">${withLabel ? "Attempt " : ""}${ago(s.checkedAt)}<span class="cell-detail">Latest attempt</span></span>`;
    return `<span>${s.rows.length && !relevant.length ? "Not applicable" : "Not checked"}</span>`;
  }
  const newest = new Date(Math.max(...times)).toISOString(), oldest = new Date(Math.min(...times)).toISOString();
  const age = ago(newest) === ago(oldest) ? ago(newest) : `${ago(newest)} – ${ago(oldest)}`;
  const attempts = dated.filter(r => containerFacts(r).failure || r.check_issue === "missing_result").length;
  const unchecked = relevant.length - dated.length - updated.length;
  const notes = [updated.length ? `${updated.length} updated` : "", attempts ? attempts === dated.length ? "Latest attempts" : `${attempts} ${plural(attempts, "attempt", "attempts")}` : "", unchecked ? `${unchecked} unchecked` : "", s.stale ? "Last known" : ""].filter(Boolean);
  const title = `Container results and attempts, not inventory sync. Newest: ${window.FleetdeckDates.dateTime(newest)}. Oldest: ${window.FleetdeckDates.dateTime(oldest)}.${notes.length ? ` ${notes.join("; ")}.` : ""}`;
  return `<span title="${esc(title)}">${withLabel ? "Checked " : ""}${esc(age)}${notes.length ? `<span class="cell-detail">${esc(notes.join(" · "))}</span>` : ""}</span>`;
}

function dockhandUpdateEligible(row) {
  const age = Date.now() - Date.parse(row.checked_at);
  return row.status === "update" && row.image_update === true && ["running", "stopped", "exited", "created"].includes(row.state) && !row.check_issue &&
    !row.advisory && !row.newer_version && !row.pinned_digest && !row.deferred_hours && age >= 0 && age <= 86400000 &&
    !/(^|[/_.-])(dockhand|fleetdeck|homelab-manager)(?=$|[:@/_.-])/i.test(`${row.name}/${row.image}`);
}
function dockhandUpdateButton(v, row = null) {
  if (!v.dockhand || !v.dockhand_updates_allowed) return "";
  const status = v.dockhand;
  const eligible = row ? dockhandUpdateEligible(row) : status.containers?.some(dockhandUpdateEligible);
  if (!eligible) return "";
  const enabled = eligible && !status.stale && !status.error && !status.check_error && !status.checking && !v.active_job && !v.blocked && !v.monitor_only && canUpgrade(v.target);
  const reason = enabled ? "Confirm selected container updates through Dockhand; containers may restart" : v.monitor_only ? MONITOR_TIP : v.blocked || (v.active_job ? "Wait for the current machine job" : "Request a fresh container check or review update exclusions");
  return `<button class="small primary dockhand-update-action" aria-label="${esc(row ? `Update ${row.name} through Dockhand` : `Update containers on ${v.target.name}`)}" data-dockhand-update="${esc(v.target.id)}" ${row ? `data-container-id="${esc(row.container_id)}"` : ""} ${enabled ? "" : "disabled"} title="${esc(reason)}">${icon("download")}Update</button>`;
}

function containerRowAction(v) {
  const policy = dockerSettings(v);
  if (policy.mode === "disabled") return "";
  if (v.dockhand && v.dockhand_updates_allowed) return dockhandUpdateButton(v);
  if (v.dockhand || policy.mode === "external") return managerLink(v, policy.manager_name || "Manager");
  return "";
}

function containerVersion(v) {
  const version = v.dockhand ? v.dockhand.docker_version : v.stacks?.docker_version;
  return version ? `<span class="machine-os docker-version" title="Docker Engine version from the last provider report">Docker ${esc(version)}</span>` : "";
}

function containerEngineLight(v) {
  let tone = "", label = "Docker engine status unknown";
  if (v.target.status === "stopped") {
    tone = "off"; label = "Host stopped; Docker unavailable";
  } else if (["running", "online"].includes(v.target.status)) {
    if (v.dockhand) {
      if (!v.dockhand.stale && !v.dockhand.error && v.dockhand.docker_version && Number.isFinite(Date.parse(v.dockhand.synced_at))) {
        tone = "on"; label = "Docker reachable at last provider sync";
      }
    } else if (dockerEnabled(v) && Number.isFinite(Date.parse(v.stacks?.checked_at))) {
      if (v.stacks.docker === "down") {
        tone = "off"; label = "Docker did not answer the last scan";
      } else if (v.stacks.docker === "yes" && !v.stacks.error) {
        tone = "on"; label = "Docker reachable at last container scan";
      }
    }
  }
  return `<span class="state container-engine-state" role="img" aria-label="${label}" title="${label}"><span class="dot ${tone}"></span></span>`;
}

function containerMachineIdentity(v) {
  return `<span class="machine-main">${containerEngineLight(v)}<span class="machine-identity container-label">Containers${containerProvider(v)}</span><span class="machine-meta">${containerVersion(v)}</span></span>`;
}

function containerProvider(v) {
  const policy = dockerSettings(v), monitor = policy.mode === "monitor" || v.monitor_only;
  let label = "Fleetdeck", symbol = "server", description = "Managed by Fleetdeck · Compose only";
  if (v.dockhand) { label = "Dockhand"; symbol = "external"; description = "Managed by Dockhand"; }
  else if (policy.mode === "external") { label = policy.manager_name || "External manager"; symbol = "external"; description = `Monitored externally by ${label}`; }
  else if (policy.mode === "disabled") { label = "Off"; symbol = "power"; description = "Container checks and management disabled"; }
  else if (monitor) { label = "Fleetdeck · Monitor only"; symbol = "eye"; description = "Monitor only: checked and reported; Fleetdeck installs no container updates"; }
  return `<span class="scope-provider" role="img" aria-label="${esc(description)}" title="${esc(description)}">${icon(symbol)}<span class="provider-label">${esc(label)}</span></span>`;
}

function containerScope(v) {
  return `Containers${containerProvider(v)}`;
}

function dockerCell(v) {
  if (!hasDocker(v)) return "";
  const empty = filter => `<span class="metric-slot-empty" data-metric-slot="${filter}" aria-hidden="true"></span>`;
  const policy = dockerSettings(v);
  if (policy.mode === "disabled") return `<span class="container-indicators">${["all", "review", "coverage", "failures"].map(empty).join("")}</span>`;
  if (!v.dockhand && !dockerEnabled(v)) return "";
  const s = containerSummary(v), id = esc(v.target.id), selected = state.containerFilters?.[v.target.id] || "all";
  const manager = s.provider === "dockhand" ? "Monitored externally by Dockhand" : "Managed by Fleetdeck native Docker";
  const badge = (filter, symbol, value, label, tone) => `<button class="container-metric-hit container-indicator ${tone || ""}" data-open-docker="${id}" data-container-filter="${filter}" data-focus-key="docker:${id}:${filter}:${symbol}" aria-expanded="${state.containerExpanded.has(v.target.id) && selected === filter}" title="${esc(label)}" aria-label="${esc(`${label}. ${manager}. View container details`)}"><span class="update-metric ${tone || ""}">${symbol === "spin" ? '<span class="spin"></span>' : icon(symbol)}${value === null ? "" : `<span>${value}</span>`}</span></button>`;
  const label = s.usable ? `${s.updates} ${plural(s.updates, "container with an image update", "containers with image updates")}${s.stale ? " · last-known results" : ""}` : "Container update count unavailable";
  let body = s.value === 0 ? empty("updates") : badge("updates", "container", s.value, label, `container${s.stale ? " last-known" : ""}${s.unknown ? " unknown" : ""}`);
  body += s.reviews ? badge("review", "tag", s.reviews, `${s.reviews} containers with version / pinned-digest advisories or held updates`, "container-review") : empty("review");
  body += s.coverage ? badge("coverage", "coverage", s.coverage,
    `${s.coverage} coverage ${plural(s.coverage, "exception", "exceptions")}: local, excluded, new or unmatched containers`, "container-coverage") : empty("coverage");
  body += s.failures ? badge("failures", "alert", s.failures, `${s.failures} ${plural(s.failures, "latest container or stack check failed", "latest container or stack checks failed")}; known updates are retained`, "container-failure") : empty("failures");
  return `<span class="container-indicators">${body}</span>`;
}

function containerResult(row, checking = false) {
  const f = containerFacts(row), lines = [], add = (tone, symbol, text, tip = "") => lines.push(`<div class="container-result ${tone}">${icon(symbol)}<span${tip ? ` title="${esc(tip)}"` : ""}>${esc(text)}</span></div>`);
  if (f.update) add("container-tone", "container", `Image update reported${f.failure || row.status === "unknown" ? ` · last known${row.reported_at ? ` ${ago(row.reported_at)}` : ""}` : ""}`);
  if (f.current && checking && row.source === "fleetdeck_update") lines.push('<div class="container-result muted" role="status"><span class="spin"></span><span>Checking image…</span></div>');
  else if (f.current) add("ok", "check", row.newer_version ? "Current tag is up to date" : "Up to date", row.source === "fleetdeck_update" ? "Dockhand confirmed the image was pulled and the replacement container created. Status is current as of that update; the next normal image check will reassess it." : "");
  const oldReview = f.failure && row.advisory_at && row.checked_at && Date.parse(row.advisory_at) < Date.parse(row.checked_at);
  if (row.newer_version) add("container-review-tone", "tag", `${oldReview ? "Last-known newer version" : "Newer version"}: ${row.newer_version} · manual tag change`);
  if (row.pinned_digest) add("container-review-tone", "tag", "Pinned digest moved · manual review required");
  if (row.deferred_hours) add("container-review-tone", "clock", `${oldReview ? "Last-known held update" : "Update deferred"} · ${row.deferred_hours} h cooldown remaining${oldReview ? " when checked" : ""}`);
  if (f.review && !row.newer_version && !row.pinned_digest && !row.deferred_hours) add("container-review-tone", "tag", row.status === "deferred" ? "Update deferred by policy" : "Version / pinned digest advisory");
  if (f.failure) add("bad", "alert", ({ rate_limit: "Check failed · registry rate limit", authentication: "Check failed · registry authentication", timeout: "Check failed · registry timeout", stack: "Stack image check failed" })[row.check_issue] || "Check failed · registry unavailable");
  if (row.status === "local") add("muted", "server", "Local image · no registry comparison available");
  if (row.status === "excluded") add("muted", "info", row.coverage_reason || "Excluded by policy");
  if (row.status === "unknown") add("muted", "info", "Check result unavailable", "Fleetdeck has no result it can reliably match to this container. Request a fresh check; if it remains unavailable, inspect the result in your container manager.");
  return lines.join("") || '<span class="muted">No confirmed check result</span>';
}

function containerCheckGuidance(v, s) {
  const native = s.provider === "native", policy = dockerSettings(v);
  const scheduled = !!state.settings.scheduled_container_checks;
  const interval = state.schedule?.check_every;
  const hasSchedule = Number.isFinite(interval) && interval > 0;
  const allowed = native ? dockerEnabled(v) && !v.target.check_only : !!state.settings.dockhand_checks_allowed;
  let outcome;
  if (!hasSchedule) outcome = "Fleetdeck’s schedule is off. It will not start container checks automatically.";
  else if (!scheduled) outcome = "Fleetdeck’s scheduled rounds will not request container checks.";
  else if (!allowed) outcome = native ? "Scheduled container checks are on, but native checks are disabled for this machine." : "Scheduled container checks are on, but Dockhand check permission is disabled. Enable dockhand.allow_checks to allow fresh checks.";
  else if (v.target.check_only) outcome = "Fleetdeck’s scheduled container checks skip this check-only machine.";
  else outcome = `Fleetdeck requests fresh ${native ? "Compose image" : "Dockhand"} checks every ${every(interval)}.`;
  if (!native) outcome += " Dockhand’s own schedule is unchanged.";
  outcome += allowed ? " Manual fresh checks are enabled." : " Manual fresh checks are disabled.";
  const poll = state.settings.dockhand_poll_seconds;
  const settings = `<section class="container-check-settings" aria-label="Current container check settings"><strong>Current options</strong><dl><div><dt>Container provider</dt><dd>${native ? `Fleetdeck native Docker${policy.mode === "monitor" || v.monitor_only ? " · Monitor only" : ""}` : "Dockhand"}</dd></div>${!native ? `<div><dt>Install container updates</dt><dd>${v.dockhand_updates_allowed ? "Enabled · confirmation required" : "Disabled"}</dd></div>` : ""}<div><dt>Manual container checks</dt><dd>${allowed ? "Enabled" : "Disabled"}</dd></div><div><dt>Fleetdeck scheduled container checks</dt><dd>${scheduled ? "On" : "Off"}</dd></div><div><dt>Fleetdeck check interval</dt><dd>${hasSchedule ? `Every ${esc(every(interval))}` : "Scheduling off"}</dd></div>${!native ? `<div><dt>Read saved Dockhand reports</dt><dd>${Number.isFinite(poll) ? `Every ${esc(every(poll))}` : "Not reported"}</dd></div>` : ""}</dl><p class="container-check-outcome">${esc(outcome)}</p><div class="container-option-actions"><button class="small" data-docker-settings="${esc(v.target.id)}">Docker management</button><a class="button small" href="/settings">Schedule settings</a></div></section>`;
  const operation = native
    ? "Native checks confirm image downloads before they start. Update stack recreates affected running services after confirmation; stopped services stay stopped. Compose services only; standalone containers and newer-version-tag advisories are not provided by this checker."
    : "Dockhand compares registry digests without downloading image layers. Container updates run in Dockhand. When Install container updates is enabled for this machine, Fleetdeck can request them after confirmation. Otherwise, open Dockhand to install them.";
  const schedule = !native && s.scheduledAt ? `<p class="container-schedule-report">Dockhand scheduled: ${esc(ago(s.scheduledAt))} · ${esc(({ success: s.scheduledErrors ? `Completed with ${s.scheduledErrors} ${plural(s.scheduledErrors, "error", "errors")}` : "Completed", failed: "Failed", running: "Running", warning: "Completed with warnings", queued: "Queued", skipped: "Skipped" })[s.scheduledStatus] || "Result unavailable")}${s.scheduledChecked != null ? ` · ${s.scheduledChecked} containers checked` : ""}.</p>` : "";
  const history = `<section class="container-help-notes"><strong>How to change them</strong><p>Use <strong>Docker management</strong> for this machine’s provider${native ? " and policy" : ", connection and fresh-check permission"}. Use <strong>Schedule settings</strong> for <code>schedule.check_docker: true</code> (include containers) or <code>false</code> (OS only), and <code>schedule.check_every</code> for the interval.</p><p>${operation} Refresh inventory and dashboard polling only read saved reports. The machine’s Update button handles OS updates.</p><strong>Check history</strong><p>${native ? "Native check" : "Last Fleetdeck request"}: ${s.checkedAt ? esc(ago(s.checkedAt)) : "never"}.</p>${schedule}<p class="muted">Synced = reports fetched. Last checked = each row’s result or labelled failed attempt. Unknown means no reliable result; local images have no registry comparison. Counts may overlap. ${state.settings.dockhand_results_persistent === false ? "State storage is unavailable; results last until restart." : "Confirmed results are saved across Fleetdeck restarts."}</p></section>`;
  return `<details class="container-help"><summary>Current options and how to change them</summary><div class="container-help-body">${settings}${history}</div></details>`;
}

function containerName(row) {
  const value = String(row.state || "unknown"), stateName = value.toLowerCase();
  const tone = stateName === "running" ? "running" : ["restarting", "paused"].includes(stateName) ? "paused" : stateName === "dead" ? "dead" : "stopped";
  const dot = row.nativeStack && !row.container_id ? "" : `<span class="container-runtime-dot ${tone}" role="img" aria-label="${esc(`Container ${value}`)}" title="${esc(value)}"></span>`;
  const shortId = id => esc(String(id).replace(/^sha256:/, "").slice(0, 12));
  const ids = row.nativeStack && row.image_id ? `<div class="cell-detail muted svc-ids" title="The container is made from the first image ID. The second is what the tag points at on this machine now. They can differ for an image that is current (multi-platform images) when Docker keeps a separate ID per platform.">container image ${shortId(row.image_id)} · tag ${row.tag_id ? shortId(row.tag_id) : "unknown"}</div>` : "";
  return `<div class="container-name-line">${dot}<span>${esc(row.name)}</span></div><div class="cell-detail muted">${esc(value)}</div>${ids}`;
}

function containerPanel(v) {
  const s = containerSummary(v), id = esc(v.target.id), native = s.provider === "native";
  const requestedFilter = state.containerFilters?.[v.target.id] || "all";
  const filter = requestedFilter === "coverage" && !s.coverage ? "all" : requestedFilter, compare = (a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  const items = [...s.rows].sort((a, b) => compare(a.stack || "Standalone", b.stack || "Standalone") || compare(a.name, b.name) || compare(a.image, b.image));
  const shown = items.filter(row => filter === "all" || containerFacts(row)[({ updates: "update", current: "current", review: "review", coverage: "coverage", failures: "failure" })[filter]]);
  const scan = native && !!state.settings.scan_images, id2 = id;
  const scanButton = scan && v.stacks?.stacks?.length ? `<button class="small" data-act="scan" data-id="${id2}" ${state.scanning ? "disabled" : ""} title="Scans these images for known vulnerabilities with Trivy, here on the dashboard. Nothing runs on the machine.">${state.scanning ? "Scanning…" : "Scan images"}</button>` : "";
  const button = native ? `<button class="small" data-act="stacks" data-id="${id}" ${!canUpgrade(v.target) || v.active_job ? "disabled" : ""} title="Download newer Compose images to compare; confirmation is required">${icon("refresh")}Fetch image updates</button>${scanButton}`
    : `<button class="small" data-dockhand-check="${id}" ${!state.settings.dockhand_checks_allowed || s.checking ? "disabled" : ""} title="${state.settings.dockhand_checks_allowed ? "Request a fresh registry digest check; no image layers or restarts" : "Enable dockhand.allow_checks in Settings to allow fresh checks"}">${icon("refresh")}${s.checking ? "Checking…" : "Check in Dockhand"}</button>`;
  const mode = native ? `Fleetdeck native · ${dockerSettings(v).mode === "monitor" || v.monitor_only ? "Monitor only" : "Compose"}` : "Dockhand";

  const tabs = [["all", "All", s.rows.length], ...(s.updates ? [["updates", "Image updates", s.updates]] : []), ...(s.current ? [["current", "Up to date", s.current]] : []), ...(s.reviews ? [["review", "Review", s.reviews]] : []), ...(s.coverage ? [["coverage", "Coverage", s.coverage]] : []), ...(s.failures ? [["failures", "Failures", s.failures]] : [])];
  const filters = `<div class="container-filters" role="group" aria-label="Filter container results">${tabs.map(([value, text, count]) => `<button class="small ghost" data-container-view-filter="${value}" data-id="${id}" data-focus-key="container-filter:${id}:${value}" aria-pressed="${filter === value}">${text}${count === null ? "" : ` <span>${count}</span>`}</button>`).join("")}</div>`;
  const actions = (native && dockerSettings(v).mode === "managed") || (!native && v.dockhand_updates_allowed);
  const seen = new Set();
  const action = row => {
    if (!native) return dockhandUpdateButton(v, row);
    if (!actions || !row.nativeStack || seen.has(row.stack)) return "";
    seen.add(row.stack);
    const stack = row.nativeStack;
    if (stack.is_dashboard) return '<span class="muted">Update externally</span>';
    if (!stack.update_available || !stack.services?.some(service => service.outdated && ["running", "restarting"].includes(service.status))) return "";
    const allowed = !v.monitor_only && canUpgrade(v.target) && !v.blocked && !v.active_job && !stack.error && stack.state !== "stopped" && v.stacks?.compose !== "none";
    return `<button class="small primary" data-stack="${esc(stack.name)}" data-id="${id}" ${allowed ? "" : "disabled"} title="${esc(v.monitor_only ? MONITOR_TIP : v.blocked || stack.error || "Download images and recreate running services")}">Update stack</button>`;
  };
  const scanned = new Set();
  const vulnCell = row => {
    if (!row.nativeStack || scanned.has(row.stack)) return "";
    scanned.add(row.stack);
    return vulnBadge(v, row.nativeStack);
  };
  const table = `<div class="group-scroll"><table class="container-results-table ${native ? "native-container-table" : "dockhand-table"}" aria-label="${esc(v.target.name)} containers sorted by stack"><colgroup><col class="cr-name"><col class="cr-stack"><col class="cr-image"><col class="cr-status"><col class="cr-checked">${scan ? '<col class="cr-vulns">' : ""}${actions ? '<col class="cr-action">' : ""}</colgroup><thead><tr><th>${native ? "Service" : "Container"}</th><th>Stack</th><th>Image</th><th>Status</th><th>Last checked</th>${scan ? "<th>Vulnerabilities</th>" : ""}${actions ? '<th>Action</th>' : ""}</tr></thead><tbody>${shown.map(row => `<tr><td class="container-name${native ? "" : " dockhand-name"}">${containerName(row)}</td><td data-label="Stack" class="muted">${esc(row.stack || "Standalone")}</td><td data-label="Image">${esc(row.image)}</td><td data-label="Status">${containerResult(row, s.checking)}</td><td data-label="Last checked" data-mobile-label="${containerFacts(row).failure || row.check_issue === "missing_result" ? "Last attempt" : "Last checked"}" class="muted" title="${esc(({ dockhand_schedule: "From Dockhand scheduled-check history", fleetdeck_button: "From a check requested through Fleetdeck", fleetdeck_schedule: "From a scheduled Fleetdeck check", fleetdeck_native: "From Fleetdeck native Compose checks", fleetdeck_update: "Confirmed Dockhand deployment time; not an image-check result" })[row.source] || "From Dockhand pending-update reports")}">${row.status === "excluded" ? "Excluded" : row.status === "local" ? "Not applicable" : row.checked_at ? `${containerFacts(row).failure || row.check_issue === "missing_result" ? '<span class="container-attempt-prefix">Attempt: </span>' : ""}${ago(row.checked_at)}` : row.updated_at && row.source === "fleetdeck_update" ? `Updated ${ago(row.updated_at)}` : "Not checked"}</td>${scan ? `<td data-label="Vulnerabilities">${vulnCell(row)}</td>` : ""}${actions ? `<td data-label="Action">${action(row)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
  const notices = [v.active_job?.action === "dockhand-update" ? '<p class="container-notice-line" role="status"><span class="spin"></span>Updating containers and syncing replacement identities…</p>' : "", s.checking ? '<p class="container-notice-line" role="status">Checking container images… Previous results remain last-known until completion.</p>' : "", s.error ? `<p class="container-notice-line bad" role="status">${esc(s.error)} Last-known results are shown where available.</p>` : "", s.checkError ? `<p class="container-notice-line bad" role="status">${esc(s.checkError)}</p>` : "", s.historyError ? `<p class="container-notice-line warn">${esc(s.historyError)} Confirmed manual results remain usable.</p>` : ""].join("");
  const guidance = containerCheckGuidance(v, s);
  const categoryNote = filter === "coverage" && !native ? '<p class="container-notice-line muted">Local images have no registry counterpart, so repeating a check cannot compare them. Policy exclusions are intentional; new or replaced containers need a fresh matching result.</p>' : filter === "review" ? '<p class="container-notice-line muted">New version tags and pinned digests require manual review. Held image updates wait for their cooldown. These items are separate from applicable image updates.</p>' : "";
  const scope = native ? '<p class="container-notice-line muted">Coverage: Compose stacks only. Standalone containers are outside Fleetdeck checks.</p>' : "";
  const prune = native && dockerSettings(v).mode === "managed" ? `<label class="container-prune muted" title="Removes all unused, untagged images on this machine, not only this stack"><input type="checkbox" data-prune ${remember.get("pruneImages") ? "checked" : ""}> remove unused, untagged images after updating</label>` : "";
  return `<section class="group docker-host" id="docker-${id}"><div class="group-head"><span class="container-panel-title">Container updates · ${esc(v.target.name)}</span><div class="container-panel-actions"><span class="host-mode muted">${esc(mode)}</span>${button}${native ? `<button class="small" data-docker-settings="${id}">Settings</button>` : managerLink(v, "Open Dockhand")}</div>${detailsCloseButton(v.target.id, "containers")}</div><div class="container-overview"><div class="container-results-toolbar">${filters}<span class="container-sync muted">${native ? (s.error ? "Last attempt" : "Checked") : "Synced"} ${s.syncedAt ? ago(s.syncedAt) : "never"}${s.stale ? " · last-known results" : ""}</span></div>${notices}${scope}${prune}${categoryNote}${guidance}</div>${shown.length ? table : `<p class="container-empty muted">${s.rows.length ? "No matching per-container results in this category." : native ? (!v.stacks ? "Container images have not been checked. Fetch image updates to compare Compose images; nothing is restarted." : "No Docker Compose stacks found. Standalone containers are not checked by Fleetdeck; their update status is unknown.") : "Container status unavailable."}</p>`}</section>`;
}

function stackPanel(v) {
  if (v.dockhand || dockerEnabled(v)) return containerPanel(v);
  const policy = dockerSettings(v), id = esc(v.target.id);
  return `<section class="group docker-host" id="docker-${id}"><div class="group-head"><span class="container-panel-title">Container updates · ${esc(v.target.name)}</span><div class="container-panel-actions"><span class="host-mode muted">${esc(policy.mode === "disabled" ? "Off" : `Managed by ${policy.manager_name}`)}</span><button class="small" data-docker-settings="${id}">Settings</button></div>${detailsCloseButton(v.target.id, "containers")}</div><p class="container-empty muted">${policy.mode === "disabled" ? "Docker checks and deployments are disabled. OS updates remain independent." : "Container checks and deployments are handled by the configured external manager."}</p>${policy.mode === "external" ? `<p class="container-empty">${managerLink(v, `Open ${policy.manager_name}`)}</p>` : ""}</section>`;
}
