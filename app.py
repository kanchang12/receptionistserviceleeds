"""
VoiceBot SaaS Platform
Twilio telephony + Gemini AI backbone
Flask + PostgreSQL + Gunicorn
"""

import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import json
import math
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, Response, abort)
from werkzeug.security import generate_password_hash, check_password_hash

from utils import gemini_service, twilio_service
from twilio.twiml.voice_response import VoiceResponse

# ─── APP CONFIG ────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# ─── DATABASE ──────────────────────────────

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/voicebot')


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def query_db(sql, params=None, one=False, commit=False):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(sql, params)
        if commit:
            conn.commit()
            return cur.rowcount
        if one:
            return cur.fetchone()
        return cur.fetchall()
    except Exception as e:
        print(f"[DB Error] {e}")
        conn.rollback()
        return None if one else []
    finally:
        cur.close()
        conn.close()


def execute_db(sql, params=None):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB Error] {e}")
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def insert_db(sql, params=None):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        conn.commit()
        row_id = cur.fetchone()[0] if cur.description else None
        return row_id
    except Exception as e:
        print(f"[DB Error] {e}")
        conn.rollback()
        return None
    finally:
        cur.close()
        conn.close()


# ─── IN-MEMORY CACHE ──────────────────────
# Stores conversation state during live calls. No external dependency needed.

_mem_store = {}


def cache_set(key, value, ex=3600):
    _mem_store[key] = value


def cache_get(key, as_json=False):
    return _mem_store.get(key)


def cache_incr(key):
    _mem_store[key] = _mem_store.get(key, 0) + 1
    return _mem_store[key]


def cache_decr(key):
    _mem_store[key] = max(0, _mem_store.get(key, 0) - 1)
    return _mem_store[key]


def safe_json(val, default=None):
    """Parse JSON string or return as-is if already parsed by psycopg2."""
    if val is None:
        return default
    if isinstance(val, (dict, list)):
        return val
    return json.loads(val)


# ─── AUTH DECORATORS ───────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


def client_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'client':
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ─── HELPERS ───────────────────────────────

TIER_LIMITS = {
    'starter': {'minutes': 200, 'numbers': 1, 'price': 29},
    'growth': {'minutes': 600, 'numbers': 2, 'price': 79},
    'enterprise': {'minutes': 2000, 'numbers': 5, 'price': 199},
}


def get_business_for_user(user_id):
    return query_db("SELECT * FROM businesses WHERE user_id=%s LIMIT 1", (user_id,), one=True)


def get_business_by_number(phone_number):
    """Look up which business owns this phone number + number-specific config."""
    return query_db("""
        SELECT b.*, p.greeting as number_greeting, p.agent_personality as number_personality,
               p.label as number_label, p.id as phone_number_id
        FROM businesses b
        JOIN phone_numbers p ON p.business_id = b.id
        WHERE p.number = %s AND p.status = 'assigned'
        LIMIT 1
    """, (phone_number,), one=True)


def get_caller_history(business_id, caller_number, limit=2):
    """Get last N calls from this caller to this business — for AI context."""
    rows = query_db("""
        SELECT summary, category, sentiment, created_at, duration_seconds
        FROM calls
        WHERE business_id = %s AND caller_number = %s AND status = 'completed'
        ORDER BY created_at DESC LIMIT %s
    """, (business_id, caller_number, limit))
    if not rows:
        return None
    return [{'date': str(r['created_at']), 'summary': r['summary'],
             'category': r['category'], 'sentiment': r['sentiment'],
             'duration': r['duration_seconds']} for r in rows]


def get_current_month():
    return datetime.now().strftime('%Y-%m')


def get_or_create_usage(business_id):
    month = get_current_month()
    row = query_db("SELECT * FROM minutes_usage WHERE business_id=%s AND month=%s",
                   (business_id, month), one=True)
    if not row:
        biz = query_db("SELECT tier FROM businesses WHERE id=%s", (business_id,), one=True)
        tier = biz['tier'] if biz else 'starter'
        limit = TIER_LIMITS.get(tier, TIER_LIMITS['starter'])['minutes']
        execute_db("INSERT INTO minutes_usage (business_id, month, minutes_limit) VALUES (%s,%s,%s)",
                   (business_id, month, limit))
        row = query_db("SELECT * FROM minutes_usage WHERE business_id=%s AND month=%s",
                       (business_id, month), one=True)
    return row


def add_call_minutes(business_id, seconds):
    """Add minutes to usage, check thresholds, send alerts."""
    month = get_current_month()
    minutes = round(seconds / 60, 2)
    execute_db("UPDATE minutes_usage SET minutes_used = minutes_used + %s WHERE business_id=%s AND month=%s",
               (minutes, business_id, month))
    usage = get_or_create_usage(business_id)
    if not usage:
        return
    pct = (float(usage['minutes_used']) / usage['minutes_limit'] * 100) if usage['minutes_limit'] > 0 else 0
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (business_id,), one=True)
    user = query_db("SELECT * FROM users WHERE id=%s", (biz['user_id'],), one=True) if biz else None
    if pct >= 100 and not usage['alert_100_sent']:
        execute_db("UPDATE minutes_usage SET alert_100_sent=TRUE WHERE business_id=%s AND month=%s", (business_id, month))
        if user and user['phone']:
            twilio_service.send_sms(user['phone'], f"VoiceBot Alert: You've used 100% of your monthly minutes for {biz['name']}. Calls will use overage billing at £0.08/min.")
    elif pct >= 90 and not usage['alert_90_sent']:
        execute_db("UPDATE minutes_usage SET alert_90_sent=TRUE WHERE business_id=%s AND month=%s", (business_id, month))
        if user and user['phone']:
            twilio_service.send_sms(user['phone'], f"VoiceBot Alert: You've used 90% of your monthly minutes for {biz['name']}.")
    elif pct >= 80 and not usage['alert_80_sent']:
        execute_db("UPDATE minutes_usage SET alert_80_sent=TRUE WHERE business_id=%s AND month=%s", (business_id, month))
        if user and user['phone']:
            twilio_service.send_sms(user['phone'], f"VoiceBot Alert: You've used 80% of your monthly minutes for {biz['name']}.")


