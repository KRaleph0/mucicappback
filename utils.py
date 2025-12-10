# utils.py
import re
import base64
import requests
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import config

# --- 텍스트 처리 ---
def clean_text(text):
    if not text: return ""
    text = text.lower()
    patterns = [r'\(.*?ost.*?\)', r'original motion picture soundtrack', r'soundtrack', r'ost']
    for pat in patterns: text = re.sub(pat, '', text)
    text = re.sub(r'[^a-z0-9가-힣\s]', ' ', text)
    return ' '.join(text.split())

def get_similarity(a, b):
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

def extract_spotify_id(url):
    if len(url) == 22 and re.match(r'^[a-zA-Z0-9]+$', url): return url
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    return match.group(1) if match else None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS

# --- 외부 API (Spotify 인증, 날씨, 공휴일, 영화정보 조회) ---
def get_spotify_headers():
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        raise Exception("Spotify API Key가 설정되지 않음")
    auth_str = f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f'Basic {b64_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    res = requests.post(config.SPOTIFY_AUTH_URL, headers=headers, data={'grant_type': 'client_credentials'})
    if res.status_code != 200: raise Exception(f"Spotify Auth Failed: {res.status_code}")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def get_current_weather():
    if not config.DATA_GO_KR_API_KEY: return None
    now = datetime.now()
    base_date = now.strftime("%Y%m%d")
    if now.minute < 45: now -= timedelta(hours=1)
    base_time = now.strftime("%H00")
    params = {'serviceKey': config.DATA_GO_KR_API_KEY, 'pageNo': '1', 'numOfRows': '10', 'dataType': 'JSON', 'base_date': base_date, 'base_time': base_time, 'nx': '60', 'ny': '127'}
    try:
        res = requests.get(config.WEATHER_API_URL, params=params, timeout=5)
        items = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        pty = next((item['obsrValue'] for item in items if item['category'] == 'PTY'), "0")
        if pty in ["1", "5", "2", "6"]: return "Rain"
        if pty in ["3", "7"]: return "Snow"
        return "Clear"
    except: return "Clear"

def get_today_holiday():
    if not config.DATA_GO_KR_API_KEY: return None
    now = datetime.now()
    params = {'serviceKey': config.DATA_GO_KR_API_KEY, 'solYear': now.year, 'solMonth': f"{now.month:02d}", '_type': 'json'}
    try:
        res = requests.get(config.HOLIDAY_API_URL, params=params, timeout=5)
        item_list = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if isinstance(item_list, dict): item_list = [item_list]
        today_str = now.strftime("%Y%m%d")
        for item in item_list:
            if str(item.get('locdate')) == today_str and item.get('isHoliday') == 'Y':
                return item.get('dateName')
        return None
    except: return None

def get_kobis_metadata(movie_name):
    params = {'key': config.KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        res = requests.get(config.KOBIS_MOVIE_LIST_URL, params=params).json()
        mlist = res.get('movieListResult', {}).get('movieList', [])
        if mlist:
            t = mlist[0]
            return (t.get('genreAlt', '').split(',') if t.get('genreAlt') else []), t.get('movieNmEn', ''), t.get('movieNmOg', '')
        return [], "", ""
    except: return [], "", ""