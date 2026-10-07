
(function () {
  "use strict";
  // Возврат после входа на ту же карточку: часть адреса после # сервер не видит, её переносит форма
  var form = document.querySelector("form[data-keep-hash]");
  if (form && /^#\/[A-Za-z0-9_\-\/?=&.]{0,200}$/.test(location.hash)) form.action = form.getAttribute("action") + location.hash;
  // «Показать пароль»: кнопка появляется только там, где скрипт выполнился
  Array.prototype.forEach.call(document.querySelectorAll("[data-show]"), function (btn) {
    var input = document.getElementById(btn.getAttribute("data-show"));
    if (!input) return;
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var shown = input.type === "text";
      input.type = shown ? "password" : "text";
      btn.textContent = shown ? "Показать" : "Скрыть";
      btn.setAttribute("aria-pressed", shown ? "false" : "true");
      input.focus();
    });
  });
  // Предупреждение о Caps Lock: частая причина «неверного пароля»
  var caps = document.getElementById("caps");
  if (caps) {
    var check = function (e) { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); };
    document.addEventListener("keydown", check);
    document.addEventListener("keyup", check);
  }
})();
