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

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS

# [NEW] 이 함수가 없어서 에러가 났었습니다!
def extract_spotify_id(url):
    if not url: return None
    # 22자리 ID가 직접 들어온 경우
    if len(url) == 22 and re.match(r'^[a-zA-Z0-9]+$', url): return url
    # URL (https://open.spotify.com/track/ID)
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    if match: return match.group(1)
    # URI (spotify:track:ID)
    match = re.search(r'track:([a-zA-Z0-9]{22})', url)
    if match: return match.group(1)
    return None

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
        raise Exception("Spotify API Key 누락")
    auth = base64.b64encode(f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
    res = requests.post(config.SPOTIFY_AUTH_URL, headers={
        'Authorization': f'Basic {auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }, data={'grant_type': 'client_credentials'})
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

# --- 4. [핵심] 공공데이터 API 연동 (날씨, 휴일) ---

def get_current_weather():
    """기상청 초단기실황 API 호출 -> 'Rain', 'Snow', 'Clear' 반환"""
    if not config.DATA_GO_KR_API_KEY:
        print("⚠️ 날씨 API 키가 없습니다.")
        return "Clear"

    try:
        now = datetime.now()
        base_date = now.strftime("%Y%m%d")
        # 매시 45분 이후에 API 업데이트 됨 (안전하게 1시간 전 데이터 요청 가능)
        if now.minute < 45: now -= timedelta(hours=1)
        base_time = now.strftime("%H00")

        params = {
            'serviceKey': config.DATA_GO_KR_API_KEY,
            'pageNo': '1', 'numOfRows': '10', 'dataType': 'JSON',
            'base_date': base_date, 'base_time': base_time,
            'nx': '60', 'ny': '127' # 서울 기준 좌표 (실제 서비스 시 사용자 좌표 필요)
        }
        
        # SSL 인증서 문제 방지를 위해 verify=False 옵션 사용 가능 (개발 환경)
        res = requests.get(config.WEATHER_API_URL, params=params, timeout=3)
        if res.status_code != 200: return "Clear"

        items = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        
        # PTY: 0=없음, 1=비, 2=비/눈, 3=눈, 5=빗방울, 6=진눈깨비, 7=눈날림
        pty = next((item['obsrValue'] for item in items if item['category'] == 'PTY'), "0")
        
        if pty in ["1", "5", "2", "6"]: return "Rain"
        if pty in ["3", "7"]: return "Snow"
        return "Clear"

    except Exception as e:
        print(f"[Weather API Error] {e}")
        return "Clear"

def get_today_holiday():
    """특일정보 API 호출 -> 오늘이 휴일이면 '휴일명', 아니면 None 반환"""
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
        
        if isinstance(items, dict): items = [items] # 결과가 1개면 dict로 옴
        
        today_str = now.strftime("%Y%m%d")
        for item in items:
            # 날짜 일치하고 실제 공휴일(Y)인 경우
            if str(item.get('locdate')) == today_str and item.get('isHoliday') == 'Y':
                return item.get('dateName')
        return None

    except Exception as e:
        print(f"[Holiday API Error] {e}")
        return None

def get_kobis_metadata(movie_name):
    """영화 제목으로 장르/영문제목 조회"""
    try:
        res = requests.get(config.KOBIS_MOVIE_LIST_URL, params={'key': config.KOBIS_API_KEY, 'movieNm': movie_name}).json()
        mlist = res.get('movieListResult', {}).get('movieList', [])
        if mlist:
            t = mlist[0]
            return (t.get('genreAlt', '').split(','), t.get('movieNmEn', ''), t.get('movieNmOg', ''))
        return [], "", ""
    except: return [], "", ""