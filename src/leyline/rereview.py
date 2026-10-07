"""Reviewing a pull request again: what changed since the last review, and which findings it may have fixed.

A pull request is reviewed more than once. After the first review its author pushes commits, and a second review
that starts from scratch files the same findings again, beside the ones already open: long threads of repeated
comments are the usual result. So each run of `leyline pr` is kept as a record (the head commit, when, a short form
of the page's facts) with a slim snapshot of the code at that head (see diff.snapshot), and the next run says, on
the page and in the reviewers' facts:

- what changed between the two heads (functions edited, added and removed since the last review, not since the
  base), by comparing the snapshot with the store as it is now (diff.compare);
- which facts are new since then (a caller newly broken) and which are gone (fixed, or no longer true);
- for each open finding, whether the code its evidence names changed since it was filed: "may be fixed" asks the
  reviewer to re-check it, "still applies" asks them not to file it again.

A run is a new record only when the code changed: running `leyline pr` again on the same code updates the last
record, so the page says the same thing however often it is made. Snapshots of the last few runs are kept, and of
any run an open finding was filed against; the rest are deleted, their records kept.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Optional

from . import diff

KEEP = 3   # snapshots kept per review, besides those of runs an open finding was filed against

_TABLE = """CREATE TABLE IF NOT EXISTS review_runs (
  change_id TEXT NOT NULL, seq INTEGER NOT NULL, head_sha TEXT, dirty INTEGER, fingerprint TEXT, created TEXT,
  seen TEXT, snapshot TEXT, facts TEXT, PRIMARY KEY (change_id, seq))"""


def ensure(con) -> None:
    with con:
        con.execute(_TABLE)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def when(iso: Optional[str]) -> str:
    """2026-10-07 14:03 UTC"""
    try:
        t = datetime.datetime.fromisoformat(iso or "")
    except ValueError:
        return iso or "?"
    if t.tzinfo is not None:
        t = t.astimezone(datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M UTC")


def runs(con, cid: str) -> list[dict]:
    ensure(con)
    out = []
    for r in con.execute("SELECT * FROM review_runs WHERE change_id = ? ORDER BY seq", (cid,)):
        out.append({k: r[k] for k in r.keys()} | {"facts": json.loads(r["facts"] or "[]")})
    return out


def last_reviewed(con, cid: str) -> Optional[str]:
    """When the review was last run, for the list of earlier changes."""
    rs = runs(con, cid)
    return rs[-1]["seen"] or rs[-1]["created"] if rs else None


# -- the facts, in short ----------------------------------------------------------------------------------
def summarize(f: dict) -> list[dict]:
    """The facts on a pull request's page that say something may be wrong, each with a key that stays the same from
    one run to the next, one plain line, and the nodes it is about (to match a finding's evidence)."""
    reach, tests, st = f.get("reaches") or {}, f.get("tests") or {}, f.get("structure") or {}
    out = []

    def add(key, line, ids=(), **extra):
        out.append({"key": "|".join(str(k) for k in key), "line": line, "ids": [i for i in ids if i], **extra})
    for m in reach.get("signature_changed_callers_not_edited") or []:
        add(("caller", m["id"]), f"`{m['name']}` calls code whose signature changed, and was not edited", [m["id"]])
    for x in reach.get("removed_but_still_called") or []:
        add(("removed", x["id"]), f"`{x['removed']}` was removed and is still called by {_names(x['callers'])}",
            [x["id"], *x.get("caller_ids", [])])
    for a in reach.get("other_ends_not_edited") or []:
        add(("agree", a["id"], a["channel"], a.get("address") or ""), f"`{a['name']}` must agree with the change: {a['why']}",
            [a["id"]], group=f"{a['channel']} {a.get('address') or ''}".strip(), name=a["name"])
    for a in reach.get("reads_or_calls_across_a_channel") or []:
        add(("across", a["id"], a["channel"], a.get("address") or ""), f"`{a['name']}` {a['why']}", [a["id"]])
    for c in reach.get("channels_crossed") or []:
        what = f"{c['channel']} {c.get('program') or c.get('address') or ''}".strip()
        add(("channel", c.get("hub"), c["channel"], c.get("address") or ""), f"The edit touches the {what}", [c.get("hub")])
    for r in st.get("rules_now_failing") or []:
        add(("rule", r.get("id")), f"A rule now fails: {r['kind']} {r['from']}" + (f" -> {r['to']}" if r.get("to") else ""))
    for d in st.get("new_dependencies") or []:
        add(("dep", d.get("from_id") or d["from"], d.get("to_id") or d["to"]), f"New dependency: {d['from']} now uses {d['to']}")
    for t in tests.get("likely_to_fail_unedited") or []:
        add(("fails", t), f"Test `{t}` is likely to fail")
    for u in tests.get("changed_code_no_test_reaches") or []:
        add(("untested", u.get("id") or u["name"]), f"No test on the map reaches `{u['name']}`", [u.get("id")])
    seen, kept = set(), []
    for x in out:
        if x["key"] not in seen:
            seen.add(x["key"])
            kept.append(x)
    return kept


def _names(xs: list[str], k: int = 4) -> str:
    xs = list(dict.fromkeys(xs))
    return ", ".join(f"`{x}`" for x in xs[:k]) + (f" and {len(xs) - k} more" if len(xs) > k else "")


# -- comparing with the last review -----------------------------------------------------------------------
def _snap(con, name: Optional[str]) -> Optional[Path]:
    if not name:
        return None
    p = diff.snapshot_path(con, name)
    return p if p.exists() else None


def _hashes(con, ids: list[str]) -> dict:
    out = {}
    for i in ids:
        r = con.execute("SELECT content_hash FROM nodes WHERE id = ?", (i,)).fetchone()
        out[i] = r[0] if r else None
    return out


def _label(con, i: str) -> str:
    r = con.execute("SELECT n.name, p.name, p.kind FROM nodes n LEFT JOIN nodes p ON p.id = n.parent_id WHERE n.id = ?",
                    (i,)).fetchone()
    if r is None:
        return i.rsplit(":", 1)[-1]
    return f"{r[1]}.{r[0]}" if r[2] == "type" else r[0]


def since(con, cid: str, facts_now: list[dict], fingerprint: Optional[str] = None) -> Optional[dict]:
    """What changed since the last review of this pull request whose code differs from the code now. None on a
    first review."""
    fp = fingerprint or diff._fingerprint(con)
    rs = runs(con, cid)
    prev = next((r for r in reversed(rs) if r["fingerprint"] != fp), None)
    if prev is None:
        return None
    out: dict = {"previous": {"head_sha": prev["head_sha"], "dirty": bool(prev["dirty"]), "when": prev["created"]}}
    snap = _snap(con, prev["snapshot"])
    d = None
    if snap is not None:
        try:
            before = diff._open(snap)
            try:
                d = diff.compare(before, con)
            finally:
                before.close()
        except sqlite3.Error:
            d = None
    if d is None:
        out["code"] = None
        out["code_note"] = "The map of the code at the last review is no longer kept, so what changed since cannot be listed."
    else:
        nd = d["nodes"]
        pick = lambda xs: [{"id": n["id"], "name": n["name"], "kind": n["kind"], "path": n["path"]} for n in xs
                           if n["kind"] in ("callable", "test", "type")]
        outer = lambda xs: [n for n in xs if not any(n["id"].startswith(o["id"] + ".") for o in xs if o is not n)]
        out["code"] = {"edited": pick(nd["edited"] + nd["resigned"]), "types": pick(nd["types_edited"]),
                       "added": pick(outer(nd["added"])), "removed": pick(outer(nd["removed"]))}
    before_keys = {x["key"]: x for x in prev["facts"]}
    now_keys = {x["key"]: x for x in facts_now}
    out["new_facts"] = [x for k, x in now_keys.items() if k not in before_keys]
    out["gone_facts"] = [x for k, x in before_keys.items() if k not in now_keys]
    out["findings"] = _findings(con, cid, rs, out["gone_facts"])
    return out


def _findings(con, cid: str, rs: list[dict], gone: list[dict]) -> dict:
    """Each open finding: did the code its evidence names change since it was filed? Compared with the snapshot of
    the last run before the finding was filed."""
    out = {"may_be_fixed": [], "still_applies": [], "cannot_tell": []}
    gone_ids = {i for x in gone for i in x["ids"]}
    for f in con.execute("SELECT id, reviewer, severity, claim, evidence, created FROM findings WHERE change_id = ?"
                         " AND status = 'open' ORDER BY created", (cid,)).fetchall():
        ev = json.loads(f["evidence"] or "[]")
        item = {"id": f["id"], "reviewer": f["reviewer"], "severity": f["severity"], "claim": f["claim"],
                "evidence": [_label(con, e) for e in ev]}
        filed = [r for r in rs if (r["created"] or "") <= (f["created"] or "")]
        snap = _snap(con, filed[-1]["snapshot"]) if filed else None
        if snap is None:
            item["why"] = ("it was filed before the first review Leyline kept" if not filed
                           else "the map of the code it was filed against is no longer kept")
            out["cannot_tell"].append(item)
            continue
        item["filed_at"] = filed[-1]["head_sha"]
        try:
            before = diff._open(snap)
            try:
                was = _hashes(before, ev)
            finally:
                before.close()
        except sqlite3.Error:
            item["why"] = "the map of the code it was filed against could not be read"
            out["cannot_tell"].append(item)
            continue
        is_ = _hashes(con, ev)
        changed = [e for e in ev if was.get(e) != is_.get(e)]
        facts_gone = [e for e in ev if e in gone_ids]
        if changed or facts_gone:
            item["changed"] = [_label(con, e) for e in changed]
            item["why"] = (f"its code changed since it was filed ({_names(item['changed'], 3)})" if changed else
                           "the map no longer says what it said about its code when it was filed")
            out["may_be_fixed"].append(item)
        else:
            item["why"] = "its code did not change since it was filed"
            out["still_applies"].append(item)
    return out


def mark(found: list[dict], s: Optional[dict]) -> None:
    """Flag, on the findings listed on a page, those whose code changed since they were filed."""
    if not s:
        return
    maybe = {x["id"] for x in s["findings"]["may_be_fixed"]}
    for f in found:
        if f["id"] in maybe:
            f["code_changed_since"] = True


# -- keeping a record ---------------------------------------------------------------------------------------
def record(con, cid: str, head_sha: str, dirty: bool, facts_now: list[dict], fingerprint: Optional[str] = None) -> dict:
    """Keep this run: a new record, with a snapshot of the code, when the code differs from the last run's; else the
    last record, brought up to date."""
    ensure(con)
    fp = fingerprint or diff._fingerprint(con)
    rs = runs(con, cid)
    t = now()
    if rs and rs[-1]["fingerprint"] == fp:
        last = rs[-1]
        snap = last["snapshot"] if _snap(con, last["snapshot"]) else None
        if snap is None:
            snap = _take(con, f"{cid}.head-{last['seq']}")
        with con:
            con.execute("UPDATE review_runs SET head_sha = ?, dirty = ?, seen = ?, facts = ?, snapshot = ? WHERE change_id = ?"
                        " AND seq = ?", (head_sha, int(dirty), t, json.dumps(facts_now), snap, cid, last["seq"]))
        return {"seq": last["seq"], "new": False}
    seq = (rs[-1]["seq"] + 1) if rs else 1
    name = _take(con, f"{cid}.head-{seq}")
    with con:
        con.execute("INSERT INTO review_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (cid, seq, head_sha, int(dirty), fp, t, t, name, json.dumps(facts_now)))
    _prune(con, cid)
    return {"seq": seq, "new": True}


def _take(con, name: str) -> Optional[str]:
    """A slim snapshot of the store as it is now, or None when it cannot be written (the record is kept without)."""
    try:
        diff.snapshot(con, name)
        return name
    except (OSError, sqlite3.Error):
        return None


def _prune(con, cid: str) -> None:
    rs = runs(con, cid)
    keep = {r["seq"] for r in rs[-KEEP:]}
    for f in con.execute("SELECT created FROM findings WHERE change_id = ? AND status = 'open'", (cid,)).fetchall():
        filed = [r for r in rs if (r["created"] or "") <= (f[0] or "")]
        if filed:
            keep.add(filed[-1]["seq"])
    for r in rs:
        if r["seq"] not in keep and r["snapshot"]:
            diff.drop_snapshot(con, r["snapshot"])
            with con:
                con.execute("UPDATE review_runs SET snapshot = NULL WHERE change_id = ? AND seq = ?", (cid, r["seq"]))


def forget(con, cid: str) -> int:
    """Delete a review's head snapshots and its records (with `leyline spec forget pr-<id>`)."""
    n = 0
    for r in runs(con, cid):
        if r["snapshot"] and diff.drop_snapshot(con, r["snapshot"]):
            n += 1
    with con:
        con.execute("DELETE FROM review_runs WHERE change_id = ?", (cid,))
    return n


# -- the page -----------------------------------------------------------------------------------------------
def lines(s: Optional[dict], head: str) -> list[str]:
    """The page's section "Since the last review"; nothing on a first review."""
    if not s:
        return []
    p = s["previous"]
    was = f"`{(p['head_sha'] or '')[:7]}`" + (" with uncommitted edits" if p["dirty"] else "")
    L = ["", f"## Since the last review ({was}, {when(p['when'])})", ""]
    c = s.get("code")
    if c is None:
        L.append(s.get("code_note") or "")
    else:
        parts = []
        fns = [x for x in c["edited"] if x["kind"] != "type"]
        if fns:
            parts.append(f"{len(fns)} function{'s' if len(fns) != 1 else ''} edited ({_names([x['name'] for x in fns], 6)})")
        if c["types"] and not fns:
            parts.append(f"{len(c['types'])} type{'s' if len(c['types']) != 1 else ''} changed ({_names([x['name'] for x in c['types']], 4)})")
        if c["added"]:
            parts.append(f"{len(c['added'])} added ({_names([x['name'] for x in c['added']], 6)})")
        if c["removed"]:
            parts.append(f"{len(c['removed'])} removed ({_names([x['name'] for x in c['removed']], 6)})")
        L.append(f"Between {was} and {head}: " + ("; ".join(parts) if parts else "nothing on the map changed") + ".")
    if s["new_facts"] or s["gone_facts"]:
        L.append("")
    for facts, lead, agree in ((s["new_facts"], "**New:**", "must agree with the change"),
                               (s["gone_facts"], "Gone (fixed, or no longer true):", "no longer need to agree with it")):
        said = _grouped(facts, agree)
        L += [f"- {lead} {x}." for x in said[:8]]
        if len(said) > 8:
            L.append(f"- and {len(said) - 8} more {'new' if lead.startswith('**') else 'gone'}.")
    fs = s["findings"]
    if any(fs.values()):
        L += ["", "Open findings:"]
        for x in fs["may_be_fixed"]:
            L.append(f"- {x['id']} ({x['severity']}, {x['reviewer']}): {_said(x['claim'])} **May be fixed:** {x['why']}."
                     " Re-check it rather than filing it again.")
        for x in fs["still_applies"]:
            L.append(f"- {x['id']} ({x['severity']}, {x['reviewer']}): {_said(x['claim'])} **Still applies:** {x['why']}."
                     " Do not file it again.")
        for x in fs["cannot_tell"]:
            L.append(f"- {x['id']} ({x['severity']}, {x['reviewer']}): {_said(x['claim'])} Cannot tell whether its code"
                     f" changed: {x['why']}.")
    return L


def _grouped(facts: list[dict], agree: str) -> list[str]:
    """The facts' lines, with four or more other ends of one channel said once."""
    facts = [x | {"group": " ".join(x["key"].split("|")[2:]).strip(), "name": x["line"].split("`")[1]}
             if x["key"].startswith("agree|") and not x.get("group") and x["line"].count("`") >= 2 else x
             for x in facts]   # a run kept before facts carried their group
    groups: dict = {}
    for x in facts:
        if x.get("group"):
            groups.setdefault(x["group"], []).append(x)
    out, done = [], set()
    for x in facts:
        g = groups.get(x.get("group") or "")
        if g and len(g) > 3:
            if x["group"] not in done:
                done.add(x["group"])
                out.append(f"{len(g)} other ends of the {x['group']} {agree} ({_names([y['name'] for y in g], 3)})")
            continue
        out.append(x["line"])
    return out


def _said(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > 160:
        text = text[:160].rsplit(" ", 1)[0] + "..."
    return text if text.endswith((".", "!", "?")) else text + "."


def for_facts(s: Optional[dict]) -> dict:
    """since_last_review, as the reviewers' facts carry it."""
    if not s:
        return {"first_review": True}
    return {**s, "ask": "Start here. Re-check each finding under may_be_fixed: settle it with the person if the code now"
                        " does the right thing, and do not file it again. Do not refile findings under still_applies."
                        " Review new_facts and the code changed since the last review first."}
