"""
Smart Shop Manager - ONLINE (multi-shop, subscription) edition
Flask + SQLAlchemy. Works with SQLite (testing) or PostgreSQL (production).
Run locally:  python app.py          ->  http://localhost:5000
"""
import os
import io
import re
import csv
import json
import uuid
import hmac
import hashlib
import secrets
import threading
import datetime as dt
from functools import wraps

import requests
from flask import (Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort,
                   send_from_directory, Response, g)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

try:
    from pywebpush import webpush, WebPushException
    HAS_PUSH = True
except Exception:
    HAS_PUSH = False

# --------------------------------------------------------------------------
# Configuration (all from environment variables - see .env.example)
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
for _d in (DATA_DIR, UPLOAD_DIR, TMP_DIR):
    os.makedirs(_d, exist_ok=True)

APP_NAME = os.environ.get("APP_NAME", "Smart Shop Manager")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
TZ_OFFSET = float(os.environ.get("TZ_OFFSET_HOURS", "0"))          # Ghana = 0
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")
VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_EMAIL = os.environ.get("VAPID_EMAIL", "mailto:admin@example.com")
CRON_KEY = os.environ.get("CRON_KEY", "")

db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(DATA_DIR, "smartshop.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app = Flask(__name__, static_folder=None, template_folder=BASE_DIR)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me-" + APP_NAME,
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https"),
    REMEMBER_COOKIE_SECURE=BASE_URL.startswith("https"),
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=14),
)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."

# --------------------------------------------------------------------------
# Permissions (owner ticks these for each staff member)
# --------------------------------------------------------------------------
PERMS = [
    ("sell", "Sell at the counter (POS)"),
    ("discount", "Give discounts"),
    ("credit_sale", "Sell on credit"),
    ("view_stock", "See the stock list"),
    ("see_cost", "See cost prices"),
    ("manage_items", "Add and edit items"),
    ("delete_items", "Delete items"),
    ("restock", "Restock items"),
    ("debts", "Use the Debt Book"),
    ("view_sales", "See sales list and reports"),
    ("view_profit", "See profit"),
    ("cancel_sales", "Cancel sales"),
    ("close_day", "Close the day"),
    ("import_data", "Import from Excel"),
    ("alerts", "Get phone notifications"),
]
PERM_KEYS = [p for p, _ in PERMS]
PRESETS = {
    "Cashier": ["sell", "view_stock", "debts", "close_day"],
    "Senior cashier": ["sell", "discount", "credit_sale", "view_stock", "debts", "close_day", "view_sales"],
    "Store keeper": ["view_stock", "see_cost", "manage_items", "restock"],
    "Manager": [p for p in PERM_KEYS if p not in ("delete_items",)],
}
DEFAULT_NOTIFY = {"each_sale": False, "big_sale": 500, "low_stock": True, "cancel": True,
                  "staff_login": True, "daily": True}
ALL_PAYMENTS = ["Cash", "MoMo", "Card", "Bank", "Credit"]


def now():
    return dt.datetime.utcnow() + dt.timedelta(hours=TZ_OFFSET)


def today():
    return now().date()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    price = db.Column(db.Float, default=0)
    days = db.Column(db.Integer, default=30)
    max_users = db.Column(db.Integer, default=0)     # 0 = unlimited
    max_items = db.Column(db.Integer, default=0)
    features = db.Column(db.Text, default="")
    active = db.Column(db.Boolean, default=True)
    sort = db.Column(db.Integer, default=0)


class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(60), default="")
    email = db.Column(db.String(160), default="")
    address = db.Column(db.String(200), default="")
    logo = db.Column(db.String(200), default="")
    currency = db.Column(db.String(10), default="GH\u20b5")
    receipt_footer = db.Column(db.String(200), default="Thank you for shopping with us!")
    paper = db.Column(db.String(10), default="58mm")
    low_stock = db.Column(db.Integer, default=5)
    expiry_days = db.Column(db.Integer, default=30)
    accent = db.Column(db.String(10), default="#24326e")
    payments = db.Column(db.String(120), default="Cash,MoMo,Credit")
    notify = db.Column(db.Text, default=json.dumps(DEFAULT_NOTIFY))
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"))
    status = db.Column(db.String(20), default="trial")   # trial / active / suspended
    sub_ends = db.Column(db.DateTime)
    created = db.Column(db.DateTime, default=now)
    plan = db.relationship("Plan")

    @property
    def notify_cfg(self):
        try:
            return {**DEFAULT_NOTIFY, **json.loads(self.notify or "{}")}
        except ValueError:
            return dict(DEFAULT_NOTIFY)

    @property
    def payment_list(self):
        return [p for p in (self.payments or "Cash").split(",") if p]

    @property
    def is_live(self):
        return self.status != "suspended" and self.sub_ends is not None and self.sub_ends >= now()

    @property
    def days_left(self):
        if not self.sub_ends:
            return 0
        return max((self.sub_ends.date() - today()).days, 0)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey("shop.id"), index=True)
    name = db.Column(db.String(120), default="")
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(160), default="")
    phone = db.Column(db.String(40), default="")
    password_hash = db.Column(db.String(300), nullable=False)
    role = db.Column(db.String(20), default="staff")       # owner / staff / superadmin
    title = db.Column(db.String(40), default="Cashier")
    perms = db.Column(db.Text, default="[]")
    active = db.Column(db.Boolean, default=True)
    last_seen = db.Column(db.DateTime)
    created = db.Column(db.DateTime, default=now)
    shop = db.relationship("Shop")

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def perm_list(self):
        try:
            return json.loads(self.perms or "[]")
        except ValueError:
            return []

    def can(self, p):
        if self.role == "owner":
            return True
        if self.role == "superadmin":
            return False
        return p in self.perm_list

    @property
    def is_owner(self):
        return self.role == "owner"

    @property
    def online(self):
        return self.last_seen is not None and (now() - self.last_seen).total_seconds() < 300


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), default="Others")
    qty = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0)
    price = db.Column(db.Float, default=0)
    barcode = db.Column(db.String(80), default="")
    supplier = db.Column(db.String(120), default="")
    expiry = db.Column(db.Date)
    image = db.Column(db.String(200), default="")
    low_limit = db.Column(db.Integer)
    created = db.Column(db.DateTime, default=now)
    updated = db.Column(db.DateTime, default=now)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True, nullable=False)
    number = db.Column(db.Integer, default=0)
    ts = db.Column(db.DateTime, default=now, index=True)
    user_id = db.Column(db.Integer)
    user_name = db.Column(db.String(120), default="")
    subtotal = db.Column(db.Float, default=0)
    discount = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    cost_total = db.Column(db.Float, default=0)
    payment = db.Column(db.String(20), default="Cash")
    tendered = db.Column(db.Float, default=0)
    change_given = db.Column(db.Float, default=0)
    deposit = db.Column(db.Float, default=0)
    owed = db.Column(db.Float, default=0)
    customer = db.Column(db.String(120), default="")
    phone = db.Column(db.String(40), default="")
    momo_ref = db.Column(db.String(80), default="")
    source = db.Column(db.String(20), default="")        # '' / offline / imported
    client_uid = db.Column(db.String(64), unique=True)
    voided = db.Column(db.Boolean, default=False)
    void_by = db.Column(db.String(120), default="")
    void_ts = db.Column(db.DateTime)
    items = db.relationship("SaleItem", backref="sale", cascade="all, delete-orphan")


class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sale.id"), index=True)
    item_id = db.Column(db.Integer)
    name = db.Column(db.String(160))
    qty = db.Column(db.Integer)
    price = db.Column(db.Float)
    cost = db.Column(db.Float)


class Restock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    ts = db.Column(db.DateTime, default=now)
    user_name = db.Column(db.String(120))
    item_id = db.Column(db.Integer)
    name = db.Column(db.String(160))
    qty_added = db.Column(db.Integer)
    old_qty = db.Column(db.Integer)
    new_qty = db.Column(db.Integer)
    old_cost = db.Column(db.Float)
    new_cost = db.Column(db.Float)
    old_price = db.Column(db.Float)
    new_price = db.Column(db.Float)
    supplier = db.Column(db.String(120), default="")
    note = db.Column(db.String(200), default="")


class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(40), default="")
    balance = db.Column(db.Float, default=0)
    created = db.Column(db.DateTime, default=now)


class DebtTx(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    customer_id = db.Column(db.Integer, index=True)
    ts = db.Column(db.DateTime, default=now)
    user_name = db.Column(db.String(120))
    kind = db.Column(db.String(20))          # credit / payment / manual / cancelled
    amount = db.Column(db.Float)
    method = db.Column(db.String(20), default="")
    sale_id = db.Column(db.Integer)
    note = db.Column(db.String(200), default="")


class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    ts = db.Column(db.DateTime, default=now, index=True)
    user_name = db.Column(db.String(120))
    action = db.Column(db.String(60))
    detail = db.Column(db.String(300), default="")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    ts = db.Column(db.DateTime, default=now, index=True)
    title = db.Column(db.String(160))
    body = db.Column(db.String(400), default="")
    kind = db.Column(db.String(20), default="info")
    url = db.Column(db.String(200), default="")
    read = db.Column(db.Boolean, default=False)


class PushSub(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, index=True)
    shop_id = db.Column(db.Integer, index=True)
    endpoint = db.Column(db.String(600), unique=True)
    data = db.Column(db.Text)
    created = db.Column(db.DateTime, default=now)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, index=True)
    plan_id = db.Column(db.Integer)
    amount = db.Column(db.Float)
    reference = db.Column(db.String(100), unique=True)
    status = db.Column(db.String(20), default="pending")
    provider = db.Column(db.String(20), default="paystack")
    note = db.Column(db.String(200), default="")
    ts = db.Column(db.DateTime, default=now)


class PlatformSetting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, default="")


PLATFORM_DEFAULTS = {
    "trial_days": "14",
    "support_phone": "",
    "support_whatsapp": "",
    "manual_payment_info": "Pay by MoMo to 0XX XXX XXXX (Name: Your Business). "
                           "Use your shop name as reference, then WhatsApp us the receipt.",
}


def pset(key):
    r = db.session.get(PlatformSetting, key)
    return r.value if r else PLATFORM_DEFAULTS.get(key, "")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
@login_manager.user_loader
def load_user(uid):
    u = db.session.get(User, int(uid))
    return u if u and u.active else None


def sid():
    return current_user.shop_id


def shop_q(model):
    return model.query.filter_by(shop_id=current_user.shop_id)


def get_or_404(model, oid):
    obj = db.session.get(model, oid)
    if not obj or getattr(obj, "shop_id", None) != current_user.shop_id:
        abort(404)
    return obj


def to_float(v):
    try:
        s = str(v).replace(",", "").replace("GH\u20b5", "").replace("GHS", "").strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def to_int(v):
    f = to_float(v)
    if f is None or f != int(f):
        return None
    return int(f)


