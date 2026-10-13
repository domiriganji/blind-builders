"""The site owner's AI settings page (/account/admin/ai): the site's own Claude API key, kept safe, the model
members use with it, the main AI (Claude, or another provider with its own key, model and prices), who looks at
pictures of built pages, who pays, and monthly spending limits. The rules live in studio/site_ai.py; the
providers in studio/providers.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from .. import credits, limits, providers, site_ai
from ..auth import clean_text
from ..security import verify_password
from ..web import checked_form, date_words, error_title, flash, form_text, redirect, render, require_user
from . import account

router = APIRouter()
NAV = None

PAGE = "/account/admin/ai"
TITLE = "AI settings for your key"
PASSWORD_TRIES_PER_HOUR = 5
HOUR = 60 * 60
MAX_LIMIT_CENTS = 1_000_000  # $10,000
# A longer list of models is a select box instead of radio buttons, so it is quick to move past.
MAX_MODEL_RADIOS = 20
PRICE_WORDS = (("other_price_in", "in (what the AI reads)"), ("other_price_out", "out (what it writes)"))
PRICE_NAMES = tuple(name for name, _ in PRICE_WORDS)
VISION_OPTIONS = [
    (site_ai.VISION_CLAUDE, "Claude (recommended). It uses your Claude key, so it needs one."),
    (site_ai.VISION_MAIN, "The main model. Choose this only if you checked that the model can look at pictures."),
]


def _values() -> dict:
    return {
        "model": site_ai.get("model") or "",
        "pay_mode": site_ai.pay_mode(),
        "site_month": _dollars(site_ai.site_month_limit()),
        "member_month": _dollars(site_ai.member_month_limit()),
    }


def _dollars(cents: int) -> str:
    return "0" if not cents else credits.format_cents(cents).lstrip("$")


def provider_options(with_custom: bool) -> list[tuple[str, str]]:
    options = [(p.id, f"{p.name}: {p.about}") for p in providers.PRESETS]
    if with_custom:
        options.append((providers.CUSTOM, f"{providers.CUSTOM_PROVIDER.name}: {providers.CUSTOM_PROVIDER.about}"))
    return options


def _model_choices(models: list[dict]) -> list[tuple[str, str]]:
    """Each model in a provider's list as (id, label), with its prices when the list gives them."""
    options = []
    for m in models:
        label = m["id"] + (f" ({m['name']})" if m.get("name") else "")
        if m.get("prices"):
            label += (f": ${providers.price_text(m['prices'][0])} per million tokens in, "
                      f"${providers.price_text(m['prices'][1])} out")
        options.append((m["id"], label))
    return options


def list_models(provider, key: str | None) -> tuple[list[dict], str]:
    """The provider's live list of models for a saved key, or ([], a plain reason it could not be read).

    It can ask the provider over the network, so async handlers call it (and _render) with run_in_threadpool."""
    if provider is None or not key:
        return [], ""
    try:
        return providers.cached_models(provider, key), ""
    except providers.ProviderError as exc:
        return [], exc.user_message


def _main_values(models: list[dict]) -> dict:
    model = site_ai.other_model()
    price_in, price_out = site_ai.get("other_price_in"), site_ai.get("other_price_out")
    listed = next((m for m in models if m["id"] == model), None)
    if listed and listed.get("prices") and not (price_in or price_out):
        price_in, price_out = (providers.price_text(v) for v in listed["prices"])  # pre-filled from the list
    in_list = any(m["id"] == model for m in models)
    # prices_for: the model the prices in the form belong to, sent back with the form, so a changed model never
    # keeps another model's prices without the owner confirming them (see save_main).
    return {"main_ai": site_ai.main_choice(), "other_model": model if in_list else "",
            "other_model_typed": "" if in_list else model, "other_price_in": price_in, "other_price_out": price_out,
            "prices_for": model, "vision": site_ai.vision_choice()}


