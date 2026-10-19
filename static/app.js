(() => {
  const D = window.MUU;
  const $ = (sel, root = document) => root.querySelector(sel);
  const records = new Map(D.results.map(r => [r.id, r]));
  const choiceByKey = Object.fromEntries(D.choices.map(c => [c.key, c]));
  const labelNames = new Set(D.choices.map(c => c.label));

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const siteName = id => D.sites[id] || id || "—";
  const choiceText = (label, vlan) => `${label || "—"} (VLAN ${vlan || "—"})`;

  function fillSelect(sel, items, current, placeholder) {
    sel.innerHTML = (placeholder ? `<option value="">${esc(placeholder)}</option>` : "") +
      items.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
    sel.value = current ?? "";
    if (sel.value !== (current ?? "")) sel.value = "";
  }
  const siteItems = () => Object.entries(D.sites);
  const choiceItems = () => D.choices.map(c => [c.key, c.text]);

  function flash(msg, kind = "ok") {
    $("#flash").innerHTML = `<div class="alert ${kind}">${msg}</div>`;
  }

  // Does a record still match the search it was found with?
  function matchesSearch(r) {
    const s = D.sel;
    const siteOk = s.site === D.all ? r.site_ids.some(id => id in D.sites) : r.site_ids.includes(s.site);
    const clientOk = s.client === D.all ? !!r.choice : r.labels.some(l => `${l}|${r.vlan}` === s.client);
    return siteOk && clientOk;
  }

  // ---------------------------------------------------------------- table
  const tbody = $("#rows");
  const filterBox = $("#filter");

  function rowHtml(r) {
    const stale = !matchesSearch(r);
    const others = r.labels.filter(l => l !== r.label);
    return `<tr data-id="${esc(r.id)}" class="${stale ? "stale" : ""}">
      <td class="mono">${esc(r.mac_display)}</td>
      <td>${esc(r.name)}</td>
      <td>${esc(r.site_names.join(", ") || "—")}</td>
      <td>${esc(r.vlan)}</td>
      <td>${esc(r.label)}${others.length ? ` <span class="muted">+${esc(others.join(", "))}</span>` : ""}</td>
      <td class="notes">${esc(r.notes)}</td>
      <td class="nowrap muted">${esc(r.updated_display)}</td>
      <td class="actions"><button type="button" class="edit">Edit</button>${stale ? '<span class="tag">no longer matches</span>' : ""}</td>
    </tr>`;
  }

  function render() {
    if (!tbody) return;
    const q = (filterBox.value || "").toLowerCase().replace(/[:\-.]/g, "");
    const rows = [...records.values()].filter(r => !q ||
      r.mac.includes(q) || (r.name + " " + r.notes).toLowerCase().replace(/[:\-.]/g, "").includes(q));
    tbody.innerHTML = rows.map(rowHtml).join("");
    $("#count").textContent = `${rows.length} matching entr${rows.length === 1 ? "y" : "ies"}`;
    $("#empty").hidden = rows.length > 0;
  }

  if (tbody) {
    filterBox.addEventListener("input", render);
    tbody.addEventListener("click", e => {
      const btn = e.target.closest("button.edit");
      if (btn) openEdit(records.get(btn.closest("tr").dataset.id));
    });
    render();
  }

  // ---------------------------------------------------------------- dialogs
  function wizard(dlg, { review, submit }) {
    const err = $(".alert.error", dlg);
    const step = confirm => {
      $(".step-edit", dlg).hidden = confirm;
      $(".step-confirm", dlg).hidden = !confirm;
      $(".next", dlg).hidden = confirm;
      $(".back", dlg).hidden = !confirm;
      $(".confirm", dlg).hidden = !confirm;
      if (confirm) {  // stop a double-click on Review from also confirming
        const btn = $(".confirm", dlg);
        btn.disabled = true;
        setTimeout(() => { btn.disabled = false; }, 600);
      }
    };
    const showErr = msg => { err.innerHTML = msg; err.hidden = !msg; };
    $(".cancel", dlg).onclick = () => dlg.close();
    $(".back", dlg).onclick = () => { showErr(""); step(false); };
    $(".next", dlg).onclick = () => {
      const msg = review();
      showErr(msg || "");
      if (!msg) step(true);
    };
    $(".confirm", dlg).onclick = async () => {
      const btn = $(".confirm", dlg);
      btn.disabled = true; btn.textContent = "Saving…";
      try { await submit(showErr); }
      finally { btn.disabled = false; btn.textContent = btn.dataset.label; }
    };
    $(".confirm", dlg).dataset.label = $(".confirm", dlg).textContent;
    return { reset() { showErr(""); step(false); dlg.showModal(); } };
  }

  async function call(method, url, body) {
    let resp;
    try {
      resp = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      return { ok: false, data: { error: "Could not reach the MAC Update Utility server." } };
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { data = { error: `Server returned HTTP ${resp.status}.` }; }
    return { ok: resp.ok, status: resp.status, data };
  }

  const diffRow = (k, a, b) => {
    const changed = a !== b;
    return `<tr class="${changed ? "changed" : ""}"><th>${esc(k)}</th><td>${esc(a)}</td>` +
      `<td class="arrow">${changed ? "→" : ""}</td><td>${changed ? esc(b) : '<span class="muted">unchanged</span>'}</td></tr>`;
  };

  // Edit
  const editDlg = $("#edit-dlg");
  let editing = null;
  const edit = wizard(editDlg, {
    review() {
      const r = editing, s = $("#e-site").value, c = choiceByKey[$("#e-client").value];
      if (!s || !c) return "Choose a site and Client Label.";
      if (s === r.site_id && c.key === r.choice && r.site_ids.length === 1) return "Nothing has been changed.";
      $("#edit-diff").innerHTML =
        diffRow("Site", r.site_names.join(", ") || "—", siteName(s)) +
        diffRow("Client Label", choiceText(r.label, r.vlan), c.text);
      return "";
    },
    async submit(showErr) {
      const { ok, status, data } = await call("PUT", `/api/usermacs/${editing.id}`, {
        site_id: $("#e-site").value, client: $("#e-client").value,
        updated_at: editing.updated_at,
      });
      if (data.record) records.set(data.record.id, data.record);
      if (!ok) {
        if (status === 409 && data.record) { render(); editing = data.record; }
        return showErr(esc(data.error || "Update failed."));
      }
      editDlg.close();
      render();
      const r = data.record;
      flash(data.unchanged ? `No change needed for <b>${esc(r.mac_display)}</b>.`
        : `Updated <b>${esc(r.mac_display)}</b>: ${esc(r.site_names.join(", "))} · VLAN ${esc(r.vlan)} · ${esc(r.label)}` +
          (matchesSearch(r) ? "" : " (it no longer matches this search)"));
      const tr = tbody.querySelector(`tr[data-id="${CSS.escape(r.id)}"]`);
      if (tr) { tr.classList.add("just-saved"); tr.scrollIntoView({ block: "center" }); }
    },
  });

  function openEdit(r) {
    editing = r;
    $("#edit-title").textContent = `Edit ${r.mac_display}`;
    $("#edit-sub").textContent = [r.name, r.notes].filter(Boolean).join(" · ");
    fillSelect($("#e-site"), siteItems(), r.site_id, r.site_id in D.sites ? null : "Choose a site…");
    fillSelect($("#e-client"), choiceItems(), r.choice, r.choice ? null : "Choose a Client Label…");
    const others = r.labels.filter(l => l !== r.label && !labelNames.has(l));
    const note = $("#e-other-labels");
    note.hidden = !others.length && r.site_ids.length < 2;
    note.textContent = [
      others.length ? `Other labels kept as-is: ${others.join(", ")}.` : "",
      r.site_ids.length > 1 ? `This entry is currently on ${r.site_ids.length} sites; saving sets it to the one site chosen.` : "",
    ].join(" ");
    edit.reset();
  }

  // Add
  const addDlg = $("#add-dlg");
  const normMac = s => (s || "").replace(/[^0-9a-f]/gi, "").toLowerCase();
  const fmtMac = m => m.match(/../g).join(":");
  const add = wizard(addDlg, {
    review() {
      const mac = normMac($("#a-mac").value);
      if (mac.length !== 12) return "Enter a MAC address with 12 hex digits.";
      if (parseInt(mac.slice(0, 2), 16) & 1) return "That is a multicast MAC address, not a device.";
      if (!$("#a-site").value || !$("#a-client").value)
        return "Site and Client Label are both required.";
      $("#add-diff").innerHTML = [
        ["MAC", fmtMac(mac)], ["Site", siteName($("#a-site").value)],
        ["Client Label", choiceByKey[$("#a-client").value].text], ["Name", $("#a-name").value.trim() || "—"],
        ["Notes", $("#a-notes").value.trim() || "—"],
      ].map(([k, v]) => `<tr><th>${esc(k)}</th><td colspan="3">${esc(v)}</td></tr>`).join("");
      return "";
    },
    async submit(showErr) {
      const { ok, status, data } = await call("POST", "/api/usermacs", {
        mac: $("#a-mac").value, site_id: $("#a-site").value, client: $("#a-client").value,
        name: $("#a-name").value, notes: $("#a-notes").value,
      });
      if (!ok) {
        let msg = esc(data.error || "Add failed.");
        if (status === 409 && data.record) {
          const r = data.record;
          msg += `<br><span class="small">Existing entry: ${esc(r.site_names.join(", ") || "no site")} · VLAN ${esc(r.vlan)} · ${esc(r.labels.join(", ") || "no label")}</span>`;
        }
        return showErr(msg);
      }
      addDlg.close();
      const r = data.record;
      if (tbody) { records.set(r.id, r); render(); }
      flash(`Added <b>${esc(r.mac_display)}</b>: ${esc(r.site_names.join(", "))} · VLAN ${esc(r.vlan)} · ${esc(r.label)}`);
    },
  });

  $("#add-btn").addEventListener("click", () => {
    $("#a-mac").value = $("#a-name").value = $("#a-notes").value = "";
    const pre = (v, list) => v !== D.all && list.some(([k]) => k === v) ? v : "";
    fillSelect($("#a-site"), siteItems(), pre(D.sel.site, siteItems()), "Choose a site…");
    fillSelect($("#a-client"), choiceItems(), pre(D.sel.client, choiceItems()), "Choose a Client Label…");
    add.reset();
    $("#a-mac").focus();
  });
})();
