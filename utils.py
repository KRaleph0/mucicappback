import re
import base64
import requests
import json
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import config
from config import CLOUDFLARE_SECRET_KEY

# --- 1. 텍스트 처리 및 기타 유틸 ---
def clean_text(text):
    if not text: return ""
    text = text.lower()
    patterns = [r'\(.*?ost.*?\)', r'original motion picture soundtrack', r'soundtrack', r'ost']
    for pat in patterns: text = re.sub(pat, '', text)
    text = re.sub(r'[^a-z0-9가-힣\s]', ' ', text)
    return ' '.join(text.split())

def get_similarity(a, b):
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS

# 🚨 [수정] ID 추출 로직 개선 (가장 중요!)
def extract_spotify_id(url):
    if not url: return None
    url = url.strip()

    # 1. URL이 아닌 그냥 숫자만 들어왔을 경우 (모의 서버 ID)
    if url.isdigit():
        return url
        
    # 2. http://.../spotify.com/<ID> 형태 (모의 서버 링크)
    # 'spotify.com/' 문자열 뒤에 있는 값을 찾습니다.
    match_mock = re.search(r'spotify\.com/([\w\d]+)', url)
    if match_mock:
        return match_mock.group(1)

    # 3. 정식 Spotify 링크 (open.spotify.com/track/<ID>)
    match_real = re.search(r'track/([\w\d]+)', url)
    if match_real:
        return match_real.group(1)

    # 4. 최후의 수단: 마지막 슬래시 뒤의 값 (쿼리 스트링 제거)
    # 예: spotify.com/3 -> 5
    return url.split('/')[-1].split('?')[0]

# --- 2. 보안 (Turnstile) ---
def verify_turnstile(token):
    if not token: return False, "캡차 토큰이 없습니다."
    try:
        res = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": CLOUDFLARE_SECRET_KEY, "response": token}
        ).json()
        return res.get("success"), "캡차 인증 실패"
    except: return False, "보안 검증 오류"

# --- 3. 외부 API 연동 (Spotify) ---
def get_spotify_headers():
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        return {}
    try:
        auth = base64.b64encode(f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
        res = requests.post(config.SPOTIFY_AUTH_URL, headers={
            'Authorization': f'Basic {auth}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }, data={'grant_type': 'client_credentials'}, timeout=5)
        if res.status_code == 200:
            return {'Authorization': f'Bearer {res.json().get("access_token")}'}
    except: pass
    return {}

# --- 4. 공공데이터 API 연동 ---
def get_current_weather():
    if not config.DATA_GO_KR_API_KEY: return "Clear"
    try:
        now = datetime.now()
        base_date = now.strftime("%Y%m%d")
        if now.minute < 45: now -= timedelta(hours=1)
        base_time = now.strftime("%H00")

        params = {
            'serviceKey': config.DATA_GO_KR_API_KEY,
            'pageNo': '1', 'numOfRows': '10', 'dataType': 'JSON',
            'base_date': base_date, 'base_time': base_time,
            'nx': '60', 'ny': '127'
        }
        res = requests.get(config.WEATHER_API_URL, params=params, timeout=3)
        if res.status_code != 200: return "Clear"

        items = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        pty = next((item['obsrValue'] for item in items if item['category'] == 'PTY'), "0")
        
        if pty in ["1", "5", "2", "6"]: return "Rain"
        if pty in ["3", "7"]: return "Snow"
        return "Clear"
    except: return "Clear"

def get_today_holiday():
    if not config.DATA_GO_KR_API_KEY: return None
    try:
        now = datetime.now()
        params = {
            'serviceKey': config.DATA_GO_KR_API_KEY,
            'solYear': now.year, 
            'solMonth': f"{now.month:02d}",
            '_type': 'json'
        }
        res = requests.get(config.HOLIDAY_API_URL, params=params, timeout=3)
        items = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if isinstance(items, dict): items = [items]
        
        today_str = now.strftime("%Y%m%d")
        for item in items:
            if str(item.get('locdate')) == today_str and item.get('isHoliday') == 'Y':
                return item.get('dateName')
        return None
    except: return None