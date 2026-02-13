"""
Gemini AI Service — ALL AI operations:
- Onboarding interview questions
- Live call: agent response generation WITH caller history
- Call transcription from recording URL
- Call analysis: category, sentiment, summary, intent
- Auto-ticket extraction
- Daily report generation
"""

import os
import json
import base64
import requests

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')
GEMINI_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}'


def _call_gemini(prompt, system_instruction=None, response_json=False, max_tokens=4096):
    headers = {'Content-Type': 'application/json'}
    body = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.3, 'maxOutputTokens': max_tokens}
    }
    if system_instruction:
        body['systemInstruction'] = {'parts': [{'text': system_instruction}]}
    if response_json:
        body['generationConfig']['responseMimeType'] = 'application/json'

    try:
        resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        text = resp.json()['candidates'][0]['content']['parts'][0]['text']
        if response_json:
            text = text.strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0]
            return json.loads(text)
        return text
    except Exception as e:
        print(f"[Gemini Error] {e}")
        return None


def _call_gemini_audio(audio_url, prompt, system_instruction=None):
    """Send audio to Gemini for transcription + analysis."""
    headers = {'Content-Type': 'application/json'}
    try:
        audio_resp = requests.get(audio_url, timeout=60)
        audio_resp.raise_for_status()
        audio_b64 = base64.b64encode(audio_resp.content).decode('utf-8')
        mime = audio_resp.headers.get('Content-Type', 'audio/mpeg')
    except Exception as e:
        print(f"[Audio fetch error] {e}")
        return None

    body = {
        'contents': [{'parts': [
            {'inlineData': {'mimeType': mime, 'data': audio_b64}},
            {'text': prompt}
        ]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 8192,
            'responseMimeType': 'application/json'
        }
    }
    if system_instruction:
        body['systemInstruction'] = {'parts': [{'text': system_instruction}]}

    try:
        resp = requests.post(GEMINI_URL, headers=headers, json=body, timeout=120)
        resp.raise_for_status()
        text = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        return json.loads(text)
    except Exception as e:
        print(f"[Gemini Audio Error] {e}")
        return None


# ─────────────────────────────────────────
# ONBOARDING
# ─────────────────────────────────────────

def get_onboarding_questions(business_name, business_type):
    prompt = f"""Generate 8 setup interview questions for an AI receptionist.
Business: {business_name} ({business_type})

Return JSON array: [{{"id":1,"question":"...","field_name":"...","required":true/false}}]
Fields needed: greeting, business_hours, services_offered, transfer_number,
after_hours_message, common_questions, restricted_info, special_instructions"""

    result = _call_gemini(prompt, response_json=True,
                          system_instruction="Return only valid JSON array.")
    if result:
        return result
    return [
        {"id": 1, "question": f"What services does {business_name} offer?", "field_name": "services_offered", "required": True},
        {"id": 2, "question": "What are your business hours?", "field_name": "business_hours", "required": True},
        {"id": 3, "question": "How should the AI greet callers?", "field_name": "greeting", "required": True},
        {"id": 4, "question": "What are the most common reasons customers call?", "field_name": "common_questions", "required": True},
        {"id": 5, "question": "What number should calls transfer to when the AI can't help?", "field_name": "transfer_number", "required": False},
        {"id": 6, "question": "What happens after hours?", "field_name": "after_hours_message", "required": False},
        {"id": 7, "question": "Any information the AI must never share?", "field_name": "restricted_info", "required": False},
        {"id": 8, "question": "Any other special instructions?", "field_name": "special_instructions", "required": False},
    ]


def build_agent_config(business_name, business_type, onboarding_data):
    prompt = f"""Build an AI receptionist config from onboarding answers.
Business: {business_name} ({business_type})
Answers: {json.dumps(onboarding_data)}

Return JSON with keys:
greeting, business_hours, after_hours_message, transfer_number,
agent_personality (detailed system prompt), services, faq (array of q/a),
restricted_info, special_instructions"""

    result = _call_gemini(prompt, response_json=True,
                          system_instruction="Expert AI agent configurator. Return valid JSON only.")
    if result:
        return result
    return {
        "greeting": f"Hello, thank you for calling {business_name}. How can I help you today?",
        "business_hours": "", "after_hours_message": f"We're currently closed. Please call back during business hours.",
        "transfer_number": "", "agent_personality": f"You are a friendly receptionist for {business_name}.",
        "services": "", "faq": [], "restricted_info": "", "special_instructions": ""
    }


# ─────────────────────────────────────────
# LIVE CALL — AGENT RESPONSE WITH HISTORY
# ─────────────────────────────────────────

def generate_agent_response(caller_input, business_config, conversation_log=None, caller_history=None):
    """
    Generate AI response during live call.
    caller_history = last 2 calls from this phone number (summary + category)
    """
    config = business_config if isinstance(business_config, dict) else json.loads(business_config or '{}')

    # Build conversation context
    conv_text = ""
    if conversation_log:
        for entry in conversation_log[-10:]:  # last 10 turns max
            conv_text += f"{entry.get('role','')}: {entry.get('text','')}\n"

    # Build caller history context
    history_text = "No previous calls from this number. This is a new caller."
    known_name = ""
    if caller_history:
        # Extract known name from previous calls
        for h in caller_history:
            if h.get('caller_name'):
                known_name = h['caller_name']
                break
        if known_name:
            history_text = f"RETURNING CALLER — Name: {known_name}\nPrevious calls from this caller:\n"
        else:
            history_text = "RETURNING CALLER (name unknown) — Previous calls from this caller:\n"
        for h in caller_history:
            history_text += f"- {h.get('date','')}: {h.get('summary','No summary')} (Category: {h.get('category','unknown')}, Sentiment: {h.get('sentiment','unknown')})\n"

    faq_text = ""
    if config.get('faq'):
        for item in config['faq']:
            faq_text += f"Q: {item.get('q','')}\nA: {item.get('a','')}\n"

    kb_text = ""
    if config.get('knowledge_base'):
        for doc in config['knowledge_base'][:5]:
            kb_text += f"--- {doc.get('title','')}: {doc.get('content','')}\n"

    # Build name rule based on whether we know the caller
    if known_name:
        name_rule = f"- IMPORTANT: This is a RETURNING caller named {known_name}. Greet them by name. Do NOT ask for their name — you already know it."
    else:
        name_rule = "- If this is the start of the conversation (turn 0 or 1), politely ask the caller for their name early on."

    prompt = f"""CALLER PHONE NUMBER: {config.get('caller_number', 'unknown')}

CALLER HISTORY:
{history_text}

CURRENT CONVERSATION:
{conv_text}

CALLER JUST SAID: "{caller_input}"

KNOWLEDGE BASE:
{faq_text}
{kb_text}

BUSINESS INFO:
Services: {config.get('services', 'Not specified')}
Hours: {config.get('business_hours', 'Not specified')}
Transfer number: {config.get('transfer_number', 'None')}
Restricted info (NEVER share): {config.get('restricted_info', 'None')}
Special instructions: {config.get('special_instructions', 'None')}

RULES:
{name_rule}
- NEVER repeat the greeting. If the conversation already shows you greeted, move forward — do NOT say hello/how are you again.
- If this is a RETURNING caller, acknowledge it naturally. Reference their previous calls if relevant.
- Respond in 1-3 sentences. Be warm, helpful, concise.
- If you can't help, offer to transfer.
- NEVER share restricted information.
- No markdown/special chars — this is spoken aloud.
- If caller seems frustrated (especially if repeat caller), be extra empathetic.

RESPOND AS THE RECEPTIONIST:"""

    personality = config.get('agent_personality',
                             'You are a friendly, professional receptionist.')

    response = _call_gemini(prompt, system_instruction=personality)
    return response or "I'm sorry, could you repeat that? I want to make sure I help you properly."


# ─────────────────────────────────────────
# CALL ANALYSIS (POST-CALL)
# ─────────────────────────────────────────

def analyze_call_recording(recording_url, business_name, business_type):
    """Full analysis from recording: transcript + category + sentiment + summary + ticket."""
    prompt = """Analyze this call recording. Return JSON:
{
  "transcript": "full transcript with Agent:/Caller: labels",
  "summary": "2-3 sentence summary",
  "category": "order|complaint|enquiry|booking|support|return|spam|other",
  "sentiment": "positive|neutral|negative",
  "caller_name": "caller's name if mentioned, otherwise empty string",
  "caller_intent": "one sentence",
  "resolution": "resolved|escalated|unresolved|voicemail",
  "action_items": ["..."],
  "should_create_ticket": true/false,
  "ticket_data": {"type":"...","priority":"low|normal|high|urgent","subject":"...","description":"...","caller_name":"..."}
}"""
    sys = f"Analyzing calls for {business_name} ({business_type}). Return valid JSON only."
    result = _call_gemini_audio(recording_url, prompt, system_instruction=sys)
    if result:
        return result
    return _empty_analysis()


def analyze_call_transcript(transcript_text, business_name, business_type):
    """Analyze from text transcript when recording isn't available."""
    prompt = f"""Analyze this transcript. Return JSON:
{{
  "summary": "2-3 sentence summary",
  "category": "order|complaint|enquiry|booking|support|return|spam|other",
  "sentiment": "positive|neutral|negative",
  "caller_name": "caller's name if mentioned, otherwise empty string",
  "caller_intent": "one sentence",
  "resolution": "resolved|escalated|unresolved|voicemail",
  "action_items": ["..."],
  "should_create_ticket": true/false,
  "ticket_data": {{"type":"...","priority":"...","subject":"...","description":"...","caller_name":"..."}}
}}

TRANSCRIPT:
{transcript_text}"""

    sys = f"Analyzing calls for {business_name} ({business_type}). Return valid JSON only."
    result = _call_gemini(prompt, system_instruction=sys, response_json=True)
    return result or _empty_analysis()


def _empty_analysis():
    return {
        "transcript": "", "summary": "Unable to analyze.", "category": "other",
        "sentiment": "neutral", "caller_intent": "unknown", "resolution": "unresolved",
        "action_items": [], "should_create_ticket": False, "ticket_data": None
    }


# ─────────────────────────────────────────
# DAILY REPORTS
# ─────────────────────────────────────────

def generate_daily_report(calls_data, business_name):
    prompt = f"""Daily call report for {business_name}.
Calls: {json.dumps(calls_data, default=str)}

Return JSON:
{{"total_calls":N,"total_minutes":N,"top_category":"...","sentiment_breakdown":{{"positive":N,"neutral":N,"negative":N}},
"summary":"3-4 sentences","concerns":"...","recommendations":"..."}}"""

    return _call_gemini(prompt, response_json=True,
                        system_instruction="Business analytics assistant. Valid JSON only.") or {}
