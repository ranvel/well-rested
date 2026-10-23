/* Well RESTed admin: the little behaviour the pages need beyond HTMX. Ours, like style.css.
   Everything is driven by data attributes; no page ever puts its own data into script text. */

(function () {
	"use strict";

	// --- scheme-only fields ---------------------------------------------------------
	// A form with scheme radios shows an element marked data-scheme="basic" (or "bearer",
	// "custom") only while that radio is checked. Its inputs are disabled while hidden, so
	// a value typed under one scheme is never submitted under another, and comes back if
	// the user flips the radio back.

	function applyScheme(form) {
		var checked = form.querySelector('input[name="scheme"]:checked');
		var scheme = checked ? checked.value : "";
		form.querySelectorAll("[data-scheme]").forEach(function (el) {
			var off = el.dataset.scheme !== scheme;
			el.hidden = off;
			el.querySelectorAll("input, select, textarea").forEach(function (field) {
				field.disabled = off;
			});
		});
	}

	document.querySelectorAll("form").forEach(function (form) {
		if (!form.querySelector('input[name="scheme"]')) return;
		applyScheme(form);
		form.addEventListener("change", function (event) {
			if (event.target.name === "scheme") applyScheme(form);
		});
	});

	// --- copy buttons ---------------------------------------------------------------
	// <button class="copy" data-copy="X-WR-Upstream: jira"> puts its data-copy text on the
	// clipboard. The admin is loopback-only, so the page is always a secure context and
	// navigator.clipboard exists; should it still refuse, the <code> just before the button
	// is selected so ⌘C finishes the job.

	function flash(button, text) {
		var label = button.textContent;
		button.textContent = text;
		button.disabled = true;
		setTimeout(function () {
			button.textContent = label;
			button.disabled = false;
		}, 1200);
	}

	function selectBefore(button) {
		var code = button.previousElementSibling;
		if (!code || !window.getSelection) return;
		var range = document.createRange();
		range.selectNodeContents(code);
		var selection = window.getSelection();
		selection.removeAllRanges();
		selection.addRange(range);
	}

	document.addEventListener("click", function (event) {
		var button = event.target.closest("button.copy[data-copy]");
		if (!button) return;
		var text = button.dataset.copy;
		var write = navigator.clipboard && navigator.clipboard.writeText
			? navigator.clipboard.writeText(text)
			: Promise.reject(new Error("no clipboard"));
		write.then(
			function () { flash(button, "copied"); },
			function () { selectBefore(button); flash(button, "select + \u2318C"); }
		);
	});
})();
