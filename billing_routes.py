"""Stripe billing routes: Checkout for Plus signup, Customer Portal for card/cancel,
and a webhook that flips the user's plan in response to subscription events.

Flow:
  1. User clicks Upgrade on /upgrade → POST /api/billing/checkout {plan: monthly|annual}
  2. We ensure a Stripe Customer exists (create if missing, link to user), then
     open a Checkout Session and redirect the browser to it.
  3. After payment Stripe fires `checkout.session.completed` to /api/billing/webhook;
     we set users.plan='plus' and persist subscription_id.
  4. Plus users can hit POST /api/billing/portal to open the hosted Customer
     Portal for card updates / cancellation. When a cancellation takes effect
     Stripe sends `customer.subscription.deleted` and we downgrade to free.
"""
import asyncio
import logging
import os

import stripe
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import auth
import database as db

logger = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_ANNUAL = os.getenv("STRIPE_PRICE_ANNUAL", "")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "http://127.0.0.1:8000/app?upgraded=1")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "http://127.0.0.1:8000/upgrade?status=canceled")
# Where the Stripe Customer Portal returns users to after they close the hosted
# session (update card, cancel, etc.). Defaults to the in-app home.
STRIPE_PORTAL_RETURN_URL = os.getenv("STRIPE_PORTAL_RETURN_URL", "http://127.0.0.1:8000/app")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter(prefix="/api/billing")


def _require_stripe_configured() -> None:
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Billing is not configured")


def _price_for(plan: str) -> str:
    if plan == "annual":
        if not STRIPE_PRICE_ANNUAL:
            raise HTTPException(503, "Annual plan is not configured")
        return STRIPE_PRICE_ANNUAL
    if plan == "monthly":
        if not STRIPE_PRICE_MONTHLY:
            raise HTTPException(503, "Monthly plan is not configured")
        return STRIPE_PRICE_MONTHLY
    raise HTTPException(400, "plan must be 'monthly' or 'annual'")


async def _ensure_stripe_customer(user: dict) -> str:
    """Return the user's Stripe Customer ID, creating one if they don't have it yet."""
    existing = user.get("stripe_customer_id")
    if existing:
        return existing
    customer = await asyncio.to_thread(
        stripe.Customer.create,
        email=user.get("email"),
        name=user.get("full_name"),
        metadata={"user_id": user["user_id"]},
    )
    await db.set_user_stripe_customer(user["user_id"], customer.id)
    return customer.id


@router.post("/checkout")
async def create_checkout(request: Request):
    """Start a Stripe Checkout Session for a monthly or annual Plus subscription.
    Returns {url} — the caller redirects the browser there. We also accept
    standard form posts for a no-JS fallback (returns a 303 redirect)."""
    _require_stripe_configured()
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    body = {}
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        form = await request.form()
        body = dict(form)
    plan = (body.get("plan") or "monthly").lower()
    price_id = _price_for(plan)

    customer_id = await _ensure_stripe_customer(user)
    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=STRIPE_SUCCESS_URL,
            cancel_url=STRIPE_CANCEL_URL,
            allow_promotion_codes=True,
            client_reference_id=user["user_id"],
            metadata={"user_id": user["user_id"], "plan_choice": plan},
        )
    except stripe.StripeError as e:
        logger.warning("Stripe checkout creation failed: %s", e)
        raise HTTPException(502, "Could not start checkout. Please try again.")
    return JSONResponse({"url": session.url})


@router.post("/portal")
async def open_portal(request: Request):
    """Create a Stripe Customer Portal session so the user can update card,
    view invoices, or cancel. Returns {url}."""
    _require_stripe_configured()
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No Stripe customer on file — upgrade first.")
    try:
        portal = await asyncio.to_thread(
            stripe.billing_portal.Session.create,
            customer=customer_id,
            return_url=STRIPE_PORTAL_RETURN_URL,
        )
    except stripe.StripeError as e:
        logger.warning("Stripe portal creation failed: %s", e)
        raise HTTPException(502, "Could not open billing portal.")
    return JSONResponse({"url": portal.url})


# ----- Webhook --------------------------------------------------------------


def _obj_get(obj, key, default=None):
    """StripeObject (from construct_event) doesn't expose dict.get — use
    attribute access with a fallback. Works for both SDK objects and plain dicts."""
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default


async def _handle_checkout_completed(obj) -> None:
    """Plan flip on successful first payment."""
    customer_id = _obj_get(obj, "customer")
    subscription_id = _obj_get(obj, "subscription")
    metadata = _obj_get(obj, "metadata") or {}
    user_id = _obj_get(metadata, "user_id") or _obj_get(obj, "client_reference_id")
    if not user_id:
        logger.warning("checkout.session.completed without user_id metadata: %s", _obj_get(obj, "id"))
        return
    await db.set_user_plan(
        user_id, "plus",
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
    )
    logger.info("User %s upgraded to plus (sub=%s)", user_id, subscription_id)


async def _handle_subscription_deleted(obj) -> None:
    """Sub fully ended (past-due dunning exhausted, or scheduled cancel hit the
    period end). Downgrade the user to free."""
    customer_id = _obj_get(obj, "customer")
    if not customer_id:
        return
    user = await db.get_user_by_stripe_customer(customer_id)
    if not user:
        logger.warning("subscription.deleted for unknown customer %s", customer_id)
        return
    await db.set_user_plan(user["id"], "free")
    logger.info("User %s downgraded to free (sub=%s)", user["id"], _obj_get(obj, "id"))


_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_completed,
    "customer.subscription.deleted": _handle_subscription_deleted,
}


@router.post("/webhook")
async def webhook(request: Request):
    """Stripe → us. Verify signature, dispatch to a handler. Must return 2xx
    quickly or Stripe retries."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Webhook secret not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Invalid signature")

    handler = _EVENT_HANDLERS.get(event["type"])
    if handler:
        try:
            await handler(event["data"]["object"])
        except Exception as e:
            logger.exception("Webhook handler for %s failed: %s", event["type"], e)
            raise HTTPException(500, "Handler error")
    return JSONResponse({"received": True})
