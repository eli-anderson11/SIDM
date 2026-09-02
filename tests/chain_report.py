"""Per-PR chain report for SIDM.

Runs the SidmProcessor over a small committed fixture and reports the state of the
processing + plotting chain, so a reviewer can see -- side by side, current (main)
vs. this PR -- what executes, what errors, what warns, and how the cutflows move.
It does NOT gate the merge; it informs the reviewer.

Two modes:
    python tests/chain_report.py compute state.json              # run chain, dump state to file
    python tests/chain_report.py render base.json pr.json        # diff two states -> markdown

CI runs `compute` once on the base commit and once on the PR, then `render`s the
diff into the GitHub Actions job summary. `render` exits 1 (a soft-red check) only
when the PR introduces a NEW error; see new_errors() for the exact list.

NOTE: this reports EXECUTION + regression, not physics correctness -- a silently
wrong value or cut can still execute cleanly.
"""
import json
import os
import re
import sys
import traceback

import matplotlib
matplotlib.use("Agg")  # headless: utilities imports pyplot at import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e_common as e2e

# the processor skips a hist on TWO paths: fill-function "could not be evaluated"
# (histogram.py:65) and "could not be filled" (histogram.py:81) -- catch both
_SKIPPED_HIST_RE = re.compile(r"histogram with the name (\w+) could not be (?:evaluated|filled)")

# The processor's four cut-failure warning formats (selection.py:35, :44, :83,
# :119). Parsed EXACTLY (anchored regex + equality on the captured names) rather
# than by substring: cut names like `4mu` or `ljs` are substrings of unrelated
# warnings, and substring matching would flag an intentional cut removal as a
# failure.
# MAINTENANCE: keep these in sync with the print() strings in selection.py. A
# reworded warning there silently disables detection of that failure class (a
# real new failure stays green) -- it fails safe, but the coverage is lost.
_EVT_EVAL_RE = re.compile(r"^Warning: Unable to evaluate (.+?) Skipping\.")
_EVT_APPLY_RE = re.compile(r"^Warning: Unable to apply event cuts to (\S+)\. Skipping\.")
_NESTED_APPLY_RE = re.compile(r"^Warning: Unable to apply (.+?) for nested (\S+) collection\. Skipping\.")
_OBJ_APPLY_RE = re.compile(r"^Warning: Unable to apply (.+?) for (\S+)\. Skipping\.")


