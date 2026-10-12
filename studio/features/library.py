"""Tool library: tools that blind and low-vision people made for each other, and keeping them working
when screen readers, operating systems and AI tools update. There are no staff: members report what
broke, volunteer to help, and mark problems fixed. The tables are defined in find.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..web import checked_form, current_user, flash, redirect, render, require_user
from .find import (CATEGORY_PAIRS, OUTCOMES, SECRET_MESSAGE, SIGHT_ONSETS, clean, find_by_url, insert_member_tool,
                   looks_secret, report_defaults, report_summaries, reports_for, tool_or_404, validate_tool)

router = APIRouter(prefix="/library")
NAV = {"label": "Tool library", "url": "/library",
       "description": "Tools that blind people made for each other, and help to keep them working when screen readers and apps update."}
SCHEMA = ""

FILTERS = [("all", "All"), ("upkeep", "Need help after an update")]


def _upkeep(tool_id: int) -> dict:
    with db.session() as conn:
        problems = [dict(r) for r in conn.execute(
            "SELECT p.*, u.pseudonym FROM upkeep_requests p JOIN users u ON u.id = p.user_id"
            " WHERE p.tool_id = ? ORDER BY p.id", (tool_id,)).fetchall()]
        volunteers = [dict(r) for r in conn.execute(
            "SELECT v.*, u.pseudonym FROM upkeep_volunteers v JOIN users u ON u.id = v.user_id"
            " WHERE v.tool_id = ? ORDER BY v.id", (tool_id,)).fetchall()]
    for p in problems:
        p["date"] = p["created_at"][:10]
    return {"open": [p for p in problems if p["status"] == "open"],
            "resolved": [p for p in problems if p["status"] == "resolved"],
            "volunteers": volunteers}


def _library_tool_or_redirect(tool_id: int):
    tool = tool_or_404(tool_id)
    if not tool["made_by_blind"]:
        return tool, redirect(f"/find/tool/{tool_id}")
    return tool, None


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
    title = tool["name"] + (": please fix the problems" if any_errors else "")
    return render(
        request, "library/tool.html", title=title, tool=tool, upkeep=upkeep, my_volunteer=my_volunteer,
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
    return render(request, "library/index.html", title=NAV["label"], tools=tools, show=show, filters=FILTERS,
                  counts=counts, summaries=report_summaries([t["id"] for t in tools]))


@router.get("/add")
def add_form(request: Request, user: dict = Depends(require_user)):
    return render(request, "library/add.html", title="Add a tool a blind person made",
                  values={"category": "Other"}, errors={}, category_pairs=CATEGORY_PAIRS)


@router.post("/add")
async def add_tool(request: Request, user: dict = Depends(require_user)):
    form = await checked_form(request)
    values, errors = validate_tool(form, require_blind=True)
    if "url" not in errors:
        existing = find_by_url(values["url"])
        if existing:
            if existing["made_by_blind"]:
                flash(request, f"{existing['name']} is already in the Tool library. Here is its entry.", "info")
                return redirect(f"/library/tool/{existing['id']}")
            flash(request, f"{existing['name']} is already in the directory with this address, but nobody has "
                           "recorded that a blind or low-vision person made it. Here is its entry.", "info")
            return redirect(f"/find/tool/{existing['id']}")
    if errors:
        return render(request, "library/add.html", title="Add a tool a blind person made: please fix the problems",
                      values=values, errors=errors, category_pairs=CATEGORY_PAIRS, status_code=400)
    tool_id = insert_member_tool(values, user["id"])
    flash(request, f"{values['name']} is now in the Tool library. Thank you.", "success")
    return redirect(f"/library/tool/{tool_id}")


@router.get("/tool/{tool_id}")
def tool_page(request: Request, tool_id: int):
    tool, other = _library_tool_or_redirect(tool_id)
    return other or render_library_tool(request, tool)


@router.post("/tool/{tool_id}/problem")
async def report_problem(request: Request, tool_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool, other = _library_tool_or_redirect(tool_id)
    if other:
        raise HTTPException(status_code=404, detail="We could not find that tool in the Tool library.")
    values = {"problem": clean(form, "problem", 3000), "after_update": clean(form, "after_update", 200)}
    errors: dict[str, str] = {}
    if not values["problem"]:
        errors["problem"] = "Say what stopped working."
    elif len(str(form.get("problem", ""))) > 3000:
        errors["problem"] = "Keep it under 3,000 characters."
    elif looks_secret(values["problem"]):
        errors["problem"] = SECRET_MESSAGE
    if not values["after_update"]:
        errors["after_update"] = "Say which update it broke after, for example NVDA 2026.1 or iOS 27. Write 'not sure' if you do not know."
    elif looks_secret(values["after_update"]):
        errors["after_update"] = SECRET_MESSAGE
    if errors:
        return render_library_tool(request, tool, problem_values=values, problem_errors=errors, status_code=400)
    with db.session() as conn:
        conn.execute("INSERT INTO upkeep_requests (tool_id, user_id, problem, after_update, status, created_at)"
                     " VALUES (?, ?, ?, ?, 'open', ?)",
                     (tool_id, user["id"], values["problem"], values["after_update"], db.now()))
        conn.execute("UPDATE tools SET needs_upkeep = 1 WHERE id = ?", (tool_id,))
    flash(request, "Your problem report is saved. Members who volunteer for this tool can now see it.", "success")
    return redirect(f"/library/tool/{tool_id}#problems-title")


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
    message = "The problem is marked as fixed."
    if not still_open:
        message += " No open problems are left for this tool."
    flash(request, message, "success")
    return redirect(f"/library/tool/{tool_id}#problems-title")


@router.post("/tool/{tool_id}/volunteer")
async def volunteer(request: Request, tool_id: int, user: dict = Depends(require_user)):
    form = await checked_form(request)
    tool, other = _library_tool_or_redirect(tool_id)
    if other:
        raise HTTPException(status_code=404, detail="We could not find that tool in the Tool library.")
    values = {"skills": clean(form, "skills", 1000)}
    errors: dict[str, str] = {}
    if not values["skills"]:
        errors["skills"] = "Say how you can help, for example: I test with JAWS on Windows 11, or I can write Python."
    elif looks_secret(values["skills"]):
        errors["skills"] = SECRET_MESSAGE
    if errors:
        return render_library_tool(request, tool, volunteer_values=values, volunteer_errors=errors, status_code=400)
    with db.session() as conn:
        existed = conn.execute("SELECT id FROM upkeep_volunteers WHERE tool_id = ? AND user_id = ?",
                               (tool_id, user["id"])).fetchone()
        conn.execute("INSERT INTO upkeep_volunteers (tool_id, user_id, skills, created_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT (tool_id, user_id) DO UPDATE SET skills = excluded.skills",
                     (tool_id, user["id"], values["skills"], db.now()))
    flash(request, "Your skills are updated." if existed else
          "Thank you. You are on the list of members helping keep this tool working.", "success")
    return redirect(f"/library/tool/{tool_id}#volunteers-title")


@router.post("/tool/{tool_id}/volunteer/remove")
async def stop_volunteering(request: Request, tool_id: int, user: dict = Depends(require_user)):
    await checked_form(request)
    tool_or_404(tool_id)
    with db.session() as conn:
        cur = conn.execute("DELETE FROM upkeep_volunteers WHERE tool_id = ? AND user_id = ?", (tool_id, user["id"]))
    if cur.rowcount:
        flash(request, "You are no longer on the list for this tool.", "success")
    return redirect(f"/library/tool/{tool_id}#volunteers-title")
