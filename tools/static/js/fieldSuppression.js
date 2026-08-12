/* Hide the form rows a chosen option does not ask for.
 *
 * A credential's kind decides whether the vault, status and date questions mean
 * anything: an individual login and a 2FA token are one person's own, so the
 * chapter never vaults or rotates them and asking is worse than not asking - it
 * invites a guessed answer that then reads as a fact.
 *
 * Driven entirely by two attributes the Django widget renders from the model
 * constant (ResourceCredential.NO_ROTATION_KINDS) and the form constant
 * (SUPPRESSED_BY_KIND_KEYS):
 *
 *   data-suppress-when="0,4"                       which option values suppress
 *   data-suppress-fields="vaultCollection,status"  which form-row data-fields go
 *
 * So this file names no field and no kind, and the browser cannot come to hold a
 * different opinion than the server about which is which. The server clears the
 * same fields in ResourceCredentialForm.clean() - this half makes the form
 * short, that half makes the rule true.
 */
(function () {
  "use strict";

  function splitList(value) {
    if (!value) {
      return [];
    }
    return value.split(",").map(function (part) {
      return part.trim();
    }).filter(Boolean);
  }

  function bind(control) {
    var suppressWhen = splitList(control.getAttribute("data-suppress-when"));
    var fieldNames = splitList(control.getAttribute("data-suppress-fields"));
    if (!suppressWhen.length || !fieldNames.length) {
      return;
    }

    var form = control.form || document;
    var rows = fieldNames.map(function (name) {
      return form.querySelector('[data-field="' + name + '"]');
    }).filter(Boolean);

    function apply(clearValues) {
      var suppressed = suppressWhen.indexOf(control.value) !== -1;
      rows.forEach(function (row) {
        row.hidden = suppressed;
        if (!suppressed || !clearValues) {
          return;
        }
        // Emptied, not merely hidden. A row that is out of sight still posts its
        // value, and the editor would have no way to see what they were about to
        // save. The server blanks these too, so this is about what the page
        // shows being the truth rather than about the saved data.
        var inputs = row.querySelectorAll("input, select, textarea");
        for (var index = 0; index < inputs.length; index += 1) {
          var input = inputs[index];
          if (input.type === "checkbox" || input.type === "radio") {
            input.checked = false;
          } else if (input.tagName === "SELECT") {
            input.selectedIndex = 0;
          } else {
            input.value = "";
          }
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    }

    // The first pass does NOT clear. On an edit form the initial render is the
    // stored row, and wiping it on page load would silently discard data the
    // editor never touched - they might have opened the page to change the
    // label. Clearing starts once they change the kind themselves.
    apply(false);
    control.addEventListener("change", function () {
      apply(true);
    });
  }

  var controls = document.querySelectorAll("[data-suppress-when]");
  for (var index = 0; index < controls.length; index += 1) {
    bind(controls[index]);
  }
})();