def parse_date(s):
    s = (s or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def text_match(q, *fields):
    q = (q or "").strip().lower()
    if not q:
        return True
    hay = " ".join(str(f or "") for f in fields).lower()
    if q in hay:
        return True
    toks = [t.rstrip("s") for t in q.split() if t.strip()]
    return bool(toks) and all(t in hay for t in toks)


def limit_of(item, shop=None):
    shop = shop or current_user.shop
    return item.low_limit if item.low_limit is not None else shop.low_stock


def is_low(item, shop=None):
    return item.qty <= limit_of(item, shop)


def expiry_status(item, shop=None):
    if not item.expiry:
        return None, None
    shop = shop or current_user.shop
    days = (item.expiry - today()).days
    if days < 0:
        return "expired", days
    if days <= shop.expiry_days:
        return "soon", days
    return None, days


def log(action, detail=""):
    if current_user.is_authenticated and current_user.shop_id:
        db.session.add(Activity(shop_id=current_user.shop_id, user_name=current_user.name or current_user.username,
                                action=action, detail=detail[:300]))


def period_range(kind, f=None, t=None):
    d = today()
    if kind == "yesterday":
        s = d - dt.timedelta(days=1)
        e = d
    elif kind == "week":
        s = d - dt.timedelta(days=d.weekday())
        e = s + dt.timedelta(days=7)
    elif kind == "month":
        s = d.replace(day=1)
        e = (s + dt.timedelta(days=32)).replace(day=1)
    elif kind == "lastmonth":
        e = d.replace(day=1)
        s = (e - dt.timedelta(days=1)).replace(day=1)
    elif kind == "all":
        s, e = dt.date(2000, 1, 1), dt.date(2999, 1, 1)
    elif kind == "custom" and parse_date(f) and parse_date(t):
        s, e = sorted([parse_date(f), parse_date(t)])
        e = e + dt.timedelta(days=1)
    else:
        s, e = d, d + dt.timedelta(days=1)
    return dt.datetime.combine(s, dt.time()), dt.datetime.combine(e, dt.time())


def sales_summary(shop_id, s, e):
    r = db.session.query(func.count(Sale.id), func.coalesce(func.sum(Sale.total), 0),
                         func.coalesce(func.sum(Sale.cost_total), 0), func.coalesce(func.sum(Sale.discount), 0),
                         func.coalesce(func.sum(Sale.owed), 0)).filter(
        Sale.shop_id == shop_id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).one()
    return dict(n=r[0], total=float(r[1]), cost=float(r[2]), profit=float(r[1]) - float(r[2]),
                disc=float(r[3]), owed=float(r[4]))


# ---------- CSRF
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


CSRF_EXEMPT = {"paystack_webhook", "cron_daily", "static", "service_worker"}


@app.before_request
def before():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not tok or not hmac.compare_digest(tok, session.get("_csrf", "")):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Session expired. Refresh the page."), 400
            flash("Your session expired. Please try again.", "error")
            return redirect(request.referrer or url_for("index"))
    if current_user.is_authenticated:
        if not current_user.last_seen or (now() - current_user.last_seen).total_seconds() > 60:
            current_user.last_seen = now()
            db.session.commit()
        shop = current_user.shop
        g.shop = shop
        # subscription lock: data can be viewed, but no changes when expired
        allowed = {"logout", "billing", "billing_pay", "billing_callback", "push_subscribe", "notif_read",
                   "login", "static"}
        if shop and not shop.is_live and request.method == "POST" and request.endpoint not in allowed:
            msg = "Your subscription has ended. Please renew to continue."
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error=msg), 402
            flash(msg, "error")
            return redirect(url_for("billing"))


@app.context_processor
def inject():
    shop = getattr(g, "shop", None)
    cur = shop.currency if shop else "GH\u20b5"
    unread = 0
    if current_user.is_authenticated and current_user.shop_id and current_user.can("alerts"):
        unread = Notification.query.filter_by(shop_id=current_user.shop_id, read=False).count()

    def money(v):
        return f"{cur} {float(v or 0):,.2f}"
    return dict(csrf=csrf_token, shop=shop, money=money, APP_NAME=APP_NAME, PERMS=PERMS, unread=unread,
                VAPID_PUBLIC=VAPID_PUBLIC if HAS_PUSH else "", now=now, fmt_dt=fmt_dt, fmt_d=fmt_d,
                pset=pset, PRESETS=PRESETS)


def fmt_dt(v):
    return v.strftime("%d/%m/%Y %H:%M") if v else ""


def fmt_d(v):
    return v.strftime("%d/%m/%Y") if v else ""


def perm_required(p):
    def deco(f):
        @wraps(f)
        @login_required
        def inner(*a, **k):
            if current_user.role == "superadmin":
                return redirect(url_for("super_home"))
            if not current_user.can(p):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="You are not allowed to do this."), 403
                flash("You are not allowed to open that page. Ask the shop owner.", "error")
                return redirect(url_for("dashboard"))
            return f(*a, **k)
        return inner
    return deco


def owner_required(f):
    @wraps(f)
    @login_required
    def inner(*a, **k):
        if not current_user.is_owner:
            flash("Only the shop owner can open that page.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **k)
    return inner


def super_required(f):
    @wraps(f)
    @login_required
    def inner(*a, **k):
        if current_user.role != "superadmin":
            abort(404)
        return f(*a, **k)
    return inner


def shop_member(f):
    @wraps(f)
    @login_required
    def inner(*a, **k):
        if current_user.role == "superadmin":
            return redirect(url_for("super_home"))
        return f(*a, **k)
    return inner


# ---------- notifications (in-app + phone push)
def _send_push(sub_rows, payload):
    with app.app_context():
        dead = []
        for sub_id, data in sub_rows:
            try:
                webpush(subscription_info=json.loads(data), data=json.dumps(payload),
                        vapid_private_key=VAPID_PRIVATE, vapid_claims={"sub": VAPID_EMAIL}, ttl=3600)
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (404, 410):
                    dead.append(sub_id)
            except Exception:
                pass
        if dead:
            PushSub.query.filter(PushSub.id.in_(dead)).delete(synchronize_session=False)
            db.session.commit()


def notify(shop_id, title, body="", kind="info", url="/dashboard"):
    db.session.add(Notification(shop_id=shop_id, title=title[:160], body=body[:400], kind=kind, url=url))
    db.session.commit()
    if not (HAS_PUSH and VAPID_PRIVATE and VAPID_PUBLIC):
        return
    users = [u.id for u in User.query.filter_by(shop_id=shop_id, active=True).all() if u.can("alerts")]
    if not users:
        return
    subs = [(s.id, s.data) for s in PushSub.query.filter(PushSub.user_id.in_(users)).all()]
    if subs:
        threading.Thread(target=_send_push, args=(subs, dict(title=title, body=body, url=url)),
                         daemon=True).start()


def start_subscription(shop, plan, days=None, note=""):
    base = shop.sub_ends if shop.sub_ends and shop.sub_ends > now() else now()
    shop.sub_ends = base + dt.timedelta(days=days or plan.days)
    shop.plan_id = plan.id if plan else shop.plan_id
    if shop.status != "suspended":
        shop.status = "active"
    db.session.add(Activity(shop_id=shop.id, user_name="System", action="Subscription",
                            detail=f"{plan.name if plan else ''} until {fmt_d(shop.sub_ends)} {note}"))


def check_limits(kind):
    shop = current_user.shop
    plan = shop.plan
    if not plan:
        return None
    if kind == "users" and plan.max_users:
        n = User.query.filter_by(shop_id=shop.id).count()
        if n >= plan.max_users:
            return f"Your plan allows {plan.max_users} users. Upgrade in Billing to add more."
    if kind == "items" and plan.max_items:
        n = Item.query.filter_by(shop_id=shop.id).count()
        if n >= plan.max_items:
            return f"Your plan allows {plan.max_items} items. Upgrade in Billing to add more."
    return None


def save_upload(fs, prefix):
    """Saves an uploaded picture (shrunk) and returns the file name."""
    if not fs or not fs.filename:
        return None
    ext = os.path.splitext(fs.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise ValueError("Please choose a picture (PNG or JPG).")
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        name += ext
        fs.save(os.path.join(UPLOAD_DIR, name))
        return name
    try:
        im = ImageOps.exif_transpose(Image.open(fs.stream))
        im.thumbnail((900, 900))
        if prefix.startswith("logo"):
            name += ".png"
            im.save(os.path.join(UPLOAD_DIR, name), "PNG")
        else:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            name += ".jpg"
            im.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=82)
    except (UnidentifiedImageError, OSError):
        raise ValueError("That file is not a picture we can read. Please choose a PNG or JPG photo.")
    return name


# Only these files may be downloaded as static files (never app.py or settings).
STATIC_FILES = {"app.css", "app.js", "pos.js", "icon-192.png", "icon-512.png", "badge.png", "app.ico"}


@app.route("/static/<path:filename>", endpoint="static")
def static_files(filename):
    name = os.path.basename(filename)       # old links like /static/css/app.css still work
    if name not in STATIC_FILES:
        abort(404)
    return send_from_directory(BASE_DIR, name, max_age=86400)


@app.route("/uploads/<path:name>")
@login_required
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400 * 30)


@app.route("/logo/<int:shop_id>")
def shop_logo(shop_id):
    s = db.session.get(Shop, shop_id)
    if not s or not s.logo:
        abort(404)
    return send_from_directory(UPLOAD_DIR, s.logo, max_age=86400)


# ==========================================================================
# Public site, sign-up, login
# ==========================================================================
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("super_home" if current_user.role == "superadmin" else "dashboard"))
    plans = Plan.query.filter_by(active=True).order_by(Plan.sort, Plan.price).all()
    return render_template("landing.html", plans=plans, trial_days=pset("trial_days"))


@app.route("/terms")
def terms():
    return render_template("legal.html", page="terms")


@app.route("/privacy")
def privacy():
    return render_template("legal.html", page="privacy")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    f = request.form
    if request.method == "POST":
        err = None
        email = f.get("email", "").strip().lower()
        username = f.get("username", "").strip().lower()
        if not f.get("shop_name", "").strip() or not f.get("name", "").strip():
            err = "Please type your shop name and your name."
        elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            err = "Please type a correct email address."
        elif not re.match(r"^[a-z0-9._-]{3,40}$", username):
            err = "Username: 3-40 letters or numbers, no spaces."
        elif len(f.get("password", "")) < 6:
            err = "Password must be at least 6 characters."
        elif f.get("password") != f.get("password2"):
            err = "The two passwords are not the same."
        elif User.query.filter((func.lower(User.username) == username) | (func.lower(User.email) == email)).first():
            err = "That username or email is already used. Try logging in."
        elif not f.get("agree"):
            err = "Please accept the terms."
        if err:
            flash(err, "error")
            return render_template("signup.html", f=f)
        trial = int(pset("trial_days") or 14)
        shop = Shop(name=f["shop_name"].strip(), phone=f.get("phone", "").strip(), email=email,
                    address=f.get("address", "").strip(), status="trial",
                    sub_ends=now() + dt.timedelta(days=trial))
        db.session.add(shop)
        db.session.flush()
        u = User(shop_id=shop.id, name=f["name"].strip(), username=username, email=email,
                 phone=f.get("phone", "").strip(), role="owner", title="Owner")
        u.set_password(f["password"])
        db.session.add(u)
        db.session.add(Activity(shop_id=shop.id, user_name=u.name, action="Shop created",
                                detail=f"Free trial for {trial} days"))
        db.session.commit()
        login_user(u, remember=True)
        flash(f"Welcome! Your free trial runs for {trial} days. Start by adding your items.", "ok")
        return redirect(url_for("dashboard"))
    return render_template("signup.html", f={})


