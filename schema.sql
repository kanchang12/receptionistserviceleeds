-- VoiceBot SaaS Platform - Complete Schema
-- Twilio telephony + Gemini AI backbone

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── USERS ────────────────────────────────────
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(200) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(30),
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'client' CHECK (role IN ('admin', 'client')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'onboarding', 'ready_for_review', 'active', 'suspended', 'cancelled')),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ─── BUSINESSES ───────────────────────────────
CREATE TABLE businesses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(300) NOT NULL,
    business_type VARCHAR(100),
    industry VARCHAR(100),
    config JSONB DEFAULT '{}',
    agent_personality TEXT DEFAULT '',
    greeting TEXT DEFAULT '',
    after_hours_message TEXT DEFAULT '',
    transfer_number VARCHAR(30) DEFAULT '',
    business_hours JSONB DEFAULT '{}',
    faq JSONB DEFAULT '[]',
    restricted_info TEXT DEFAULT '',
    tier VARCHAR(20) NOT NULL DEFAULT 'starter' CHECK (tier IN ('starter', 'growth', 'enterprise')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    stripe_customer_id VARCHAR(100),
    stripe_subscription_id VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_businesses_user ON businesses(user_id);

-- ─── PHONE NUMBERS ────────────────────────────
CREATE TABLE phone_numbers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID REFERENCES businesses(id) ON DELETE SET NULL,
    number VARCHAR(30) NOT NULL UNIQUE,
    twilio_sid VARCHAR(60),
    country VARCHAR(5) DEFAULT 'GB',
    status VARCHAR(20) DEFAULT 'available' CHECK (status IN ('available', 'assigned', 'released')),
    monthly_cost DECIMAL(6,2) DEFAULT 1.00,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_phone_business ON phone_numbers(business_id);
CREATE INDEX idx_phone_number ON phone_numbers(number);

-- ─── CALLS ────────────────────────────────────
CREATE TABLE calls (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    twilio_call_sid VARCHAR(60) UNIQUE,
    caller_number VARCHAR(30),
    called_number VARCHAR(30),
    direction VARCHAR(10) DEFAULT 'inbound',
    duration_seconds INTEGER DEFAULT 0,
    recording_url TEXT,
    recording_sid VARCHAR(60),
    transcript TEXT,
    summary TEXT,
    category VARCHAR(30) DEFAULT 'other',
    sentiment VARCHAR(15) DEFAULT 'neutral',
    caller_intent TEXT,
    resolution VARCHAR(20) DEFAULT 'unresolved',
    action_items JSONB DEFAULT '[]',
    ai_notes JSONB DEFAULT '{}',
    conversation_log JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'completed', 'missed', 'voicemail', 'transferred')),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);
CREATE INDEX idx_calls_business ON calls(business_id);
CREATE INDEX idx_calls_caller ON calls(caller_number);
CREATE INDEX idx_calls_created ON calls(created_at DESC);
CREATE INDEX idx_calls_sid ON calls(twilio_call_sid);

-- ─── TICKETS ──────────────────────────────────
CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    call_id UUID REFERENCES calls(id) ON DELETE SET NULL,
    type VARCHAR(30) NOT NULL DEFAULT 'enquiry' CHECK (type IN ('order', 'complaint', 'booking', 'return', 'enquiry', 'support', 'other')),
    status VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    priority VARCHAR(10) NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    subject VARCHAR(300),
    description TEXT,
    caller_name VARCHAR(200),
    caller_number VARCHAR(30),
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_tickets_business ON tickets(business_id);
CREATE INDEX idx_tickets_status ON tickets(status);

-- ─── MINUTES USAGE ────────────────────────────
CREATE TABLE minutes_usage (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    month VARCHAR(7) NOT NULL,
    minutes_used DECIMAL(10,2) DEFAULT 0,
    minutes_limit INTEGER NOT NULL DEFAULT 200,
    overage_minutes DECIMAL(10,2) DEFAULT 0,
    alert_80_sent BOOLEAN DEFAULT FALSE,
    alert_90_sent BOOLEAN DEFAULT FALSE,
    alert_100_sent BOOLEAN DEFAULT FALSE,
    UNIQUE(business_id, month)
);
CREATE INDEX idx_usage_business_month ON minutes_usage(business_id, month);

-- ─── ONBOARDING CALLS ─────────────────────────
CREATE TABLE onboarding_calls (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    twilio_call_sid VARCHAR(60),
    recording_url TEXT,
    transcript TEXT,
    extracted_data JSONB DEFAULT '{}',
    questions_asked JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
    created_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

-- ─── KNOWLEDGE BASE ───────────────────────────
CREATE TABLE knowledge_base (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    title VARCHAR(300) NOT NULL,
    content TEXT NOT NULL,
    doc_type VARCHAR(30) DEFAULT 'faq' CHECK (doc_type IN ('faq', 'policy', 'script', 'document', 'note')),
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_kb_business ON knowledge_base(business_id);

-- ─── INVOICES ─────────────────────────────────
CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    stripe_invoice_id VARCHAR(100),
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'GBP',
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed', 'refunded')),
    description TEXT,
    period_start DATE,
    period_end DATE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ─── INTEGRATIONS ─────────────────────────────
CREATE TABLE integrations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    type VARCHAR(30) NOT NULL CHECK (type IN ('gmail', 'google_calendar', 'google_sheets', 'webhook', 'slack', 'zapier')),
    credentials JSONB DEFAULT '{}',
    config JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'inactive',
    created_at TIMESTAMP DEFAULT NOW()
);

-- ─── TIER LIMITS ──────────────────────────────
INSERT INTO users (name, email, phone, password_hash, role, status)
VALUES ('Admin', 'admin@voicebot.com', '+447000000000',
        'pbkdf2:sha256:600000$placeholder$placeholder', 'admin', 'active');