def compute_state():
    """Run the chain over the fixture and return a JSON-able state dict."""
    state = {
        "n_channels_run": 0, "cutflows": {}, "cutflows_weighted": {},
        "broken_collections": {}, "valid_collections": [], "valid_collection_count": 0,
        "warnings": [], "skipped_hists": [],
        "channel_health": {"zero_final": [], "no_cut_rows": []},
        "dropped_representative_channels": [], "reps_used": [],
        "cutflow_collection": None,
        "scaled_sum_weights": None, "error": None, "inventory_error": None,
        "cutflows_data": {}, "cutflows_data_weighted": {},
        "n_channels_data": 0, "error_data": None,
    }
    # Static inventory FIRST, in its own try: these are pure config reads that need
    # no chain run, and keeping them independent means a chain crash cannot blank
    # the baseline inventory (which would turn every pre-existing broken collection
    # into a "newly broken" false red on the other side).
    try:
        state["broken_collections"] = e2e.broken_collections()
        state["valid_collections"] = e2e.valid_collections()
        state["valid_collection_count"] = len(state["valid_collections"])
    except Exception:
        state["inventory_error"] = traceback.format_exc()
    try:
        known = set(e2e.all_channels())
        # the cutflow pass needs one valid collection to run under; fall back if
        # pv_base is renamed/removed rather than crashing this side
        if "pv_base" in state["valid_collections"]:
            state["cutflow_collection"] = "pv_base"
        elif state["valid_collections"]:
            state["cutflow_collection"] = state["valid_collections"][0]
        else:
            raise RuntimeError("no valid hist collection available for the cutflow pass")
        out_sel, warns_sel = e2e.run_chain(sorted(known), [state["cutflow_collection"]])
        state["cutflows"] = e2e.cutflow_counts(out_sel)
        state["cutflows_weighted"] = e2e.cutflow_weighted(out_sel)
        state["channel_health"] = e2e.channel_health(out_sel)
        state["scaled_sum_weights"] = e2e.scaled_sum_weights(out_sel)
        state["n_channels_run"] = len(state["cutflows"])
        # a channel renamed/removed on this side is skipped and reported, not a crash
        reps = [c for c in e2e.REPRESENTATIVE_CHANNELS if c in known]
        state["reps_used"] = reps
        state["dropped_representative_channels"] = sorted(
            set(e2e.REPRESENTATIVE_CHANNELS) - known)
        _, warns_hist = e2e.run_chain(reps, state["valid_collections"])
        # Data-fixture pass: the same channel sweep over a real 2018C data
        # slice spanning a golden-JSON dead-run boundary (see DATA_FIXTURE in
        # _e2e_common). Data-only code paths -- golden-JSON filtering above
        # all -- are invisible to the MC fixture. Own try so a data-only crash
        # is reported as exactly that and the MC results above survive.
        warns_data = set()
        try:
            out_data, warns_data = e2e.run_chain(
                sorted(known), [state["cutflow_collection"]], data=True)
            state["cutflows_data"] = e2e.cutflow_counts(out_data)
            state["cutflows_data_weighted"] = e2e.cutflow_weighted(out_data)
            state["n_channels_data"] = len(state["cutflows_data"])
        except Exception:
            state["error_data"] = traceback.format_exc()
        warnings = sorted(warns_sel | warns_hist | warns_data)
        state["warnings"] = warnings
        state["skipped_hists"] = sorted({m.group(1) for w in warnings
                                         for m in [_SKIPPED_HIST_RE.search(w)] if m})
    except Exception:
        state["error"] = traceback.format_exc()
    return state


def _fmt_list(items, empty="_none_"):
    return "\n".join(f"- `{x}`" for x in items) if items else empty


def _menu(state):
    """All collections present in this side's menu (valid or broken)."""
    return set(state.get("valid_collections", [])) | set(state.get("broken_collections", {}))


def worsened_collections(base, pr):
    """{collection: [hists newly missing on the PR side]} -- catches both brand-new
    broken collections AND already-broken collections that lost MORE hists (a
    key-set diff would miss the latter entirely)."""
    b, p = base.get("broken_collections", {}), pr.get("broken_collections", {})
    out = {}
    for coll, missing in p.items():
        delta = sorted(set(missing) - set(b.get(coll, [])))
        if delta:
            out[coll] = delta
    return out


def repaired_collections(base, pr):
    """{collection: [hists no longer missing on the PR side]} for collections STILL
    in the PR's menu -- full and partial repairs both show up. A broken collection
    deleted from the menu altogether is not a repair; see deleted_broken_collections."""
    b, p = base.get("broken_collections", {}), pr.get("broken_collections", {})
    pr_menu = _menu(pr)
    out = {}
    for coll, missing in b.items():
        if coll not in pr_menu:
            continue
        delta = sorted(set(missing) - set(p.get(coll, [])))
        if delta:
            out[coll] = delta
    return out


def deleted_broken_collections(base, pr):
    """Broken collections that the PR removed from the menu entirely (neither valid
    nor broken on the PR side) -- shown as deletions, not mistaken for repairs."""
    return sorted(set(base.get("broken_collections", {})) - _menu(pr))


def failing_cut_signatures(warnings):
    """Stable signatures of the processor's cut-failure warnings. Only these three
    warning classes count as a cut failing; an intentional cut removal emits no
    warning at all, so it can never match."""
    sigs = set()
    for w in warnings:
        m = _EVT_EVAL_RE.match(w)
        if m:
            sigs.add(f"event cut `{m.group(1)}` could not be evaluated")
            continue
        m = _EVT_APPLY_RE.match(w)
        if m:
            sigs.add(f"event-cut mask could not be applied to collection `{m.group(1)}`")
            continue
        m = _NESTED_APPLY_RE.match(w)
        if m:
            sigs.add(f"object cut `{m.group(1)}` could not be applied to nested `{m.group(2)}` collection")
            continue
        m = _OBJ_APPLY_RE.match(w)
        if m:
            sigs.add(f"object cut `{m.group(1)}` could not be applied to `{m.group(2)}`")
    return sigs