def _render(request: Request, user: dict, *, values: dict | None = None, errors: dict | None = None,
            key_errors: dict | None = None, other_errors: dict | None = None, main_errors: dict | None = None,
            other_values: dict | None = None, main_values: dict | None = None, status_code: int = 200):
    settings = request.app.state.settings
    errors, key_errors = errors or {}, key_errors or {}
    other_errors, main_errors = other_errors or {}, main_errors or {}
    title = account._done_title(request, TITLE)
    if errors or key_errors or other_errors or main_errors:
        title = error_title(TITLE, {**errors, **key_errors, **other_errors, **main_errors})
    other = site_ai.other_provider()
    other_key = site_ai.other_key(settings) if other else None
    models, models_problem = list_models(other, other_key)
    if "other_model" in main_errors and not models:
        # With no list there is no list field: the message goes on the box to type in, so its link leads there.
        main_errors = {("other_model_typed" if k == "other_model" else k): v for k, v in main_errors.items()}
    main_values = main_values or _main_values(models)
    main_options = [(site_ai.MAIN_CLAUDE, f"Claude (the default), with the model {site_ai.model_name(site_ai.site_model(settings))}, "
                                         "chosen under Model, payment and limits")]
    if other is not None:
        main_options.append((site_ai.MAIN_OTHER, f"{other.name}, with the model and prices below"))
    other_values = other_values or {"other_provider": other.id if other else "",
                                    "other_name": site_ai.get("other_name"), "other_address": site_ai.get("other_base")}
    values = values or _values()
    if values.get("model") not in site_ai.MODEL_NAMES:
        values["model"] = site_ai.site_model(settings)
    model_options = [(model_id, f"{name}{', recommended' if model_id == 'claude-opus-5-5' else ''}: {about}")
                     for model_id, name, about in site_ai.MODELS]
    return render(
        request, "owner/ai.html", title=title, values=values, errors=errors, key_errors=key_errors,
        key_source=site_ai.key_source(settings), key_last4=site_ai.key_last4(settings),
        key_unreadable=site_ai.key_unreadable(settings),
        key_saved_at=date_words(site_ai.get("site_key_saved_at")),
        model_options=model_options, pay_modes=site_ai.PAY_MODES,
        spent=credits.format_cents(site_ai.spent_this_month()),
        renews=site_ai.renews_on(),
        other=other, other_last4=site_ai.get("other_key_last4") if other else "",
        other_saved_at=date_words(site_ai.get("other_saved_at")),
        other_unreadable=site_ai.other_key_unreadable(settings), other_errors=other_errors,
        other_values=other_values, provider_options=provider_options(with_custom=True),
        main_errors=main_errors, main_values=main_values, main_options=main_options,
        other_model_options=_model_choices(models), model_radios=len(models) <= MAX_MODEL_RADIOS,
        models_problem=models_problem, vision_options=VISION_OPTIONS,
        main_label=site_ai.main_label(settings), main_ready=site_ai.site_ai_ready(settings),
        main_is_claude=site_ai.main_is_claude(), vision_main=site_ai.vision_choice() == site_ai.VISION_MAIN,
        status_code=status_code)


def _password_problem(request: Request, user: dict, password: str) -> str | None:
    """A plain message if the owner's password is missing or not right. Wrong tries are limited."""
    key = limits.key("owner-password", user["id"])
    if limits.count("owner_password", key, HOUR) >= PASSWORD_TRIES_PER_HOUR:
        return ("Several password tries did not work, so changing the site's key is paused for an hour to protect "
                "the site. Nothing was changed. Please try again after that.")
    if not password:
        return "Enter the password you sign in with. Changing the site's key needs it."
    if not verify_password(password[:1000], user["password_hash"]):
        limits.record("owner_password", key, HOUR)
        return "That is not the password you sign in with, so nothing was changed. Check it and try again."
    return None