_fails = {}
WEAK_TEST_PASSWORDS = {"admin123", "password", "123456", "admin"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        ident = request.form.get("username", "").strip().lower()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        key = f"{ip}|{ident}"
        n, first = _fails.get(key, (0, now()))
        if n >= 8 and (now() - first).total_seconds() < 900:
            flash("Too many wrong tries. Wait 15 minutes and try again.", "error")
            return render_template("login.html")
        u = User.query.filter((func.lower(User.username) == ident) | (func.lower(User.email) == ident)).first()
        if not u or not u.check_password(request.form.get("password", "")):
            _fails[key] = (n + 1, first if n else now())
            flash("Wrong username or password.", "error")
            return render_template("login.html")
        host = (request.host or "").split(":")[0]
        local = host in ("localhost", "127.0.0.1") or host.startswith(("192.168.", "10.", "172."))
        if u.role == "superadmin" and request.form.get("password") in WEAK_TEST_PASSWORDS and not local:
            flash("For safety, the test password does not work on a public link. "
                  "Set your own platform password (see the guide) and try again.", "error")
            return render_template("login.html")
        if not u.active:
            flash("This account is switched off. Ask the shop owner.", "error")
            return render_template("login.html")
        if u.shop and u.shop.status == "suspended":
            flash("This shop account is suspended. Please contact support.", "error")
            return render_template("login.html")
        _fails.pop(key, None)
        login_user(u, remember=bool(request.form.get("remember")))
        session.permanent = True
        u.last_seen = now()
        if u.shop_id:
            db.session.add(Activity(shop_id=u.shop_id, user_name=u.name or u.username, action="Logged in",
                                    detail=request.headers.get("User-Agent", "")[:120]))
            db.session.commit()
            if u.role == "staff" and u.shop.notify_cfg.get("staff_login"):
                notify(u.shop_id, f"{u.name or u.username} logged in", f"Started work at {now():%H:%M}",
                       "staff", "/staff")
        db.session.commit()
        nxt = request.args.get("next", "")
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    if current_user.shop_id:
        log("Logged out")
        db.session.commit()
    logout_user()
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        f = request.form
        if not current_user.check_password(f.get("old", "")):
            flash("Your current password is not correct.", "error")
        elif len(f.get("new", "")) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif f.get("new") != f.get("new2"):
            flash("The two new passwords are not the same.", "error")
        else:
            current_user.set_password(f["new"])
            log("Changed password")
            db.session.commit()
            flash("Password changed.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html")


# ==========================================================================
# Dashboard & live monitoring
# ==========================================================================
@app.route("/dashboard")
@shop_member
def dashboard():
    shop = current_user.shop
    items = shop_q(Item).all()
    low = sorted([i for i in items if is_low(i, shop)], key=lambda i: i.qty)
    exp = sorted([(i, *expiry_status(i, shop)) for i in items if expiry_status(i, shop)[0]], key=lambda x: x[2])
    data = dict(
        today=sales_summary(shop.id, *period_range("today")),
        week=sales_summary(shop.id, *period_range("week")),
        month=sales_summary(shop.id, *period_range("month")),
        n_items=len(items), pieces=sum(max(i.qty, 0) for i in items),
        debt=db.session.query(func.coalesce(func.sum(Customer.balance), 0)).filter(
            Customer.shop_id == shop.id, Customer.balance > 0).scalar(),
    )
    return render_template("dashboard.html", d=data, low=low[:30], exp=exp[:30])


@app.route("/api/live")
@shop_member
def api_live():
    shop = current_user.shop
    t = sales_summary(shop.id, *period_range("today"))
    out = dict(ok=True, today_total=t["total"], today_n=t["n"], unread=0)
    if current_user.can("view_profit"):
        out["today_profit"] = t["profit"]
    if current_user.can("view_sales") or current_user.is_owner:
        s, e = period_range("today")
        rows = Sale.query.filter(Sale.shop_id == shop.id, Sale.ts >= s, Sale.ts < e).order_by(
            Sale.id.desc()).limit(15).all()
        out["sales"] = [dict(id=r.id, number=r.number, time=r.ts.strftime("%H:%M"), by=r.user_name,
                             pay=r.payment, total=r.total, voided=r.voided,
                             items=", ".join(f"{i.qty}x {i.name}" for i in r.items)[:90]) for r in rows]
        hours = db.session.query(func.extract("hour", Sale.ts), func.sum(Sale.total)).filter(
            Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(
            func.extract("hour", Sale.ts)).all()
        out["hours"] = {int(h): float(v) for h, v in hours}
        staff = User.query.filter_by(shop_id=shop.id, active=True).all()
        out["staff"] = [dict(name=u.name or u.username, title=u.title if u.role == "staff" else "Owner",
                             online=u.online, seen=u.last_seen.strftime("%d/%m %H:%M") if u.last_seen else "never",
                             sold=float(db.session.query(func.coalesce(func.sum(Sale.total), 0)).filter(
                                 Sale.shop_id == shop.id, Sale.user_id == u.id, Sale.voided.is_(False),
                                 Sale.ts >= s, Sale.ts < e).scalar()))
                        for u in staff]
    if current_user.can("alerts"):
        out["unread"] = Notification.query.filter_by(shop_id=shop.id, read=False).count()
    return jsonify(out)


# ==========================================================================
# Point of sale
# ==========================================================================
@app.route("/pos")
@perm_required("sell")
def pos():
    customers = [c.name for c in shop_q(Customer).order_by(Customer.name).all()]
    return render_template("pos.html", customers=customers)


@app.route("/api/items")
@shop_member
def api_items():
    if not (current_user.can("sell") or current_user.can("view_stock")):
        return jsonify(ok=False, error="Not allowed"), 403
    shop = current_user.shop
    rows = shop_q(Item).order_by(Item.name).all()
    return jsonify(ok=True, items=[dict(id=i.id, name=i.name, category=i.category or "", price=i.price,
                                        qty=i.qty, barcode=i.barcode or "", low=is_low(i, shop),
                                        img=url_for("uploads", name=i.image) if i.image else "")
                                   for i in rows])


def next_number(shop_id):
    return (db.session.query(func.coalesce(func.max(Sale.number), 0)).filter(Sale.shop_id == shop_id).scalar()
            or 0) + 1


@app.route("/api/sale", methods=["POST"])
@perm_required("sell")
def api_sale():
    shop = current_user.shop
    d = request.get_json(silent=True) or {}
    uid = str(d.get("client_uid") or "")[:64] or None
    if uid:
        old = Sale.query.filter_by(client_uid=uid, shop_id=shop.id).first()
        if old:
            return jsonify(ok=True, sale_id=old.id, number=old.number, duplicate=True,
                           receipt=url_for("receipt", sale_id=old.id))
    offline = bool(d.get("offline"))
    lines = d.get("lines") or []
    if not lines:
        return jsonify(ok=False, error="The cart is empty.")
    payment = d.get("payment") or "Cash"
    if payment not in shop.payment_list:
        return jsonify(ok=False, error=f"{payment} payment is switched off for this shop.")
    discount = to_float(d.get("discount")) or 0
    if discount and not current_user.can("discount"):
        return jsonify(ok=False, error="You are not allowed to give discounts.")
    if payment == "Credit" and not current_user.can("credit_sale"):
        return jsonify(ok=False, error="You are not allowed to sell on credit.")
    cart = []
    for ln in lines:
        it = db.session.get(Item, int(ln.get("id", 0)))
        q = to_int(ln.get("qty"))
        if not it or it.shop_id != shop.id:
            return jsonify(ok=False, error="An item in the cart no longer exists. Refresh the page.")
        if not q or q <= 0:
            return jsonify(ok=False, error=f"Bad quantity for {it.name}.")
        if q > it.qty and not offline:
            return jsonify(ok=False, error=f"Only {it.qty} of '{it.name}' left in stock.")
        cart.append((it, q))
    sub = round(sum(it.price * q for it, q in cart), 2)
    if discount < 0 or discount > sub:
        return jsonify(ok=False, error="Discount must be between 0 and the subtotal.")
    total = round(sub - discount, 2)
    paid = to_float(d.get("paid"))
    tendered = change = deposit = owed = 0.0
    cust = (d.get("customer") or "").strip()[:120]
    phone = (d.get("phone") or "").strip()[:40]
    if payment == "Cash":
        paid = total if paid is None else paid
        if paid + 0.001 < total:
            return jsonify(ok=False, error=f"Customer gave less than the total ({total:,.2f}).")
        tendered, change = paid, round(paid - total, 2)
    elif payment == "Credit":
        if not cust:
            return jsonify(ok=False, error="Type the customer's name for a credit sale.")
        deposit = paid or 0
        if deposit < 0 or deposit >= total:
            return jsonify(ok=False, error="For credit, the amount paid now must be less than the total.")
        owed = round(total - deposit, 2)
    else:
        tendered = total
    ts = now()
    if offline and d.get("offline_ts"):
        try:
            ts = dt.datetime.fromisoformat(str(d["offline_ts"])[:19])
        except ValueError:
            pass
    sale = Sale(shop_id=shop.id, number=next_number(shop.id), ts=ts, user_id=current_user.id,
                user_name=current_user.name or current_user.username, subtotal=sub, discount=discount, total=total,
                cost_total=round(sum(it.cost * q for it, q in cart), 2), payment=payment, tendered=tendered,
                change_given=change, deposit=deposit, owed=owed, customer=cust, phone=phone,
                momo_ref=(d.get("momo_ref") or "")[:80], source="offline" if offline else "", client_uid=uid)
    db.session.add(sale)
    went_low = []
    for it, q in cart:
        was_low = is_low(it, shop)
        sale.items.append(SaleItem(item_id=it.id, name=it.name, qty=q, price=it.price, cost=it.cost))
        it.qty = max(it.qty - q, 0)
        it.updated = ts
        if is_low(it, shop) and not was_low:
            went_low.append(it)
    db.session.flush()
    if payment == "Credit":
        c = Customer.query.filter(Customer.shop_id == shop.id, func.lower(Customer.name) == cust.lower()).first()
        if not c:
            c = Customer(shop_id=shop.id, name=cust, phone=phone, balance=0)
            db.session.add(c)
            db.session.flush()
        c.balance = round((c.balance or 0) + owed, 2)
        db.session.add(DebtTx(shop_id=shop.id, customer_id=c.id, user_name=sale.user_name, kind="credit",
                              amount=owed, sale_id=sale.id,
                              note=f"Receipt #{sale.number:05d}" + (f", paid {deposit:,.2f} now" if deposit else "")))
    db.session.commit()
    cfg = shop.notify_cfg
    cur = shop.currency
    big = to_float(cfg.get("big_sale")) or 0
    if cfg.get("each_sale") or (big and total >= big):
        notify(shop.id, f"Sale {cur} {total:,.2f} ({payment})",
               f"{sale.user_name}: " + ", ".join(f"{q}x {it.name}" for it, q in cart)[:200], "sale",
               url_for("receipt", sale_id=sale.id))
    if went_low and cfg.get("low_stock"):
        notify(shop.id, "Low stock: " + ", ".join(i.name for i in went_low)[:120],
               "; ".join(f"{i.name}: {i.qty} left" for i in went_low)[:380], "stock", "/items?view=low")
    return jsonify(ok=True, sale_id=sale.id, number=sale.number, receipt=url_for("receipt", sale_id=sale.id),
                   change=change, owed=owed, low=[f"{i.name} ({i.qty} left)" for i in went_low])


@app.route("/receipt/<int:sale_id>")
@shop_member
def receipt(sale_id):
    s = get_or_404(Sale, sale_id)
    if not (current_user.can("view_sales") or current_user.can("sell")):
        abort(403)
    lines = [f"{s.shop_id and current_user.shop.name}", f"Receipt #{s.number:05d}  {fmt_dt(s.ts)}"]
    lines += [f"{i.qty} x {i.name} = {i.qty * i.price:,.2f}" for i in s.items]
    lines += [f"TOTAL: {current_user.shop.currency} {s.total:,.2f} ({s.payment})", current_user.shop.receipt_footer]
    wa_text = "\n".join(lines)
    return render_template("receipt.html", s=s, wa_text=wa_text, auto=request.args.get("print"))


# ==========================================================================
# Items & stock
# ==========================================================================
@app.route("/items")
@perm_required("view_stock")
def items():
    shop = current_user.shop
    q, cat, view = request.args.get("q", ""), request.args.get("cat", ""), request.args.get("view", "")
    rows = []
    for i in shop_q(Item).order_by(Item.name).all():
        if cat and i.category != cat:
            continue
        if not text_match(q, i.name, i.category, i.barcode, i.supplier):
            continue
        st, days = expiry_status(i, shop)
        low = is_low(i, shop)
        if view == "low" and not low:
            continue
        if view == "out" and i.qty > 0:
            continue
        if view == "expiring" and not st:
            continue
        rows.append(dict(i=i, low=low, exp=st, days=days, limit=limit_of(i, shop)))
    cats = [c[0] for c in db.session.query(Item.category).filter(Item.shop_id == shop.id).distinct().order_by(
        Item.category) if c[0]]
    see_cost = current_user.can("see_cost")
    totals = dict(n=len(rows), pieces=sum(max(r["i"].qty, 0) for r in rows),
                  cost=sum(max(r["i"].qty, 0) * r["i"].cost for r in rows) if see_cost else 0,
                  sell=sum(max(r["i"].qty, 0) * r["i"].price for r in rows))
    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Item Name", "Category", "Quantity"] + (["Cost Price"] if see_cost else []) +
                   ["Selling Price", "Barcode", "Supplier", "Expiry"])
        for r in rows:
            i = r["i"]
            w.writerow([i.name, i.category, i.qty] + ([i.cost] if see_cost else []) +
                       [i.price, i.barcode, i.supplier, fmt_d(i.expiry)])
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=stock_list.csv"})
    return render_template("items.html", rows=rows, cats=cats, q=q, cat=cat, view=view, totals=totals,
                           see_cost=see_cost)


@app.route("/items/new", methods=["GET", "POST"])
@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@perm_required("manage_items")
def item_form(item_id=None):
    shop = current_user.shop
    item = get_or_404(Item, item_id) if item_id else None
    if not item:
        lim = check_limits("items")
        if lim:
            flash(lim, "error")
            return redirect(url_for("items"))
    f = request.form
    if request.method == "POST":
        name = f.get("name", "").strip()
        qty, cost, price = to_int(f.get("qty")), to_float(f.get("cost")), to_float(f.get("price"))
        if not current_user.can("see_cost") and item:
            cost = item.cost
        exp = parse_date(f.get("expiry")) if f.get("expiry", "").strip() else None
        low = to_int(f.get("low_limit")) if f.get("low_limit", "").strip() else None
        bc = f.get("barcode", "").strip()
        err = None
        if not name:
            err = "Type the item name."
        elif qty is None or qty < 0:
            err = "Quantity must be a whole number."
        elif cost is None or cost < 0 or price is None or price < 0:
            err = "Cost and selling price must be numbers."
        elif f.get("expiry", "").strip() and not exp:
            err = "Expiry date must look like 31/12/2026."
        elif bc and shop_q(Item).filter(Item.barcode == bc, Item.id != (item.id if item else 0)).first():
            err = "That barcode / code is already used by another item."
        if err:
            flash(err, "error")
            return render_template("item_form.html", item=item, f=f, cats=categories())
        try:
            img = save_upload(request.files.get("image"), "item")
        except ValueError as e:
            flash(str(e), "error")
            return render_template("item_form.html", item=item, f=f, cats=categories())
        user = current_user.name or current_user.username
        if item:
            changes = []
            if item.price != price:
                changes.append(f"price {item.price:,.2f} > {price:,.2f}")
            if item.qty != qty:
                changes.append(f"qty {item.qty} > {qty}")
                db.session.add(Restock(shop_id=shop.id, user_name=user, item_id=item.id, name=name,
                                       qty_added=qty - item.qty, old_qty=item.qty, new_qty=qty, old_cost=item.cost,
                                       new_cost=cost, old_price=item.price, new_price=price,
                                       note="Stock correction (edited item)"))
            item.name, item.category, item.qty, item.cost, item.price = name, f.get("category", "").strip() or "Others", qty, cost, price
            item.barcode, item.supplier, item.expiry, item.low_limit = bc, f.get("supplier", "").strip(), exp, low
            if img:
                item.image = img
            if f.get("remove_image"):
                item.image = ""
            item.updated = now()
            log("Edited item", f"{name}: " + ", ".join(changes) if changes else name)
        else:
            item = Item(shop_id=shop.id, name=name, category=f.get("category", "").strip() or "Others", qty=qty,
                        cost=cost, price=price, barcode=bc, supplier=f.get("supplier", "").strip(), expiry=exp,
                        low_limit=low, image=img or "")
            db.session.add(item)
            db.session.flush()
            if qty:
                db.session.add(Restock(shop_id=shop.id, user_name=user, item_id=item.id, name=name, qty_added=qty,
                                       old_qty=0, new_qty=qty, old_cost=cost, new_cost=cost, old_price=price,
                                       new_price=price, note="Opening stock (new item)"))
            log("Added item", f"{name} ({qty} @ {price:,.2f})")
        db.session.commit()
        flash(f"Saved '{name}'.", "ok")
        return redirect(url_for("items", q=request.args.get("back_q", "")))
    return render_template("item_form.html", item=item, f={}, cats=categories())


def categories():
    base = ["Curtains", "Bedsheets", "Pampers", "Bowls", "Cups", "Take-away Packs", "Food Items", "Others"]
    have = [c[0] for c in db.session.query(Item.category).filter(Item.shop_id == sid()).distinct() if c[0]]
    return sorted(set(base) | set(have))


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@perm_required("delete_items")
def item_delete(item_id):
    item = get_or_404(Item, item_id)
    log("Deleted item", item.name)
    db.session.delete(item)
    db.session.commit()
    flash(f"Deleted '{item.name}'.", "ok")
    return redirect(url_for("items"))


# ==========================================================================
# Restock
# ==========================================================================
@app.route("/restock", methods=["GET", "POST"])
@perm_required("restock")
def restock():
    shop = current_user.shop
    if request.method == "POST":
        f = request.form
        item = get_or_404(Item, to_int(f.get("item_id")) or 0)
        qty = to_int(f.get("qty"))
        cost = to_float(f.get("cost")) if current_user.can("see_cost") else item.cost
        price = to_float(f.get("price"))
        if not qty or qty <= 0:
            flash("Quantity to add must be a whole number.", "error")
        elif cost is None or price is None or cost < 0 or price < 0:
            flash("Prices must be numbers.", "error")
        else:
            user = current_user.name or current_user.username
            db.session.add(Restock(shop_id=shop.id, user_name=user, item_id=item.id, name=item.name, qty_added=qty,
                                   old_qty=item.qty, new_qty=item.qty + qty, old_cost=item.cost, new_cost=cost,
                                   old_price=item.price, new_price=price, supplier=f.get("supplier", "").strip(),
                                   note=f.get("note", "").strip()))
            item.qty += qty
            item.cost, item.price = cost, price
            if f.get("supplier", "").strip():
                item.supplier = f["supplier"].strip()
            item.updated = now()
            log("Restocked", f"{item.name} +{qty} (now {item.qty})")
            db.session.commit()
            flash(f"Added {qty} to '{item.name}'. Now {item.qty} in stock.", "ok")
        return redirect(url_for("restock", item=item.id))
    items_ = shop_q(Item).order_by(Item.name).all()
    hist = shop_q(Restock).order_by(Restock.id.desc()).limit(300).all()
    sel = get_or_404(Item, to_int(request.args.get("item"))) if request.args.get("item") else None
    return render_template("restock.html", items=items_, hist=hist, sel=sel)


@app.route("/restock/<int:rid>/delete", methods=["POST"])
@owner_required
def restock_delete(rid):
    r = get_or_404(Restock, rid)
    db.session.delete(r)
    log("Deleted restock record", r.name)
    db.session.commit()
    return redirect(url_for("restock"))


# ==========================================================================
# Debt book
# ==========================================================================
@app.route("/debts", methods=["GET", "POST"])
@perm_required("debts")
def debts():
    shop = current_user.shop
    if request.method == "POST":
        f = request.form
        act = f.get("action")
        user = current_user.name or current_user.username
        if act == "add_debt":
            name, amt = f.get("name", "").strip(), to_float(f.get("amount"))
            if not name or not amt or amt <= 0:
                flash("Type the customer's name and an amount.", "error")
            else:
                c = shop_q(Customer).filter(func.lower(Customer.name) == name.lower()).first()
                if not c:
                    c = Customer(shop_id=shop.id, name=name, phone=f.get("phone", "").strip(), balance=0)
                    db.session.add(c)
                    db.session.flush()
                c.balance = round(c.balance + amt, 2)
                db.session.add(DebtTx(shop_id=shop.id, customer_id=c.id, user_name=user, kind="manual", amount=amt,
                                      note=f.get("note", "").strip() or "Debt added"))
                log("Added debt", f"{name}: {amt:,.2f}")
                db.session.commit()
                flash(f"{name} now owes {shop.currency} {c.balance:,.2f}.", "ok")
                return redirect(url_for("debt_detail", cid=c.id))
        elif act == "pay":
            c = get_or_404(Customer, to_int(f.get("cid")) or 0)
            amt = to_float(f.get("amount"))
            method = f.get("method") if f.get("method") in ALL_PAYMENTS else "Cash"
            if not amt or amt <= 0 or amt > c.balance + 0.001:
                flash(f"Amount must be more than 0 and not more than {c.balance:,.2f}.", "error")
            else:
                c.balance = round(c.balance - amt, 2)
                db.session.add(DebtTx(shop_id=shop.id, customer_id=c.id, user_name=user, kind="payment", amount=amt,
                                      method=method, note=f.get("note", "").strip()))
                log("Debt payment", f"{c.name} paid {amt:,.2f} ({method})")
                db.session.commit()
                flash(f"{c.name} paid {shop.currency} {amt:,.2f}. Still owes {shop.currency} {c.balance:,.2f}.", "ok")
            return redirect(url_for("debt_detail", cid=c.id))
    q = request.args.get("q", "")
    show_all = request.args.get("all")
    rows = [c for c in shop_q(Customer).order_by(Customer.balance.desc(), Customer.name).all()
            if (show_all or c.balance > 0.001) and text_match(q, c.name, c.phone)]
    total = sum(c.balance for c in shop_q(Customer).all() if c.balance > 0)
    return render_template("debts.html", rows=rows, total=total, q=q, show_all=show_all)


@app.route("/debts/<int:cid>")
@perm_required("debts")
def debt_detail(cid):
    c = get_or_404(Customer, cid)
    tx = DebtTx.query.filter_by(customer_id=c.id, shop_id=sid()).order_by(DebtTx.id.desc()).all()
    return render_template("debt_detail.html", c=c, tx=tx)


# ==========================================================================
# Sales & reports
# ==========================================================================
@app.route("/sales")
@perm_required("view_sales")
def sales():
    shop = current_user.shop
    period = request.args.get("p", "today")
    s, e = period_range(period, request.args.get("from"), request.args.get("to"))
    sm = sales_summary(shop.id, s, e)
    base = Sale.query.filter(Sale.shop_id == shop.id, Sale.ts >= s, Sale.ts < e)
    rows = base.order_by(Sale.ts.desc(), Sale.id.desc()).limit(500).all()
    good = (SaleItem.query.join(Sale).filter(Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s,
                                             Sale.ts < e))
    top = db.session.query(SaleItem.name, func.sum(SaleItem.qty), func.sum(SaleItem.qty * SaleItem.price),
                           func.sum(SaleItem.qty * (SaleItem.price - SaleItem.cost))).join(Sale).filter(
        Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(
        SaleItem.name).order_by(func.sum(SaleItem.qty).desc()).limit(50).all()
    by_pay = db.session.query(Sale.payment, func.count(Sale.id), func.sum(Sale.total),
                              func.sum(Sale.total - Sale.cost_total)).filter(
        Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(Sale.payment).all()
    by_user = db.session.query(Sale.user_name, func.count(Sale.id), func.sum(Sale.total),
                               func.sum(Sale.total - Sale.cost_total)).filter(
        Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(Sale.user_name).all()
    days = {}
    for r in base.filter(Sale.voided.is_(False)).all():
        k = r.ts.date()
        d = days.setdefault(k, [0, 0.0, 0.0])
        d[0] += 1
        d[1] += r.total
        d[2] += r.total - r.cost_total
    days = sorted(days.items(), reverse=True)
    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        prof = current_user.can("view_profit")
        w.writerow(["Receipt", "Date", "Cashier", "Customer", "Payment", "Items", "Total"] +
                   (["Profit"] if prof else []) + ["Status"])
        for r in rows:
            w.writerow([r.number, fmt_dt(r.ts), r.user_name, r.customer, r.payment,
                        "; ".join(f"{i.qty}x {i.name}" for i in r.items), r.total] +
                       ([round(r.total - r.cost_total, 2)] if prof else []) +
                       ["CANCELLED" if r.voided else ("OLD RECORD" if r.source == "imported" else "OK")])
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=sales.csv"})
    _ = good
    return render_template("sales.html", sm=sm, rows=rows, top=top, by_pay=by_pay, by_user=by_user, days=days,
                           period=period, s=s, e=e - dt.timedelta(days=1), args=request.args)


@app.route("/sales/<int:sale_id>/cancel", methods=["POST"])
@perm_required("cancel_sales")
def sale_cancel(sale_id):
    s = get_or_404(Sale, sale_id)
    if s.voided:
        flash("Already cancelled.", "error")
        return redirect(request.referrer or url_for("sales"))
    for si in s.items:
        it = db.session.get(Item, si.item_id) if si.item_id else None
        if it and it.shop_id == s.shop_id:
            it.qty += si.qty
    if s.owed and s.customer:
        c = shop_q(Customer).filter(func.lower(Customer.name) == s.customer.lower()).first()
        if c:
            c.balance = round(c.balance - s.owed, 2)
            db.session.add(DebtTx(shop_id=s.shop_id, customer_id=c.id, user_name=current_user.name, kind="cancelled",
                                  amount=-s.owed, sale_id=s.id, note=f"Sale #{s.number:05d} cancelled"))
    s.voided, s.void_by, s.void_ts = True, current_user.name or current_user.username, now()
    log("Cancelled sale", f"#{s.number:05d} {s.total:,.2f}")
    db.session.commit()
    if current_user.shop.notify_cfg.get("cancel") and not current_user.is_owner:
        notify(s.shop_id, f"Sale #{s.number:05d} was cancelled", f"By {s.void_by}. Amount {s.total:,.2f}",
               "warning", url_for("receipt", sale_id=s.id))
    flash(f"Sale #{s.number:05d} cancelled. Items went back into stock.", "ok")
    return redirect(request.referrer or url_for("sales"))


@app.route("/sales/delete", methods=["POST"])
@owner_required
def sales_delete():
    f = request.form
    if not current_user.check_password(f.get("password", "")):
        flash("Password not correct. Nothing was deleted.", "error")
        return redirect(request.referrer or url_for("sales"))
    mode = f.get("mode")
    q = Sale.query.filter(Sale.shop_id == sid())
    if mode == "one":
        q = q.filter(Sale.id == to_int(f.get("sale_id")))
    elif mode == "cancelled":
        q = q.filter(Sale.voided.is_(True))
    elif mode == "period":
        s, e = period_range(f.get("p"), f.get("from"), f.get("to"))
        q = q.filter(Sale.ts >= s, Sale.ts < e)
    else:
        abort(400)
    rows = q.all()
    for r in rows:
        DebtTx.query.filter_by(sale_id=r.id, shop_id=sid()).update({"sale_id": None})
        db.session.delete(r)
    log("Deleted sales history", f"{len(rows)} sale(s), mode={mode}")
    db.session.commit()
    flash(f"{len(rows)} sale(s) deleted from history.", "ok")
    return redirect(url_for("sales", p=f.get("p", "today")))


def closing_data(shop, day):
    s, e = dt.datetime.combine(day, dt.time()), dt.datetime.combine(day + dt.timedelta(days=1), dt.time())
    sm = sales_summary(shop.id, s, e)
    pays = {p: dict(n=n, t=float(t or 0), dep=float(dep or 0), ow=float(ow or 0))
            for p, n, t, dep, ow in db.session.query(Sale.payment, func.count(Sale.id), func.sum(Sale.total),
                                                     func.sum(Sale.deposit), func.sum(Sale.owed)).filter(
                Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(Sale.payment)}
    dp = {m: float(t or 0) for m, t in db.session.query(DebtTx.method, func.sum(DebtTx.amount)).filter(
        DebtTx.shop_id == shop.id, DebtTx.kind == "payment", DebtTx.ts >= s, DebtTx.ts < e).group_by(DebtTx.method)}
    sold = db.session.query(SaleItem.name, func.sum(SaleItem.qty), func.sum(SaleItem.qty * SaleItem.price)).join(
        Sale).filter(Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.ts >= s, Sale.ts < e).group_by(
        SaleItem.name).order_by(func.sum(SaleItem.qty).desc()).all()
    items_ = Item.query.filter_by(shop_id=shop.id).all()
    sold_ids = {si.item_id for si in SaleItem.query.join(Sale).filter(Sale.shop_id == shop.id, Sale.ts >= s,
                                                                      Sale.ts < e).all()}
    cash_in = pays.get("Cash", {}).get("t", 0) + pays.get("Credit", {}).get("dep", 0) + dp.get("Cash", 0)
    return dict(sm=sm, pays=pays, dp=dp, sold=sold, cash_in=cash_in,
                finished=[i for i in items_ if i.qty <= 0 and i.id in sold_ids],
                low=[i for i in items_ if is_low(i, shop)],
                voided=Sale.query.filter(Sale.shop_id == shop.id, Sale.voided.is_(True), Sale.ts >= s,
                                         Sale.ts < e).count(),
                debts=Sale.query.filter(Sale.shop_id == shop.id, Sale.voided.is_(False), Sale.payment == "Credit",
                                        Sale.ts >= s, Sale.ts < e).all())


@app.route("/close-day")
@perm_required("close_day")
def close_day():
    day = parse_date(request.args.get("date", "")) or today()
    return render_template("close_day.html", day=day, c=closing_data(current_user.shop, day),
                           auto=request.args.get("print"))


@app.route("/close-day/send", methods=["POST"])
@perm_required("close_day")
def close_day_send():
    shop = current_user.shop
    day = parse_date(request.form.get("date", "")) or today()
    c = closing_data(shop, day)
    notify(shop.id, f"Day closed: {shop.currency} {c['sm']['total']:,.2f} sold",
           f"{c['sm']['n']} sales on {fmt_d(day)}. Cash expected {shop.currency} {c['cash_in']:,.2f}. "
           f"Closed by {current_user.name or current_user.username}.", "daily",
           url_for("close_day", date=day.strftime("%d/%m/%Y")))
    log("Closed day", fmt_d(day))
    db.session.commit()
    flash("Day closed. The owner has been notified.", "ok")
    return redirect(url_for("close_day", date=day.strftime("%d/%m/%Y")))


# ==========================================================================
# Import from Excel / CSV
# ==========================================================================
ITEM_ALIASES = {
    "name": ["item name", "name", "item", "items", "product", "product name", "description", "goods"],
    "category": ["category", "type", "group", "section", "department"],
    "qty": ["quantity", "qty", "quantity left", "qty left", "stock", "in stock", "pieces", "count"],
    "cost": ["cost price", "cost", "buying price", "cp", "unit cost", "purchase price"],
    "price": ["selling price", "price", "sp", "sell price", "unit price", "retail price", "retails price"],
    "barcode": ["barcode", "bar code", "code", "item code", "sku"],
    "supplier": ["supplier", "vendor"],
    "expiry": ["expiry date", "expiry", "exp", "exp date", "expires"],
    "low": ["low stock alert", "low stock", "alert", "alert at", "reorder level"],
}
SALE_ALIASES = {
    "date": ["date", "day", "sale date"],
    "item": ["item name", "item", "name", "product", "description"],
    "qty": ["quantity", "qty", "pieces", "count"],
    "price": ["selling price", "price", "unit price", "sp"],
    "total": ["total amount", "total", "amount", "sales"],
    "cost": ["cost price", "cost", "unit cost"],
    "payment": ["payment", "paid by", "payment method", "method"],
    "customer": ["customer", "customer name", "buyer"],
    "receipt": ["receipt no", "receipt", "invoice"],
}


def _norm_head(h):
    h = str(h or "").strip().lower()
    h = re.sub(r"\(.*?\)", " ", h)
    for c in ("gh\u20b5", "ghs", "ghc"):
        h = h.replace(c, " ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", h).split())


def read_table(fs):
    name = (fs.filename or "").lower()
    if name.endswith(".xls"):
        raise ValueError("Old .xls file: open it in Excel, File > Save As > Excel Workbook (.xlsx).")
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(fs.read()), read_only=True, data_only=True)
        rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")):
        raw = fs.read().decode("utf-8-sig", "replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(raw), dialect))
    else:
        raise ValueError("Choose an Excel (.xlsx) or CSV file.")
    hi = next((i for i, r in enumerate(rows[:20]) if sum(1 for c in r if c is not None and str(c).strip()) >= 2),
              None)
    if hi is None:
        raise ValueError("The sheet looks empty.")
    heads = [_norm_head(c) for c in rows[hi]]
    out = []
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        if any(c is not None and str(c).strip() for c in r):
            d = {heads[j]: r[j] for j in range(min(len(heads), len(r))) if heads[j]}
            d["_row"] = n
            out.append(d)
    return heads, out


def _pick(d, al):
    for a in al:
        v = d.get(a)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%d/%m/%Y")
    return str(v).strip()


def _num(v):
    if v is None or str(v).strip() == "":
        return None
    return float(v) if isinstance(v, (int, float)) else to_float(v)


def _date(v):
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))).date()
    d = parse_date(str(v).split(" ")[0])
    if not d:
        raise ValueError(f"Date '{v}' is not clear (use DD/MM/YYYY)")
    return d


def parse_items(heads, rows):
    if not any(a in heads for a in ITEM_ALIASES["name"]):
        raise ValueError("Could not find the 'Item Name' column.")
    have = {i.name.lower(): i for i in shop_q(Item).all()}
    by_bc = {i.barcode: i for i in shop_q(Item).all() if i.barcode}
    out, seen = [], set()
    for d in rows:
        p = dict(row=d["_row"], err="")
        A = ITEM_ALIASES
        p["name"], p["category"] = _txt(_pick(d, A["name"])), _txt(_pick(d, A["category"]))
        p["barcode"], p["supplier"] = _txt(_pick(d, A["barcode"])), _txt(_pick(d, A["supplier"]))
        q = _pick(d, A["qty"])
        p["qty"] = None
        if q is not None:
            qv = _num(q)
            if qv is None or qv < 0 or qv != int(qv):
                p["err"] = "Quantity must be a whole number"
            else:
                p["qty"] = int(qv)
        p["cost"], p["price"] = _num(_pick(d, A["cost"])), _num(_pick(d, A["price"]))
        try:
            e = _date(_pick(d, A["expiry"]))
            p["expiry"] = e.isoformat() if e else ""
        except ValueError as ex:
            p["err"], p["expiry"] = str(ex), ""
        lv = _num(_pick(d, A["low"]))
        p["low"] = int(lv) if lv is not None and lv >= 0 else None
        if not p["name"]:
            p["err"] = "No item name"
        ex = (by_bc.get(p["barcode"]) if p["barcode"] else None) or have.get(p["name"].lower())
        if not p["err"] and not ex and p["name"].lower() not in seen and p["price"] is None:
            p["err"] = "No selling price for a new item"
        p["status"] = ("Error: " + p["err"]) if p["err"] else (f"Update (now {ex.qty})" if ex else "New item")
        seen.add(p["name"].lower())
        out.append(p)
    return out


def parse_sales(heads, rows):
    if not any(a in heads for a in SALE_ALIASES["date"]):
        raise ValueError("Could not find the 'Date' column.")
    have = {i.name.lower(): i for i in shop_q(Item).all()}
    out = []
    for d in rows:
        A = SALE_ALIASES
        p = dict(row=d["_row"], err="", dup=False)
        try:
            day = _date(_pick(d, A["date"]))
            p["date"] = day.isoformat() if day else ""
            if not day:
                p["err"] = "No date"
            elif day > today():
                p["err"] = "Date is in the future"
        except ValueError as ex:
            p["date"], p["err"] = "", str(ex)
        p["item"] = _txt(_pick(d, A["item"])) or "Sales (from book)"
        qv = _num(_pick(d, A["qty"]))
        p["qty"] = int(qv) if qv and qv == int(qv) and qv > 0 else (1 if qv is None else 0)
        if not p["qty"]:
            p["err"] = p["err"] or "Quantity must be a whole number"
        price, total = _num(_pick(d, A["price"])), _num(_pick(d, A["total"]))
        if price is None and total is not None and p["qty"]:
            price = total / p["qty"]
        if price is None:
            p["err"] = p["err"] or "No price or total"
        p["price"] = round(price or 0, 2)
        m = have.get(p["item"].lower())
        c = _num(_pick(d, A["cost"]))
        p["cost"] = round(c if c is not None else (m.cost if m else 0), 2)
        p["item_id"] = m.id if m else None
        pay = str(_pick(d, A["payment"]) or "").lower()
        p["payment"] = "MoMo" if any(k in pay for k in ("momo", "mobile", "mtn", "telecel", "airtel")) else (
            "Credit" if any(k in pay for k in ("credit", "owe", "debt")) else "Cash")
        p["customer"], p["receipt"] = _txt(_pick(d, A["customer"])), _txt(_pick(d, A["receipt"]))
        if p["payment"] == "Credit" and not p["customer"]:
            p["err"] = p["err"] or "Credit needs a customer name"
        if not p["err"]:
            day0 = dt.datetime.fromisoformat(p["date"])
            p["dup"] = db.session.query(SaleItem.id).join(Sale).filter(
                Sale.shop_id == sid(), Sale.source == "imported", Sale.voided.is_(False), Sale.ts >= day0,
                Sale.ts < day0 + dt.timedelta(days=1), func.lower(SaleItem.name) == p["item"].lower(),
                SaleItem.qty == p["qty"], func.abs(SaleItem.price - p["price"]) < 0.005).first() is not None
        p["status"] = ("Error: " + p["err"]) if p["err"] else ("Already imported" if p["dup"] else "OK")
        out.append(p)
    return out


@app.route("/import", methods=["GET", "POST"])
@perm_required("import_data")
def import_data():
    shop = current_user.shop
    preview, kind, token = None, request.form.get("kind", "items"), None
    if request.method == "POST":
        user = current_user.name or current_user.username
        if request.form.get("token"):                         # confirm import
            token = re.sub(r"[^a-f0-9]", "", request.form["token"])
            path = os.path.join(TMP_DIR, f"imp_{shop.id}_{token}.json")
            if not os.path.exists(path):
                flash("The preview expired. Choose the file again.", "error")
                return redirect(url_for("import_data"))
            with open(path) as fh:
                data = json.load(fh)
            os.remove(path)
            kind, rows = data["kind"], data["rows"]
            if kind == "items":
                add_qty = request.form.get("qty_mode", "add") == "add"
                upd = bool(request.form.get("update_prices"))
                added = updated = 0
                for p in rows:
                    if p["err"]:
                        continue
                    ex = (shop_q(Item).filter_by(barcode=p["barcode"]).first() if p["barcode"] else None) or \
                        shop_q(Item).filter(func.lower(Item.name) == p["name"].lower()).first()
                    exp = dt.date.fromisoformat(p["expiry"]) if p["expiry"] else None
                    if ex:
                        old = ex.qty
                        if p["qty"] is not None:
                            ex.qty = ex.qty + p["qty"] if add_qty else p["qty"]
                        if upd and p["cost"] is not None:
                            ex.cost = p["cost"]
                        if upd and p["price"] is not None:
                            ex.price = p["price"]
                        ex.category = p["category"] or ex.category
                        ex.barcode = p["barcode"] or ex.barcode
                        ex.supplier = p["supplier"] or ex.supplier
                        ex.expiry = exp or ex.expiry
                        if p["low"] is not None:
                            ex.low_limit = p["low"]
                        if ex.qty != old:
                            db.session.add(Restock(shop_id=shop.id, user_name=user, item_id=ex.id, name=ex.name,
                                                   qty_added=ex.qty - old, old_qty=old, new_qty=ex.qty,
                                                   old_cost=ex.cost, new_cost=ex.cost, old_price=ex.price,
                                                   new_price=ex.price, note="Imported from Excel"))
                        updated += 1
                    else:
                        lim = check_limits("items")
                        if lim:
                            flash(lim, "error")
                            break
                        it = Item(shop_id=shop.id, name=p["name"], category=p["category"] or "Others",
                                  qty=p["qty"] or 0, cost=p["cost"] or 0, price=p["price"] or 0,
                                  barcode=p["barcode"], supplier=p["supplier"], expiry=exp, low_limit=p["low"])
                        db.session.add(it)
                        db.session.flush()
                        added += 1
                log("Imported items", f"{added} new, {updated} updated")
                db.session.commit()
                flash(f"{added} new item(s) added, {updated} updated.", "ok")
                return redirect(url_for("items"))
            else:
                skip = bool(request.form.get("skip_dups"))
                reduce_ = bool(request.form.get("reduce_stock"))
                add_debt = bool(request.form.get("add_debt"))
                groups, order = {}, []
                for p in rows:
                    if p["err"] or (skip and p["dup"]):
                        continue
                    key = (p["date"], p["receipt"]) if p["receipt"] else ("row", p["row"])
                    if key not in groups:
                        groups[key], _ = [], order.append(key)
                    groups[key].append(p)
                n = total = 0
                for key in order:
                    g_ = groups[key]
                    sub = round(sum(p["qty"] * p["price"] for p in g_), 2)
                    pay, cust = g_[0]["payment"], g_[0]["customer"]
                    s = Sale(shop_id=shop.id, number=next_number(shop.id),
                             ts=dt.datetime.fromisoformat(g_[0]["date"]).replace(hour=12), user_id=current_user.id,
                             user_name=f"{user} (old record)", subtotal=sub, total=sub,
                             cost_total=round(sum(p["qty"] * p["cost"] for p in g_), 2), payment=pay,
                             tendered=sub if pay != "Credit" else 0, owed=sub if pay == "Credit" else 0,
                             customer=cust, source="imported")
                    db.session.add(s)
                    for p in g_:
                        s.items.append(SaleItem(item_id=p["item_id"], name=p["item"], qty=p["qty"], price=p["price"],
                                                cost=p["cost"]))
                        if reduce_ and p["item_id"]:
                            it = db.session.get(Item, p["item_id"])
                            if it and it.shop_id == shop.id:
                                it.qty = max(it.qty - p["qty"], 0)
                    db.session.flush()
                    if pay == "Credit" and add_debt:
                        c = shop_q(Customer).filter(func.lower(Customer.name) == cust.lower()).first()
                        if not c:
                            c = Customer(shop_id=shop.id, name=cust, balance=0)
                            db.session.add(c)
                            db.session.flush()
                        c.balance = round(c.balance + sub, 2)
                        db.session.add(DebtTx(shop_id=shop.id, customer_id=c.id, user_name=user, kind="credit",
                                              amount=sub, sale_id=s.id, note="Old credit sale from book"))
                    n += 1
                    total += sub
                log("Imported old sales", f"{n} sale(s), {total:,.2f}")
                db.session.commit()
                flash(f"{n} old sale(s) imported, total {shop.currency} {total:,.2f}.", "ok")
                return redirect(url_for("sales", p="all"))
        fs = request.files.get("file")
        if not fs or not fs.filename:
            flash("Choose a file first.", "error")
        else:
            try:
                heads, rows = read_table(fs)
                preview = parse_items(heads, rows) if kind == "items" else parse_sales(heads, rows)
                token = secrets.token_hex(12)
                with open(os.path.join(TMP_DIR, f"imp_{shop.id}_{token}.json"), "w") as fh:
                    json.dump(dict(kind=kind, rows=preview), fh)
            except Exception as e:
                flash(f"Could not read the file: {e}", "error")
                preview = None
    return render_template("import.html", preview=preview, kind=kind, token=token)


@app.route("/import/template/<kind>")
@perm_required("import_data")
def import_template(kind):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    if kind == "items":
        ws.append(["Item Name", "Category", "Quantity", "Cost Price", "Selling Price", "Barcode", "Supplier",
                   "Expiry Date (DD/MM/YYYY)", "Low Stock Alert"])
        ws.append(["Curtain Blue Velvet", "Curtains", 20, 45, 70, "", "", "", ""])
        ws.append(["Pampers Size 4", "Pampers", 12, 80, 105, "PS4", "", "31/12/2027", 5])
    else:
        ws.append(["Date (DD/MM/YYYY)", "Item Name", "Quantity", "Selling Price", "Total Amount", "Cost Price",
                   "Payment (Cash/MoMo/Credit)", "Customer", "Receipt No"])
        ws.append(["01/09/2026", "Curtain Blue Velvet", 2, 70, "", "", "Cash", "", ""])
        ws.append(["02/09/2026", "Sales (from book)", 1, "", 850, "", "Cash", "", ""])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="24326E")
    for col in "ABCDEFGHI":
        ws.column_dimensions[col].width = 20
    bio = io.BytesIO()
    wb.save(bio)
    return Response(bio.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={kind}_template.xlsx"})


# ==========================================================================
# Settings, staff, activity, notifications (owner)
# ==========================================================================
@app.route("/settings", methods=["GET", "POST"])
@owner_required
def settings():
    shop = current_user.shop
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip():
            flash("Shop name cannot be empty.", "error")
            return redirect(url_for("settings"))
        shop.name = f["name"].strip()[:120]
        for k in ("phone", "address", "email", "receipt_footer"):
            setattr(shop, k, f.get(k, "").strip()[:200])
        shop.currency = (f.get("currency") or "GH\u20b5")[:10]
        shop.paper = "80mm" if f.get("paper") == "80mm" else "58mm"
        shop.low_stock = max(to_int(f.get("low_stock")) or 0, 0)
        shop.expiry_days = max(to_int(f.get("expiry_days")) or 30, 0)
        if re.match(r"^#[0-9a-fA-F]{6}$", f.get("accent", "")):
            shop.accent = f["accent"]
        pays = [p for p in ALL_PAYMENTS if f.get("pay_" + p)]
        shop.payments = ",".join(pays or ["Cash"])
        shop.notify = json.dumps({
            "each_sale": bool(f.get("n_each_sale")), "big_sale": to_float(f.get("n_big_sale")) or 0,
            "low_stock": bool(f.get("n_low_stock")), "cancel": bool(f.get("n_cancel")),
            "staff_login": bool(f.get("n_staff_login")), "daily": bool(f.get("n_daily"))})
        try:
            logo = save_upload(request.files.get("logo"), "logo")
            if logo:
                shop.logo = logo
        except ValueError as e:
            flash(str(e), "error")
        if f.get("remove_logo"):
            shop.logo = ""
        log("Changed settings")
        db.session.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", ALL_PAYMENTS=ALL_PAYMENTS)


def build_backup(shop_id):
    """All data of one shop as an Excel file (bytes)."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(["Item Name", "Category", "Quantity", "Cost Price", "Selling Price", "Barcode", "Supplier", "Expiry"])
    for i in Item.query.filter_by(shop_id=shop_id).order_by(Item.name):
        ws.append([i.name, i.category, i.qty, i.cost, i.price, i.barcode, i.supplier, fmt_d(i.expiry)])
    ws = wb.create_sheet("Sales")
    ws.append(["Receipt", "Date", "Cashier", "Customer", "Payment", "Items", "Total", "Cost", "Cancelled"])
    for s in Sale.query.filter_by(shop_id=shop_id).order_by(Sale.ts):
        ws.append([s.number, fmt_dt(s.ts), s.user_name, s.customer, s.payment,
                   "; ".join(f"{x.qty}x {x.name} @ {x.price}" for x in s.items), s.total, s.cost_total,
                   "YES" if s.voided else ""])
    ws = wb.create_sheet("Debts")
    ws.append(["Customer", "Phone", "Owes"])
    for c in Customer.query.filter_by(shop_id=shop_id).order_by(Customer.name):
        ws.append([c.name, c.phone, c.balance])
    ws = wb.create_sheet("Restocks")
    ws.append(["Date", "Item", "Added", "Now", "Cost", "By", "Note"])
    for r in Restock.query.filter_by(shop_id=shop_id).order_by(Restock.ts):
        ws.append([fmt_dt(r.ts), r.name, r.qty_added, r.new_qty, r.new_cost, r.user_name, r.note])
    ws = wb.create_sheet("Staff")
    ws.append(["Name", "Username", "Phone", "Email", "Role", "Created"])
    for u in User.query.filter_by(shop_id=shop_id):
        ws.append([u.name, u.username, u.phone, u.email, u.title, fmt_dt(u.created)])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


@app.route("/settings/backup")
@owner_required
def backup_download():
    data = build_backup(sid())
    log("Downloaded backup")
    db.session.commit()
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=backup_{today().isoformat()}.xlsx"})


@app.route("/staff")
@owner_required
def staff():
    users = User.query.filter_by(shop_id=sid()).order_by(User.role.desc(), User.name).all()
    return render_template("staff.html", users=users)


@app.route("/staff/new", methods=["GET", "POST"])
@app.route("/staff/<int:uid>", methods=["GET", "POST"])
@owner_required
def staff_form(uid=None):
    u = get_or_404(User, uid) if uid else None
    if u and u.role == "owner" and u.id != current_user.id:
        abort(403)
    if not u:
        lim = check_limits("users")
        if lim:
            flash(lim, "error")
            return redirect(url_for("staff"))
    f = request.form
    if request.method == "POST":
        if f.get("action") == "delete" and u and u.role == "staff":
            log("Deleted staff", u.username)
            PushSub.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
            db.session.commit()
            flash("Staff removed. Their past sales stay in the reports.", "ok")
            return redirect(url_for("staff"))
        username = f.get("username", "").strip().lower()
        err = None
        if not f.get("name", "").strip():
            err = "Type the staff member's name."
        elif not re.match(r"^[a-z0-9._-]{3,40}$", username):
            err = "Username: 3-40 letters/numbers, no spaces (e.g. ama.mamabea)."
        elif User.query.filter(func.lower(User.username) == username, User.id != (u.id if u else 0)).first():
            err = "That username is taken. Try adding your shop name, e.g. ama.mamabea"
        elif not u and len(f.get("password", "")) < 6:
            err = "Password must be at least 6 characters."
        elif u and f.get("password") and len(f["password"]) < 6:
            err = "New password must be at least 6 characters."
        if err:
            flash(err, "error")
            return render_template("staff_form.html", u=u, f=f)
        if not u:
            u = User(shop_id=sid(), role="staff")
            db.session.add(u)
        u.name, u.username, u.phone = f["name"].strip(), username, f.get("phone", "").strip()
        if u.role == "staff":
            u.title = f.get("title", "Cashier").strip()[:40] or "Cashier"
            u.perms = json.dumps([p for p in PERM_KEYS if f.get("perm_" + p)])
            u.active = bool(f.get("active"))
        if f.get("password"):
            u.set_password(f["password"])
        log("Saved staff", f"{u.username} ({u.title})")
        db.session.commit()
        flash(f"Saved {u.name}. They log in with username '{u.username}'.", "ok")
        return redirect(url_for("staff"))
    return render_template("staff_form.html", u=u, f={})


@app.route("/activity")
@owner_required
def activity():
    q = request.args.get("q", "")
    rows = [a for a in shop_q(Activity).order_by(Activity.id.desc()).limit(1000).all()
            if text_match(q, a.user_name, a.action, a.detail)][:500]
    return render_template("activity.html", rows=rows, q=q)


@app.route("/notifications")
@perm_required("alerts")
def notifications():
    rows = shop_q(Notification).order_by(Notification.id.desc()).limit(200).all()
    shop_q(Notification).filter_by(read=False).update({"read": True})
    db.session.commit()
    return render_template("notifications.html", rows=rows)


@app.route("/api/notifications/read", methods=["POST"])
@shop_member
def notif_read():
    shop_q(Notification).filter_by(read=False).update({"read": True})
    db.session.commit()
    return jsonify(ok=True)


@app.route("/push/subscribe", methods=["POST"])
@shop_member
def push_subscribe():
    d = request.get_json(silent=True) or {}
    ep = (d.get("endpoint") or "")[:600]
    if not ep:
        return jsonify(ok=False), 400
    s = PushSub.query.filter_by(endpoint=ep).first() or PushSub(endpoint=ep)
    s.user_id, s.shop_id, s.data = current_user.id, sid(), json.dumps(d)
    db.session.add(s)
    db.session.commit()
    notify(sid(), "Notifications are ON", f"This device will now get alerts for {current_user.shop.name}.", "info")
    return jsonify(ok=True)


# ==========================================================================
# Billing (Paystack: MoMo + card in Ghana) - shop owner
# ==========================================================================
@app.route("/billing")
@owner_required
def billing():
    plans = Plan.query.filter_by(active=True).order_by(Plan.sort, Plan.price).all()
    pays = Payment.query.filter_by(shop_id=sid()).order_by(Payment.id.desc()).limit(50).all()
    return render_template("billing.html", plans=plans, pays=pays, paystack=bool(PAYSTACK_SECRET))


@app.route("/billing/pay/<int:plan_id>", methods=["POST"])
@owner_required
def billing_pay(plan_id):
    plan = db.session.get(Plan, plan_id)
    if not plan or not plan.active:
        abort(404)
    if not PAYSTACK_SECRET:
        flash("Online payment is not set up yet. Use the MoMo instructions below.", "error")
        return redirect(url_for("billing"))
    shop = current_user.shop
    ref = f"SSM-{shop.id}-{uuid.uuid4().hex[:10]}"
    db.session.add(Payment(shop_id=shop.id, plan_id=plan.id, amount=plan.price, reference=ref))
    db.session.commit()
    try:
        r = requests.post("https://api.paystack.co/transaction/initialize", timeout=20,
                          headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"},
                          json=dict(email=current_user.email or shop.email or f"shop{shop.id}@example.com",
                                    amount=int(round(plan.price * 100)), currency="GHS", reference=ref,
                                    callback_url=BASE_URL + url_for("billing_callback"),
                                    metadata=dict(shop_id=shop.id, plan_id=plan.id))).json()
        if r.get("status"):
            return redirect(r["data"]["authorization_url"])
        flash("Payment could not start: " + str(r.get("message")), "error")
    except Exception as e:
        flash(f"Payment could not start: {e}", "error")
    return redirect(url_for("billing"))


def confirm_payment(ref):
    p = Payment.query.filter_by(reference=ref).first()
    if not p or p.status == "success":
        return p
    r = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", timeout=20,
                     headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"}).json()
    data = r.get("data") or {}
    if r.get("status") and data.get("status") == "success" and int(data.get("amount", 0)) >= int(round(p.amount * 100)):
        p.status = "success"
        shop, plan = db.session.get(Shop, p.shop_id), db.session.get(Plan, p.plan_id)
        start_subscription(shop, plan, note=f"(paid {ref})")
        db.session.commit()
        notify(shop.id, "Payment received - thank you!", f"{plan.name} active until {fmt_d(shop.sub_ends)}.",
               "info", "/billing")
    elif data.get("status") in ("failed", "abandoned"):
        p.status = data["status"]
        db.session.commit()
    return p


@app.route("/billing/callback")
@login_required
def billing_callback():
    ref = request.args.get("reference", "")
    p = confirm_payment(ref) if ref and PAYSTACK_SECRET else None
    if p and p.status == "success":
        flash("Payment successful. Your subscription is active.", "ok")
    else:
        flash("We could not confirm the payment yet. If money left your account, contact support.", "error")
    return redirect(url_for("billing"))


@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    sig = request.headers.get("X-Paystack-Signature", "")
    calc = hmac.new(PAYSTACK_SECRET.encode(), request.get_data(), hashlib.sha512).hexdigest()
    if not PAYSTACK_SECRET or not hmac.compare_digest(sig, calc):
        abort(401)
    ev = request.get_json(silent=True) or {}
    if ev.get("event") == "charge.success":
        confirm_payment((ev.get("data") or {}).get("reference", ""))
    return "ok"


# ==========================================================================
# Platform owner (you) - super admin
# ==========================================================================
@app.route("/super")
@super_required
def super_home():
    shops = Shop.query.order_by(Shop.id.desc()).all()
    t0, t1 = period_range("today")
    m0, m1 = period_range("month")
    info = []
    for s in shops:
        owner = User.query.filter_by(shop_id=s.id, role="owner").first()
        last = db.session.query(func.max(User.last_seen)).filter(User.shop_id == s.id).scalar()
        idle = (now() - last).days if last else (now() - s.created).days
        info.append(dict(s=s, owner=owner, last=last, idle=idle, users=User.query.filter_by(shop_id=s.id).count(),
                         items=Item.query.filter_by(shop_id=s.id).count(),
                         sales_today=Sale.query.filter(Sale.shop_id == s.id, Sale.ts >= t0, Sale.ts < t1).count()))
    revenue = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.status == "success", Payment.ts >= m0, Payment.ts < m1).scalar()
    stats = dict(shops=len(shops), live=sum(1 for s in shops if s.is_live and s.status == "active"),
                 trial=sum(1 for s in shops if s.status == "trial" and s.is_live),
                 expired=sum(1 for s in shops if not s.is_live), revenue=revenue)
    return render_template("super_home.html", info=info, stats=stats, plans=Plan.query.order_by(Plan.sort).all())


@app.route("/super/shop/<int:shop_id>", methods=["POST"])
@super_required
def super_shop(shop_id):
    s = db.session.get(Shop, shop_id) or abort(404)
    f = request.form
    act = f.get("action")
    if act == "extend":
        days = to_int(f.get("days")) or 30
        plan = db.session.get(Plan, to_int(f.get("plan_id")) or 0) or s.plan
        start_subscription(s, plan, days=days, note="(manual by platform)")
        amt = to_float(f.get("amount"))
        if amt:
            db.session.add(Payment(shop_id=s.id, plan_id=plan.id if plan else None, amount=amt, status="success",
                                   provider="manual", reference=f"MAN-{uuid.uuid4().hex[:10]}",
                                   note=f.get("note", "")[:200]))
        flash(f"{s.name}: active until {fmt_d(s.sub_ends)}.", "ok")
    elif act == "suspend":
        s.status = "suspended"
        flash(f"{s.name} suspended.", "ok")
    elif act == "unsuspend":
        s.status = "active" if s.plan_id else "trial"
        flash(f"{s.name} re-activated.", "ok")
    elif act == "reset_owner":
        o = User.query.filter_by(shop_id=s.id, role="owner").first()
        pw = secrets.token_urlsafe(6)
        o.set_password(pw)
        flash(f"New password for {o.username}: {pw}  (tell the owner to change it)", "ok")
    db.session.commit()
    return redirect(url_for("super_home"))


@app.route("/super/shop/<int:shop_id>/export")
@super_required
def super_shop_export(shop_id):
    s = db.session.get(Shop, shop_id) or abort(404)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", s.name).strip("_") or f"shop{s.id}"
    return Response(build_backup(s.id), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={safe}_data_{today().isoformat()}.xlsx"})


def delete_shop_everything(shop):
    """Permanently removes a shop, its staff and all its data. Payment records are kept for your accounts."""
    files = [shop.logo] + [i.image for i in Item.query.filter_by(shop_id=shop.id).all()]
    sale_ids = db.session.query(Sale.id).filter(Sale.shop_id == shop.id)
    SaleItem.query.filter(SaleItem.sale_id.in_(sale_ids)).delete(synchronize_session=False)
    for model in (Sale, Item, Restock, DebtTx, Customer, Activity, Notification, PushSub, User):
        model.query.filter_by(shop_id=shop.id).delete(synchronize_session=False)
    for p in Payment.query.filter_by(shop_id=shop.id).all():
        p.note = (f"[deleted shop: {shop.name}] " + (p.note or ""))[:200]
    name = shop.name
    db.session.delete(shop)
    db.session.commit()
    for f in files:
        if f:
            try:
                os.remove(os.path.join(UPLOAD_DIR, os.path.basename(f)))
            except OSError:
                pass
    for f in os.listdir(TMP_DIR):
        if f.startswith(f"imp_{shop.id}_"):
            try:
                os.remove(os.path.join(TMP_DIR, f))
            except OSError:
                pass
    return name


@app.route("/super/shop/<int:shop_id>/delete", methods=["POST"])
@super_required
def super_shop_delete(shop_id):
    s = db.session.get(Shop, shop_id) or abort(404)
    f = request.form
    if not current_user.check_password(f.get("password", "")):
        flash("Your password is not correct. Nothing was deleted.", "error")
    elif f.get("confirm_name", "").strip().lower() != s.name.strip().lower():
        flash(f"The shop name you typed does not match '{s.name}'. Nothing was deleted.", "error")
    else:
        name = delete_shop_everything(s)
        flash(f"'{name}' and all its data were deleted permanently.", "ok")
    return redirect(url_for("super_home"))


@app.route("/super/plans", methods=["POST"])
@super_required
def super_plans():
    f = request.form
    pid = to_int(f.get("id"))
    p = db.session.get(Plan, pid) if pid else Plan()
    if f.get("action") == "delete" and pid:
        p.active = False
    else:
        p.name = f.get("name", "Plan").strip()[:60]
        p.price = to_float(f.get("price")) or 0
        p.days = to_int(f.get("days")) or 30
        p.max_users = to_int(f.get("max_users")) or 0
        p.max_items = to_int(f.get("max_items")) or 0
        p.features = f.get("features", "")[:500]
        p.sort = to_int(f.get("sort")) or 0
        p.active = bool(f.get("active"))
        db.session.add(p)
    db.session.commit()
    flash("Plans saved.", "ok")
    return redirect(url_for("super_home") + "#plans")


@app.route("/super/settings", methods=["POST"])
@super_required
def super_settings():
    for k in PLATFORM_DEFAULTS:
        if k in request.form:
            r = db.session.get(PlatformSetting, k) or PlatformSetting(key=k)
            r.value = request.form[k].strip()
            db.session.add(r)
    db.session.commit()
    flash("Platform settings saved.", "ok")
    return redirect(url_for("super_home") + "#settings")


# ==========================================================================
# Daily summary (call from a scheduler / cron once a day, e.g. 9 PM)
# ==========================================================================
@app.route("/cron/daily")
def cron_daily():
    if not CRON_KEY or not hmac.compare_digest(request.args.get("key", ""), CRON_KEY):
        abort(404)
    n = 0
    s, e = period_range("today")
    for shop in Shop.query.all():
        if not shop.is_live:
            if shop.sub_ends and (now() - shop.sub_ends).days == 0:
                notify(shop.id, "Subscription ended", "Renew in Billing to keep selling.", "warning", "/billing")
            continue
        if shop.days_left in (3, 1):
            notify(shop.id, f"Subscription ends in {shop.days_left} day(s)", "Renew in Billing.", "warning",
                   "/billing")
        if shop.notify_cfg.get("daily"):
            sm = sales_summary(shop.id, s, e)
            low = sum(1 for i in Item.query.filter_by(shop_id=shop.id).all() if is_low(i, shop))
            notify(shop.id, f"Today: {shop.currency} {sm['total']:,.2f} from {sm['n']} sale(s)",
                   f"Profit {shop.currency} {sm['profit']:,.2f}. {low} item(s) low on stock.", "daily", "/sales")
            n += 1
    return jsonify(ok=True, shops=n)


# ==========================================================================
# Installable app (PWA)
# ==========================================================================
@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": APP_NAME, "short_name": "Smart Shop", "start_url": "/dashboard", "scope": "/",
        "display": "standalone", "background_color": "#f5f2ea", "theme_color": "#24326e",
        "description": "Sell, track stock and watch your shop from anywhere.",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "maskable"}]})


@app.route("/sw.js")
def service_worker():
    r = send_from_directory(BASE_DIR, "sw.js")
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.errorhandler(404)
def e404(e):
    return render_template("error.html", code=404, msg="That page was not found."), 404


@app.errorhandler(403)
def e403(e):
    return render_template("error.html", code=403, msg="You are not allowed to open this."), 403


@app.errorhandler(413)
def e413(e):
    flash("That file is too big (max 10 MB).", "error")
    return redirect(request.referrer or url_for("index"))


# ==========================================================================
# Setup commands
# ==========================================================================
def init_db():
    db.create_all()
    if not Plan.query.first():
        db.session.add_all([
            Plan(name="Starter", price=79, days=30, max_users=2, max_items=300, sort=1,
                 features="1 owner + 1 cashier\nUp to 300 items\nPOS, stock, debt book\nPhone notifications"),
            Plan(name="Business", price=149, days=30, max_users=6, max_items=0, sort=2,
                 features="Up to 6 staff\nUnlimited items\nReports & profit\nExcel import\nPriority support"),
            Plan(name="Business Yearly", price=1490, days=365, max_users=6, max_items=0, sort=3,
                 features="Everything in Business\n2 months free"),
        ])
    email = (os.environ.get("SUPERADMIN_EMAIL") or "").strip().lower()
    pw = os.environ.get("SUPERADMIN_PASSWORD") or ""
    if email and pw:
        u = User.query.filter_by(role="superadmin").first()
        if not u:
            u = User(username="platform", email=email, name="Platform owner", role="superadmin", title="Platform")
            u.set_password(pw)
            db.session.add(u)
        elif u.email != email or not u.check_password(pw):
            u.email = email
            u.set_password(pw)
    db.session.commit()


def safe_init():
    """Several server workers may start at once; only one creates the tables, the others wait and retry."""
    import time
    for attempt in range(6):
        try:
            with app.app_context():
                init_db()
            return
        except Exception:
            with app.app_context():
                db.session.rollback()
            time.sleep(0.5 + attempt)
    with app.app_context():
        init_db()


safe_init()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "genkeys":
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        import base64
        k = ec.generate_private_key(ec.SECP256R1())
        priv = base64.urlsafe_b64encode(k.private_numbers().private_value.to_bytes(32, "big")).decode().rstrip("=")
        pub = base64.urlsafe_b64encode(k.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")
        print("VAPID_PUBLIC_KEY=" + pub)
        print("VAPID_PRIVATE_KEY=" + priv)
        print("SECRET_KEY=" + secrets.token_hex(32))
        print("CRON_KEY=" + secrets.token_hex(16))
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=bool(os.environ.get("DEBUG")))