def _comparable_hist_coverage(base, pr):
    """The hist pass ran over the same channels x collections on both sides, so a
    skipped-hist delta is attributable to the PR (not to a coverage difference)."""
    return (base.get("reps_used") == pr.get("reps_used")
            and base.get("valid_collections") == pr.get("valid_collections"))


def new_errors(base, pr):
    """Reasons this PR introduces NEW errors -> drives the soft red check.
    Red: a chain crash on the PR side (or on BOTH sides -- either an environment/
    harness change in the PR, or main itself is broken; loud either way); a newly
    broken or worsened hist collection; a hist newly failing to evaluate/fill; a
    cut newly failing to evaluate/apply (matched by exact warning class -- an
    intentional cut removal emits no warning and stays green). New *warnings*
    outside those failure classes do NOT count (informational/green)."""
    reasons = []
    if pr.get("error"):
        if base.get("error"):
            reasons.append("the chain crashed on BOTH sides -- either an environment or "
                           "harness change in this PR broke it (only `sidm/` is swapped "
                           "for the base run), or `main` itself is currently broken (then "
                           "other PRs' reports are red too); tracebacks in the report")
        else:
            reasons.append("the chain crashed on this PR (traceback in the report)")
    if pr.get("error_data"):
        if base.get("error_data"):
            reasons.append("the chain crashed on the DATA fixture on BOTH sides -- "
                           "either an environment or harness change in this PR broke "
                           "it, or `main` itself is currently broken for data")
        else:
            reasons.append("the chain crashed on the DATA fixture on this PR "
                           "(traceback in the report); the MC fixture alone cannot "
                           "see data-only code paths such as golden-JSON filtering")
    if pr.get("inventory_error") and not base.get("inventory_error"):
        reasons.append("the hist-collection inventory failed to build on this PR "
                       "(traceback in the report)")
    # collection regressions are judgeable only when both inventories built
    if not base.get("inventory_error") and not pr.get("inventory_error"):
        worsened = worsened_collections(base, pr)
        if worsened:
            reasons.append("newly broken or worsened hist collection(s): "
                           + "; ".join(f"`{c}` newly missing {m}" for c, m in sorted(worsened.items())))
    # runtime regressions are judgeable only when both chains ran
    if not base.get("error") and not pr.get("error"):
        new_failures = sorted(failing_cut_signatures(pr.get("warnings", []))
                              - failing_cut_signatures(base.get("warnings", [])))
        if new_failures:
            reasons.append("cut(s) newly failing to evaluate/apply at runtime: "
                           + "; ".join(new_failures))
        if _comparable_hist_coverage(base, pr):
            new_skips = sorted(set(pr.get("skipped_hists", []))
                               - set(base.get("skipped_hists", [])))
            if new_skips:
                reasons.append(f"hist(s) newly failing to evaluate/fill at runtime: {new_skips}")
    return reasons


