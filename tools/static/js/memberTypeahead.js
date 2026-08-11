/* Member typeahead: pick one member by typing instead of scrolling a <select>.
 *
 * Paired with forms.UserTypeaheadWidget and tools/common/_userTypeahead.html.
 * Binds every [data-typeahead] on the page, so a form with two member pickers
 * needs no extra wiring.
 *
 * Two invariants this file exists to keep:
 *
 *  1. The hidden input and what is on screen never disagree. Picking sets both;
 *     clearing empties both. There is no path that leaves a typed name next to a
 *     stale id, which is the classic bug in a control shaped like this.
 *  2. Names and emails are member data and are written with textContent, never
 *     innerHTML - the same rule the group page's search box follows.
 */
(function () {
  "use strict";

  var MIN_QUERY = 2;      // matches the server, which returns [] below this
  var DEBOUNCE_MS = 200;

  function bind(root) {
    var endpoint = root.dataset.endpoint;
    var valueInput = root.querySelector("[data-typeahead-value]");
    var searchInput = root.querySelector("[data-typeahead-input]");
    var results = root.querySelector("[data-typeahead-results]");
    var chosen = root.querySelector("[data-typeahead-chosen]");
    var chosenLabel = root.querySelector("[data-typeahead-chosen-label]");
    var clearButton = root.querySelector("[data-typeahead-clear]");
    if (!endpoint || !valueInput || !searchInput || !results || !chosen) {
      return;
    }

    var timer = null;
    var requestSeq = 0;

    function closeResults() {
      results.hidden = true;
      results.textContent = "";
    }

    function choose(member) {
      valueInput.value = member.id;
      chosenLabel.textContent = member.label;
      chosen.hidden = false;
      searchInput.hidden = true;
      searchInput.value = "";
      closeResults();
      // Fire change so anything listening on the field (nothing yet, but a
      // dependent field is the obvious next thing) sees the new value.
      valueInput.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function clear() {
      valueInput.value = "";
      chosenLabel.textContent = "";
      chosen.hidden = true;
      searchInput.hidden = false;
      closeResults();
      searchInput.focus();
      valueInput.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function optionButton(member) {
      var button = document.createElement("button");
      button.type = "button";           // never submit the form
      button.className = "search-result-row";
      button.setAttribute("data-typeahead-option", "");

      var body = document.createElement("span");
      var name = document.createElement("span");
      name.className = "typeahead-option-name";
      name.textContent = member.label;
      body.appendChild(name);

      if (member.username) {
        var detail = document.createElement("span");
        detail.className = "typeahead-option-detail";
        detail.textContent = " · " + member.username;
        body.appendChild(detail);
      }

      button.appendChild(body);
      button.addEventListener("click", function () { choose(member); });
      return button;
    }

    function render(members) {
      results.textContent = "";
      if (members.length === 0) {
        var empty = document.createElement("div");
        empty.className = "search-result-empty";
        empty.textContent = "No active member matches that.";
        results.appendChild(empty);
      } else {
        members.forEach(function (member) { results.appendChild(optionButton(member)); });
      }
      results.hidden = false;
    }

    function search(query) {
      // Every response carries the sequence number of its request, so a slow
      // early reply cannot overwrite the results of a later, narrower one.
      var mySeq = ++requestSeq;
      var url = endpoint + "?q=" + encodeURIComponent(query);
      fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : { results: [] }; })
        .then(function (payload) {
          if (mySeq !== requestSeq) { return; }
          render(payload.results || []);
        })
        .catch(function () {
          if (mySeq !== requestSeq) { return; }
          results.textContent = "";
          var failed = document.createElement("div");
          failed.className = "search-result-empty";
          failed.textContent = "Could not reach the member list. Check your connection and try again.";
          results.appendChild(failed);
          results.hidden = false;
        });
    }

    searchInput.addEventListener("input", function () {
      var query = searchInput.value.trim();
      window.clearTimeout(timer);
      if (query.length < MIN_QUERY) {
        // Not an error state: below two characters every match would be noise,
        // so the panel closes rather than showing a hundred names.
        requestSeq++;      // abandon any reply still in flight
        closeResults();
        return;
      }
      timer = window.setTimeout(function () { search(query); }, DEBOUNCE_MS);
    });

    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeResults();
        return;
      }
      if (event.key === "ArrowDown") {
        var first = results.querySelector("[data-typeahead-option]");
        if (first) {
          event.preventDefault();      // do not scroll the page instead
          first.focus();
        }
        return;
      }
      if (event.key === "Enter") {
        // A lone Enter in a search box would submit the whole form with nothing
        // picked. Pick the only match if there is exactly one, otherwise do
        // nothing - never guess between two people.
        var options = results.querySelectorAll("[data-typeahead-option]");
        event.preventDefault();
        if (options.length === 1) { options[0].click(); }
      }
    });

    results.addEventListener("keydown", function (event) {
      var options = Array.prototype.slice.call(results.querySelectorAll("[data-typeahead-option]"));
      var at = options.indexOf(document.activeElement);
      if (at === -1) { return; }
      if (event.key === "ArrowDown" && at < options.length - 1) {
        event.preventDefault();
        options[at + 1].focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (at === 0) { searchInput.focus(); } else { options[at - 1].focus(); }
      } else if (event.key === "Escape") {
        closeResults();
        searchInput.focus();
      }
    });

    if (clearButton) {
      clearButton.addEventListener("click", clear);
    }

    // Clicking away closes the panel. Guarded on the panel being open so this
    // does nothing on every stray click on the page.
    document.addEventListener("click", function (event) {
      if (!results.hidden && !root.contains(event.target)) { closeResults(); }
    });
  }

  document.querySelectorAll("[data-typeahead]").forEach(bind);
})();