def is_within_business_hours(business_hours):
    """Check if current time is within business hours config."""
    if not business_hours:
        return True  # no hours set = always open
    if isinstance(business_hours, str):
        try:
            business_hours = json.loads(business_hours)
        except Exception:
            return True
    now = datetime.now()
    day_name = now.strftime('%A').lower()
    today_hours = business_hours.get(day_name)
    if not today_hours or today_hours.get('closed'):
        return False
    try:
        open_time = datetime.strptime(today_hours.get('open', '09:00'), '%H:%M').time()
        close_time = datetime.strptime(today_hours.get('close', '17:00'), '%H:%M').time()
        return open_time <= now.time() <= close_time
    except Exception:
        return True


# ───────────────────────────────────────────
# PUBLIC ROUTES
# ───────────────────────────────────────────

@app.route('/')
def landing():
    return render_template('public/landing.html')


@app.route('/pricing')
def pricing():
    return render_template('public/landing.html', _anchor='pricing')


# ───────────────────────────────────────────
# AUTH ROUTES
# ───────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        business_name = request.form.get('business_name', '').strip()
        business_type = request.form.get('business_type', '').strip()

        if not all([name, email, password, business_name]):
            flash('All fields are required.', 'error')
            return render_template('auth/register.html')

        existing = query_db("SELECT id FROM users WHERE email=%s", (email,), one=True)
        if existing:
            flash('Email already registered.', 'error')
            return render_template('auth/register.html')

        pw_hash = generate_password_hash(password)
        user_id = insert_db(
            "INSERT INTO users (name, email, phone, password_hash, role, status) VALUES (%s,%s,%s,%s,'client','pending') RETURNING id",
            (name, email, phone, pw_hash))

        if user_id:
            biz_id = insert_db(
                "INSERT INTO businesses (user_id, name, business_type, status, tier) VALUES (%s,%s,%s,'pending','starter') RETURNING id",
                (user_id, business_name, business_type))

            session['user_id'] = str(user_id)
            session['role'] = 'client'
            session['business_id'] = str(biz_id) if biz_id else None
            session.permanent = True

            flash('Account created! Complete your onboarding to set up your AI receptionist.', 'success')
            return redirect(url_for('client_dashboard'))

        flash('Registration failed. Try again.', 'error')
    return render_template('auth/register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = query_db("SELECT * FROM users WHERE email=%s", (email,), one=True)
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = str(user['id'])
            session['role'] = user['role']
            session.permanent = True

            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))

            biz = get_business_for_user(user['id'])
            if biz:
                session['business_id'] = str(biz['id'])
            return redirect(url_for('client_dashboard'))

        flash('Invalid email or password.', 'error')
    return render_template('auth/login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('landing'))


# ───────────────────────────────────────────
# CLIENT DASHBOARD
# ───────────────────────────────────────────

@app.route('/dashboard')
@login_required
@client_required
def client_dashboard():
    biz_id = session.get('business_id')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    if not biz:
        flash('No business found.', 'error')
        return redirect(url_for('landing'))

    usage = get_or_create_usage(biz_id)
    recent_calls = query_db(
        "SELECT * FROM calls WHERE business_id=%s ORDER BY created_at DESC LIMIT 10", (biz_id,))
    open_tickets = query_db(
        "SELECT * FROM tickets WHERE business_id=%s AND status IN ('open','in_progress') ORDER BY created_at DESC LIMIT 10", (biz_id,))
    phone_numbers = query_db("SELECT * FROM phone_numbers WHERE business_id=%s AND status='assigned'", (biz_id,))

    # Stats
    today = datetime.now().date()
    today_calls = query_db(
        "SELECT COUNT(*) as cnt FROM calls WHERE business_id=%s AND created_at::date=%s",
        (biz_id, today), one=True)
    active_calls = int(cache_get(f"active_calls:{biz_id}") or 0)
    total_calls = query_db(
        "SELECT COUNT(*) as cnt FROM calls WHERE business_id=%s", (biz_id,), one=True)

    # Category breakdown
    categories = query_db(
        "SELECT category, COUNT(*) as cnt FROM calls WHERE business_id=%s GROUP BY category ORDER BY cnt DESC",
        (biz_id,))

    # Sentiment breakdown
    sentiments = query_db(
        "SELECT sentiment, COUNT(*) as cnt FROM calls WHERE business_id=%s GROUP BY sentiment ORDER BY cnt DESC",
        (biz_id,))

    # Status breakdown
    statuses = query_db(
        "SELECT status, COUNT(*) as cnt FROM calls WHERE business_id=%s GROUP BY status ORDER BY cnt DESC",
        (biz_id,))

    # Calls per day (last 7 days)
    daily_calls = query_db("""
        SELECT created_at::date as day, COUNT(*) as cnt
        FROM calls WHERE business_id=%s AND created_at > NOW() - INTERVAL '7 days'
        GROUP BY created_at::date ORDER BY day
    """, (biz_id,))

    # Avg duration
    avg_duration = query_db(
        "SELECT AVG(duration_seconds) as avg_dur FROM calls WHERE business_id=%s AND status='completed'",
        (biz_id,), one=True)

    return render_template('client/dashboard.html',
                           business=biz, usage=usage, calls=recent_calls,
                           tickets=open_tickets, phone_numbers=phone_numbers,
                           today_calls=today_calls['cnt'] if today_calls else 0,
                           total_calls=total_calls['cnt'] if total_calls else 0,
                           active_calls=active_calls, tier_limits=TIER_LIMITS,
                           categories=categories or [], sentiments=sentiments or [],
                           statuses=statuses or [], daily_calls=daily_calls or [],
                           avg_duration=avg_duration['avg_dur'] if avg_duration and avg_duration['avg_dur'] else 0)


@app.route('/dashboard/calls')
@login_required
@client_required
def client_calls():
    biz_id = session.get('business_id')
    page = int(request.args.get('page', 1))
    per_page = 20
    offset = (page - 1) * per_page

    total = query_db("SELECT COUNT(*) as cnt FROM calls WHERE business_id=%s", (biz_id,), one=True)
    total_count = total['cnt'] if total else 0
    total_pages = max(1, math.ceil(total_count / per_page))

    calls = query_db(
        "SELECT * FROM calls WHERE business_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (biz_id, per_page, offset))

    return render_template('client/calls.html', calls=calls,
                           page=page, total_pages=total_pages, total_count=total_count)


@app.route('/dashboard/calls/<call_id>')
@login_required
@client_required
def client_call_detail(call_id):
    biz_id = session.get('business_id')
    call = query_db("SELECT * FROM calls WHERE id=%s AND business_id=%s",
                    (call_id, biz_id), one=True)
    if not call:
        abort(404)
    ticket = query_db("SELECT * FROM tickets WHERE call_id=%s", (call_id,), one=True)
    return render_template('client/call_detail.html', call=call, ticket=ticket)


@app.route('/dashboard/tickets')
@login_required
@client_required
def client_tickets():
    biz_id = session.get('business_id')
    status_filter = request.args.get('status', 'all')
    if status_filter != 'all':
        tickets = query_db(
            "SELECT t.*, c.caller_number as call_from FROM tickets t LEFT JOIN calls c ON t.call_id=c.id WHERE t.business_id=%s AND t.status=%s ORDER BY t.created_at DESC",
            (biz_id, status_filter))
    else:
        tickets = query_db(
            "SELECT t.*, c.caller_number as call_from FROM tickets t LEFT JOIN calls c ON t.call_id=c.id WHERE t.business_id=%s ORDER BY t.created_at DESC",
            (biz_id,))
    return render_template('client/tickets.html', tickets=tickets, status_filter=status_filter)


@app.route('/dashboard/tickets/<ticket_id>/update', methods=['POST', 'GET'])
@login_required
@client_required
def client_update_ticket(ticket_id):
    biz_id = session.get('business_id')
    new_status = request.form.get('status')
    notes = request.form.get('notes', '')
    execute_db("UPDATE tickets SET status=%s, notes=%s, updated_at=NOW() WHERE id=%s AND business_id=%s",
               (new_status, notes, ticket_id, biz_id))
    flash('Ticket updated.', 'success')
    return redirect(url_for('client_tickets'))


@app.route('/dashboard/settings', methods=['GET', 'POST'])
@login_required
@client_required
def client_settings():
    biz_id = session.get('business_id')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)

    if request.method == 'POST':
        greeting = request.form.get('greeting', '')
        agent_personality = request.form.get('agent_personality', '')
        after_hours = request.form.get('after_hours_message', '')
        transfer_number = request.form.get('transfer_number', '')
        restricted_info = request.form.get('restricted_info', '')

        # Parse business hours from form
        hours = {}
        for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']:
            if request.form.get(f'{day}_closed'):
                hours[day] = {'closed': True}
            else:
                hours[day] = {
                    'open': request.form.get(f'{day}_open', '09:00'),
                    'close': request.form.get(f'{day}_close', '17:00')
                }

        execute_db("""UPDATE businesses SET greeting=%s, agent_personality=%s, after_hours_message=%s,
                      transfer_number=%s, restricted_info=%s, business_hours=%s, updated_at=NOW()
                      WHERE id=%s""",
                   (greeting, agent_personality, after_hours, transfer_number,
                    restricted_info, json.dumps(hours), biz_id))
        flash('Settings saved.', 'success')
        return redirect(url_for('client_settings'))

    kb = query_db("SELECT * FROM knowledge_base WHERE business_id=%s AND active=TRUE ORDER BY created_at", (biz_id,))
    return render_template('client/settings.html', business=biz, knowledge_base=kb)


@app.route('/dashboard/knowledge-base/add', methods=['POST', 'GET'])
@login_required
@client_required
def client_add_kb():
    biz_id = session.get('business_id')
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    doc_type = request.form.get('doc_type', 'faq')
    if title and content:
        execute_db("INSERT INTO knowledge_base (business_id, title, content, doc_type) VALUES (%s,%s,%s,%s)",
                   (biz_id, title, content, doc_type))
        flash('Knowledge base entry added.', 'success')
    return redirect(url_for('client_settings'))


@app.route('/dashboard/knowledge-base/<kb_id>/delete', methods=['POST', 'GET'])
@login_required
@client_required
def client_delete_kb(kb_id):
    biz_id = session.get('business_id')
    execute_db("UPDATE knowledge_base SET active=FALSE WHERE id=%s AND business_id=%s", (kb_id, biz_id))
    flash('Entry removed.', 'success')
    return redirect(url_for('client_settings'))


@app.route('/dashboard/billing')
@login_required
@client_required
def client_billing():
    biz_id = session.get('business_id')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    usage = get_or_create_usage(biz_id)
    invoices = query_db("SELECT * FROM invoices WHERE business_id=%s ORDER BY created_at DESC", (biz_id,))
    return render_template('client/billing.html', business=biz, usage=usage,
                           invoices=invoices, tier_limits=TIER_LIMITS)


@app.route('/dashboard/onboarding')
@login_required
@client_required
def client_onboarding():
    biz_id = session.get('business_id')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    return render_template('client/onboarding.html', business=biz)


@app.route('/dashboard/onboarding/start', methods=['POST', 'GET'])
@login_required
@client_required
def client_start_onboarding():
    """Trigger outbound onboarding call to the business owner."""
    biz_id = session.get('business_id')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    user = query_db("SELECT * FROM users WHERE id=%s", (session['user_id'],), one=True)

    if not user or not user['phone']:
        flash('Please add a phone number to your account first.', 'error')
        return redirect(url_for('client_settings'))

    # Generate questions
    questions = gemini_service.get_onboarding_questions(biz['name'], biz['business_type'])

    # Create onboarding record
    ob_id = insert_db(
        "INSERT INTO onboarding_calls (business_id, questions_asked, status) VALUES (%s,%s,'pending') RETURNING id",
        (biz_id, json.dumps(questions)))

    # Cache questions for webhook access
    cache_set(f"onboarding_q:{ob_id}", questions, ex=3600)

    # Make outbound call
    try:
        client = twilio_service.get_client()
        from_number = os.environ.get('TWILIO_FROM_NUMBER', '').strip()
        if not from_number:
            flash('TWILIO_FROM_NUMBER environment variable is not set. Please set it to your Twilio phone number (E.164 format, e.g. +447700900123).', 'error')
            return redirect(url_for('client_onboarding'))
        call = client.calls.create(
            to=user['phone'],
            from_=from_number,
            url=f"{os.environ.get('APP_BASE_URL')}/webhook/onboarding-start?business_id={biz_id}&onboarding_id={ob_id}",
            method='POST',
            record=True,
            status_callback=f"{os.environ.get('APP_BASE_URL')}/webhook/onboarding-status?onboarding_id={ob_id}",
        )
        execute_db("UPDATE onboarding_calls SET twilio_call_sid=%s, status='in_progress' WHERE id=%s",
                   (call.sid, ob_id))
        execute_db("UPDATE users SET status='onboarding' WHERE id=%s", (session['user_id'],))
        flash('Onboarding call initiated! Answer your phone.', 'success')
    except Exception as e:
        print(f"[Onboarding Call Error] {e}")
        flash(f'Could not start call: {str(e)}', 'error')

    return redirect(url_for('client_onboarding'))


# ───────────────────────────────────────────
# ADMIN DASHBOARD
# ───────────────────────────────────────────

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    total_clients = query_db("SELECT COUNT(*) as cnt FROM users WHERE role='client'", one=True)
    pending = query_db("SELECT COUNT(*) as cnt FROM users WHERE status='ready_for_review'", one=True)
    active = query_db("SELECT COUNT(*) as cnt FROM users WHERE status='active' AND role='client'", one=True)
    today = datetime.now().date()
    today_calls = query_db("SELECT COUNT(*) as cnt FROM calls WHERE created_at::date=%s", (today,), one=True)
    recent_calls = query_db("SELECT c.*, b.name as business_name FROM calls c JOIN businesses b ON c.business_id=b.id ORDER BY c.created_at DESC LIMIT 20")
    pending_clients = query_db("""
        SELECT u.*, b.name as business_name, b.business_type, b.id as biz_id
        FROM users u JOIN businesses b ON b.user_id=u.id
        WHERE u.status IN ('pending', 'ready_for_review') ORDER BY u.created_at DESC
    """)
    onboarding_calls = query_db("""
        SELECT oc.*, b.name as business_name, u.name as user_name, u.email as user_email
        FROM onboarding_calls oc
        LEFT JOIN businesses b ON oc.business_id=b.id
        LEFT JOIN users u ON b.user_id=u.id
        ORDER BY oc.created_at DESC LIMIT 20
    """)
    all_users = query_db("SELECT * FROM users ORDER BY created_at DESC LIMIT 50")
    phone_numbers = query_db("""
        SELECT p.*, b.name as business_name FROM phone_numbers p
        LEFT JOIN businesses b ON p.business_id=b.id ORDER BY p.created_at DESC
    """)
    businesses = query_db("SELECT id, name FROM businesses ORDER BY name")

    return render_template('admin/dashboard.html',
                           total_clients=total_clients['cnt'] if total_clients else 0,
                           pending_count=pending['cnt'] if pending else 0,
                           active_count=active['cnt'] if active else 0,
                           today_calls=today_calls['cnt'] if today_calls else 0,
                           recent_calls=recent_calls, pending_clients=pending_clients,
                           onboarding_calls=onboarding_calls, all_users=all_users,
                           phone_numbers=phone_numbers, businesses=businesses)


@app.route('/admin/clients/<user_id>/activate', methods=['POST', 'GET'])
@login_required
@admin_required
def admin_activate_client(user_id):
    execute_db("UPDATE users SET status='active' WHERE id=%s", (user_id,))
    biz = query_db("SELECT id FROM businesses WHERE user_id=%s", (user_id,), one=True)
    if biz:
        execute_db("UPDATE businesses SET status='active' WHERE id=%s", (biz['id'],))
    flash('Client activated.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/clients/create', methods=['POST'])
@login_required
@admin_required
def admin_create_client():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '').strip()
    business_name = request.form.get('business_name', '').strip()
    business_type = request.form.get('business_type', '').strip()
    tier = request.form.get('tier', 'starter')
    status = request.form.get('status', 'active')

    if not all([name, email, password, business_name]):
        flash('Name, email, password and business name are required.', 'error')
        return redirect(url_for('admin_dashboard'))

    existing = query_db("SELECT id FROM users WHERE email=%s", (email,), one=True)
    if existing:
        flash('Email already registered.', 'error')
        return redirect(url_for('admin_dashboard'))

    pw_hash = generate_password_hash(password)
    user_id = insert_db(
        "INSERT INTO users (name, email, phone, password_hash, role, status) VALUES (%s,%s,%s,%s,'client',%s) RETURNING id",
        (name, email, phone, pw_hash, status))

    if user_id:
        greeting = request.form.get('greeting', '').strip()
        agent_personality = request.form.get('agent_personality', '').strip()
        insert_db(
            "INSERT INTO businesses (user_id, name, business_type, status, tier, greeting, agent_personality) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user_id, business_name, business_type, status, tier, greeting, agent_personality))
        flash(f'Client {name} created.', 'success')
    else:
        flash('Failed to create client.', 'error')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/clients/<user_id>/suspend', methods=['POST', 'GET'])
@login_required
@admin_required
def admin_suspend_client(user_id):
    execute_db("UPDATE users SET status='suspended' WHERE id=%s", (user_id,))
    flash('Client suspended.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/numbers/add', methods=['POST'])
@login_required
@admin_required
def admin_add_number():
    number = request.form.get('number', '').strip()
    label = request.form.get('label', '').strip()
    business_id = request.form.get('business_id', '').strip() or None
    greeting = request.form.get('greeting', '').strip()
    agent_personality = request.form.get('agent_personality', '').strip()
    status = 'assigned' if business_id else 'available'

    if not number:
        flash('Phone number is required.', 'error')
        return redirect(url_for('admin_dashboard'))

    existing = query_db("SELECT id FROM phone_numbers WHERE number=%s", (number,), one=True)
    if existing:
        flash('Number already exists.', 'error')
        return redirect(url_for('admin_dashboard'))

    insert_db(
        "INSERT INTO phone_numbers (number, label, business_id, greeting, agent_personality, status) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (number, label, business_id, greeting, agent_personality, status))
    flash(f'Number {number} added.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/numbers/<number_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_number(number_id):
    num = query_db("SELECT * FROM phone_numbers WHERE id=%s", (number_id,), one=True)
    if num and num.get('twilio_sid'):
        twilio_service.release_number(num['twilio_sid'])
    execute_db("DELETE FROM phone_numbers WHERE id=%s", (number_id,))
    flash('Number deleted.', 'success')
    return redirect(url_for('admin_dashboard'))



# ───────────────────────────────────────────
# TWILIO WEBHOOKS — THE CALL ENGINE
# ───────────────────────────────────────────

@app.route('/webhook/incoming-call', methods=['POST', 'GET'])
def webhook_incoming_call():
    """Main entry: Twilio hits this when a call comes in to any of our numbers."""
    called_number = request.values.get('Called', '')
    caller_number = request.values.get('From', '')
    call_sid = request.values.get('CallSid', '')

    # Find which business owns this number
    biz = get_business_by_number(called_number)
    if not biz:
        resp = VoiceResponse()
        resp.say("Sorry, this number is not configured. Goodbye.", voice='Polly.Amy')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    biz_id = str(biz['id'])

    # Create call record
    insert_db("""INSERT INTO calls (business_id, twilio_call_sid, caller_number, called_number, direction, status)
                 VALUES (%s,%s,%s,%s,'inbound','in_progress') RETURNING id""",
              (biz_id, call_sid, caller_number, called_number))

    # Track active calls
    cache_incr(f"active_calls:{biz_id}")

    # Ensure we get notified when call ends (critical for analysis)
    twilio_service.set_call_status_callback(call_sid)

    # Start recording
    twilio_service.start_recording(call_sid)

    # Check business hours
    hours = biz.get('business_hours')
    if hours and isinstance(hours, str):
        try:
            hours = json.loads(hours)
        except Exception:
            hours = {}

    if not is_within_business_hours(hours):
        after_msg = biz.get('after_hours_message') or f"Thank you for calling {biz['name']}. We are currently closed."
        return Response(twilio_service.twiml_after_hours(after_msg), mimetype='text/xml')

    # Get caller history for AI context
    caller_hist = get_caller_history(biz_id, caller_number, limit=2)
    cache_set(f"caller_hist:{call_sid}", caller_hist or [], ex=1800)

    # Initialize conversation log in cache
    cache_set(f"conv:{call_sid}", [], ex=1800)

    # Store number-specific personality in cache for gather-response
    number_personality = biz.get('number_personality') or biz.get('agent_personality') or ''
    cache_set(f"num_personality:{call_sid}", number_personality, ex=1800)

    # Greet and gather — prefer number-specific greeting
    greeting = biz.get('number_greeting') or biz.get('greeting') or f"Hello, thank you for calling {biz['name']}. How can I help you?"
    return Response(
        twilio_service.twiml_greet_and_gather(greeting, biz_id, call_sid),
        mimetype='text/xml')


@app.route('/webhook/gather-response', methods=['POST', 'GET'])
def webhook_gather_response():
    """Handle speech input from caller, generate AI response."""
    speech_result = request.values.get('SpeechResult', '')
    biz_id = request.args.get('business_id', '')
    call_sid = request.args.get('call_sid', '')
    turn = int(request.args.get('turn', 0))

    if not speech_result:
        resp = VoiceResponse()
        resp.say("I didn't catch that. Could you please repeat?", voice='Polly.Amy')
        g = Gather(input='speech',
                   action=f"/webhook/gather-response?business_id={biz_id}&call_sid={call_sid}&turn={turn}",
                   method='POST', timeout=5, speech_timeout=3, language='en-GB')
        resp.append(g)
        return Response(str(resp), mimetype='text/xml')

    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    if not biz:
        resp = VoiceResponse()
        resp.say("Sorry, there was an error. Goodbye.", voice='Polly.Amy')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    # Get conversation log from cache, fallback to DB
    conv_log = cache_get(f"conv:{call_sid}", as_json=True) or []
    if not conv_log:
        call_row2 = query_db("SELECT conversation_log FROM calls WHERE twilio_call_sid=%s", (call_sid,), one=True)
        if call_row2 and call_row2['conversation_log']:
            conv_log = safe_json(call_row2['conversation_log'], [])
    conv_log.append({'role': 'Caller', 'text': speech_result})

    # Get caller history from cache
    caller_hist = cache_get(f"caller_hist:{call_sid}", as_json=True)

    # Get knowledge base docs for context
    kb_docs = query_db("SELECT title, content FROM knowledge_base WHERE business_id=%s AND active=TRUE", (biz_id,))

    # Build config for Gemini — prefer number-specific personality
    num_personality = cache_get(f"num_personality:{call_sid}") or ''
    if not num_personality:
        # Fallback: look up from DB via call's called_number
        call_row = query_db("SELECT called_number FROM calls WHERE twilio_call_sid=%s", (call_sid,), one=True)
        if call_row and call_row['called_number']:
            pn = query_db("SELECT agent_personality FROM phone_numbers WHERE number=%s", (call_row['called_number'],), one=True)
            num_personality = (pn['agent_personality'] if pn and pn['agent_personality'] else '') or ''
    config = {
        'greeting': biz.get('greeting', ''),
        'agent_personality': num_personality or biz.get('agent_personality', ''),
        'services': biz.get('config', {}).get('services', '') if isinstance(biz.get('config'), dict) else '',
        'business_hours': str(biz.get('business_hours', '')),
        'transfer_number': biz.get('transfer_number', ''),
        'restricted_info': biz.get('restricted_info', ''),
        'special_instructions': '',
        'faq': biz.get('faq') if isinstance(biz.get('faq'), list) else [],
        'knowledge_base': [{'title': d['title'], 'content': d['content']} for d in kb_docs] if kb_docs else []
    }

    # Generate AI response with caller history
    ai_response = gemini_service.generate_agent_response(
        speech_result, config, conversation_log=conv_log, caller_history=caller_hist)

    # Check for transfer intent
    transfer_keywords = ['transfer', 'speak to someone', 'human', 'real person', 'manager']
    needs_transfer = any(kw in speech_result.lower() for kw in transfer_keywords)
    if needs_transfer and biz.get('transfer_number'):
        conv_log.append({'role': 'Agent', 'text': 'Transferring to human.'})
        cache_set(f"conv:{call_sid}", conv_log, ex=1800)
        return Response(
            twilio_service.twiml_transfer(biz['transfer_number']),
            mimetype='text/xml')

    conv_log.append({'role': 'Agent', 'text': ai_response})
    cache_set(f"conv:{call_sid}", conv_log, ex=1800)
    # Persist to DB so status webhook can read it (multi-worker)
    execute_db("UPDATE calls SET conversation_log=%s WHERE twilio_call_sid=%s", (json.dumps(conv_log), call_sid))

    # Max 15 turns then end gracefully
    if turn >= 15:
        resp = VoiceResponse()
        resp.say(ai_response + " Thank you for calling. Have a great day!", voice='Polly.Amy')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    return Response(
        twilio_service.twiml_respond_and_gather(ai_response, biz_id, call_sid, turn),
        mimetype='text/xml')


@app.route('/webhook/transfer', methods=['POST', 'GET'])
def webhook_transfer():
    biz_id = request.args.get('business_id', '')
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    transfer = biz.get('transfer_number', '') if biz else ''
    if transfer:
        return Response(twilio_service.twiml_transfer(transfer), mimetype='text/xml')
    resp = VoiceResponse()
    resp.say("Sorry, no one is available. Please try again later.", voice='Polly.Amy')
    resp.hangup()
    return Response(str(resp), mimetype='text/xml')


@app.route('/webhook/call-status', methods=['POST', 'GET'])
def webhook_call_status():
    """Twilio status callback — fires when call completes."""
    call_sid = request.values.get('CallSid', '')
    status = request.values.get('CallStatus', '')
    duration = int(request.values.get('CallDuration', 0) or 0)
    print(f"[call-status] SID={call_sid} Status={status} Duration={duration}")

    if status == 'completed':
        call = query_db("SELECT * FROM calls WHERE twilio_call_sid=%s", (call_sid,), one=True)
        if not call:
            print(f"[call-status] No call record found for {call_sid}")
            return '', 204

        biz_id = str(call['business_id'])

        # Always update status first — no matter what happens next
        execute_db("""UPDATE calls SET status='completed', duration_seconds=%s, completed_at=NOW()
                      WHERE twilio_call_sid=%s""", (duration, call_sid))
        print(f"[call-status] Status set to completed for {call_sid}")

        try:
            # Get conversation log from cache or DB
            conv_log = cache_get(f"conv:{call_sid}", as_json=True) or []
            if not conv_log and call.get('conversation_log'):
                conv_log = safe_json(call['conversation_log'], [])
            print(f"[call-status] Conv log has {len(conv_log)} entries")

            # Get recording URL
            recording_url = twilio_service.get_recording_url(call_sid)
            print(f"[call-status] Recording URL: {recording_url}")

            # Analyze call with Gemini
            biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
            analysis = None
            if recording_url and biz:
                print(f"[call-status] Analyzing via recording...")
                analysis = gemini_service.analyze_call_recording(
                    recording_url, biz['name'], biz.get('business_type', ''))
            elif conv_log and biz:
                print(f"[call-status] Analyzing via transcript...")
                transcript = "\n".join(f"{e['role']}: {e['text']}" for e in conv_log)
                analysis = gemini_service.analyze_call_transcript(
                    transcript, biz['name'], biz.get('business_type', ''))
            else:
                print(f"[call-status] No recording and no conv_log — cannot analyze")

            if analysis:
                print(f"[call-status] Analysis done: category={analysis.get('category')}, sentiment={analysis.get('sentiment')}")
                execute_db("""UPDATE calls SET recording_url=%s, transcript=%s, summary=%s,
                              category=%s, sentiment=%s, caller_intent=%s, resolution=%s,
                              action_items=%s, conversation_log=%s
                              WHERE twilio_call_sid=%s""",
                           (recording_url,
                            analysis.get('transcript', ''), analysis.get('summary', ''),
                            analysis.get('category', 'other'), analysis.get('sentiment', 'neutral'),
                            analysis.get('caller_intent', ''), analysis.get('resolution', 'unresolved'),
                            json.dumps(analysis.get('action_items', [])), json.dumps(conv_log),
                            call_sid))

                # Auto-create ticket if needed
                if analysis.get('should_create_ticket') and analysis.get('ticket_data'):
                    td = analysis['ticket_data']
                    execute_db("""INSERT INTO tickets (business_id, call_id, type, priority, subject,
                                  description, caller_name, caller_number)
                                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (biz_id, str(call['id']), td.get('type', 'enquiry'),
                                td.get('priority', 'normal'), td.get('subject', 'New ticket'),
                                td.get('description', ''), td.get('caller_name', ''),
                                call.get('caller_number', '')))
            else:
                print(f"[call-status] Analysis returned None")
                execute_db("""UPDATE calls SET recording_url=%s, conversation_log=%s
                              WHERE twilio_call_sid=%s""",
                           (recording_url or '', json.dumps(conv_log), call_sid))
        except Exception as e:
            print(f"[call-status] Analysis error for {call_sid}: {e}")
            import traceback
            traceback.print_exc()

        # Update minutes
        add_call_minutes(biz_id, duration)

        # Decrement active calls
        cache_decr(f"active_calls:{biz_id}")

    elif status in ('busy', 'no-answer', 'canceled', 'failed'):
        execute_db("UPDATE calls SET status='missed' WHERE twilio_call_sid=%s", (call_sid,))
        call = query_db("SELECT business_id FROM calls WHERE twilio_call_sid=%s", (call_sid,), one=True)
        if call:
            cache_decr(f"active_calls:{str(call['business_id'])}")

    return '', 204


@app.route('/webhook/voicemail-complete', methods=['POST', 'GET'])
def webhook_voicemail_complete():
    recording_url = request.values.get('RecordingUrl', '')
    call_sid = request.values.get('CallSid', '')
    execute_db("UPDATE calls SET status='voicemail', recording_url=%s WHERE twilio_call_sid=%s",
               (recording_url, call_sid))
    resp = VoiceResponse()
    resp.say("Thank you. Goodbye.", voice='Polly.Amy')
    resp.hangup()
    return Response(str(resp), mimetype='text/xml')


@app.route('/webhook/call-fallback', methods=['POST', 'GET'])
def webhook_call_fallback():
    resp = VoiceResponse()
    resp.say("We're experiencing technical difficulties. Please try again later.", voice='Polly.Amy')
    resp.hangup()
    return Response(str(resp), mimetype='text/xml')


@app.route('/webhook/recording-status', methods=['POST', 'GET'])
def webhook_recording_status():
    return '', 204


@app.route('/webhook/incoming-sms', methods=['POST', 'GET'])
def webhook_incoming_sms():
    return '', 204


# ─── ONBOARDING WEBHOOKS ──────────────────

@app.route('/webhook/onboarding-start', methods=['POST', 'GET'])
def webhook_onboarding_start():
    """First question of the onboarding interview."""
    biz_id = request.args.get('business_id', '')
    ob_id = request.args.get('onboarding_id', '')

    row = query_db("SELECT questions_asked FROM onboarding_calls WHERE id=%s", (ob_id,), one=True)
    questions = safe_json(row['questions_asked'], []) if row and row['questions_asked'] else []

    if not questions or len(questions) == 0:
        resp = VoiceResponse()
        resp.say("Sorry, there was a setup error. We'll call you back.", voice='Polly.Amy')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    # Welcome message + first question — all in one Response
    resp = VoiceResponse()
    resp.say("Hello! I'm going to ask you a few questions to set up your AI receptionist. Let's get started!",
             voice='Polly.Amy', language='en-GB')
    resp.pause(length=1)
    g = resp.gather(input='speech',
                    action=f"/webhook/onboarding-answer?business_id={biz_id}&onboarding_id={ob_id}&q=0",
                    method='POST', timeout=8, speech_timeout='5', language='en-GB')
    g.say(questions[0]['question'], voice='Polly.Amy', language='en-GB')
    resp.say("I didn't hear a response. Let me move to the next question.", voice='Polly.Amy')
    resp.redirect(f"/webhook/onboarding-next?business_id={biz_id}&onboarding_id={ob_id}&q=1")
    return Response(str(resp), mimetype='text/xml')


@app.route('/webhook/onboarding-answer', methods=['POST', 'GET'])
def webhook_onboarding_answer():
    """Process an onboarding answer, move to next question."""
    speech = request.values.get('SpeechResult', '')
    biz_id = request.args.get('business_id', '')
    ob_id = request.args.get('onboarding_id', '')
    q_idx = int(request.args.get('q', 0))

    questions = cache_get(f"onboarding_q:{ob_id}", as_json=True) or []
    if not questions:
        row = query_db("SELECT questions_asked FROM onboarding_calls WHERE id=%s", (ob_id,), one=True)
        questions = safe_json(row['questions_asked'], []) if row and row['questions_asked'] else []

    # Store answer
    answers = cache_get(f"onboarding_a:{ob_id}", as_json=True) or {}
    if not answers:
        row2 = query_db("SELECT extracted_data FROM onboarding_calls WHERE id=%s", (ob_id,), one=True)
        answers = safe_json(row2['extracted_data'], {}) if row2 and row2['extracted_data'] else {}
    if q_idx < len(questions):
        field_name = questions[q_idx].get('field_name', f'question_{q_idx}')
        answers[field_name] = speech
    cache_set(f"onboarding_a:{ob_id}", answers, ex=3600)
    # Persist answers to DB so they survive across workers
    execute_db("UPDATE onboarding_calls SET extracted_data=%s WHERE id=%s", (json.dumps(answers), ob_id))

    # Next question or finish
    next_idx = q_idx + 1
    if next_idx < len(questions):
        return Response(
            twilio_service.twiml_onboarding_question(
                questions[next_idx]['question'], biz_id, ob_id, next_idx),
            mimetype='text/xml')

    # All questions answered — build agent config
    biz = query_db("SELECT * FROM businesses WHERE id=%s", (biz_id,), one=True)
    if biz:
        config = gemini_service.build_agent_config(biz['name'], biz.get('business_type', ''), answers)
        execute_db("""UPDATE businesses SET
                      greeting=%s, agent_personality=%s, after_hours_message=%s,
                      transfer_number=%s, restricted_info=%s, faq=%s, config=%s,
                      status='ready_for_review', updated_at=NOW() WHERE id=%s""",
                   (config.get('greeting', ''), config.get('agent_personality', ''),
                    config.get('after_hours_message', ''), config.get('transfer_number', ''),
                    config.get('restricted_info', ''), json.dumps(config.get('faq', [])),
                    json.dumps(config), biz_id))
        execute_db("UPDATE users SET status='ready_for_review' WHERE id=%s", (biz['user_id'],))

    # Save onboarding data
    execute_db("""UPDATE onboarding_calls SET extracted_data=%s, status='completed', completed_at=NOW()
                  WHERE id=%s""", (json.dumps(answers), ob_id))

    return Response(twilio_service.twiml_onboarding_complete(), mimetype='text/xml')


@app.route('/webhook/onboarding-next', methods=['POST', 'GET'])
def webhook_onboarding_next():
    """Skip to next onboarding question (when no speech detected)."""
    biz_id = request.args.get('business_id', '')
    ob_id = request.args.get('onboarding_id', '')
    q_idx = int(request.args.get('q', 0))
    questions = cache_get(f"onboarding_q:{ob_id}", as_json=True) or []
    if not questions:
        row = query_db("SELECT questions_asked FROM onboarding_calls WHERE id=%s", (ob_id,), one=True)
        questions = safe_json(row['questions_asked'], []) if row and row['questions_asked'] else []

    if q_idx < len(questions):
        return Response(
            twilio_service.twiml_onboarding_question(
                questions[q_idx]['question'], biz_id, ob_id, q_idx),
            mimetype='text/xml')

    return Response(twilio_service.twiml_onboarding_complete(), mimetype='text/xml')


@app.route('/webhook/onboarding-status', methods=['POST', 'GET'])
def webhook_onboarding_status():
    return '', 204


# ───────────────────────────────────────────
# API ENDPOINTS
# ───────────────────────────────────────────

@app.route('/api/usage')
@login_required
def api_usage():
    biz_id = session.get('business_id')
    usage = get_or_create_usage(biz_id)
    return jsonify({
        'minutes_used': float(usage['minutes_used']) if usage else 0,
        'minutes_limit': usage['minutes_limit'] if usage else 200,
        'active_calls': int(cache_get(f"active_calls:{biz_id}") or 0)
    })


@app.route('/api/calls/live')
@login_required
def api_live_calls():
    biz_id = session.get('business_id')
    active = query_db(
        "SELECT * FROM calls WHERE business_id=%s AND status='in_progress' ORDER BY created_at DESC",
        (biz_id,))
    return jsonify([{
        'id': str(c['id']), 'caller': c['caller_number'],
        'started': str(c['created_at']), 'duration': c['duration_seconds']
    } for c in (active or [])])


# ───────────────────────────────────────────
# ERRORS
# ───────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template('public/error.html', code=403, msg='Access denied.'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('public/error.html', code=404, msg='Page not found.'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('public/error.html', code=500, msg='Something went wrong.'), 500


# ───────────────────────────────────────────
# STARTUP: ADMIN + MIGRATIONS
# ───────────────────────────────────────────
with app.app_context():
    try:
        admin = query_db("SELECT id FROM users WHERE email=%s", ('kanchan.g12@gmail.com',), one=True)
        if admin:
            execute_db("UPDATE users SET role='admin', status='active' WHERE email=%s", ('kanchan.g12@gmail.com',))
        else:
            from werkzeug.security import generate_password_hash
            execute_db(
                "INSERT INTO users (name, email, password_hash, role, status) VALUES (%s,%s,%s,'admin','active')",
                ('Kanchan', 'kanchan.g12@gmail.com', generate_password_hash('admin@123'))
            )
        print("✅ Admin user ready: kanchan.g12@gmail.com")
    except Exception as e:
        print(f"⚠️ Admin setup: {e}")

    # Auto-migrate: add new columns if missing
    for col, typ in [('label', "VARCHAR(100) DEFAULT ''"), ('greeting', "TEXT DEFAULT ''"), ('agent_personality', "TEXT DEFAULT ''")]:
        try:
            execute_db(f"ALTER TABLE phone_numbers ADD COLUMN {col} {typ}")
            print(f"✅ Added phone_numbers.{col}")
        except Exception:
            pass  # column already exists

# ───────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=True, port=5000)