def render(base, pr):
    """Render a markdown diff report (current=base vs after=pr)."""
    out = ["## SIDM chain report", "", "_Current (`main`) → this PR. Reviewer-facing and advisory "
           "(does not hard-block merge). Verifies execution + regression, not physics correctness._", ""]
    _errs = new_errors(base, pr)
    out += [("### ❌ This PR introduces new errors\n\n" + "\n".join(f"- {r}" for r in _errs))
            if _errs else "### ✅ No new errors introduced by this PR", ""]

    chain_ok = not base.get("error") and not pr.get("error")
    inv_ok = not base.get("inventory_error") and not pr.get("inventory_error")

    if pr.get("error"):
        out += ["### ❌ The chain ERRORED on this PR", "",
                "```", pr["error"].strip()[-2500:], "```", ""]
    if pr.get("error_data"):
        out += ["### ❌ The chain ERRORED on the DATA fixture on this PR", "",
                "```", pr["error_data"].strip()[-2500:], "```", ""]
    if base.get("error_data"):
        out += ["<details><summary>Base-side DATA-fixture traceback</summary>", "",
                "```", base["error_data"].strip()[-2500:], "```", "", "</details>", ""]
    if base.get("error"):
        out += ["> Note: the base (`main`) chain errored; the cutflow/warning comparison "
                "below is unavailable. The static collection inventory is unaffected.", ""]
        out += ["<details><summary>Base-side traceback</summary>", "",
                "```", base["error"].strip()[-2500:], "```", "", "</details>", ""]
    for side, key in (("base (`main`)", base), ("PR", pr)):
        if key.get("inventory_error"):
            out += [f"> Note: the {side} hist-collection inventory failed to build; the "
                    "collection comparison is unavailable on that side.", ""]

    b_brk, p_brk = base.get("broken_collections", {}), pr.get("broken_collections", {})
    b_warn, p_warn = set(base.get("warnings", [])), set(pr.get("warnings", []))
    b_skip, p_skip = set(base.get("skipped_hists", [])), set(pr.get("skipped_hists", []))
    cf_b, cf_p = base.get("cutflows", {}), pr.get("cutflows", {})
    cfw_b, cfw_p = base.get("cutflows_weighted", {}), pr.get("cutflows_weighted", {})
    bh_b = base.get("channel_health", {})
    bh_p = pr.get("channel_health", {})
    # chain-derived diffs are meaningful only when both chains ran; a crashed side
    # has empty cutflows and would fabricate "all channels removed" etc.
    changed = sorted(c for c in set(cf_b) & set(cf_p)
                     if cf_b[c] != cf_p[c] or cfw_b.get(c, {}) != cfw_p.get(c, {})) if chain_ok else []
    added_ch = sorted(set(cf_p) - set(cf_b)) if chain_ok else []
    removed_ch = sorted(set(cf_b) - set(cf_p)) if chain_ok else []
    sw_b, sw_p = base.get("scaled_sum_weights"), pr.get("scaled_sum_weights")
    sw_changed = chain_ok and sw_b != sw_p
    data_ok = not base.get("error_data") and not pr.get("error_data")
    cfd_b, cfd_p = base.get("cutflows_data", {}), pr.get("cutflows_data", {})
    cfdw_b = base.get("cutflows_data_weighted", {})
    cfdw_p = pr.get("cutflows_data_weighted", {})
    changed_data = sorted(c for c in set(cfd_b) & set(cfd_p)
                          if cfd_b[c] != cfd_p[c]
                          or cfdw_b.get(c, {}) != cfdw_p.get(c, {})) if data_ok else []

    def _sw(v):
        return "—" if v is None else str(v)

    out += [
        "### Summary", "",
        f"| metric | current | this PR |",
        f"|---|--:|--:|",
        f"| channels executed | {base.get('n_channels_run',0)} | {pr.get('n_channels_run',0)} |",
        f"| valid hist collections | {base.get('valid_collection_count',0)} | {pr.get('valid_collection_count',0)} |",
        f"| broken collections | {len(b_brk)} | {len(p_brk)} |",
        f"| catch-and-skip warnings | {len(b_warn)} | {len(p_warn)} |",
        f"| hists skipped (could not evaluate/fill) | {len(b_skip)} | {len(p_skip)} |",
        f"| channels ending at 0 events (count regressions invisible) | {len(bh_b.get('zero_final',[]))} | {len(bh_p.get('zero_final',[]))} |",
        f"| channels with no event-cut rows (only the initial total) | {len(bh_b.get('no_cut_rows',[]))} | {len(bh_p.get('no_cut_rows',[]))} |",
        f"| scaled Σ genWeight (Σw / skim_factor) | {_sw(sw_b)} | {_sw(sw_p)} |",
        f"| channels with changed cutflow | — | {len(changed) if chain_ok else '—'} |",
        f"| channels executed on the data fixture | {base.get('n_channels_data', 0)} | {pr.get('n_channels_data', 0)} |",
        f"| channels with changed DATA cutflow | — | {len(changed_data) if data_ok else '—'} |",
        "",
    ]
    if not chain_ok:
        out += ["_Chain-derived rows above reflect only the side(s) that ran._", ""]

    # Highlight what THIS PR changes
    worsened = worsened_collections(base, pr) if inv_ok else {}
    repaired = repaired_collections(base, pr) if inv_ok else {}
    deleted_brk = deleted_broken_collections(base, pr) if inv_ok else []
    b_valid, p_valid = set(base.get("valid_collections", [])), set(pr.get("valid_collections", []))
    vc_added = sorted(p_valid - b_valid) if inv_ok else []
    vc_removed = sorted(b_valid - p_valid) if inv_ok else []
    new_warn = sorted(p_warn - b_warn) if chain_ok else []
    gone_warn = sorted(b_warn - p_warn) if chain_ok else []
    dropped_reps = pr.get("dropped_representative_channels", [])
    coverage_note = (chain_ok and not _comparable_hist_coverage(base, pr))

    out += ["### What this PR changes", ""]
    any_change = (worsened or repaired or deleted_brk or vc_added or vc_removed
                  or new_warn or gone_warn or changed or added_ch or removed_ch
                  or changed_data or sw_changed
                  or pr.get("error") or pr.get("inventory_error")
                  or pr.get("error_data"))
    if chain_ok and inv_ok and not any_change:
        out += ["No change to chain execution, collections, warnings, or cutflows. ✅", ""]
    else:
        if not chain_ok:
            out += ["_Cutflow/warning deltas unavailable -- the chain crashed on "
                    + ("both sides" if base.get("error") and pr.get("error")
                       else ("the base side" if base.get("error") else "the PR side"))
                    + "; static collection deltas are still shown._", ""]
        if worsened:
            out += [f"**Newly broken or worsened collections ({len(worsened)}):**",
                    "\n".join(f"- `{c}` → newly missing {m}" for c, m in sorted(worsened.items())), ""]
        if repaired:
            out += [f"**Collections repaired (fully or partially) ({len(repaired)}):**",
                    "\n".join(f"- `{c}` → no longer missing {m}" for c, m in sorted(repaired.items())), ""]
        if deleted_brk:
            out += [f"**Broken collections removed from the menu ({len(deleted_brk)}):**",
                    _fmt_list(deleted_brk), ""]
        if vc_added or vc_removed:
            out += [f"**Valid collections added:** {vc_added or '_none_'}  "
                    f"**removed:** {vc_removed or '_none_'}", ""]
        if sw_changed:
            out += [f"**Scaled Σ genWeight changed:** {_sw(sw_b)} → {_sw(sw_p)}", ""]
        if new_warn:
            out += [f"**New warnings ({len(new_warn)}):**", _fmt_list(new_warn), ""]
        if gone_warn:
            out += [f"**Warnings no longer emitted ({len(gone_warn)}):**", _fmt_list(gone_warn), ""]
        if added_ch or removed_ch:
            out += [f"**Channels added:** {added_ch or '_none_'}  **removed:** {removed_ch or '_none_'}", ""]
        if changed:
            out += [f"**Cutflow changed in {len(changed)} channel(s)** _(cumulative events "
                    "passing each event-level cut; raw and weighted)_:", "",
                    "| channel | cut | raw current | raw PR | weighted current | weighted PR |",
                    "|---|---|--:|--:|--:|--:|"]
            for c in changed[:50]:
                cuts = sorted(set(cf_b[c]) | set(cf_p[c])
                              | set(cfw_b.get(c, {})) | set(cfw_p.get(c, {})))
                for cut in cuts:
                    a, b_ = cf_b[c].get(cut), cf_p[c].get(cut)
                    wa, wb = cfw_b.get(c, {}).get(cut), cfw_p.get(c, {}).get(cut)
                    if a != b_ or wa != wb:
                        row = [str(x) if x is not None else "—" for x in (a, b_, wa, wb)]
                        out.append(f"| {c} | {cut} | {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
            if len(changed) > 50:
                out.append(f"\n_…and {len(changed) - 50} more changed channels._")
            out.append("")
        if changed_data:
            out += [f"**Data-fixture cutflow changed in {len(changed_data)} channel(s)** "
                    "_(raw and weighted, as above but over the 2018C data slice)_:", "",
                    "| channel | cut | raw current | raw PR | weighted current | weighted PR |",
                    "|---|---|--:|--:|--:|--:|"]
            for c in changed_data[:50]:
                cuts = sorted(set(cfd_b[c]) | set(cfd_p[c])
                              | set(cfdw_b.get(c, {})) | set(cfdw_p.get(c, {})))
                for cut in cuts:
                    a, b_ = cfd_b[c].get(cut), cfd_p[c].get(cut)
                    wa, wb = cfdw_b.get(c, {}).get(cut), cfdw_p.get(c, {}).get(cut)
                    if a != b_ or wa != wb:
                        row = [str(x) if x is not None else "—" for x in (a, b_, wa, wb)]
                        out.append(f"| {c} | {cut} | {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
            if len(changed_data) > 50:
                out.append(f"\n_…and {len(changed_data) - 50} more changed channels._")
            out.append("")
    if coverage_note:
        out += ["_The hist pass covered different channels/collections on the two sides "
                "(a representative channel or collection was added/renamed/removed), so "
                "skipped-hist deltas are shown but not used for the red check._", ""]
    if dropped_reps:
        out += [f"_Representative channels not defined in this PR's selections (skipped in "
                f"the hist pass): {dropped_reps}_", ""]

    # Always-present current-state detail (collapsed). A crashed side has empty
    # defaults, which would read as an affirmative "_none_" -- say "unavailable".
    out += ["<details><summary>Current broken collections (baseline state)</summary>", ""]
    if base.get("inventory_error"):
        out += ["_unavailable — the base-side inventory failed to build_"]
    else:
        out += [f"- `{c}` → missing {sorted(v)}" for c, v in sorted(b_brk.items())] or ["_none_"]
    out += ["", "</details>", ""]
    if not pr.get("inventory_error") and not base.get("inventory_error") and p_brk != b_brk:
        out += ["<details><summary>Broken collections AFTER this PR</summary>", ""]
        out += [f"- `{c}` → missing {sorted(v)}" for c, v in sorted(p_brk.items())] or ["_none_"]
        out += ["", "</details>", ""]
    out += ["<details><summary>Current catch-and-skip warnings (baseline state)</summary>", ""]
    out += ["_unavailable — the base-side chain crashed_" if base.get("error")
            else _fmt_list(sorted(b_warn))]
    out += ["", "</details>", ""]
    health_lines = ([f"- `{c}` — 0 events after the last cut" for c in bh_p.get("zero_final", [])]
                    + [f"- `{c}` — no event-cut rows (only the initial event count)"
                       for c in bh_p.get("no_cut_rows", [])])
    out += ["<details><summary>Channels whose cutflow the fixture cannot regression-test (PR side)</summary>", ""]
    out += (["_unavailable — the PR-side chain crashed_"] if pr.get("error")
            else (health_lines or ["_none_"]))
    out += ["", "</details>", ""]
    return "\n".join(out)


def main(argv):
    # compute writes JSON to a FILE (not stdout): the chain emits banner/progress
    # text to stdout at the C level (fastjet), which would corrupt a stdout dump.
    if len(argv) == 2 and argv[0] == "compute":
        with open(argv[1], "w", encoding="utf-8") as fh:
            json.dump(compute_state(), fh, indent=2, sort_keys=True)
    elif len(argv) == 3 and argv[0] == "render":
        with open(argv[1], encoding="utf-8") as fh:
            base = json.load(fh)
        with open(argv[2], encoding="utf-8") as fh:
            pr = json.load(fh)
        print(render(base, pr))
        if new_errors(base, pr):   # soft red check -- report is already printed
            sys.exit(1)
    else:
        sys.exit("usage: chain_report.py compute OUT.json | render base.json pr.json")


if __name__ == "__main__":
    main(sys.argv[1:])
