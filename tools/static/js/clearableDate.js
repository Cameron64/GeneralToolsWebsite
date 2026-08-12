/* Clear button for <input type="date">.
 *
 * Chrome renders no clear affordance on a date input, so a date that has been
 * set can only be removed by focusing the field and pressing Delete - which
 * nothing on screen says. An optional date that cannot be un-set strands a claim
 * about reality that somebody has since found to be wrong.
 *
 * The button ships hidden and is only revealed here, so a page with scripting
 * off shows the plain date input rather than a button that does nothing.
 *
 * Binds by data attribute: any ClearableDateInput anywhere on the page is picked
 * up, and no template has to know this file exists.
 */
(function () {
  "use strict";

  function bind(wrapper) {
    var input = wrapper.querySelector("input");
    var button = wrapper.querySelector("[data-clearable-date-clear]");
    if (!input || !button) {
      return;
    }

    function sync() {
      // The button exists to remove a value. With no value there is nothing to
      // remove, and a control that does nothing teaches the reader to distrust
      // the other controls.
      button.hidden = input.value === "";
    }

    button.addEventListener("click", function () {
      input.value = "";
      // Dispatched because something else may be watching this field - the
      // credential form's kind suppression reads date inputs - and setting
      // .value from script fires no event of its own.
      input.dispatchEvent(new Event("change", { bubbles: true }));
      sync();
      input.focus();
    });

    input.addEventListener("change", sync);
    input.addEventListener("input", sync);
    sync();
  }

  var wrappers = document.querySelectorAll("[data-clearable-date]");
  for (var index = 0; index < wrappers.length; index += 1) {
    bind(wrappers[index]);
  }
})();
