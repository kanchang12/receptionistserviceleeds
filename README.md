# VoiceBot SaaS — AI Receptionist Platform

Multi-tenant AI voice receptionist. Gemini handles everything — onboarding interviews, live call conversations (with caller history), post-call analysis, auto-ticketing. Twilio handles telephony.

## Architecture

```
Caller → Twilio Number → Webhook → Flask App → Gemini AI → TwiML Response
                                      ↓
                              PostgreSQL + Redis
                                      ↓
                           Dashboard (Analysis, Tickets)
```

### Stack
- **Telephony**: Twilio (numbers, webhooks, TwiML, recording, SMS)
- **AI Backbone**: Gemini 2.0 Flash (onboarding, live agent, transcription, analysis, ticketing)
- **Backend**: Flask + Gunicorn (gthread workers)
- **Database**: PostgreSQL
- **Cache**: Redis (conversation state, caller history, active call tracking)
- **Deployment**: Docker / Koyeb / Railway

### Caller History Context
When a call comes in, the system looks up the last 2 calls from that phone number. The summary, category, and sentiment from those calls are passed to Gemini so the AI agent can personalise its responses (e.g. "Welcome back — I see you called about a delivery issue last time…").

## Setup

```bash
cp .env.example .env
# Fill in: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, GEMINI_API_KEY, DATABASE_URL, etc.

pip install -r requirements.txt
psql $DATABASE_URL < schema.sql

# Development
python app.py

# Production
gunicorn app:app -c gunicorn_config.py
```

## Webhook Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/webhook/incoming-call` | POST | Twilio voice webhook — main entry |
| `/webhook/gather-response` | POST | Process speech, generate AI response |
| `/webhook/call-status` | POST | Call completed — trigger analysis |
| `/webhook/transfer` | POST | Transfer to human |
| `/webhook/voicemail-complete` | POST | After-hours voicemail saved |
| `/webhook/call-fallback` | POST | Error fallback |
| `/webhook/onboarding-start` | POST | Begin onboarding interview |
| `/webhook/onboarding-answer` | POST | Process each onboarding answer |
| `/webhook/onboarding-next` | GET/POST | Skip to next question |

## Pricing Tiers

| Tier | Price | Minutes | Numbers |
|---|---|---|---|
| Starter | £29/mo | 200 | 1 |
| Growth | £79/mo | 600 | 2 |
| Enterprise | £199/mo | 2000 | 5 |

Overage: £0.08/min. Usage alerts at 80%, 90%, 100% via SMS.

## File Structure

```
voicebot-saas/
├── app.py                  # Flask app — all routes, webhooks, DB ops
├── gunicorn_config.py      # Production server config
├── schema.sql              # PostgreSQL schema (11 tables)
├── requirements.txt
├── Procfile
├── Dockerfile
├── .env.example
├── utils/
│   ├── gemini_service.py   # All Gemini AI operations
│   └── twilio_service.py   # All Twilio telephony operations
├── static/
│   ├── css/style.css       # Dark editorial design
│   └── js/dashboard.js     # Live polling
└── templates/
    ├── base.html
    ├── dashboard_base.html
    ├── auth/               # login, register
    ├── client/             # dashboard, calls, call_detail, tickets, settings, billing, onboarding
    ├── admin/              # dashboard, clients, numbers
    └── public/             # landing, error
```

## DevDash 2026

Built for the DevDash hackathon (Jan 20 – Feb 20, 2026). Uses Gemini as the sole AI backbone for all intelligence — no ElevenLabs, no OpenAI.