@router.get(PAGE)
def page(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    return _render(request, user)


@router.post(PAGE)
async def save_settings(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    values = {
        "model": form_text(form, "model", 40),
        "pay_mode": form_text(form, "pay_mode", 20),
        "site_month": form_text(form, "site_month", 20),
        "member_month": form_text(form, "member_month", 20),
    }
    errors: dict[str, str] = {}
    if values["model"] not in site_ai.MODEL_NAMES:
        errors["model"] = "Choose one of the models in the list."
    if values["pay_mode"] not in dict(site_ai.PAY_MODES):
        errors["pay_mode"] = "Choose who pays for AI on your key."
    amounts = {}
    for name, label in (("site_month", "for the whole site"), ("member_month", "for each member")):
        cents = account._parse_dollars(values[name] or "0")
        if cents is None:
            errors[name] = f"Enter the monthly limit {label} in US dollars, like 50 or 2.50, or 0 for no limit."
        elif cents > MAX_LIMIT_CENTS:
            errors[name] = f"Enter at most {credits.format_cents(MAX_LIMIT_CENTS)}."
        else:
            amounts[name] = cents
    if (values["pay_mode"] == "free" and "site_month" in amounts and "member_month" in amounts
            and not amounts["site_month"] and not amounts["member_month"]):
        errors["site_month"] = ("When the AI is free for members, set at least one limit, so your bill "
                                "cannot grow without end.")
    if errors:
        return await run_in_threadpool(_render, request, user, values=values, errors=errors, status_code=400)
    site_ai.save({"model": values["model"], "pay_mode": values["pay_mode"],
                  "site_month_cents": amounts["site_month"], "member_month_cents": amounts["member_month"]},
                 user["id"])
    account._announce(request, "Your AI settings are saved")
    return redirect(PAGE)


@router.post(PAGE + "/key")
async def save_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    key = str(form.get("api_key", "")).strip()
    key_errors: dict[str, str] = {}
    problem = account.check_key_format(key)
    if problem:
        key_errors["api_key"] = problem
    password_problem = _password_problem(request, user, str(form.get("password", "")))
    if password_problem:
        key_errors["password"] = password_problem
    if not key_errors:
        result = await run_in_threadpool(account.verify_api_key, key)
        if result is not True:
            key_errors["api_key"] = str(result) if result else "Anthropic did not accept this key, so it was not saved."
    if key_errors:
        return await run_in_threadpool(_render, request, user, key_errors=key_errors, status_code=400)
    site_ai.save_key(request.app.state.settings, key, user["id"])
    account._announce(request, "The site's Claude API key is saved")
    settings = request.app.state.settings
    uses = ("Members' AI work on the site's key uses it from now on."
            if site_ai.main_is_claude() or not site_ai.other_ready(settings)
            else "Web search steps on your keys use it from now on.")
    flash(request, f"The key ending in {key[-4:]} is saved, encrypted. {uses}", "success")
    return redirect(PAGE)


@router.post(PAGE + "/key/remove")
async def remove_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    problem = _password_problem(request, user, str(form.get("remove_password", "")))
    if problem:
        return await run_in_threadpool(_render, request, user, key_errors={"remove_password": problem}, status_code=400)
    site_ai.remove_key()
    account._announce(request, "The site's Claude API key is removed")
    flash(request, "The key is removed from this site. To stop it working everywhere, delete it in the Anthropic "
                   "Console too.", "success")
    return redirect(PAGE)


# ----- another provider: its key, then the main AI and who looks at pictures ---------------------------------

@router.post(PAGE + "/other-key")
async def save_other_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    provider_id = form_text(form, "other_provider", 40)
    values = {"other_provider": provider_id, "other_name": clean_text(form_text(form, "other_name", 200), 60),
              "other_address": form_text(form, "other_address", 400).strip()}
    key = form_text(form, "other_api_key", 1000).strip()
    errors: dict[str, str] = {}
    provider = None
    if provider_id not in providers.PRESET_IDS and provider_id != providers.CUSTOM:
        errors["other_provider"] = "Choose the provider this key is from."
    elif provider_id == providers.CUSTOM:
        if not values["other_name"]:
            errors["other_name"] = "Type the provider's name, as members will read it, for example Example AI."
        problem = await run_in_threadpool(providers.address_problem, values["other_address"])
        if problem:
            errors["other_address"] = problem
        else:
            provider = providers.resolved(providers.CUSTOM, base_url=providers.normalize_address(values["other_address"]),
                                          name=values["other_name"])
    else:
        provider = providers.get(provider_id)
    problem = providers.key_problem(key, provider.name if provider else "the provider")
    if problem:
        errors["other_api_key"] = problem
    password_problem = _password_problem(request, user, str(form.get("other_password", "")))
    if password_problem:
        errors["other_password"] = password_problem
    if not errors and provider is not None:
        try:
            await run_in_threadpool(providers.fetch_models, provider, key)
        except providers.ProviderError as exc:
            errors["other_api_key"] = exc.user_message
    if errors or provider is None:
        return await run_in_threadpool(_render, request, user, other_errors=errors, other_values=values, status_code=400)
    main_reset = site_ai.save_other_key(request.app.state.settings, provider, key, user["id"])
    providers.forget_cache()
    account._announce(request, f"The key for {provider.name} is saved")
    flash(request, f"The key ending in {key[-4:]} is saved, encrypted. Next, under Main AI for members on your key, "
                   f"choose {provider.name} and its model, and set its prices."
                   + (" Until then, members' everyday AI work on your keys uses Claude." if main_reset else ""),
          "success")
    return redirect(PAGE)


@router.post(PAGE + "/other-key/remove")
async def remove_other_key(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    problem = _password_problem(request, user, str(form.get("other_remove_password", "")))
    if problem:
        return await run_in_threadpool(_render, request, user, other_errors={"other_remove_password": problem}, status_code=400)
    other = site_ai.other_provider()
    name = other.name if other else "the other provider"
    site_ai.remove_other_key()
    providers.forget_cache()
    account._announce(request, f"The key for {name} is removed")
    flash(request, f"Members' AI work on your keys uses Claude again. To stop the key working everywhere, delete it on "
                   f"{name}'s website too.", "success")
    return redirect(PAGE)


@router.post(PAGE + "/main")
async def save_main(request: Request, user: dict = Depends(require_user)):
    account._require_admin(user)
    form = await checked_form(request)
    settings = request.app.state.settings
    values = {"main_ai": form_text(form, "main_ai", 20), "other_model": form_text(form, "other_model", 200).strip(),
              "other_model_typed": form_text(form, "other_model_typed", 200).strip(),
              "other_price_in": form_text(form, "other_price_in", 20).strip(),
              "other_price_out": form_text(form, "other_price_out", 20).strip(),
              "prices_for": form_text(form, "prices_for", 200).strip(),
              "vision": form_text(form, "vision", 20)}
    errors: dict[str, str] = {}
    other = site_ai.other_provider()
    if values["main_ai"] not in (site_ai.MAIN_CLAUDE, site_ai.MAIN_OTHER):
        errors["main_ai"] = "Choose the main AI for members on your key."
    elif values["main_ai"] == site_ai.MAIN_OTHER and (other is None or not site_ai.other_key(settings)):
        errors["main_ai"] = ("Save a key for another provider first, under Another provider's key, then choose it "
                             "here.")
    if values["vision"] not in (site_ai.VISION_CLAUDE, site_ai.VISION_MAIN):
        errors["vision"] = "Choose who looks at pictures of built pages."
    to_save: dict[str, str] = {}
    note = ""
    wants_other = values["main_ai"] == site_ai.MAIN_OTHER
    if other is not None:
        model = values["other_model_typed"] or values["other_model"]
        if model or wants_other:
            problem = providers.model_problem(model)
            if problem:
                errors["other_model_typed" if values["other_model_typed"] else "other_model"] = problem
            else:
                to_save["other_model"] = model
        prices: dict[str, float | None] = {}
        for name, word in PRICE_WORDS:
            prices[name] = providers.parse_price(values[name]) if values[name] else None
            if values[name] and prices[name] is None:
                errors[name] = f"Enter the price per million tokens {word} in US dollars, like 2.50 or 0.15."
            elif prices[name] is not None and prices[name] <= 0:
                prices[name] = None
                errors[name] = (f"Enter a price above 0 for tokens {word}, in US dollars, from {other.name}'s price "
                                "page. With a price of 0, your limits and members' credits could not count this AI's "
                                "cost.")
        # Prices the form showed for another model, sent back unchanged with a new model.
        stored = (site_ai.get("other_price_in"), site_ai.get("other_price_out"))
        prices_for = values["prices_for"] or site_ai.other_model()
        carried = bool(model and prices_for and model != prices_for and any(stored) and all(
            prices[n] is not None and providers.price_text(prices[n]) == old for n, old in zip(PRICE_NAMES, stored)))
        empty = not values["other_price_in"] and not values["other_price_out"]
        if "other_model" in to_save and (empty or carried):
            models, _ = await run_in_threadpool(list_models, other, site_ai.other_key(settings))
            listed = next((m for m in models if m["id"] == model and m.get("prices")), None)
            if listed:
                prices = {"other_price_in": listed["prices"][0], "other_price_out": listed["prices"][1]}
                values.update({k: providers.price_text(v) for k, v in prices.items()})
                note = (f" The prices are from {other.name}'s list: ${values['other_price_in']} per million tokens in "
                        f"and ${values['other_price_out']} out. Check them on {other.name}'s price page.")
                if carried:
                    note = f" The prices you had were for {prices_for}, so they were changed.{note}"
            elif carried:
                values["prices_for"] = model  # saving again with these prices confirms them
                errors["other_price_in"] = (f"These prices were set for the model {prices_for}. Check {other.name}'s "
                                            f"prices for {model} and enter them, then save again. If they are the "
                                            "same, save again as they are.")
        for name, word in PRICE_WORDS:
            if prices[name] is not None:
                to_save[name] = providers.price_text(prices[name])
            elif wants_other and name not in errors:
                errors[name] = (f"Enter the price per million tokens {word} in US dollars, from {other.name}'s price "
                                "page. The site needs it to keep within your limits and to charge credits.")
    if errors:
        return await run_in_threadpool(_render, request, user, main_errors=errors, main_values=values, status_code=400)
    site_ai.save({"main_ai": values["main_ai"], "vision": values["vision"], **to_save}, user["id"])
    account._announce(request, "The main AI is saved")
    if wants_other:
        flash(request, f"Members' everyday AI work on your keys now uses {other.name}, model {to_save['other_model']}. "
                       f"Web search steps still use Claude.{note}", "success")
    else:
        flash(request, "Members' AI work on your keys uses Claude." + (note if note else ""), "success")
    return redirect(PAGE)
