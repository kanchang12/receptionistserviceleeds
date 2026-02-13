"""
ElevenLabs Service — Fetch Prime client call data from ElevenLabs Conversational AI.
Pulls conversations from ElevenLabs API and maps them to our calls table format.
"""

import os
import requests
import time

ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY', '')
ELEVENLABS_BASE = 'https://api.elevenlabs.io/v1/convai'


def _headers():
    return {'xi-api-key': ELEVENLABS_API_KEY}


def list_conversations(agent_id, limit=30, after_unix=None):
    """Get conversations for an agent. Returns list of conversation summaries."""
    try:
        params = {'agent_id': agent_id, 'page_size': limit, 'summary_mode': 'include'}
        if after_unix:
            params['call_start_after_unix'] = int(after_unix)
        resp = requests.get(f"{ELEVENLABS_BASE}/conversations", headers=_headers(), params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('conversations', [])
        print(f"[ElevenLabs] list_conversations error: {resp.status_code} {resp.text[:200]}")
        return []
    except Exception as e:
        print(f"[ElevenLabs] list_conversations error: {e}")
        return []


def get_conversation_detail(conversation_id):
    """Get full conversation detail including transcript."""
    try:
        resp = requests.get(f"{ELEVENLABS_BASE}/conversations/{conversation_id}", headers=_headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"[ElevenLabs] get_detail error: {resp.status_code}")
        return None
    except Exception as e:
        print(f"[ElevenLabs] get_detail error: {e}")
        return None


def map_conversation_to_call(conv_summary, conv_detail=None, business_id=None):
    """Map ElevenLabs conversation to our calls table format."""
    meta = {}
    transcript_entries = []
    analysis = {}

    if conv_detail:
        meta = conv_detail.get('metadata', {})
        analysis = conv_detail.get('analysis', {}) or {}
        raw_transcript = conv_detail.get('transcript', [])
        for t in raw_transcript:
            role = 'Agent' if t.get('role') == 'agent' else 'Caller'
            transcript_entries.append({'role': role, 'text': t.get('message', '')})

    # Build transcript text
    transcript_text = '\n'.join(f"{e['role']}: {e['text']}" for e in transcript_entries)

    # Extract caller name from data collection or transcript
    caller_name = ''
    data_collected = analysis.get('data_collection_results', {}) or {}
    if isinstance(data_collected, dict):
        caller_name = data_collected.get('caller_name', {}).get('value', '') if isinstance(data_collected.get('caller_name'), dict) else ''
        if not caller_name:
            caller_name = data_collected.get('name', {}).get('value', '') if isinstance(data_collected.get('name'), dict) else ''

    # Map status
    el_status = conv_summary.get('status', 'done')
    status_map = {'done': 'completed', 'initiated': 'missed', 'in-progress': 'in_progress', 'failed': 'missed', 'processing': 'in_progress'}
    status = status_map.get(el_status, 'completed')

    # Summary from analysis or conversation summary
    summary = ''
    if analysis.get('transcript_summary'):
        summary = analysis['transcript_summary']
    elif conv_summary.get('transcript_summary'):
        summary = conv_summary['transcript_summary']

    # Sentiment / category from analysis evaluation results
    eval_results = analysis.get('evaluation_criteria_results', {}) or {}
    sentiment = 'neutral'
    category = 'enquiry'

    # Try to extract from eval criteria if available
    if isinstance(eval_results, dict):
        for key, val in eval_results.items():
            if 'sentiment' in key.lower():
                sentiment = (val.get('result', 'neutral') or 'neutral').lower()
            if 'category' in key.lower() or 'type' in key.lower():
                category = (val.get('result', 'enquiry') or 'enquiry').lower()

    return {
        'business_id': business_id,
        'elevenlabs_conversation_id': conv_summary.get('conversation_id', ''),
        'caller_number': meta.get('phone_number', '') or '',
        'called_number': '',
        'caller_name': caller_name,
        'direction': conv_summary.get('direction', 'inbound'),
        'status': status,
        'duration_seconds': conv_summary.get('call_duration_secs', 0) or 0,
        'transcript': transcript_text,
        'summary': summary,
        'category': category,
        'sentiment': sentiment,
        'caller_intent': analysis.get('call_successful', ''),
        'resolution': 'resolved' if conv_summary.get('call_successful') == 'success' else 'unresolved',
        'conversation_log': transcript_entries,
        'start_time_unix': conv_summary.get('start_time_unix_secs', 0),
    }
