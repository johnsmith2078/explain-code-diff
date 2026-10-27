(() => {
  "use strict";
  const files = Array.from(document.querySelectorAll("details.file"));
  const search = document.getElementById("search");
  const count = document.getElementById("search-count");
  const indexed = files.map(file => ({file, text: file.textContent.toLocaleLowerCase()}));
  search.addEventListener("input", () => {
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    indexed.forEach(({file, text}) => {
      const matches = !query || text.includes(query);
      file.hidden = !matches;
      const link = document.querySelector('[data-target="' + file.id + '"]');
      if (link) link.hidden = !matches;
      if (matches) {
        visible += 1;
        if (query) file.open = true;
      }
    });
    document.querySelectorAll(".scope").forEach(scope => {
      scope.hidden = Boolean(query) && !Array.from(scope.querySelectorAll(".file")).some(file => !file.hidden);
    });
    count.textContent = visible + " / " + files.length + " 个文件条目";
    document.getElementById("no-results").hidden = visible !== 0 || !query;
  });
  document.getElementById("context-toggle").addEventListener("change", event => {
    document.body.classList.toggle("hide-context", event.target.checked);
  });
  document.getElementById("expand-all").addEventListener("click", () => files.forEach(file => { file.open = true; }));
  document.getElementById("collapse-all").addEventListener("click", () => files.forEach(file => { file.open = false; }));
  document.getElementById("print-report").addEventListener("click", () => window.print());
  document.querySelectorAll(".file-link").forEach(link => {
    link.addEventListener("click", () => { document.getElementById(link.dataset.target).open = true; });
  });
  function revealAnchor() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
    const node = document.getElementById(id);
    const file = node && node.closest("details.file");
    if (file) {
      file.open = true;
      file.hidden = false;
      file.closest(".scope").hidden = false;
    }
  }
  window.addEventListener("hashchange", revealAnchor);
  revealAnchor();
  let printState = [];
  window.addEventListener("beforeprint", () => {
    printState = Array.from(document.querySelectorAll("details")).map(node => [node, node.open]);
    printState.forEach(([node]) => { node.open = true; });
  });
  window.addEventListener("afterprint", () => printState.forEach(([node, open]) => { node.open = open; }));
})();
