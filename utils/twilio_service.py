"""
Twilio Service — ALL telephony operations:
- Number search, purchase, release, webhook config
- TwiML generation for call flows (greeting, gather, transfer, voicemail)
- Onboarding interview TwiML flow
- SMS notifications
- Recording management
"""

import os
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather

TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH = os.environ.get('TWILIO_AUTH_TOKEN', '')
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'https://yourdomain.com')

_client = None


def get_client():
    global _client
    if _client is None:
        _client = Client(TWILIO_SID, TWILIO_AUTH)
    return _client


# ─── NUMBER MANAGEMENT ────────────────────

def search_numbers(country='GB', limit=10):
    try:
        numbers = get_client().available_phone_numbers(country).local.list(
            voice_enabled=True, sms_enabled=True, limit=limit)
        return [{'number': n.phone_number, 'friendly_name': n.friendly_name,
                 'locality': getattr(n, 'locality', ''), 'region': getattr(n, 'region', '')}
                for n in numbers]
    except Exception as e:
        print(f"[Twilio] search_numbers error: {e}")
        return []


def buy_number(phone_number, business_id):
    try:
        n = get_client().incoming_phone_numbers.create(
            phone_number=phone_number,
            voice_url=f"{APP_BASE_URL}/webhook/incoming-call",
            voice_method='POST',
            voice_fallback_url=f"{APP_BASE_URL}/webhook/call-fallback",
            status_callback=f"{APP_BASE_URL}/webhook/call-status",
            status_callback_method='POST',
            sms_url=f"{APP_BASE_URL}/webhook/incoming-sms",
            friendly_name=f"VoiceBot-{business_id}")
        return {'sid': n.sid, 'number': n.phone_number, 'status': 'active'}
    except Exception as e:
        print(f"[Twilio] buy_number error: {e}")
        return None


def release_number(twilio_sid):
    try:
        get_client().incoming_phone_numbers(twilio_sid).delete()
        return True
    except Exception as e:
        print(f"[Twilio] release error: {e}")
        return False


def update_webhooks(twilio_sid):
    try:
        get_client().incoming_phone_numbers(twilio_sid).update(
            voice_url=f"{APP_BASE_URL}/webhook/incoming-call",
            voice_method='POST',
            status_callback=f"{APP_BASE_URL}/webhook/call-status",
            status_callback_method='POST')
        return True
    except Exception as e:
        print(f"[Twilio] update_webhooks error: {e}")
        return False


def set_call_status_callback(call_sid):
    """Set status callback on an in-progress call so we get notified when it ends."""
    try:
        get_client().calls(call_sid).update(
            status_callback=f"{APP_BASE_URL}/webhook/call-status",
            status_callback_method='POST',
            status_callback_event=['completed', 'busy', 'no-answer', 'canceled', 'failed'])
        return True
    except Exception as e:
        print(f"[Twilio] set_call_status_callback error: {e}")
        return False


def list_numbers():
    try:
        return [{'sid': n.sid, 'number': n.phone_number, 'friendly_name': n.friendly_name}
                for n in get_client().incoming_phone_numbers.list()]
    except Exception as e:
        print(f"[Twilio] list error: {e}")
        return []


# ─── TWIML GENERATION ─────────────────────

def twiml_greet_and_gather(greeting, business_id, call_sid):
    """Answer call: greet + gather speech."""
    resp = VoiceResponse()
    g = Gather(input='speech', action=f"/webhook/gather-response?business_id={business_id}&call_sid={call_sid}&turn=0",
               method='POST', timeout=5, speech_timeout=3, language='en-GB')
    g.say(greeting, voice='Polly.Amy', language='en-GB')
    resp.append(g)
    resp.say("I didn't catch that. Let me transfer you to someone who can help.", voice='Polly.Amy')
    resp.redirect(f"/webhook/transfer?business_id={business_id}&call_sid={call_sid}")
    return str(resp)


def twiml_respond_and_gather(ai_response, business_id, call_sid, turn):
    """Say AI response, gather next input."""
    resp = VoiceResponse()
    g = Gather(input='speech', action=f"/webhook/gather-response?business_id={business_id}&call_sid={call_sid}&turn={turn + 1}",
               method='POST', timeout=5, speech_timeout=3, language='en-GB')
    g.say(ai_response, voice='Polly.Amy', language='en-GB')
    resp.append(g)
    resp.say("Thank you for calling. Have a great day!", voice='Polly.Amy')
    resp.hangup()
    return str(resp)


def twiml_transfer(transfer_number, message=None):
    """Transfer call to human."""
    resp = VoiceResponse()
    resp.say(message or "Let me transfer you now.", voice='Polly.Amy', language='en-GB')
    resp.dial(transfer_number, timeout=30)
    resp.say("Sorry, nobody is available right now. Please try again later.", voice='Polly.Amy')
    resp.hangup()
    return str(resp)


def twiml_after_hours(message):
    """After hours: play message, take voicemail."""
    resp = VoiceResponse()
    resp.say(message, voice='Polly.Amy', language='en-GB')
    resp.say("Please leave a message after the beep.", voice='Polly.Amy')
    resp.record(max_length=120, action="/webhook/voicemail-complete",
                transcribe=False, play_beep=True)
    resp.say("Thank you. Goodbye.", voice='Polly.Amy')
    resp.hangup()
    return str(resp)


def twiml_onboarding_question(question_text, business_id, onboarding_id, question_idx):
    """Onboarding interview: ask question, gather speech answer."""
    resp = VoiceResponse()
    g = Gather(input='speech',
               action=f"/webhook/onboarding-answer?business_id={business_id}&onboarding_id={onboarding_id}&q={question_idx}",
               method='POST', timeout=8, speech_timeout=5, language='en-GB')
    g.say(question_text, voice='Polly.Amy', language='en-GB')
    resp.append(g)
    resp.say("I didn't hear a response. Let me move to the next question.", voice='Polly.Amy')
    resp.redirect(f"/webhook/onboarding-next?business_id={business_id}&onboarding_id={onboarding_id}&q={question_idx + 1}")
    return str(resp)


def twiml_onboarding_complete():
    """End onboarding call."""
    resp = VoiceResponse()
    resp.say("That's all the questions I have. Thank you! Your AI receptionist is being configured now. "
             "We'll send you a text when it's ready. Goodbye!", voice='Polly.Amy', language='en-GB')
    resp.hangup()
    return str(resp)


# ─── SMS ──────────────────────────────────

def send_sms(to, body, from_number=None):
    try:
        from_num = from_number or os.environ.get('TWILIO_FROM_NUMBER', '')
        msg = get_client().messages.create(to=to, from_=from_num, body=body)
        return msg.sid
    except Exception as e:
        print(f"[Twilio SMS] error: {e}")
        return None


# ─── RECORDING ────────────────────────────

def get_recording_url(call_sid):
    try:
        recordings = get_client().recordings.list(call_sid=call_sid, limit=1)
        if recordings:
            return f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Recordings/{recordings[0].sid}.mp3"
        return None
    except Exception as e:
        print(f"[Twilio] get_recording error: {e}")
        return None


def start_recording(call_sid):
    try:
        get_client().calls(call_sid).recordings.create(
            recording_status_callback=f"{APP_BASE_URL}/webhook/recording-status",
            recording_status_callback_method='POST')
        return True
    except Exception as e:
        print(f"[Twilio] start_recording error: {e}")
        return False
