"""Tool library: tools that blind and low-vision people made for each other, and keeping them working
when screen readers, operating systems and AI tools update. Members report what broke, volunteer to help,
and mark problems fixed; there are no staff. The tables are defined in find.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..web import checked_form, current_user, date_words, error_title, flash, redirect, render, require_user
from .find import (CATEGORY_PAIRS, KEEP, NO_REPORTS, OUTCOMES, PUBLIC_SECRET_MESSAGE, SIGHT_LABEL, SIGHT_ONSETS, already_listed,
                   can_change, clean, find_by_url, insert_member_tool, long_message, looks_secret, pop_done,
                   report_defaults, report_summaries, reports_for, saved_at, set_done, too_long, tool_or_404,
                   validate_tool)

router = APIRouter(prefix="/library")
NAV = {"label": "Tool library", "url": "/library",
       "description": "Tools that blind and low-vision people made for each other, and help to keep them working when screen readers and apps update."}
SCHEMA = ""

FILTERS = [("all", "All"), ("upkeep", "Need help after an update")]
PROBLEM_MAX, AFTER_MAX, SKILLS_MAX = 3000, 200, 1000
ADD_TITLE = "Add a tool a blind or low-vision person made"


def _upkeep(tool_id: int) -> dict:
    with db.session() as conn:
        problems = [dict(r) for r in conn.execute(
            "SELECT p.*, u.pseudonym FROM upkeep_requests p JOIN users u ON u.id = p.user_id"
            " WHERE p.tool_id = ? ORDER BY p.id", (tool_id,)).fetchall()]
        volunteers = [dict(r) for r in conn.execute(
            "SELECT v.*, u.pseudonym FROM upkeep_volunteers v JOIN users u ON u.id = v.user_id"
            " WHERE v.tool_id = ? ORDER BY v.id", (tool_id,)).fetchall()]
    for p in problems:
        p["date"] = date_words(p["created_at"])
    return {"open": [p for p in problems if p["status"] == "open"],
            "resolved": [p for p in problems if p["status"] == "resolved"],
            "volunteers": volunteers}


def _library_tool_or_redirect(tool_id: int):
    tool = tool_or_404(tool_id)
    if not tool["made_by_blind"]:
        return tool, redirect(f"/find/tool/{tool_id}")
    return tool, None


def library_title(tool: dict) -> str:
    return f"{tool['name']} in the Tool library"


def render_library_tool(request: Request, tool: dict, *, report_values: dict | None = None,
                        report_errors: dict | None = None, problem_values: dict | None = None,
                        problem_errors: dict | None = None, volunteer_values: dict | None = None,
                        volunteer_errors: dict | None = None, status_code: int = 200):
    user = current_user(request)
    upkeep = _upkeep(tool["id"])
    my_volunteer = next((v for v in upkeep["volunteers"] if user and v["user_id"] == user["id"]), None)
    for p in upkeep["open"]:
        p["can_resolve"] = bool(user) and (p["user_id"] == user["id"] or tool["added_by"] == user["id"])
    any_errors = report_errors or problem_errors or volunteer_errors
    done = pop_done(request, "library", tool["id"])
    title = error_title(library_title(tool), any_errors) if any_errors else (done or library_title(tool))
    return render(
        request, "library/tool.html", title=title, tool=tool, upkeep=upkeep, my_volunteer=my_volunteer,
        can_change=can_change(tool, user), sight_label=SIGHT_LABEL,
        reports=reports_for(tool["id"]), summary=report_summaries([tool["id"]])[tool["id"]],
        values=report_values if report_values is not None else report_defaults(user),
        errors=report_errors or {}, problem_values=problem_values or {}, problem_errors=problem_errors or {},
        volunteer_values=volunteer_values or {"skills": my_volunteer["skills"] if my_volunteer else ""},
        volunteer_errors=volunteer_errors or {}, outcomes=OUTCOMES, sight_onsets=SIGHT_ONSETS,
        status_code=status_code)


@router.get("")
def index(request: Request, show: str = "all"):
    show = show if show in dict(FILTERS) else "all"
    with db.session() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM upkeep_requests p WHERE p.tool_id = t.id AND p.status = 'open') AS open_count,"
            " (SELECT COUNT(*) FROM upkeep_volunteers v WHERE v.tool_id = t.id) AS volunteer_count"
            " FROM tools t WHERE t.made_by_blind = 1 ORDER BY t.name COLLATE NOCASE").fetchall()]
    counts = {"all": len(rows), "upkeep": sum(1 for r in rows if r["needs_upkeep"])}
    tools = [r for r in rows if show == "all" or r["needs_upkeep"]]
    title = NAV["label"] if show == "all" else "Tool library: tools that need help after an update"
    return render(request, "library/index.html", title=title, tools=tools, show=show, filters=FILTERS,
                  counts=counts, summaries=report_summaries([t["id"] for t in tools]), no_reports=NO_REPORTS)


@router.get("/add")
def add_form(request: Request, user: dict = Depends(require_user)):
    return render(request, "library/add.html", title=ADD_TITLE,
                  values={"category": "Other"}, errors={}, category_pairs=CATEGORY_PAIRS)


@router.post("/add")
async def add_tool(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values, errors = validate_tool(form, require_blind=True)
    if "url" not in errors:
        existing = find_by_url(values["url"])
        if existing:
            if existing["made_by_blind"]:
                set_done(request, "library", existing["id"], f"Already in the Tool library: {existing['name']}")
                flash(request, already_listed(existing, values.get("name", ""), "the Tool library"), "info")
                return redirect(f"/library/tool/{existing['id']}")
            set_done(request, "find", existing["id"], f"Already in the directory: {existing['name']}")
            flash(request, f"{existing['name']} is already in the directory at this web address, but nobody has "
                           "recorded that a blind or low-vision person made it, so it is not in the Tool library. "
                           "This is its page.", "info")
            return redirect(f"/find/tool/{existing['id']}")
    if errors:
        return render(request, "library/add.html", title=error_title(ADD_TITLE, errors),
                      values=values, errors=errors, category_pairs=CATEGORY_PAIRS, status_code=400)
    tool_id = insert_member_tool(values, user["id"])
    set_done(request, "library", tool_id, f"Added to the Tool library: {values['name']}")
    flash(request, f"{values['name']} is now in the Tool library{saved_at(values)}. Thank you.", "success")
    return redirect(f"/library/tool/{tool_id}")


@router.get("/tool/{tool_id}")
def tool_page(request: Request, tool_id: int):
    tool, other = _library_tool_or_redirect(tool_id)
    return other or render_library_tool(request, tool)


def _back_to_tool(tool_id: int, anchor: str = ""):
    """The forms on a tool page post to addresses under it. Someone whose session ran out signs in and lands
    on one of those addresses; send them back to the tool page instead of an error."""
    tool_or_404(tool_id)
    return redirect(f"/library/tool/{tool_id}" + (f"#{anchor}" if anchor else ""))


@router.get("/tool/{tool_id}/problem")
def report_problem_get(tool_id: int):
    return _back_to_tool(tool_id, "problem-form-title")


@router.get("/tool/{tool_id}/problem/{problem_id}/resolve")
def resolve_problem_get(tool_id: int, problem_id: int):
    return _back_to_tool(tool_id, "problems-title")


@router.get("/tool/{tool_id}/volunteer")
def volunteer_get(tool_id: int):
    return _back_to_tool(tool_id, "volunteer-form-title")


@router.get("/tool/{tool_id}/volunteer/remove")
def stop_volunteering_get(tool_id: int):
    return _back_to_tool(tool_id, "volunteer-form-title")


@router.post("/tool/{tool_id}/problem")
async def report_problem(request: Request, tool_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool, other = _library_tool_or_redirect(tool_id)
    if other:
        raise HTTPException(status_code=404, detail="We could not find that tool in the Tool library.")
    values = {"problem": clean(form, "problem", KEEP), "after_update": clean(form, "after_update", KEEP)}
    errors: dict[str, str] = {}
    if not values["problem"]:
        errors["problem"] = "Say what stopped working."
    elif too_long(values, "problem", PROBLEM_MAX):
        errors["problem"] = long_message(PROBLEM_MAX)
    elif looks_secret(values["problem"]):
        errors["problem"] = PUBLIC_SECRET_MESSAGE
    if not values["after_update"]:
        errors["after_update"] = "Say which update it broke after, for example NVDA 2026.1 or iOS 27. Write 'not sure' if you do not know."
    elif too_long(values, "after_update", AFTER_MAX):
        errors["after_update"] = long_message(AFTER_MAX) + " The name and number of the update is enough."
    elif looks_secret(values["after_update"]):
        errors["after_update"] = PUBLIC_SECRET_MESSAGE
    if errors:
        return render_library_tool(request, tool, problem_values=values, problem_errors=errors, status_code=400)
    with db.session() as conn:
        same = conn.execute(
            "SELECT id FROM upkeep_requests WHERE tool_id = ? AND user_id = ? AND status = 'open' AND problem = ?"
            " AND after_update = ?", (tool_id, user["id"], values["problem"], values["after_update"])).fetchone()
        if same is None:
            conn.execute("INSERT INTO upkeep_requests (tool_id, user_id, problem, after_update, status, created_at)"
                         " VALUES (?, ?, ?, ?, 'open', ?)",
                         (tool_id, user["id"], values["problem"], values["after_update"], db.now()))
        conn.execute("UPDATE tools SET needs_upkeep = 1 WHERE id = ?", (tool_id,))
    if same is None:
        set_done(request, "library", tool_id, f"Your problem report is saved: {tool['name']}")
        flash(request, "Members who volunteer for this tool can now read it under Open problems.", "success")
    else:
        set_done(request, "library", tool_id, f"You already reported this problem: {tool['name']}")
        flash(request, "It is listed once, so nothing changed.", "info")
    return redirect(f"/library/tool/{tool_id}")


@router.post("/tool/{tool_id}/problem/{problem_id}/resolve")
async def resolve_problem(request: Request, tool_id: int, problem_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    tool = tool_or_404(tool_id)
    with db.session() as conn:
        row = conn.execute("SELECT * FROM upkeep_requests WHERE id = ? AND tool_id = ?", (problem_id, tool_id)).fetchone()
        if row is None or (row["user_id"] != user["id"] and tool["added_by"] != user["id"]):
            raise HTTPException(status_code=404, detail="We could not find that problem report.")
        conn.execute("UPDATE upkeep_requests SET status = 'resolved' WHERE id = ?", (problem_id,))
        still_open = conn.execute("SELECT COUNT(*) AS n FROM upkeep_requests WHERE tool_id = ? AND status = 'open'",
                                  (tool_id,)).fetchone()["n"]
        if not still_open:
            conn.execute("UPDATE tools SET needs_upkeep = 0 WHERE id = ?", (tool_id,))
    set_done(request, "library", tool_id, f"Problem marked as fixed: {tool['name']}")
    if still_open:
        flash(request, f"{still_open} open {'problem is' if still_open == 1 else 'problems are'} left.", "info")
    else:
        flash(request, "No open problems are left for this tool.", "success")
    return redirect(f"/library/tool/{tool_id}")


@router.post("/tool/{tool_id}/volunteer")
async def volunteer(request: Request, tool_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool, other = _library_tool_or_redirect(tool_id)
    if other:
        raise HTTPException(status_code=404, detail="We could not find that tool in the Tool library.")
    values = {"skills": clean(form, "skills", KEEP)}
    errors: dict[str, str] = {}
    if not values["skills"]:
        errors["skills"] = "Say how you can help, for example: I test with JAWS on Windows 11, or I can write Python."
    elif too_long(values, "skills", SKILLS_MAX):
        errors["skills"] = long_message(SKILLS_MAX)
    elif looks_secret(values["skills"]):
        errors["skills"] = PUBLIC_SECRET_MESSAGE
    if errors:
        return render_library_tool(request, tool, volunteer_values=values, volunteer_errors=errors, status_code=400)
    with db.session() as conn:
        existed = conn.execute("SELECT id FROM upkeep_volunteers WHERE tool_id = ? AND user_id = ?",
                               (tool_id, user["id"])).fetchone()
        conn.execute("INSERT INTO upkeep_volunteers (tool_id, user_id, skills, created_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT (tool_id, user_id) DO UPDATE SET skills = excluded.skills",
                     (tool_id, user["id"], values["skills"], db.now()))
    if existed:
        set_done(request, "library", tool_id, f"How you can help is updated: {tool['name']}")
        flash(request, "Thank you. Other members now see what you wrote.", "success")
    else:
        set_done(request, "library", tool_id, f"You are on the helper list: {tool['name']}")
        flash(request, "Thank you for helping.", "success")
    return redirect(f"/library/tool/{tool_id}")


@router.post("/tool/{tool_id}/volunteer/remove")
async def stop_volunteering(request: Request, tool_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    tool = tool_or_404(tool_id)
    with db.session() as conn:
        cur = conn.execute("DELETE FROM upkeep_volunteers WHERE tool_id = ? AND user_id = ?", (tool_id, user["id"]))
    if cur.rowcount:
        set_done(request, "library", tool_id, f"You are off the helper list: {tool['name']}")
        flash(request, f"You are off the helper list for {tool['name']}. What you wrote about how you can help "
                       "is removed. You can add yourself again at any time.", "success")
    else:
        set_done(request, "library", tool_id, f"You were not on the helper list: {tool['name']}")
        flash(request, "Nothing changed.", "info")
    return redirect(f"/library/tool/{tool_id}")
