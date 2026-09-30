// Перетаскивание карточек на канбане: смена этапа без перезагрузки страницы.
(function () {
  const board = document.querySelector(".board");
  if (!board) return;
  const moveUrl = board.dataset.moveUrl;
  const csrf = board.querySelector("input[name=csrfmiddlewaretoken]").value;
  let dragged = null;

  board.addEventListener("dragstart", (e) => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    dragged = tile;
    tile.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", tile.dataset.id);
  });

  board.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    board.querySelectorAll(".drop-target").forEach((c) => c.classList.remove("drop-target"));
    dragged = null;
  });

  board.querySelectorAll(".col").forEach((col) => {
    col.addEventListener("dragover", (e) => {
      if (!dragged) return;
      e.preventDefault();
      col.classList.add("drop-target");
    });
    col.addEventListener("dragleave", (e) => {
      if (!col.contains(e.relatedTarget)) col.classList.remove("drop-target");
    });
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("drop-target");
      if (!dragged) return;
      const tile = dragged;
      const from = tile.closest(".col");
      if (from === col) return;

      const body = col.querySelector(".col-body");
      const placeholder = body.querySelector(".col-empty");
      if (placeholder) placeholder.remove();
      body.appendChild(tile);
      updateCounts();

      try {
        const res = await fetch(moveUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify({ id: tile.dataset.id, stage: col.dataset.stage }),
        });
        if (!res.ok) throw new Error(res.status);
        const days = tile.querySelector(".tile-foot .muted");
        if (days) days.textContent = "0 дн. на этапе";
        if (!tile.querySelector(".tag.danger")) tile.classList.remove("alert");
      } catch (err) {
        from.querySelector(".col-body").appendChild(tile);
        updateCounts();
        alert("Не удалось сменить этап. Обновите страницу и попробуйте ещё раз.");
      }
    });
  });

  function updateCounts() {
    board.querySelectorAll(".col").forEach((col) => {
      col.querySelector(".count").textContent = col.querySelectorAll(".tile").length;
    });
  }
})();
